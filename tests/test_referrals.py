import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from agency.bots.billing_bot import BillingBot
from agency.bots.lead_bot import LeadBot
from agency.bots.report_bot import ReportBot
from agency.config import Config
from agency.db import DB
from agency.orchestrator import Orchestrator
from agency.referrals import ReferralBot, ReferralError, code_for, link_for, referrer_for_code, submit
from agency.server import make_handler
from tests.test_pipeline import SAMPLE, FakeResp

FORM = {"business_name": "Ace Roofing", "contact_name": "Al", "email": "al@aceroof.example",
        "phone": "5125550111", "niche": "roofer", "city": "Austin, TX", "consent": "on", "website": ""}


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites", obsidian_vault=tmp_path / "vault",
                  senders_file=tmp_path / "none.json", public_url="https://bots.example", app_secret="s3cret")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    db.update_lead(1, status="client", subscription_id="sub_rivera")
    return db, cfg


def test_codes_are_unguessable_and_client_only(env):
    db, cfg = env
    code = code_for(db.get_lead(1), cfg.app_secret)
    assert code.startswith("RIVERAPL-1") and referrer_for_code(db, code, cfg.app_secret)["id"] == 1
    assert referrer_for_code(db, code[:-1] + ("0" if code[-1] != "0" else "1"), cfg.app_secret) is None
    assert referrer_for_code(db, code_for(db.get_lead(2), cfg.app_secret), cfg.app_secret) is None  # not a client
    assert link_for(db.get_lead(1), cfg) == f"https://bots.example/r/{code}"


def test_submit_validation_and_opt_out_protection(env):
    db, cfg = env
    ref = db.get_lead(1)
    for bad, msg in [({**FORM, "consent": ""}, "tick the box"), ({**FORM, "email": "x"}, "valid email"),
                     ({**FORM, "business_name": ""}, "business name")]:
        with pytest.raises(ReferralError, match=msg):
            submit(db, cfg, ref, bad)
    db.suppress("gone@x.example", "opted out")
    with pytest.raises(ReferralError, match="already have"):
        submit(db, cfg, ref, {**FORM, "email": "gone@x.example"})
    assert db.is_suppressed("gone@x.example")                       # a form never undoes an opt-out
    lid = submit(db, cfg, ref, FORM)
    lead = db.get_lead(lid)
    assert lead["referred_by"] == 1 and lead["source"] == "referral" and "consented" in lead["notes"]


def test_referred_lead_gets_priority_email_and_discount(env):
    db, cfg = env
    lid = submit(db, cfg, db.get_lead(1), FORM)
    orch = Orchestrator(db, replace(cfg, min_lead_score=100))     # cold leads all blocked by score...
    orch.run_once()
    assert db.get_lead(lid)["status"] == "contacted"              # ...referred lead still emailed
    msg = orch.outreach.compose(dict(db.get_lead(lid)), 0)
    body = msg.get_content()
    assert msg["Subject"].startswith("Rivera Plumbing recommended us")
    assert "$399 one-time setup ($100 off" in body and "you requested a preview" in body
    assert BillingBot(db, cfg).setup_fee(dict(db.get_lead(lid))) == cfg.setup_fee_cents - 10_000
    assert BillingBot(db, cfg).setup_fee(dict(db.get_lead(2))) == cfg.setup_fee_cents


class StripeFake:
    def __init__(self):
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw["data"]))
        return FakeResp({"id": "coupon_1"})


def test_reward_free_month_once(env):
    db, cfg = env
    lid = submit(db, cfg, db.get_lead(1), FORM)
    live = replace(cfg, dry_run=False, stripe_key="sk_test")
    http = StripeFake()
    bot = ReferralBot(db, live, http)
    assert bot.run() == 0                                          # not paid yet
    db.update_lead(lid, status="paid")
    assert bot.run() == 1 and bot.run() == 0                       # idempotent
    coupon, sub = http.posts
    assert coupon[1]["amount_off"] == cfg.monthly_fee_cents and coupon[1]["duration"] == "once"
    assert sub[0].endswith("/subscriptions/sub_rivera") and sub[1]["discounts[0][coupon]"] == "coupon_1"
    assert bot.stats() == {"signups": 1, "won": 1, "rewards": 1}


def test_report_includes_referral_link(env):
    db, cfg = env
    db.conn.execute("INSERT INTO payments VALUES ('p1',1,54800,'usd','setup+first_month','2026-09-01T00:00:00+00:00')")
    db.conn.commit()
    msg, _ = ReportBot(db, cfg).compose(db.get_lead(1), "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    assert link_for(db.get_lead(1), cfg) in msg.get_content() and "free month" in msg.get_content()


def test_landing_page_http(env):
    db, cfg = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/r/{code_for(db.get_lead(1), cfg.app_secret)}"
    try:
        with urllib.request.urlopen(url) as r:
            page = r.read().decode()
        assert "Rivera Plumbing</strong> recommended us" in page and "$100 off setup" in page
        data = urllib.parse.urlencode(FORM).encode()
        with urllib.request.urlopen(urllib.request.Request(url, data=data)) as r:
            assert "You're in!" in r.read().decode()
        assert DB(cfg.db_path).conn.execute("SELECT COUNT(*) FROM leads WHERE referred_by=1").fetchone()[0] == 1
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url.rsplit("/", 1)[0] + "/FAKE-1ABCD")
        assert e.value.code == 404
    finally:
        httpd.shutdown()
