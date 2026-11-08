"""Referral program: clients refer other businesses, both sides win.

  * every client gets a personal link: {PUBLIC_URL}/r/<code>
  * the link opens a short "free website preview" form; the referred business
    submits it themselves and ticks a consent box, so it's a warm, opted-in lead
  * referred leads jump the outreach queue, get a "<referrer> recommended us"
    email and REFERRAL_DISCOUNT_CENTS off setup
  * when a referred lead pays, the referrer gets their next month free (a
    one-time Stripe coupon on their subscription) and a thank-you email
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from email.message import EmailMessage

import requests

from .config import Config
from .db import DB, now
from .mailer import Mailer
from .quotes import EMAIL_RE

log = logging.getLogger(__name__)
STRIPE_API = "https://api.stripe.com/v1"


def code_for(lead, secret: str) -> str:
    stem = re.sub(r"[^A-Z0-9]", "", (lead["business_name"] or "").upper())[:8] or "CLIENT"
    sig = hmac.new((secret or "referral").encode(), f"ref:{lead['id']}".encode(), hashlib.sha256).hexdigest()[:4].upper()
    return f"{stem}-{lead['id']}{sig}"


def referrer_for_code(db: DB, code: str, secret: str):
    m = re.fullmatch(r"[A-Z0-9]{1,8}-(\d+)([0-9A-F]{4})", (code or "").upper())
    if not m:
        return None
    lead = db.get_lead(int(m.group(1)))
    if lead is None or lead["status"] != "client" or code_for(lead, secret) != code.upper():
        return None
    return lead


def link_for(lead, cfg: Config) -> str | None:
    if not cfg.public_url or not cfg.referrals_enabled:
        return None
    return f"{cfg.public_url}/r/{code_for(lead, cfg.app_secret)}"


class ReferralError(ValueError):
    pass


def submit(db: DB, cfg: Config, referrer, form: dict[str, str]) -> int:
    """Create a consented, referred lead from the landing-page form."""
    if form.get("website"):
        raise ReferralError("spam")
    data = {k: (form.get(k) or "").strip()[:limit] for k, limit in
            {"business_name": 120, "contact_name": 100, "phone": 40, "email": 200,
             "city": 80, "niche": 80}.items()}
    if not data["business_name"]:
        raise ReferralError("Please enter your business name.")
    if not EMAIL_RE.match(data["email"]):
        raise ReferralError("Please enter a valid email so we can send your preview.")
    if form.get("consent") not in ("on", "yes", "1", "true"):
        raise ReferralError("Please tick the box so we're allowed to contact you.")
    existing = db.conn.execute("SELECT id FROM leads WHERE lower(email) = ?", (data["email"].lower(),)).fetchone()
    # an opt-out is never undone by a web form (anyone can type any email into it);
    # same neutral answer so the form doesn't reveal who is on the list
    if existing or db.is_suppressed(data["email"]):
        raise ReferralError("We already have your details. We'll be in touch!")
    lead_id = db.add_lead(
        business_name=data["business_name"], niche=data["niche"] or None, city=data["city"] or None,
        phone=data["phone"] or None, email=data["email"].lower(), source="referral",
        source_id=f"ref:{data['email'].lower()}", referred_by=referrer["id"],
        notes=f"Referred by {referrer['business_name']}; contact: {data['contact_name'] or '-'}; consented {now()}",
    )
    db.log("referrals", "referral_signup", f"from {referrer['business_name']}", lead_id)
    return lead_id


class ReferralBot:
    name = "referrals"

    def __init__(self, db: DB, cfg: Config, session: requests.Session | None = None,
                 mailer: Mailer | None = None):
        self.db, self.cfg = db, cfg
        self.http = session or requests.Session()
        self.mailer = mailer or Mailer(db, cfg)

    @property
    def stripe_live(self) -> bool:
        return not self.cfg.dry_run and bool(self.cfg.stripe_key)

    def _free_month(self, referrer) -> str | None:
        """One-time coupon worth one month, applied to the referrer's next invoice."""
        if not self.stripe_live or not referrer["subscription_id"]:
            return None
        auth = (self.cfg.stripe_key, "")
        coupon = self.http.post(f"{STRIPE_API}/coupons", auth=auth, timeout=30, data={
            "amount_off": self.cfg.monthly_fee_cents, "currency": self.cfg.currency, "duration": "once",
            "name": "Referral thank-you: 1 free month"})
        coupon.raise_for_status()
        sub = self.http.post(f"{STRIPE_API}/subscriptions/{referrer['subscription_id']}", auth=auth, timeout=30,
                             data={"discounts[0][coupon]": coupon.json()["id"]})
        sub.raise_for_status()
        return coupon.json()["id"]

    def _thank(self, referrer, referred) -> None:
        if not referrer["email"]:
            return
        msg = EmailMessage()
        msg["Subject"] = "Thank you! Your next month is on us"
        msg["To"] = referrer["email"]
        msg.set_content(
            f"Hi {referrer['business_name']} team,\n\n{referred['business_name']} just signed up for a website "
            f"thanks to your recommendation, so your next month (${self.cfg.monthly_fee_cents / 100:,.0f}) is free.\n\n"
            f"Know anyone else? Your link: {link_for(referrer, self.cfg) or '(reply to this email)'}\n\n"
            f"Thank you!\n{self.cfg.owner_name}\n{self.cfg.company_name}\n")
        sender = self.mailer.notify_sender()
        if sender is not None:
            try:
                self.mailer.send(sender, msg, bot=self.name, lead_id=referrer["id"],
                                 detail="referral thank-you", kind="notify")
            finally:
                self.mailer.close()

    def run(self) -> int:
        """Reward referrers whose referred leads have paid. Idempotent."""
        rows = self.db.conn.execute(
            "SELECT l.* FROM leads l WHERE l.referred_by IS NOT NULL AND l.status IN ('paid','client') "
            "AND NOT EXISTS (SELECT 1 FROM referral_rewards r WHERE r.referred_lead_id = l.id)").fetchall()
        n = 0
        for referred in rows:
            referrer = self.db.get_lead(referred["referred_by"])
            if referrer is None or referrer["status"] != "client":
                continue  # reward only active clients; retried if they come back
            try:
                coupon = self._free_month(referrer)
            except requests.RequestException as exc:
                self.db.log(self.name, "error", f"reward: {exc}", referrer["id"])
                continue
            with self.db.tx() as c:
                c.execute("INSERT OR IGNORE INTO referral_rewards (referred_lead_id, referrer_id, coupon_id, "
                          "applied_at) VALUES (?,?,?,?)", (referred["id"], referrer["id"], coupon, now()))
            self.db.log(self.name, "referral_reward", f"free month for referring {referred['business_name']}",
                        referrer["id"])
            self._thank(referrer, referred)
            n += 1
        return n

    def stats(self) -> dict:
        q = self.db.conn.execute
        return {
            "signups": q("SELECT COUNT(*) FROM leads WHERE referred_by IS NOT NULL").fetchone()[0],
            "won": q("SELECT COUNT(*) FROM leads WHERE referred_by IS NOT NULL "
                     "AND status IN ('paid','client','churned')").fetchone()[0],
            "rewards": q("SELECT COUNT(*) FROM referral_rewards").fetchone()[0],
        }


LANDING = """<h1>Free website preview</h1>
<p><strong>{referrer}</strong> recommended us. Tell us about your business and we'll build you a
free preview website, no obligation. Because you were referred, you get <strong>{discount} off setup</strong>.</p>
<form method="post" style="display:grid;gap:10px">
<label>Business name<br><input name="business_name" required maxlength="120" style="width:100%;padding:10px"></label>
<label>Your name<br><input name="contact_name" maxlength="100" style="width:100%;padding:10px"></label>
<label>Email<br><input name="email" type="email" required maxlength="200" style="width:100%;padding:10px"></label>
<label>Phone<br><input name="phone" type="tel" maxlength="40" style="width:100%;padding:10px"></label>
<label>What do you do? (e.g. plumber, salon)<br><input name="niche" maxlength="80" style="width:100%;padding:10px"></label>
<label>City<br><input name="city" maxlength="80" style="width:100%;padding:10px"></label>
<div style="position:absolute;left:-9999px" aria-hidden="true"><input name="website" tabindex="-1" autocomplete="off"></div>
<label><input type="checkbox" name="consent" required> It's OK for {company} to email me about my free preview.</label>
<button type="submit">Get my free preview</button></form>"""
