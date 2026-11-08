import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import ThreadingHTTPServer

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.outreach_bot import OutreachBot
from agency.config import Config
from agency.db import DB
from agency.mailer import Mailer, Sender
from agency.orchestrator import Orchestrator
from agency.server import StripeEvents, make_handler, verify_stripe_signature
from agency.unsubscribe import token, url
from tests.test_pipeline import SAMPLE

WHSEC = "whsec_test"


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json",
                  stripe_webhook_secret=WHSEC, app_secret="s3cret", public_url="https://bots.example")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    Orchestrator(db, cfg).run_once()
    return db, cfg


@pytest.fixture
def server(env):
    db, cfg = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", db, cfg
    httpd.shutdown()


def sign(payload: bytes, secret=WHSEC, ts=None):
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def post(url_, data: bytes, headers=None):
    req = urllib.request.Request(url_, data=data, headers=headers or {}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_signature_verification():
    body = b'{"a":1}'
    assert verify_stripe_signature(body, sign(body), WHSEC)
    assert not verify_stripe_signature(body, sign(body, "wrong"), WHSEC)
    assert not verify_stripe_signature(b'{"a":2}', sign(body), WHSEC)
    assert not verify_stripe_signature(body, sign(body, ts=int(time.time()) - 3600), WHSEC)
    assert not verify_stripe_signature(body, "", WHSEC)


def checkout(lead_id, sid="cs_1", addon=None, sub="sub_1"):
    meta = {"lead_id": str(lead_id), **({"addon": addon} if addon else {})}
    return {"type": "checkout.session.completed", "data": {"object": {
        "id": sid, "payment_status": "paid", "amount_total": 54800, "currency": "usd",
        "subscription": sub, "metadata": meta}}}


def test_webhook_pays_lead_end_to_end(server):
    base, db, cfg = server
    body = json.dumps(checkout(1)).encode()
    assert post(f"{base}/stripe/webhook", body, {"Stripe-Signature": "t=1,v1=bad"})[0] == 400
    code, resp = post(f"{base}/stripe/webhook", body, {"Stripe-Signature": sign(body)})
    assert code == 200 and json.loads(resp)["result"] == "lead_paid"
    fresh = DB(cfg.db_path)
    assert fresh.get_lead(1)["status"] == "paid" and fresh.get_lead(1)["subscription_id"] == "sub_1"
    post(f"{base}/stripe/webhook", body, {"Stripe-Signature": sign(body)})   # retry: idempotent
    assert fresh.metrics(cfg.monthly_fee_cents)["revenue_collected_cents"] == 54800


def test_events_addon_cancel_and_failed_card(env):
    db, cfg = env
    ev = StripeEvents(db, cfg)
    assert ev.handle(checkout(1)) == "lead_paid"
    db.update_lead(1, status="client")
    assert ev.handle(checkout(1, "cs_2", addon="seo", sub="sub_seo")) == "addon_activated"
    assert db.metrics(cfg.monthly_fee_cents)["addon_mrr_cents"] == 19900
    assert ev.handle({"type": "invoice.payment_failed",
                      "data": {"object": {"subscription": "sub_1"}}}) == "past_due"
    assert ev.handle({"type": "customer.subscription.deleted",
                      "data": {"object": {"id": "sub_seo", "metadata": {}}}}) == "addon_canceled"
    assert db.metrics(cfg.monthly_fee_cents)["addon_mrr_cents"] == 0
    assert ev.handle({"type": "customer.subscription.deleted",
                      "data": {"object": {"id": "sub_1", "metadata": {"lead_id": "1"}}}}) == "churned"
    assert db.get_lead(1)["status"] == "churned"
    assert ev.handle({"type": "checkout.session.completed",
                      "data": {"object": {"id": "x", "payment_status": "paid", "metadata": {"lead_id": "999"}}}}) == "unknown_lead"
    assert ev.handle({"type": "charge.refunded", "data": {"object": {}}}) == "ignored"


def test_one_click_unsubscribe(server):
    base, db, cfg = server
    email = "info@lonestarroof.example"
    link = url(email, cfg).replace("https://bots.example", base)
    with urllib.request.urlopen(link) as r:   # GET only shows a confirm page
        assert r.status == 200 and "Unsubscribe" in r.read().decode()
    assert not DB(cfg.db_path).is_suppressed(email)
    code, _ = post(link, b"List-Unsubscribe=One-Click")  # RFC 8058 one-click POST
    assert code == 200
    fresh = DB(cfg.db_path)
    assert fresh.is_suppressed(email) and fresh.get_lead(2)["status"] == "unsubscribed"
    bad = f"{base}/unsubscribe?e={email}&t=forged"
    assert post(bad, b"")[0] == 400


def test_health_has_no_customer_data(server):
    base, db, cfg = server
    with urllib.request.urlopen(f"{base}/health") as r:
        data = json.loads(r.read())
    assert data["ok"] is True and set(data) == {"ok", "dry_run", "last_event"}


def test_emails_carry_one_click_headers(env):
    db, cfg = env
    db.update_lead(1, status="deployed")
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, m): sent.append(m)
        def quit(self): pass

    live = replace(cfg, dry_run=False)
    db.update_lead(1, preview_url="https://p.example", payment_link_id="plink_1")
    mailer = Mailer(db, live, [Sender(email="a@one.example", from_name="A", smtp_host="h")],
                    smtp_factory=FakeSMTP)
    OutreachBot(db, live, mailer).run()
    msg = sent[0]
    assert msg["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert f"t={token(msg['To'], cfg.app_secret)}" in msg["List-Unsubscribe"]
    assert "Unsubscribe instantly: https://bots.example/unsubscribe" in msg.get_content()
