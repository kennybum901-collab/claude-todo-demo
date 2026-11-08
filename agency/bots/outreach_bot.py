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
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import parseaddr

from jinja2 import Environment, FileSystemLoader

from ..config import Config
from ..copywriter import draft_reply
from ..db import DB
from ..mailer import Mailer
from ..unsubscribe import url as unsubscribe_url
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

    def __init__(self, db: DB, cfg: Config, mailer: Mailer | None = None):
        self.db, self.cfg = db, cfg
        self.mailer = mailer or Mailer(db, cfg)
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)

    @property
    def live(self) -> bool:
        return self.mailer.live

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
            unsub=unsubscribe_url(lead["email"], self.cfg) if lead.get("email") else None,
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
        if self.mailer.total_remaining() <= 0:
            log.info("outreach bot: no inbox has capacity left today")
            return 0
        sent = 0
        try:
            for row in self.db.leads_by_status("deployed", "contacted"):
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
                sender = self.mailer.pick(lead)
                if sender is None:
                    if self.mailer.total_remaining() <= 0:
                        break
                    continue  # this lead's inbox is full today; others may still send
                msg = self.compose(lead, step)
                self.mailer.send(sender, msg, bot=self.name, lead_id=lead["id"],
                                 detail=f"step {step}: {msg['Subject']}")
                self.db.update_lead(
                    lead["id"], status="contacted", variant=lead.get("variant") or self.pick_variant(lead),
                    sender=sender.email.lower(), followups_sent=step,
                    last_contacted=datetime.now(timezone.utc).isoformat(),
                )
                sent += 1
        finally:
            self.mailer.close()
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
        client = self.db.conn.execute(
            "SELECT * FROM leads WHERE email = ? AND status IN ('client','paid','churned')", (email,)
        ).fetchone()
        if client:
            # existing customer (e.g. answering an add-on offer). Their status stays
            # the same; an opt-out above only stops future marketing emails.
            self.db.update_lead(client["id"], last_reply=text[:5000])
            self.db.log(self.name, "client_reply", text[:200], client["id"])
            return "client_opt_out" if status == "unsubscribed" else "client_reply"
        return status

    # ---------- bounces ----------
    BOUNCE_FROM = ("mailer-daemon", "postmaster", "mail delivery")
    ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")

    def is_bounce(self, from_header: str, subject: str, content_type: str) -> bool:
        f, subj = from_header.lower(), subject.lower()
        return (any(b in f for b in self.BOUNCE_FROM) or "multipart/report" in content_type
                or "undeliverable" in subj or "delivery status notification" in subj
                or "returned mail" in subj)

    def handle_bounce(self, text: str, inbox: str | None = None) -> int:
        """Suppress every lead address named in a bounce and count it against the inbox."""
        hits = 0
        for addr in {a.lower().rstrip(".") for a in self.ADDR_RE.findall(text)}:
            row = self.db.conn.execute(
                "SELECT * FROM leads WHERE email = ? AND status IN ('deployed','contacted')", (addr,)
            ).fetchone()
            if not row:
                continue
            self.db.suppress(addr, "bounce")
            self.db.update_lead(row["id"], status="bounced")
            self.db.log(self.name, "bounce", addr, row["id"],
                        sender=(inbox or row["sender"] or "").lower() or None)
            hits += 1
        return hits

    def process_message(self, msg, inbox: str | None = None) -> str:
        part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
        text = (part.get_payload(decode=True) or b"").decode(errors="ignore") if part else ""
        subject = msg.get("Subject", "")
        if self.is_bounce(msg.get("From", ""), subject, msg.get_content_type()):
            full = msg.as_string() if msg.is_multipart() else text
            return "bounce" if self.handle_bounce(full, inbox) else "bounce_unmatched"
        return self.classify_reply(msg.get("From", ""), f"{subject}\n{text}")

    def check_inbox(self) -> int:
        """Reads unseen mail in every inbox: bounces suppress the address, opt-outs are
        honored, everything else is flagged 'replied' for a human to close."""
        n = 0
        for sender in self.mailer.senders:
            if not (sender.imap_host and sender.smtp_user):
                continue
            try:
                with imaplib.IMAP4_SSL(sender.imap_host) as imap:
                    imap.login(sender.smtp_user, sender.password)
                    imap.select("INBOX")
                    _, ids = imap.search(None, "UNSEEN")
                    for mid in ids[0].split():
                        _, data = imap.fetch(mid, "(RFC822)")
                        self.process_message(message_from_bytes(data[0][1]), sender.email)
                        n += 1
            except (imaplib.IMAP4.error, OSError) as exc:
                self.db.log(self.name, "error", f"imap {sender.email}: {exc}")
        return n
