"""Upsell bot: grows revenue per client by offering add-on monthly services.

Rules (all configurable in .env):
  * a client is eligible UPSELL_AFTER_DAYS after their first payment
  * at most one offer per client every UPSELL_GAP_DAYS, and each add-on is offered once
  * the add-on is chosen to fit their niche (e.g. booking for salons, reviews for trades)
  * each offer has its own Stripe Payment Link (a separate subscription the client
    explicitly agrees to; we never add charges to an existing subscription)
Offers go out through the same SMTP settings, daily cap and suppression list as outreach.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import requests
from jinja2 import Environment, FileSystemLoader

from ..config import Config
from ..db import DB, now
from ..mailer import Mailer
from .billing_bot import API as STRIPE_API
from .builder_bot import TEMPLATES
from .outreach_bot import money

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Addon:
    key: str
    name: str
    price_cents: int
    pitch: str
    bullets: tuple[str, ...]
    niches: tuple[str, ...] = ()  # empty = fits everyone


CATALOG: tuple[Addon, ...] = (
    Addon("reviews", "Review Booster", 9_900,
          "Turn happy customers into 5-star Google reviews automatically.",
          ("Text/email review requests after every job",
           "Unhappy customers routed to you privately first",
           "Monthly review count report"),
          ("plumber", "roofer", "electrician", "hvac", "landscaper", "cleaner", "auto repair")),
    Addon("booking", "Online Booking", 7_900,
          "Let customers book appointments from your website 24/7.",
          ("Booking calendar on your site", "Automatic reminders cut no-shows",
           "Works on phones"),
          ("hair salon", "barber", "nail salon", "spa", "massage", "dentist", "auto detailing")),
    Addon("seo", "Local SEO", 19_900,
          "Show up higher when people near you search for what you do.",
          ("Google Business Profile optimization", "Monthly local landing page",
           "Ranking report every month")),
)
CATALOG_BY_KEY = {a.key: a for a in CATALOG}


def best_offers(niche: str | None) -> list[Addon]:
    """Niche-specific add-ons first, then the general ones."""
    niche = (niche or "").lower()
    specific = [a for a in CATALOG if niche and niche in a.niches]
    general = [a for a in CATALOG if not a.niches]
    return specific + general


class UpsellBot:
    name = "upsell"

    def __init__(self, db: DB, cfg: Config, session: requests.Session | None = None,
                 mailer: Mailer | None = None):
        self.db, self.cfg = db, cfg
        self.http = session or requests.Session()
        self.mailer = mailer or Mailer(db, cfg)
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)

    @property
    def stripe_live(self) -> bool:
        return not self.cfg.dry_run and bool(self.cfg.stripe_key)

    @property
    def email_live(self) -> bool:
        return self.mailer.live

    # ---------- eligibility ----------
    def next_offer(self, lead) -> Addon | None:
        since = self.db.client_since(lead["id"])
        if not since:
            return None
        t = datetime.now(timezone.utc)
        if t - datetime.fromisoformat(since) < timedelta(days=self.cfg.upsell_after_days):
            return None
        existing = self.db.addons_for(lead["id"])
        if existing:
            last = max(datetime.fromisoformat(a["offered_at"]) for a in existing)
            if t - last < timedelta(days=self.cfg.upsell_gap_days):
                return None
        offered = {a["addon"] for a in existing}
        return next((a for a in best_offers(lead["niche"]) if a.key not in offered), None)

    # ---------- stripe ----------
    def _payment_link(self, lead, addon: Addon) -> tuple[str, str]:
        if not self.stripe_live:
            return f"{self.cfg.company_url}/addon?lead={lead['id']}&a={addon.key}", f"dry_{lead['id']}_{addon.key}"
        auth = (self.cfg.stripe_key, "")
        price = self.http.post(f"{STRIPE_API}/prices", auth=auth, timeout=30, data={
            "currency": self.cfg.currency, "unit_amount": addon.price_cents,
            "recurring[interval]": "month",
            "product_data[name]": f"{addon.name} for {lead['business_name']}",
        })
        price.raise_for_status()
        link = self.http.post(f"{STRIPE_API}/payment_links", auth=auth, timeout=30, data={
            "line_items[0][price]": price.json()["id"], "line_items[0][quantity]": 1,
            "metadata[lead_id]": str(lead["id"]), "metadata[addon]": addon.key,
            "subscription_data[metadata][lead_id]": str(lead["id"]),
            "subscription_data[metadata][addon]": addon.key,
        })
        link.raise_for_status()
        return link.json()["url"], link.json()["id"]

    # ---------- email ----------
    def compose(self, lead, addon: Addon, payment_url: str) -> EmailMessage:
        body = self.env.get_template("email_upsell.txt.j2").render(
            lead=lead, cfg=self.cfg, addon=addon, price=money(addon.price_cents),
            payment_url=payment_url,
        )
        subject, _, text = body.partition("\n")
        msg = EmailMessage()
        msg["Subject"] = subject.removeprefix("Subject:").strip()
        msg["From"] = f"{self.cfg.owner_name} at {self.cfg.company_name} <{self.cfg.company_email}>"
        msg["To"] = lead["email"]
        msg["List-Unsubscribe"] = f"<mailto:{self.cfg.company_email}?subject=unsubscribe>"
        msg.set_content(text.strip() + "\n")
        return msg

    def send_offers(self) -> int:
        sent = 0
        try:
            for lead in self.db.leads_by_status("client"):
                if self.mailer.total_remaining() <= 0:
                    break
                if not lead["email"] or self.db.is_suppressed(lead["email"]):
                    continue
                addon = self.next_offer(lead)
                if addon is None:
                    continue
                if self.email_live and not self.stripe_live:
                    self.db.log(self.name, "skipped", "needs live Stripe link", lead["id"])
                    continue
                sender = self.mailer.pick(lead)
                if sender is None:
                    continue
                try:
                    url, link_id = self._payment_link(lead, addon)
                except requests.RequestException as exc:
                    self.db.log(self.name, "error", str(exc), lead["id"])
                    continue
                msg = self.compose(lead, addon, url)
                self.mailer.send(sender, msg, bot=self.name, lead_id=lead["id"],
                                 detail=f"upsell {addon.key}: {msg['Subject']}")
                with self.db.tx() as c:
                    c.execute(
                        "INSERT OR IGNORE INTO addons (lead_id, addon, price_cents, payment_url, "
                        "payment_link_id, offered_at) VALUES (?,?,?,?,?,?)",
                        (lead["id"], addon.key, addon.price_cents, url, link_id, now()),
                    )
                sent += 1
        finally:
            self.mailer.close()
        return sent

    # ---------- activation ----------
    def activate(self, lead_id: int, addon_key: str, subscription_id: str | None = None) -> bool:
        if addon_key not in CATALOG_BY_KEY:
            raise ValueError(f"unknown add-on {addon_key!r}; choose from {sorted(CATALOG_BY_KEY)}")
        addon = CATALOG_BY_KEY[addon_key]
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO addons (lead_id, addon, price_cents, offered_at) VALUES (?,?,?,?)",
                (lead_id, addon.key, addon.price_cents, now()),
            )
            cur = c.execute(
                "UPDATE addons SET status='active', activated_at=?, subscription_id=COALESCE(?, subscription_id) "
                "WHERE lead_id=? AND addon=? AND status != 'active'",
                (now(), subscription_id, lead_id, addon.key),
            )
        if cur.rowcount:
            self.db.log(self.name, "addon_active", f"{addon.name} +{money(addon.price_cents)}/mo", lead_id)
        return bool(cur.rowcount)

    def check_purchases(self) -> int:
        if not self.stripe_live:
            return 0
        n = 0
        auth = (self.cfg.stripe_key, "")
        offered = self.db.conn.execute(
            "SELECT * FROM addons WHERE status='offered' AND payment_link_id IS NOT NULL"
        ).fetchall()
        for a in offered:
            try:
                r = self.http.get(f"{STRIPE_API}/checkout/sessions", auth=auth, timeout=30,
                                  params={"payment_link": a["payment_link_id"], "limit": 10})
                r.raise_for_status()
            except requests.RequestException as exc:
                self.db.log(self.name, "error", str(exc), a["lead_id"])
                continue
            for s in r.json().get("data", []):
                if s.get("status") == "complete" and s.get("payment_status") == "paid":
                    self.db.record_payment(s["id"], a["lead_id"], s.get("amount_total") or 0,
                                           s.get("currency") or self.cfg.currency, f"addon:{a['addon']}")
                    if self.activate(a["lead_id"], a["addon"], s.get("subscription")):
                        n += 1
                    break
        return n

    def check_cancellations(self) -> int:
        if not self.stripe_live:
            return 0
        n = 0
        rows = self.db.conn.execute(
            "SELECT * FROM addons WHERE status='active' AND subscription_id IS NOT NULL"
        ).fetchall()
        for a in rows:
            try:
                r = self.http.get(f"{STRIPE_API}/subscriptions/{a['subscription_id']}",
                                  auth=(self.cfg.stripe_key, ""), timeout=30)
                r.raise_for_status()
            except requests.RequestException as exc:
                self.db.log(self.name, "error", str(exc), a["lead_id"])
                continue
            if r.json().get("status") in ("canceled", "unpaid", "incomplete_expired"):
                with self.db.tx() as c:
                    c.execute("UPDATE addons SET status='canceled' WHERE id=?", (a["id"],))
                self.db.log(self.name, "addon_canceled", a["addon"], a["lead_id"])
                n += 1
        return n

    def run(self) -> dict:
        return {
            "activated": self.check_purchases(),
            "canceled": self.check_cancellations(),
            "offers_sent": self.send_offers(),
        }
