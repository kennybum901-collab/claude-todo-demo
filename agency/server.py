"""Production server: `python -m agency serve`.

One process that:
  * runs the full bot pipeline every RUN_EVERY_MINUTES in a background thread
  * receives Stripe webhooks (instant payments, add-on purchases, cancellations,
    failed cards). Signatures are verified with STRIPE_WEBHOOK_SECRET
  * serves one-click unsubscribe links (required by Gmail/Yahoo for bulk senders)
  * exposes /health for an external uptime monitor (no customer data)

Standard library only (http.server); put it behind Caddy/nginx for HTTPS. See
docker-compose.yml.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
from collections import defaultdict, deque
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .bots.billing_bot import BillingBot
from .bots.upsell_bot import UpsellBot
from .config import Config
from .db import DB, now
from .orchestrator import Orchestrator
from .quotes import QuoteError, submit as submit_quote, verify as verify_quote
from .unsubscribe import verify as verify_unsubscribe

log = logging.getLogger(__name__)
SIGNATURE_TOLERANCE = 300  # seconds


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------
def verify_stripe_signature(payload: bytes, header: str, secret: str, now_ts: float | None = None) -> bool:
    """Implements Stripe's documented scheme: HMAC-SHA256 over "{t}.{payload}"."""
    if not (secret and header):
        return False
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        k, _, v = item.strip().partition("=")
        parts.setdefault(k, []).append(v)
    try:
        ts = int(parts["t"][0])
    except (KeyError, ValueError):
        return False
    if abs((now_ts or time.time()) - ts) > SIGNATURE_TOLERANCE:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", []))


class StripeEvents:
    """Applies Stripe events to the database. Every handler is idempotent, so the
    hourly polling in BillingBot/UpsellBot remains a safe backup."""

    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        self.billing = BillingBot(db, cfg)
        self.upsell = UpsellBot(db, cfg)

    def _lead_for_subscription(self, sub_id: str | None, metadata: dict) -> tuple[int | None, str | None]:
        lead_id = metadata.get("lead_id")
        addon = metadata.get("addon")
        if lead_id and str(lead_id).isdigit():
            return int(lead_id), addon
        if not sub_id:
            return None, None
        row = self.db.conn.execute("SELECT lead_id, addon FROM addons WHERE subscription_id = ?",
                                   (sub_id,)).fetchone()
        if row:
            return row["lead_id"], row["addon"]
        row = self.db.conn.execute("SELECT id FROM leads WHERE subscription_id = ?", (sub_id,)).fetchone()
        return (row["id"], None) if row else (None, None)

    def handle(self, event: dict) -> str:
        etype = event.get("type", "")
        obj = event.get("data", {}).get("object", {})
        meta = obj.get("metadata") or {}

        if etype == "checkout.session.completed":
            if obj.get("payment_status") != "paid":
                return "ignored_unpaid"
            lead_id = meta.get("lead_id")
            if not (lead_id and str(lead_id).isdigit()) or not self.db.get_lead(int(lead_id)):
                return "unknown_lead"
            lead_id = int(lead_id)
            amount, currency = obj.get("amount_total") or 0, obj.get("currency") or self.cfg.currency
            if meta.get("addon"):
                self.db.record_payment(obj["id"], lead_id, amount, currency, f"addon:{meta['addon']}")
                self.upsell.activate(lead_id, meta["addon"], obj.get("subscription"))
                return "addon_activated"
            self.billing.mark_paid(lead_id, obj["id"], amount, currency,
                                   subscription_id=obj.get("subscription"))
            return "lead_paid"

        if etype == "customer.subscription.deleted":
            lead_id, addon = self._lead_for_subscription(obj.get("id"), meta)
            if lead_id is None:
                return "unknown_subscription"
            if addon:
                with self.db.tx() as c:
                    c.execute("UPDATE addons SET status='canceled' WHERE lead_id=? AND addon=?",
                              (lead_id, addon))
                self.db.log("billing", "addon_canceled", addon, lead_id)
                return "addon_canceled"
            if self.db.get_lead(lead_id)["status"] == "client":
                self.db.update_lead(lead_id, status="churned")
                self.db.log("billing", "churned", "subscription deleted", lead_id)
            return "churned"

        if etype == "invoice.payment_failed":
            sub = obj.get("subscription")
            if isinstance(sub, dict):
                sub = sub.get("id")
            meta = (obj.get("subscription_details") or {}).get("metadata") or {}
            lead_id, _ = self._lead_for_subscription(sub, meta)
            if lead_id is None:
                return "unknown_subscription"
            self.db.log("billing", "past_due", "invoice payment failed", lead_id)
            return "past_due"

        return "ignored"


