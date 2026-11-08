"""Multi-inbox mailer: rotates sends across several sending addresses safely.

Why: one inbox can only send ~30 cold emails/day before deliverability suffers.
Scaling means *more inboxes* (ideally on several secondary domains), each with
its own cap, warm-up ramp and health check.

Configure in `senders.json` (path: SENDERS_FILE, git-ignored), see
`senders.example.json`. Without it, the single SMTP_* settings in .env are used.

Per inbox:
  * daily_cap, with an optional warm-up ramp (warmup_start, warmup_base, warmup_step)
  * sticky threads: a lead always hears from the same inbox, so follow-ups
    thread and replies land in one place
  * auto-pause when the recent bounce rate passes BOUNCE_PAUSE_RATE
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from .config import Config
from .db import DB

log = logging.getLogger(__name__)
BOUNCE_WINDOW = 50       # look at the last N sends per inbox
BOUNCE_MIN_SENDS = 20    # don't judge an inbox on fewer sends than this


@dataclass
class Sender:
    email: str
    from_name: str
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    password: str = ""
    imap_host: str = ""
    daily_cap: int = 30
    warmup_start: str | None = None   # ISO date the inbox started sending
    warmup_base: int = 5              # sends allowed on day 0
    warmup_step: int = 2              # extra sends allowed per day after that
    paused: bool = False

    @property
    def can_send_live(self) -> bool:
        return bool(self.smtp_host)

    def cap_on(self, day: date) -> int:
        if not self.warmup_start:
            return self.daily_cap
        days = (day - date.fromisoformat(self.warmup_start)).days
        if days < 0:
            return 0
        return min(self.daily_cap, self.warmup_base + self.warmup_step * days)


def load_senders(cfg: Config) -> list[Sender]:
    path = Path(cfg.senders_file)
    if path.exists():
        raw = json.loads(path.read_text())
        senders = []
        for item in raw:
            item = dict(item)
            env_name = item.pop("password_env", None)
            if env_name:
                item["password"] = os.environ.get(env_name, "")
            item.setdefault("smtp_user", item["email"])
            item.setdefault("from_name", f"{cfg.owner_name} at {cfg.company_name}")
            senders.append(Sender(**item))
        return senders
    return [Sender(
        email=cfg.company_email,
        from_name=f"{cfg.owner_name} at {cfg.company_name}",
        smtp_host=cfg.smtp_host, smtp_port=cfg.smtp_port,
        smtp_user=cfg.smtp_user, password=cfg.smtp_password, imap_host=cfg.imap_host,
        daily_cap=cfg.daily_email_cap,
    )]


class Mailer:
    def __init__(self, db: DB, cfg: Config, senders: list[Sender] | None = None,
                 smtp_factory=None):
        self.db, self.cfg = db, cfg
        self.senders = senders if senders is not None else load_senders(cfg)
        self.by_email = {s.email.lower(): s for s in self.senders}
        self._smtp_factory = smtp_factory
        self._conns: dict[str, smtplib.SMTP] = {}

    @property
    def live(self) -> bool:
        return not self.cfg.dry_run and any(s.can_send_live for s in self.senders)

    # ---------- capacity & health ----------
    def sent_today(self, sender: Sender) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        # sends logged before multi-inbox support have no sender: charge them to the
        # first inbox so upgrading mid-day can't double today's volume
        legacy = int(bool(self.senders) and sender is self.senders[0])
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind IN ('sent','dry_send') "
            "AND (sender = ? OR (sender IS NULL AND ?)) AND substr(created_at,1,10) = ?",
            (sender.email.lower(), legacy, today),
        ).fetchone()[0]

    def bounce_rate(self, sender: Sender) -> tuple[float, int]:
        """Bounces per send over this inbox's last BOUNCE_WINDOW sends."""
        addr = sender.email.lower()
        ids = [r[0] for r in self.db.conn.execute(
            "SELECT id FROM events WHERE sender = ? AND kind IN ('sent','dry_send') "
            "ORDER BY id DESC LIMIT ?", (addr, BOUNCE_WINDOW))]
        if not ids:
            return 0.0, 0
        bounces = self.db.conn.execute(
            "SELECT COUNT(*) FROM events WHERE sender = ? AND kind = 'bounce' AND id > ?",
            (addr, min(ids)),
        ).fetchone()[0]
        return bounces / len(ids), len(ids)

    def is_paused(self, sender: Sender) -> bool:
        if sender.paused:
            return True
        rate, sends = self.bounce_rate(sender)
        return sends >= BOUNCE_MIN_SENDS and rate > self.cfg.bounce_pause_rate

    def remaining(self, sender: Sender) -> int:
        if self.is_paused(sender):
            return 0
        if self.live and not sender.can_send_live:
            return 0
        cap = sender.cap_on(datetime.now(timezone.utc).date())
        return max(0, cap - self.sent_today(sender))

    def total_remaining(self) -> int:
        return sum(self.remaining(s) for s in self.senders)

    def pick(self, lead) -> Sender | None:
        """Sticky: reuse the inbox that already emailed this lead (thread continuity).
        If it is gone or paused, move the lead to the healthiest inbox."""
        sticky = self.by_email.get((lead["sender"] or "").lower()) if "sender" in lead.keys() else None
        if sticky and not self.is_paused(sticky):
            return sticky if self.remaining(sticky) > 0 else None  # wait for tomorrow
        candidates = [(self.remaining(s), s) for s in self.senders]
        candidates = [c for c in candidates if c[0] > 0]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c[0])[1]

    # ---------- sending ----------
    def send(self, sender: Sender, msg: EmailMessage, *, bot: str, lead_id: int, detail: str) -> None:
        for h in ("From", "List-Unsubscribe", "Reply-To"):
            del msg[h]
        msg["From"] = f"{sender.from_name} <{sender.email}>"
        msg["List-Unsubscribe"] = f"<mailto:{sender.email}?subject=unsubscribe>"
        live = self.live and sender.can_send_live
        if live:
            conn = self._conns.get(sender.email)
            if conn is None:
                factory = self._smtp_factory or smtplib.SMTP
                conn = factory(sender.smtp_host, sender.smtp_port, timeout=30)
                conn.starttls()
                conn.login(sender.smtp_user, sender.password)
                self._conns[sender.email] = conn
            conn.send_message(msg)
        self.db.log(bot, "sent" if live else "dry_send", detail, lead_id, sender=sender.email.lower())

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.quit()
            except smtplib.SMTPException:
                pass
        self._conns.clear()

    # ---------- reporting ----------
    def status(self) -> list[dict]:
        today = datetime.now(timezone.utc).date()
        out = []
        for s in self.senders:
            rate, sends = self.bounce_rate(s)
            out.append({
                "email": s.email, "sent_today": self.sent_today(s), "cap_today": s.cap_on(today),
                "bounce_rate": rate, "recent_sends": sends, "paused": self.is_paused(s),
                "warming": bool(s.warmup_start) and s.cap_on(today) < s.daily_cap,
            })
        return out
