"""Review Booster: automated review requests for clients with the 'reviews' add-on.

Flow
  1. When the add-on goes active, the client is emailed a private "job done"
     link: {PUBLIC_URL}/jobs/<lead_id>/<token> (bookmark it on your phone).
  2. After a job they enter the customer's name + email, or tap one of their
     recent quote requests. That queues a review request.
  3. The bot emails the customer from the client's business name (replies go
     to the client), with the review link and an optional private-feedback
     link. One reminder after REVIEW_REMINDER_DAYS if they haven't clicked.
  4. Clicks go through /rv/<id>/<token> so the monthly report can count them.

Compliance: every customer always gets the public review link. Private
feedback is an extra option, never a filter (Google policy forbids "review
gating"; the FTC prohibits suppressing negative reviews). One request per
customer per business; the suppression list and opt-out links apply.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from .config import Config
from .db import DB, now
from .mailer import Mailer
from .quotes import EMAIL_RE
from .unsubscribe import url as unsubscribe_url

log = logging.getLogger(__name__)


def _sig(purpose: str, ident: int, secret: str, n: int = 24) -> str:
    return hmac.new((secret or "reviews").encode(), f"{purpose}:{ident}".encode(), hashlib.sha256).hexdigest()[:n]


def jobs_url(lead_id: int, cfg: Config) -> str | None:
    if not (cfg.public_url and cfg.app_secret):
        return None
    return f"{cfg.public_url}/jobs/{lead_id}/{_sig('jobs', lead_id, cfg.app_secret)}"


def verify_jobs(lead_id: int, tok: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(_sig("jobs", lead_id, secret), tok or "")


def click_url(request_id: int, cfg: Config, feedback: bool = False) -> str | None:
    if not (cfg.public_url and cfg.app_secret):
        return None
    base = f"{cfg.public_url}/rv/{request_id}/{_sig('rv', request_id, cfg.app_secret)}"
    return base + ("/feedback" if feedback else "")


def verify_click(request_id: int, tok: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(_sig("rv", request_id, secret), tok or "")


def review_url_for(lead) -> str | None:
    """Explicit review_url wins; Google Places leads get their write-a-review link for free."""
    if lead["review_url"]:
        return lead["review_url"]
    sid = lead["source_id"] or ""
    if sid.startswith("gp:"):
        return f"https://search.google.com/local/writereview?placeid={sid[3:]}"
    return None


def has_booster(db: DB, lead_id: int) -> bool:
    return db.conn.execute(
        "SELECT 1 FROM addons a JOIN leads l ON l.id = a.lead_id WHERE a.lead_id = ? AND a.addon = 'reviews' "
        "AND a.status = 'active' AND l.status = 'client'", (lead_id,)).fetchone() is not None


class ReviewError(ValueError):
    pass


def request_review(db: DB, lead_id: int, name: str, email: str) -> int:
    if not has_booster(db, lead_id):
        raise ReviewError("Review Booster isn't active for this business.")
    email = (email or "").strip().lower()[:200]
    if not EMAIL_RE.match(email):
        raise ReviewError("That email address doesn't look right.")
    with db.tx() as c:
        cur = c.execute("INSERT OR IGNORE INTO review_requests (lead_id, customer_name, customer_email, created_at) "
                        "VALUES (?,?,?,?)", (lead_id, (name or "").strip()[:100], email, now()))
    if not cur.rowcount:
        raise ReviewError(f"{email} has already been asked for a review.")
    db.log("reviews", "review_requested", email, lead_id)
    return cur.lastrowid


def record_click(db: DB, request_id: int) -> str | None:
    row = db.conn.execute("SELECT r.*, l.review_url, l.source_id FROM review_requests r JOIN leads l "
                          "ON l.id = r.lead_id WHERE r.id = ?", (request_id,)).fetchone()
    if row is None:
        return None
    if not row["clicked_at"]:
        with db.tx() as c:
            c.execute("UPDATE review_requests SET clicked_at = ? WHERE id = ?", (now(), request_id))
        db.log("reviews", "review_clicked", "", row["lead_id"])
    return review_url_for(row)


class ReviewBot:
    name = "reviews"

    def __init__(self, db: DB, cfg: Config, mailer: Mailer | None = None):
        self.db, self.cfg = db, cfg
        self.mailer = mailer or Mailer(db, cfg)

    # ---------- emails ----------
    def welcome(self, client) -> bool:
        link = jobs_url(client["id"], self.cfg)
        if not client["email"] or not link:
            return False
        review = review_url_for(client)
        msg = EmailMessage()
        msg["Subject"] = "Your Review Booster is ready: bookmark this link"
        msg["To"] = client["email"]
        msg.set_content(
            f"Hi {client['business_name']} team,\n\nReview Booster is active. After each job, open this link on "
            f"your phone and enter the customer's name and email (or tap a recent quote request):\n\n  {link}\n\n"
            "We'll email them a friendly review request from your business name, plus one reminder if they forget. "
            "Their replies come straight to you.\n\n"
            + (f"Reviews will go to: {review}\n" if review else
               "One thing we need: your Google review link. Reply to this email with it and we'll set it up.\n")
            + f"\nBookmark it now: it's private to {client['business_name']}.\n\n{self.cfg.owner_name}\n{self.cfg.company_name}\n")
        sender = self.mailer.notify_sender()
        if sender is None:
            return False
        self.mailer.send(sender, msg, bot=self.name, lead_id=client["id"], detail="booster welcome", kind="notify")
        return True

    def compose(self, req, client, reminder: bool = False) -> EmailMessage:
        name = (req["customer_name"] or "").split(" ")[0] or "there"
        review = click_url(req["id"], self.cfg) or review_url_for(client)
        feedback = click_url(req["id"], self.cfg, feedback=True)
        msg = EmailMessage()
        msg["Subject"] = (f"Quick reminder from {client['business_name']}" if reminder
                          else f"Thanks for choosing {client['business_name']}!")
        msg["To"] = req["customer_email"]
        lines = [f"Hi {name},", ""]
        lines += ([f"Just a gentle nudge in case you missed it: if you have a minute, {client['business_name']} "
                   "would really appreciate a review."] if reminder else
                  [f"Thank you for choosing {client['business_name']}. If you have a minute, would you share how "
                   "it went? Reviews help a small local business more than you might think."])
        lines += ["", f"Leave a review: {review}", ""]
        if feedback:
            lines += [f"Prefer to tell us something privately as well? {feedback}", ""]
        lines += ["Thank you!", client["business_name"], "",
                  "--", f"Sent for {client['business_name']} by {self.cfg.company_name}, {self.cfg.company_address}",
                  "Reply STOP to opt out."]
        unsub = unsubscribe_url(req["customer_email"], self.cfg)
        if unsub:
            lines.append(f"Unsubscribe instantly: {unsub}")
        msg.set_content("\n".join(lines) + "\n")
        return msg

    def _send(self, req, client, reminder: bool) -> bool:
        if self.db.is_suppressed(req["customer_email"]):
            return False
        sender = self.mailer.pick({"sender": None})
        if sender is None:
            return False
        self.mailer.send(sender, self.compose(req, client, reminder), bot=self.name, lead_id=client["id"],
                         detail=f"review {'reminder' if reminder else 'request'} #{req['id']}",
                         reply_to=client["email"] or None, from_name=client["business_name"])
        with self.db.tx() as c:
            c.execute(f"UPDATE review_requests SET {'reminded_at' if reminder else 'sent_at'} = ? WHERE id = ?",
                      (now(), req["id"]))
        return True

    def run(self) -> dict:
        out = {"welcomed": 0, "sent": 0, "reminded": 0}
        try:
            for a in self.db.conn.execute(
                    "SELECT l.* FROM addons a JOIN leads l ON l.id = a.lead_id WHERE a.addon = 'reviews' "
                    "AND a.status = 'active' AND l.status = 'client'").fetchall():
                key = f"booster_welcome_{a['id']}"
                if not self.db.get_setting(key) and self.welcome(a):
                    self.db.set_setting(key, now())
                    out["welcomed"] += 1
            pending = self.db.conn.execute(
                "SELECT * FROM review_requests WHERE sent_at IS NULL ORDER BY id").fetchall()
            cutoff = (datetime.now(timezone.utc) - timedelta(days=self.cfg.review_reminder_days)).isoformat()
            due = self.db.conn.execute(
                "SELECT * FROM review_requests WHERE sent_at IS NOT NULL AND sent_at < ? AND reminded_at IS NULL "
                "AND clicked_at IS NULL AND feedback IS NULL ORDER BY id", (cutoff,)).fetchall()
            for reqs, reminder, key in ((pending, False, "sent"), (due, True, "reminded")):
                for req in reqs:
                    if not has_booster(self.db, req["lead_id"]):
                        continue
                    client = self.db.get_lead(req["lead_id"])
                    if not review_url_for(client):
                        self.db.log(self.name, "skipped", "no review link set", client["id"])
                        continue
                    if self._send(req, client, reminder):
                        out[key] += 1
        finally:
            self.mailer.close()
        return out

    def stats(self, lead_id: int, start: str | None = None, end: str | None = None) -> dict:
        sql = "SELECT COUNT(*) n, COUNT(clicked_at) c FROM review_requests WHERE lead_id = ? AND sent_at IS NOT NULL"
        args: list = [lead_id]
        if start:
            sql += " AND substr(sent_at,1,10) >= ?"
            args.append(start)
        if end:
            sql += " AND substr(sent_at,1,10) <= ?"
            args.append(end)
        r = self.db.conn.execute(sql, args).fetchone()
        return {"sent": r["n"], "clicked": r["c"]}


JOBS_PAGE = """<h1>Ask for a review</h1>
<p>{business}: enter a customer you just finished a job for.</p>
{notice}
<form method="post" style="display:grid;gap:10px">
<label>Customer name<br><input name="name" maxlength="100" style="width:100%;padding:10px"></label>
<label>Customer email<br><input name="email" type="email" required maxlength="200" style="width:100%;padding:10px"></label>
<button type="submit">Ask for a review</button></form>
{recent}"""
