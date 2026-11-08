import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.upsell_bot import UpsellBot
from agency.config import Config
from agency.db import DB
from agency.reviews import ReviewBot, ReviewError, request_review
from agency.server import make_handler
from agency.sms import SMS, in_quiet_hours, normalize_phone, suppression_key, twilio_signature, verify_twilio
from tests.test_pipeline import SAMPLE

TWILIO = dict(twilio_account_sid="AC123", twilio_auth_token="tok", twilio_from="+15125550000")


class FakeTwilio:
    def __init__(self):
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw["data"]))

        class R:
            def raise_for_status(self): pass
            def json(self): return {"sid": "SM1"}
        return R()


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=False, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites", obsidian_vault=tmp_path / "vault",
                  senders_file=tmp_path / "none.json", public_url="https://bots.example", app_secret="s",
                  sms_timezone="America/Chicago", **TWILIO)
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    db.update_lead(3, status="client", source_id="gp:ChIJbella")
    UpsellBot(db, cfg).activate(3, "reviews")
    db.set_setting("booster_welcome_3", "x")          # skip the welcome email in these tests
    return db, cfg


def test_normalize_phone():
    assert normalize_phone("(512) 555-0100") == "+15125550100"
    assert normalize_phone("1-512-555-0100") == "+15125550100"
    assert normalize_phone("+44 20 7946 0958") == "+442079460958"
    assert normalize_phone("555-0100") is None and normalize_phone("") is None


def test_quiet_hours():
    cfg = replace(Config(), sms_timezone="America/Chicago", sms_quiet_start=9, sms_quiet_end=20)
    assert in_quiet_hours(cfg, datetime(2026, 11, 9, 12, tzinfo=timezone.utc))      # 6am Chicago
    assert not in_quiet_hours(cfg, datetime(2026, 11, 9, 18, tzinfo=timezone.utc))  # noon Chicago
    assert in_quiet_hours(cfg, datetime(2026, 11, 10, 3, tzinfo=timezone.utc))      # 9pm Chicago


def test_twilio_signature_matches_official_library():
    # expected values computed with Twilio's own RequestValidator (twilio==9.13.0)
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    sig = twilio_signature("12345", "https://example.com/myapp.php?foo=1&bar=2", params)
    assert sig == "vNe7KK2kJwCsxc9K3OLkkKB3qqI="
    assert twilio_signature("abc", "https://x.example/twilio/sms",
                            {"Body": "Stop please ünïcode & = signs", "From": "+1", "SmsSid": "SM9"}) == "urTLJ/ErZXugODByC1XEGh7jn28="
    assert verify_twilio("12345", "https://example.com/myapp.php?foo=1&bar=2", params, sig)
    assert not verify_twilio("12345", "https://example.com/other", params, sig)


def test_text_requires_consent_and_valid_number(env):
    db, cfg = env
    with pytest.raises(ReviewError, match="agreed to a text"):
        request_review(db, 3, "Jane", "", "(512) 555-0100", sms_consent=False)   # no email, no consent
    with pytest.raises(ReviewError, match="phone number"):
        request_review(db, 3, "Jane", "", "12", sms_consent=True)
    rid = request_review(db, 3, "Jane", "", "(512) 555-0100", sms_consent=True)
    row = db.conn.execute("SELECT * FROM review_requests WHERE id=?", (rid,)).fetchone()
    assert row["channel"] == "sms" and row["customer_phone"] == "+15125550100" and row["sms_consent"] == 1
    with pytest.raises(ReviewError, match="already"):
        request_review(db, 3, "Jane", "", "512-555-0100", sms_consent=True)
    email_rid = request_review(db, 3, "Sam", "sam@x.example", "(512) 555-0199", sms_consent=False)
    assert db.conn.execute("SELECT channel FROM review_requests WHERE id=?", (email_rid,)).fetchone()[0] == "email"


def noon_chicago():
    return mock.patch("agency.reviews.in_quiet_hours", return_value=False)


def test_text_sent_with_stop_language_and_quiet_hours(env):
    db, cfg = env
    http = FakeTwilio()
    request_review(db, 3, "Jane Doe", "", "(512) 555-0100", sms_consent=True)
    with mock.patch("agency.reviews.in_quiet_hours", return_value=True):
        assert ReviewBot(db, cfg, http=http).run()["sent"] == 0 and http.posts == []   # waits for daytime
    with noon_chicago():
        assert ReviewBot(db, cfg, http=http).run()["sent"] == 1
    url, data = http.posts[0]
    assert url.endswith("/Accounts/AC123/Messages.json") and data["To"] == "+15125550100"
    assert data["From"] == "+15125550000"
    body = data["Body"]
    assert body.startswith("Hi Jane, thanks for choosing Bella's Hair Studio!") and body.endswith("Reply STOP to opt out.")
    assert "https://bots.example/rv/" in body and len(body) < 320


def test_messaging_service_and_dry_run(env):
    db, cfg = env
    http = FakeTwilio()
    assert SMS(replace(cfg, twilio_from="MGabc"), http).send("+15125550100", "x") == "SM1"
    assert http.posts[-1][1]["MessagingServiceSid"] == "MGabc"
    assert SMS(replace(cfg, dry_run=True), http).send("+15125550100", "x") == "dry"
    assert len(http.posts) == 1


@pytest.fixture
def server(env):
    db, cfg = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", db, cfg
    httpd.shutdown()


def test_stop_reply_suppresses_number(server):
    base, db, cfg = server
    params = {"From": "+15125550100", "To": "+15125550000", "Body": "STOP"}
    sig = twilio_signature(cfg.twilio_auth_token, "https://bots.example/twilio/sms", params)
    data = urllib.parse.urlencode(params).encode()
    with pytest.raises(urllib.error.HTTPError) as e:                     # forged request rejected
        urllib.request.urlopen(urllib.request.Request(f"{base}/twilio/sms", data=data,
                                                      headers={"X-Twilio-Signature": "bad"}))
    assert e.value.code == 403
    urllib.request.urlopen(urllib.request.Request(f"{base}/twilio/sms", data=data, headers={"X-Twilio-Signature": sig}))
    fresh = DB(cfg.db_path)
    assert fresh.is_suppressed(suppression_key("+15125550100"))
    request_review(fresh, 3, "Jane", "", "(512) 555-0100", sms_consent=True)
    with noon_chicago():
        assert ReviewBot(fresh, cfg, http=FakeTwilio()).run()["sent"] == 0  # never texted again