# ---------------------------------------------------------------------------
# Rate limiting (quote form)
# ---------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, max_hits: int, per_seconds: int):
        self.max_hits, self.per = max_hits, per_seconds
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        t = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and t - q[0] > self.per:
                q.popleft()
            if len(q) >= self.max_hits:
                return False
            q.append(t)
            return True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:520px;margin:15vh auto;padding:0 20px;color:#1d232b}}
button{{background:#1d232b;color:#fff;border:0;padding:12px 20px;border-radius:8px;font-size:1rem;cursor:pointer}}</style>
</head><body>{body}</body></html>"""


def make_handler(cfg: Config):
    quote_limiter = RateLimiter(max_hits=5, per_seconds=3600)

    class Handler(BaseHTTPRequestHandler):
        server_version = "agency"

        def _client_ip(self) -> str:
            # behind Caddy/nginx the real visitor IP is the first X-Forwarded-For entry
            fwd = self.headers.get("X-Forwarded-For", "")
            return fwd.split(",")[0].strip() or self.client_address[0]

        def _quote(self, path: str, body: bytes):
            parts = path.strip("/").split("/")  # quote/<lead_id>/<token>
            if len(parts) != 3 or not parts[1].isdigit() or not verify_quote(int(parts[1]), parts[2], cfg.app_secret):
                return self._send(404, PAGE.format(title="Not found", body="<p>This form is not active.</p>"))
            lead_id = int(parts[1])
            db = self._db()
            lead = db.get_lead(lead_id)
            back = escape(lead["preview_url"]) if lead and lead["preview_url"] else ""
            back_link = f"<p><a href='{back}'>&larr; Back to the website</a></p>" if back.startswith("http") else ""
            if not quote_limiter.allow(f"{self._client_ip()}:{lead_id}"):
                return self._send(429, PAGE.format(title="Slow down", body=(
                    "<h1>Too many requests</h1><p>Please try again later or call directly.</p>" + back_link)))
            form = {k: v[0] for k, v in parse_qs(body.decode(errors="ignore")).items()}
            try:
                submit_quote(db, cfg, lead_id, form)
            except QuoteError as exc:
                if str(exc) == "spam":  # pretend success so bots learn nothing
                    return self._send(200, PAGE.format(title="Thanks", body="<h1>Thanks!</h1>" + back_link))
                return self._send(400, PAGE.format(title="Please check the form", body=(
                    f"<h1>Almost there</h1><p>{escape(str(exc))}</p>"
                    "<p><a href='javascript:history.back()'>&larr; Go back and fix it</a></p>")))
            name = escape(lead["business_name"])
            return self._send(200, PAGE.format(title="Request sent", body=(
                f"<h1>Thanks, your request was sent!</h1><p>{name} will get back to you soon.</p>" + back_link)))

        def log_message(self, fmt, *args):  # route through logging, no PII in query strings
            log.info("%s %s -> %s", self.command, urlparse(self.path).path, args[1] if len(args) > 1 else "")

        def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _db(self) -> DB:
            return DB(cfg.db_path)  # one connection per request: sqlite objects aren't thread-safe

        # ---- routes ----
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":
                # public endpoint: liveness only, no business or customer data
                last = self._db().conn.execute("SELECT MAX(created_at) FROM events").fetchone()[0]
                self._send(200, json.dumps({"ok": True, "dry_run": cfg.dry_run, "last_event": last}),
                           "application/json")
            elif u.path == "/unsubscribe":
                q = parse_qs(u.query)
                email, tok = q.get("e", [""])[0], q.get("t", [""])[0]
                if not verify_unsubscribe(email, tok, cfg.app_secret):
                    return self._send(400, PAGE.format(title="Invalid link", body="<p>This link is invalid.</p>"))
                # GET shows a confirm button (mail scanners prefetch links; only POST unsubscribes)
                self._send(200, PAGE.format(title="Unsubscribe", body=(
                    f"<h1>Unsubscribe</h1><p>Stop all emails from {escape(cfg.company_name)} to "
                    f"<strong>{escape(email)}</strong>?</p><form method='post'>"
                    f"<input type='hidden' name='e' value='{escape(email)}'>"
                    f"<input type='hidden' name='t' value='{escape(tok)}'>"
                    "<button type='submit'>Unsubscribe</button></form>")))
            else:
                self._send(404, PAGE.format(title="Not found", body="<p>Not found.</p>"))

        def do_POST(self):
            u = urlparse(self.path)
            length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
            body = self.rfile.read(length)
            if u.path.startswith("/quote/"):
                return self._quote(u.path, body)
            if u.path == "/stripe/webhook":
                if not verify_stripe_signature(body, self.headers.get("Stripe-Signature", ""),
                                               cfg.stripe_webhook_secret):
                    return self._send(400, '{"error":"bad signature"}', "application/json")
                try:
                    event = json.loads(body)
                except ValueError:
                    return self._send(400, '{"error":"bad json"}', "application/json")
                db = self._db()
                result = StripeEvents(db, cfg).handle(event)
                db.log("webhook", "stripe", f"{event.get('type')}: {result}")
                return self._send(200, json.dumps({"received": True, "result": result}), "application/json")
            if u.path == "/unsubscribe":
                # RFC 8058 one-click: POST to the link URL (token in query) or form fields
                q = parse_qs(u.query)
                form = parse_qs(body.decode(errors="ignore"))
                email = (form.get("e") or q.get("e") or [""])[0]
                tok = (form.get("t") or q.get("t") or [""])[0]
                if not verify_unsubscribe(email, tok, cfg.app_secret):
                    return self._send(400, PAGE.format(title="Invalid link", body="<p>This link is invalid.</p>"))
                db = self._db()
                db.suppress(email, "one-click unsubscribe")
                row = db.conn.execute("SELECT id, status FROM leads WHERE email = ?", (email.lower(),)).fetchone()
                if row:
                    if row["status"] in ("new", "built", "deployed", "contacted", "replied"):
                        db.update_lead(row["id"], status="unsubscribed")
                    db.log("unsubscribe", "unsubscribed", "one-click", row["id"])
                return self._send(200, PAGE.format(title="Unsubscribed", body=(
                    f"<h1>You're unsubscribed</h1><p>{escape(email)} won't receive any more emails "
                    f"from {escape(cfg.company_name)}.</p>")))
            self._send(404, '{"error":"not found"}', "application/json")

    return Handler


def scheduler(cfg: Config, stop: threading.Event) -> None:
    while not stop.is_set():
        started = time.monotonic()
        try:
            Orchestrator(DB(cfg.db_path), cfg).run_once()
        except Exception:  # keep the scheduler alive no matter what
            log.exception("pipeline run failed")
        stop.wait(max(60, cfg.run_every_minutes * 60 - (time.monotonic() - started)))


def serve(cfg: Config, run_bots: bool = True) -> None:
    if not cfg.stripe_webhook_secret:
        log.warning("STRIPE_WEBHOOK_SECRET not set: webhooks will be rejected (polling still works)")
    if not (cfg.public_url and cfg.app_secret):
        log.warning("PUBLIC_URL/APP_SECRET not set: emails use mailto: unsubscribe only")
    stop = threading.Event()
    if run_bots:
        threading.Thread(target=scheduler, args=(cfg, stop), daemon=True, name="scheduler").start()
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cfg))
    log.info("serving on :%d (bots every %d min, dry_run=%s) at %s", cfg.port,
             cfg.run_every_minutes, cfg.dry_run, now())
    try:
        httpd.serve_forever()
    finally:
        stop.set()
        httpd.server_close()
