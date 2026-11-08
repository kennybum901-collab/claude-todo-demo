"""Billing bot: creates a Stripe Payment Link per prospect and detects payments.

Each link bundles the one-time setup fee + the monthly care plan (subscription).
Payment detection polls completed Checkout Sessions for each link, so no public
webhook server is required. Leads that pay move to status 'paid'; the
orchestrator then publishes the final (non-preview) site and marks them 'client'.
"""
from __future__ import annotations

import logging

import requests

from ..config import Config
from ..db import DB

log = logging.getLogger(__name__)
API = "https://api.stripe.com/v1"


class BillingBot:
    name = "billing"

    def __init__(self, db: DB, cfg: Config, session: requests.Session | None = None):
        self.db, self.cfg = db, cfg
        self.http = session or requests.Session()

    @property
    def live(self) -> bool:
        return not self.cfg.dry_run and bool(self.cfg.stripe_key)

    def _post(self, path: str, data: dict) -> dict:
        r = self.http.post(f"{API}{path}", data=data, auth=(self.cfg.stripe_key, ""), timeout=30)
        r.raise_for_status()
        return r.json()

    def _get(self, path: str, params: dict) -> dict:
        r = self.http.get(f"{API}{path}", params=params, auth=(self.cfg.stripe_key, ""), timeout=30)
        r.raise_for_status()
        return r.json()

    def create_payment_link(self, lead: dict) -> tuple[str, str]:
        if not self.live:
            return f"{self.cfg.company_url}/claim?lead={lead['id']}", f"dry_{lead['id']}"
        product_name = f"Website for {lead['business_name']}"
        setup = self._post("/prices", {
            "currency": self.cfg.currency, "unit_amount": self.cfg.setup_fee_cents,
            "product_data[name]": f"{product_name} - setup",
        })
        monthly = self._post("/prices", {
            "currency": self.cfg.currency, "unit_amount": self.cfg.monthly_fee_cents,
            "recurring[interval]": "month",
            "product_data[name]": f"{product_name} - hosting & care plan",
        })
        link = self._post("/payment_links", {
            "line_items[0][price]": setup["id"], "line_items[0][quantity]": 1,
            "line_items[1][price]": monthly["id"], "line_items[1][quantity]": 1,
            "metadata[lead_id]": str(lead["id"]),
            "subscription_data[metadata][lead_id]": str(lead["id"]),
        })
        return link["url"], link["id"]

    def create_links(self) -> int:
        n = 0
        for row in self.db.leads_by_status("new", "built", "deployed", "contacted", "replied"):
            if row["payment_url"]:
                continue
            try:
                url, link_id = self.create_payment_link(dict(row))
            except requests.RequestException as exc:
                self.db.log(self.name, "error", str(exc), row["id"])
                continue
            self.db.update_lead(row["id"], payment_url=url, payment_link_id=link_id)
            self.db.log(self.name, "link_created", url, row["id"])
            n += 1
        return n

    def check_payments(self) -> int:
        if not self.live:
            return 0
        paid = 0
        for row in self.db.leads_by_status("deployed", "contacted", "replied"):
            if not row["payment_link_id"]:
                continue
            try:
                sessions = self._get("/checkout/sessions",
                                     {"payment_link": row["payment_link_id"], "limit": 10})
            except requests.RequestException as exc:
                self.db.log(self.name, "error", str(exc), row["id"])
                continue
            for s in sessions.get("data", []):
                if s.get("status") == "complete" and s.get("payment_status") == "paid":
                    self.mark_paid(row["id"], s["id"], s.get("amount_total") or 0,
                                   s.get("currency") or self.cfg.currency)
                    paid += 1
                    break
        return paid

    def mark_paid(self, lead_id: int, payment_id: str, amount: int, currency: str) -> None:
        if self.db.record_payment(payment_id, lead_id, amount, currency, "setup+first_month"):
            self.db.update_lead(lead_id, status="paid")
            self.db.log(self.name, "paid", f"{amount} {currency}", lead_id)

    def run(self) -> dict:
        return {"links": self.create_links(), "paid": self.check_payments()}
