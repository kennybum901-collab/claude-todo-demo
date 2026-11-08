"""Daily review digest: one email to you with yesterday's numbers, a random
sample of the emails the bots actually sent, the newest sites attached, and
automatic quality checks - plus a one-click "pause all sending" link.

Goes to DIGEST_EMAIL (or ALERT_EMAIL, or COMPANY_EMAIL) once a day after
DIGEST_HOUR (UTC). It is sent even in DRY_RUN, so you can review the bots'
output before going live; only this email to you goes out.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import random
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from .config import Config
from .db import DB
from .mailer import Mailer
from .qa import check_email, check_site

log = logging.getLogger(__name__)


def control_token(action: str, secret: str) -> str:
    return hmac.new(secret.encode(), f"control:{action}".encode(), hashlib.sha256).hexdigest()[:32]


def verify_control(action: str, tok: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(control_token(action, secret), tok or "")


def control_url(action: str, cfg: Config) -> str | None:
    if not (cfg.public_url and cfg.app_secret):
        return None
    return f"{cfg.public_url}/control/{action}?t={control_token(action, cfg.app_secret)}"


class Digest:
    def __init__(self, db: DB, cfg: Config, mailer: Mailer | None = None, http=None, rng=None):
        self.db, self.cfg = db, cfg
        self.mailer = mailer or Mailer(db, cfg)
        self.http = http
        self.rng = rng or random.Random()

    @property
    def recipient(self) -> str:
        return self.cfg.digest_email or self.cfg.alert_email or self.cfg.company_email

    # ---------- data ----------
    def numbers(self, since: str) -> list[tuple[str, int]]:
        q = lambda sql, *a: self.db.conn.execute(sql, (since, *a)).fetchone()[0]  # noqa: E731
        ev = lambda *kinds: q(  # noqa: E731
            f"SELECT COUNT(*) FROM events WHERE created_at >= ? AND kind IN ({','.join('?' * len(kinds))})", *kinds)
        return [
            ("New leads", ev("lead_added")),
            ("Sites built", ev("built")),
            ("Emails sent", ev("sent", "dry_send")),
            ("Replies", ev("replied", "client_reply")),
            ("Opt-outs & bounces", ev("unsubscribed", "bounce")),
            ("New clients", ev("paid")),
            ("Add-ons sold", ev("addon_active")),
            ("Quote requests", ev("quote_request")),
            ("Cancellations", ev("churned")),
            ("Errors", ev("error")),
        ]

    def sample_messages(self, since: str, n: int) -> list:
        rows = self.db.conn.execute(
            "SELECT * FROM messages WHERE created_at >= ? AND kind IN ('sent','dry_send') ORDER BY id",
            (since,)).fetchall()
        by_bot: dict[str, list] = {}
        for r in rows:
            by_bot.setdefault(r["bot"], []).append(r)
        picked = [self.rng.choice(v) for v in by_bot.values()]   # one of each kind first
        rest = [r for r in rows if r not in picked]
        self.rng.shuffle(rest)
        return (picked + rest)[:n]

    def sample_sites(self, since: str, n: int) -> list:
        ids = [r[0] for r in self.db.conn.execute(
            "SELECT DISTINCT lead_id FROM events WHERE created_at >= ? AND kind = 'built'", (since,))]
        leads = [self.db.get_lead(i) for i in ids]
        leads = [l for l in leads if l is not None]
        self.rng.shuffle(leads)
        return leads[:n]

    # ---------- compose ----------
    def compose(self, now: datetime | None = None) -> tuple[EmailMessage, int]:
        now = now or datetime.now(timezone.utc)
        since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        nums = self.numbers(since)
        msgs = self.sample_messages(since, self.cfg.digest_sample)
        sites = self.sample_sites(since, 3)

        issues: list[str] = []
        email_blocks = []
        for m in msgs:
            probs = check_email(m["subject"], m["body"], self.cfg, bot=m["bot"])
            issues += [f"Email to {m['to_addr']} ({m['bot']}): {p}" for p in probs]
            flag = "  ⚠️ " + "; ".join(probs) if probs else "  ✅ checks passed"
            email_blocks.append(f"── {m['bot'].upper()} → {m['to_addr']}\n{flag}\nSubject: {m['subject']}\n\n"
                                f"{(m['body'] or '').strip()[:2500]}\n")
        site_blocks, attachments = [], []
        for lead in sites:
            probs = check_site(lead, self.cfg, http=self.http)
            issues += [f"Site for {lead['business_name']}: {p}" for p in probs]
            flag = "⚠️ " + "; ".join(probs) if probs else "✅ checks passed"
            link = lead["preview_url"] if (lead["preview_url"] or "").startswith("http") else "(attached)"
            site_blocks.append(f"- {lead['business_name']} (score {lead['score'] if lead['score'] is not None else '–'}): "
                               f"{link}\n  {flag}")
            path = Path(self.cfg.build_dir) / (lead["site_slug"] or "") / "index.html"
            if path.is_file():
                attachments.append((f"{lead['site_slug']}.html", path.read_bytes()))
        errors = self.db.conn.execute(
            "SELECT bot, detail FROM events WHERE created_at >= ? AND kind = 'error' ORDER BY id DESC LIMIT 5",
            (since,)).fetchall()
        issues += [f"Bot error ({e['bot']}): {(e['detail'] or '')[:160]}" for e in errors]

        paused = self.db.sending_paused()
        mode = "PAUSED (no marketing email is going out)" if paused else ("DRY RUN" if self.cfg.dry_run else "LIVE")
        lines = [f"Daily review for {self.cfg.company_name} · mode: {mode}", ""]
        lines += ["⚠️ NEEDS A LOOK", *[f"  - {i}" for i in issues], ""] if issues else ["✅ All quality checks passed.", ""]
        lines += ["LAST 24 HOURS", *[f"  {k:<20} {v}" for k, v in nums], ""]
        pause, resume = control_url("pause", self.cfg), control_url("resume", self.cfg)
        if paused and resume:
            lines += [f"Resume sending: {resume}", ""]
        elif pause:
            lines += [f"Something wrong? Pause all marketing email with one click: {pause}", ""]
        else:
            lines += ["Something wrong? Run `python -m agency pause` on the server to stop all marketing email.", ""]
        lines += [f"SAMPLE OF EMAILS SENT ({len(msgs)})", ""] + (email_blocks or ["  (none in the last 24 hours)\n"])
        lines += [f"NEWEST SITES ({len(sites)}, open the attachments)", *(site_blocks or ["  (none built in the last 24 hours)"])]

        msg = EmailMessage()
        msg["Subject"] = (f"{'⚠️ ' if issues else ''}Daily review: {dict(nums)['Emails sent']} emails, "
                          f"{dict(nums)['Sites built']} sites, {dict(nums)['New clients']} new clients")
        msg["To"] = self.recipient
        msg.set_content("\n".join(lines) + "\n")
        for name, data in attachments:
            msg.add_attachment(data, maintype="text", subtype="html", filename=name)
        return msg, len(issues)

    # ---------- send ----------
    def due(self, now: datetime) -> bool:
        return now.hour >= self.cfg.digest_hour and self.db.get_setting("digest_last") != now.date().isoformat()

    def send(self, now: datetime | None = None, force: bool = False) -> bool:
        now = now or datetime.now(timezone.utc)
        if not self.cfg.digest_enabled or not (force or self.due(now)):
            return False
        sender = self.mailer.notify_sender(to_owner=True)
        if sender is None:
            return False
        msg, n_issues = self.compose(now)
        try:
            self.mailer.send(sender, msg, bot="digest", lead_id=None, detail=f"daily digest ({n_issues} issues)",
                             to_owner=True)
        finally:
            self.mailer.close()
        self.db.set_setting("digest_last", now.date().isoformat())
        cutoff = (now - timedelta(days=90)).isoformat()
        with self.db.tx() as c:   # keep the email archive to 90 days
            c.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
        return True
