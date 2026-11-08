"""Win-back bot: a short, respectful second chance for people who left.

Audiences (both honor the suppression list, inbox caps and STOP replies):
  * churned clients: email 1 at WINBACK_CHURN_DAYS after cancelling, email 2
    30 days later. Offer: relaunch the saved site with the setup fee waived.
  * lost prospects (never replied to outreach): ONE email at WINBACK_LOST_DAYS,
    then never again. Offer: go live with no setup fee.

Each offer has its own Stripe Payment Link (monthly plan only, metadata
winback=1). Paying moves the lead to 'paid'; the normal onboarding step then
rebuilds the final site, deploys it and makes them a client again.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import requests
from jinja2 import Environment, FileSystemLoader

from ..config import Config
from ..db import DB, now
from ..mailer import Mailer
from ..quotes import counts as quote_counts
from ..unsubscribe import url as unsubscribe_url
from .billing_bot import API as STRIPE_API
from .billing_bot import BillingBot
from .builder_bot import TEMPLATES
from .outreach_bot import money

log = logging.getLogger(__name__)
SCHEDULE = {"churned": (1, 2), "lost": (1,)}
GAP_DAYS = 30  # between churned emails 1 and 2


class WinbackBot:
    name = "winback"

    def __init__(self, db: DB, cfg: Config, session: requests.Session | None = None,
                 mailer: Mailer | None = None):
        self.db, self.cfg = db, cfg
        self.http = session or requests.Session()
        self.mailer = mailer or Mailer(db, cfg)
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)

    @property
    def stripe_live(self) -> bool:
        return not self.cfg.dry_run and bool(self.cfg.stripe_key)

    # ---------- timing ----------
    def _since(self, lead, kind: str) -> datetime | None:
        row = self.db.conn.execute(
            "SELECT MAX(created_at) FROM events WHERE lead_id = ? AND kind = ?", (lead["id"], kind)
        ).fetchone()
        ts = row[0] or lead["updated_at"]
        return datetime.fromisoformat(ts) if ts else None

    def next_step(self, lead, audience: str) -> int | None:
        sent = self.db.conn.execute(
            "SELECT step, sent_at FROM winbacks WHERE lead_id = ? AND audience = ? ORDER BY step",
            (lead["id"], audience)).fetchall()
        steps = SCHEDULE[audience]
        if len(sent) >= len(steps):
            return None
        t = datetime.now(timezone.utc)
        if sent:
            if t - datetime.fromisoformat(sent[-1]["sent_at"]) < timedelta(days=GAP_DAYS):
                return None
            return steps[len(sent)]
        start = self._since(lead, "churned" if audience == "churned" else "lost")
        wait = self.cfg.winback_churn_days if audience == "churned" else self.cfg.winback_lost_days
        if start is None or t - start < timedelta(days=wait):
            return None
        return steps[0]

    # ---------- stripe ----------
    def _payment_link(self, lead, audience: str) -> tuple[str, str]:
        existing = self.db.conn.execute(
            "SELECT payment_url, payment_link_id FROM winbacks WHERE lead_id = ? AND audience = ? "
            "AND payment_url IS NOT NULL ORDER BY step LIMIT 1", (lead["id"], audience)).fetchone()
        if existing:
            return existing["payment_url"], existing["payment_link_id"]
        if not self.stripe_live:
            return f"{self.cfg.company_url}/welcome-back?lead={lead['id']}", f"dry_wb_{lead['id']}"
        auth = (self.cfg.stripe_key, "")
        price = self.http.post(f"{STRIPE_API}/prices", auth=auth, timeout=30, data={
            "currency": self.cfg.currency, "unit_amount": self.cfg.monthly_fee_cents,
            "recurring[interval]": "month",
            "product_data[name]": f"Website for {lead['business_name']} - hosting & care plan",
        })
        price.raise_for_status()
        link = self.http.post(f"{STRIPE_API}/payment_links", auth=auth, timeout=30, data={
            "line_items[0][price]": price.json()["id"], "line_items[0][quantity]": 1,
            "metadata[lead_id]": str(lead["id"]), "metadata[winback]": "1",
            "subscription_data[metadata][lead_id]": str(lead["id"]),
        })
        link.raise_for_status()
        return link.json()["url"], link.json()["id"]

    # ---------- email ----------
    def compose(self, lead, audience: str, step: int, payment_url: str) -> EmailMessage:
        tpl = "email_winback_client.txt.j2" if audience == "churned" else "email_winback_prospect.txt.j2"
        body = self.env.get_template(tpl).render(
            lead=lead, cfg=self.cfg, step=step, payment_url=payment_url,
            monthly=money(self.cfg.monthly_fee_cents), quotes_total=quote_counts(self.db, lead["id"]),
            unsub=unsubscribe_url(lead["email"], self.cfg),
        )
        subject, _, text = body.partition("\n")
        msg = EmailMessage()
        msg["Subject"] = subject.removeprefix("Subject:").strip()
        msg["To"] = lead["email"]
        msg.set_content(text.strip() + "\n")
        return msg

    def send(self) -> int:
        sent = 0
        try:
            for audience, status in (("churned", "churned"), ("lost", "lost")):
                for lead in self.db.leads_by_status(status):
                    if self.mailer.total_remaining() <= 0:
                        return sent
                    if not lead["email"] or self.db.is_suppressed(lead["email"]):
                        continue
                    step = self.next_step(lead, audience)
                    if step is None:
                        continue
                    if self.mailer.live and not self.stripe_live:
                        self.db.log(self.name, "skipped", "needs live Stripe link", lead["id"])
                        continue
                    sender = self.mailer.pick(lead)
                    if sender is None:
                        continue
                    try:
                        url, link_id = self._payment_link(lead, audience)
                    except requests.RequestException as exc:
                        self.db.log(self.name, "error", str(exc), lead["id"])
                        continue
                    msg = self.compose(lead, audience, step, url)
                    self.mailer.send(sender, msg, bot=self.name, lead_id=lead["id"],
                                     detail=f"winback {audience} #{step}: {msg['Subject']}")
                    with self.db.tx() as c:
                        c.execute(
                            "INSERT OR IGNORE INTO winbacks (lead_id, audience, step, payment_url, "
                            "payment_link_id, sent_at) VALUES (?,?,?,?,?,?)",
                            (lead["id"], audience, step, url, link_id, now()))
                    sent += 1
        finally:
            self.mailer.close()
        return sent

    # ---------- recovery ----------
    def recover(self, lead_id: int, payment_id: str, amount: int, currency: str,
                subscription_id: str | None) -> bool:
        lead = self.db.get_lead(lead_id)
        if lead is None or lead["status"] not in ("churned", "lost"):
            return False
        BillingBot(self.db, self.cfg).mark_paid(lead_id, payment_id, amount, currency,
                                                subscription_id=subscription_id, kind="winback")
        with self.db.tx() as c:
            c.execute("UPDATE winbacks SET recovered_at = ? WHERE lead_id = ? AND recovered_at IS NULL",
                      (now(), lead_id))
        self.db.log(self.name, "recovered", lead["status"], lead_id)
        return True

    def check_purchases(self) -> int:
        if not self.stripe_live:
            return 0
        n = 0
        rows = self.db.conn.execute(
            "SELECT DISTINCT w.lead_id, w.payment_link_id FROM winbacks w JOIN leads l ON l.id = w.lead_id "
            "WHERE w.recovered_at IS NULL AND l.status IN ('churned','lost') AND w.payment_link_id IS NOT NULL"
        ).fetchall()
        for r in rows:
            try:
                resp = self.http.get(f"{STRIPE_API}/checkout/sessions", auth=(self.cfg.stripe_key, ""),
                                     params={"payment_link": r["payment_link_id"], "limit": 10}, timeout=30)
                resp.raise_for_status()
            except requests.RequestException as exc:
                self.db.log(self.name, "error", str(exc), r["lead_id"])
                continue
            for s in resp.json().get("data", []):
                if s.get("status") == "complete" and s.get("payment_status") == "paid":
                    if self.recover(r["lead_id"], s["id"], s.get("amount_total") or 0,
                                    s.get("currency") or self.cfg.currency, s.get("subscription")):
                        n += 1
                    break
        return n

    def stats(self) -> dict:
        row = self.db.conn.execute(
            "SELECT COUNT(DISTINCT lead_id) contacted, COUNT(DISTINCT CASE WHEN recovered_at IS NOT NULL "
            "THEN lead_id END) recovered FROM winbacks").fetchone()
        return {"contacted": row["contacted"], "recovered": row["recovered"]}

    def run(self) -> dict:
        return {"recovered": self.check_purchases(), "sent": self.send()}
