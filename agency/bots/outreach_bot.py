"""Outreach bot: emails each prospect a link to the site we already built for them.

Built-in guardrails (keep these - they protect your domain and keep you legal
under CAN-SPAM / similar laws):
  * honest subject line, your real business name + postal address in every email
  * one-click opt-out (List-Unsubscribe header + "reply STOP"), honored forever
  * daily send cap and a hard limit on follow-ups
  * DRY_RUN by default: emails are rendered and logged, not sent
Check the rules where your prospects are (e.g. GDPR/PECR in the UK/EU, CASL in
Canada are stricter about cold email than CAN-SPAM).
"""
from __future__ import annotations

import imaplib
import logging
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import parseaddr

from jinja2 import Environment, FileSystemLoader

from ..config import Config
from ..copywriter import draft_reply
from ..db import DB
from .builder_bot import TEMPLATES

log = logging.getLogger(__name__)
STOP_WORDS = ("unsubscribe", "stop", "remove me", "not interested", "no thanks")

# A/B test: each lead is assigned one subject line; `report` shows which wins.
# Keep them honest - CAN-SPAM bans misleading subject lines.
SUBJECT_VARIANTS = {
    "A": "I built a website for {name}",
    "B": "{name}: your new website (free preview)",
    "C": "Made {name} a website, take a look?",
}


def money(cents: int) -> str:
    return f"${cents / 100:,.0f}"


class OutreachBot:
    name = "outreach"

    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)

    @property
    def live(self) -> bool:
        return not self.cfg.dry_run and bool(self.cfg.smtp_host)

    def compose(self, lead: dict, step: int) -> EmailMessage:
        import json

        copy_path = self.cfg.build_dir / f"{lead['id']}-copy.json"
        opener = ""
        if copy_path.exists():
            opener = json.loads(copy_path.read_text()).get("email_opener", "")
        tpl = "email_initial.txt.j2" if step == 0 else "email_followup.txt.j2"
        variant = lead.get("variant") or self.pick_variant(lead)
        body = self.env.get_template(tpl).render(
            lead=lead, cfg=self.cfg, opener=opener, step=step,
            subject=SUBJECT_VARIANTS[variant].format(name=lead["business_name"]),
            setup=money(self.cfg.setup_fee_cents), monthly=money(self.cfg.monthly_fee_cents),
        )
        subject_line, _, text = body.partition("\n")
        msg = EmailMessage()
        msg["Subject"] = subject_line.removeprefix("Subject:").strip()
        msg["From"] = f"{self.cfg.owner_name} at {self.cfg.company_name} <{self.cfg.company_email}>"
        msg["To"] = lead["email"]
        msg["List-Unsubscribe"] = f"<mailto:{self.cfg.company_email}?subject=unsubscribe>"
        msg.set_content(text.strip() + "\n")
        return msg

    @staticmethod
    def pick_variant(lead: dict) -> str:
        keys = sorted(SUBJECT_VARIANTS)
        return keys[lead["id"] % len(keys)]

    def _due(self, lead: dict) -> int | None:
        """Return which step (0 = first email, 1.. = follow-up) is due, or None."""
        if lead["status"] == "deployed":
            return 0
        if lead["status"] == "contacted" and lead["followups_sent"] < self.cfg.max_followups:
            last = datetime.fromisoformat(lead["last_contacted"])
            if datetime.now(timezone.utc) - last >= timedelta(days=self.cfg.followup_days):
                return lead["followups_sent"] + 1
        return None

    def run(self) -> int:
        budget = self.cfg.daily_email_cap - self.db.emails_sent_today()
        if budget <= 0:
            log.info("outreach bot: daily cap reached")
            return 0
        sent = 0
        smtp = None
        try:
            for row in self.db.leads_by_status("deployed", "contacted"):
                if sent >= budget:
                    break
                lead = dict(row)
                if not lead["email"]:
                    continue  # phone-only lead -> appears in `call-list`
                if self.db.is_suppressed(lead["email"]):
                    self.db.update_lead(lead["id"], status="unsubscribed")
                    continue
                step = self._due(lead)
                if step is None:
                    continue
                if self.live and not (
                    (lead["preview_url"] or "").startswith("https://")
                    and not (lead["payment_link_id"] or "").startswith("dry_")
                ):
                    # never email a prospect a local file path or a fake checkout link
                    self.db.log(self.name, "skipped", "needs live preview + payment link", lead["id"])
                    continue
                msg = self.compose(lead, step)
                if self.live:
                    if smtp is None:
                        smtp = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=30)
                        smtp.starttls()
                        smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
                    smtp.send_message(msg)
                self.db.log(self.name, "sent" if self.live else "dry_send",
                            f"step {step}: {msg['Subject']}", lead["id"])
                self.db.update_lead(
                    lead["id"], status="contacted", variant=lead.get("variant") or self.pick_variant(lead),
                    followups_sent=step, last_contacted=datetime.now(timezone.utc).isoformat(),
                )
                sent += 1
        finally:
            if smtp is not None:
                smtp.quit()
        # Prospects who never responded after all follow-ups -> lost
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cfg.followup_days * 2)
        for row in self.db.leads_by_status("contacted"):
            if (row["followups_sent"] >= self.cfg.max_followups and row["last_contacted"]
                    and datetime.fromisoformat(row["last_contacted"]) < cutoff):
                self.db.update_lead(row["id"], status="lost")
        log.info("outreach bot: %d emails (%s)", sent, "live" if self.live else "dry run")
        return sent

    # ---------- inbox processing ----------
    def classify_reply(self, sender: str, text: str) -> str:
        email = parseaddr(sender)[1].lower()
        lowered = text.lower()
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in STOP_WORDS):
            self.db.suppress(email, "reply opt-out")
            status = "unsubscribed"
        else:
            status = "replied"
        row = self.db.conn.execute(
            "SELECT * FROM leads WHERE email = ? AND status IN ('contacted','deployed','replied')",
            (email,),
        ).fetchone()
        if row:
            fields = {"status": status, "last_reply": text[:5000]}
            if status == "replied":
                # suggested answer for the human; shown in Obsidian, never auto-sent
                fields["reply_draft"] = draft_reply(dict(row), text, self.cfg)
            self.db.update_lead(row["id"], **fields)
            self.db.log(self.name, status, text[:200], row["id"])
        return status

    def check_inbox(self) -> int:
        """Reads unseen replies over IMAP: opt-outs are suppressed, everything else
        is flagged 'replied' so a human can close the deal personally."""
        if not (self.cfg.imap_host and self.cfg.smtp_user):
            return 0
        n = 0
        with imaplib.IMAP4_SSL(self.cfg.imap_host) as imap:
            imap.login(self.cfg.smtp_user, self.cfg.smtp_password)
            imap.select("INBOX")
            _, ids = imap.search(None, "UNSEEN")
            for mid in ids[0].split():
                _, data = imap.fetch(mid, "(RFC822)")
                msg = message_from_bytes(data[0][1])
                part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
                text = (part.get_payload(decode=True) or b"").decode(errors="ignore") if part else ""
                self.classify_reply(msg.get("From", ""), f"{msg.get('Subject', '')}\n{text}")
                n += 1
        return n
