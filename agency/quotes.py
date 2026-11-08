"""Quote requests: the "Get a free quote" form on every client website.

Flow: visitor submits the form on the client's site -> POST to
`{PUBLIC_URL}/quote/<lead_id>/<token>` (served by `agency serve`) -> stored in
the `quotes` table -> emailed to the business owner right away with Reply-To
set to the customer, so they can just hit reply.

Only *paying clients'* sites get the form (previews never collect data for a
business that hasn't signed up). The count shows up in the monthly report and
in Obsidian.

Spam protection: signed per-site URL, hidden honeypot field, length limits,
per-IP rate limit (in server.py), and a basic contact-required check.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from email.message import EmailMessage

from .config import Config
from .db import DB, now
from .mailer import Mailer

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LIMITS = {"name": 100, "phone": 40, "email": 200, "service": 100, "message": 3000}

def token(lead_id: int, secret: str) -> str:
    return hmac.new(secret.encode(), f"quote:{lead_id}".encode(), hashlib.sha256).hexdigest()[:24]


def verify(lead_id: int, tok: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(token(lead_id, secret), tok or "")


def form_action(lead_id: int, cfg: Config) -> str | None:
    if not (cfg.public_url and cfg.app_secret):
        return None
    return f"{cfg.public_url}/quote/{lead_id}/{token(lead_id, cfg.app_secret)}"


class QuoteError(ValueError):
    pass


def clean(form: dict[str, str]) -> dict[str, str]:
    """Validate a submission. Raises QuoteError with a visitor-friendly message."""
    if form.get("website"):  # honeypot: hidden from humans, bots fill it in
        raise QuoteError("spam")
    data = {k: (form.get(k) or "").strip()[:limit] for k, limit in LIMITS.items()}
    if not data["name"]:
        raise QuoteError("Please enter your name.")
    if not (data["phone"] or data["email"]):
        raise QuoteError("Please enter a phone number or email so they can reach you.")
    if data["email"] and not EMAIL_RE.match(data["email"]):
        raise QuoteError("That email address doesn't look right.")
    if data["phone"] and len(re.sub(r"\D", "", data["phone"])) < 7:
        raise QuoteError("That phone number doesn't look right.")
    return data


def compose_notification(lead, q: dict, cfg: Config) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"New quote request from your website: {q['name']}"
    msg["To"] = lead["email"]
    if q["email"]:
        msg["Reply-To"] = q["email"]
    lines = [
        f"You have a new quote request from {lead['business_name']}'s website.",
        "",
        f"Name:    {q['name']}",
        f"Phone:   {q['phone'] or '-'}",
        f"Email:   {q['email'] or '-'}",
        f"Service: {q['service'] or '-'}",
        "",
        "Message:",
        q["message"] or "(no message)",
        "",
        "Tip: customers who hear back within an hour are far more likely to book.",
        "Reply to this email to answer them directly." if q["email"] else "Give them a call back.",
        "",
        f"-- {cfg.company_name} (your website team)",
    ]
    msg.set_content("\n".join(lines) + "\n")
    return msg


def submit(db: DB, cfg: Config, lead_id: int, form: dict[str, str],
           mailer: Mailer | None = None) -> int:
    """Store a quote and notify the business owner. Returns the quote id."""
    lead = db.get_lead(lead_id)
    if lead is None or lead["status"] != "client":
        raise QuoteError("This form is not active.")
    q = clean(form)
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO quotes (lead_id, name, phone, email, service, message, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (lead_id, q["name"], q["phone"], q["email"], q["service"], q["message"], now()),
        )
        quote_id = cur.lastrowid
    db.log("quotes", "quote_request", q["service"] or "quote request", lead_id)
    if lead["email"]:
        mailer = mailer or Mailer(db, cfg)
        try:
            sender = mailer.notify_sender()
            if sender is not None:
                mailer.send(sender, compose_notification(lead, q, cfg), bot="quotes", lead_id=lead_id,
                            detail=f"quote #{quote_id} forwarded", kind="notify")
                with db.tx() as c:
                    c.execute("UPDATE quotes SET notified = 1 WHERE id = ?", (quote_id,))
        except Exception as exc:  # the quote is saved either way; show it in Obsidian
            log.warning("quote #%s notification failed: %s", quote_id, exc)
            db.log("quotes", "error", f"notify failed: {exc}", lead_id)
        finally:
            mailer.close()
    return quote_id


def counts(db: DB, lead_id: int, start: str | None = None, end: str | None = None) -> int:
    sql, args = "SELECT COUNT(*) FROM quotes WHERE lead_id = ?", [lead_id]
    if start:
        sql += " AND substr(created_at,1,10) >= ?"
        args.append(start)
    if end:
        sql += " AND substr(created_at,1,10) <= ?"
        args.append(end)
    return db.conn.execute(sql, args).fetchone()[0]
