from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests

from agency.__main__ import main
from agency.config import Config
from agency.doctor import FAIL, OK, SKIP, WARN, Doctor


class Resp:
    def __init__(self, data=None, code=200):
        self._d, self.status_code = data or {}, code

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Unauthorized")


class FakeHTTP:
    def __init__(self, fail=()):
        self.fail, self.urls = fail, []

    def _r(self, url):
        self.urls.append(url)
        if any(f in url for f in self.fail):
            return Resp(code=401)
        if url.endswith("/health"):
            return Resp({"ok": True, "last_heartbeat": "2026-11-09T10:00:00+00:00"})
        if "netlify" in url:
            return Resp({"email": "me@studio.example"})
        if "twilio" in url:
            return Resp({"status": "active"})
        return Resp({})

    def get(self, url, **kw):
        return self._r(url)

    def post(self, url, **kw):
        return self._r(url)


class FakeSMTP:
    def __init__(self, *a, **k): pass
    def starttls(self): pass
    def login(self, user, pw):
        if pw == "wrong":
            raise Exception("535 Authentication failed")
    def quit(self): pass


class FakeIMAP:
    def __init__(self, *a): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def login(self, *a): pass


GOOD = dict(company_email="hi@studio.example", company_address="9 Oak Ave, Austin, TX 78701",
            company_url="https://studio.example", dry_run=False, smtp_host="smtp.studio.example",
            smtp_user="alex@getstudio.example", smtp_password="app-pass", imap_host="imap.studio.example",
            stripe_key="sk_live_x", stripe_webhook_secret="whsec_x", netlify_token="nf", use_ai_copy=True,
            google_places_key="gk", public_url="https://bots.studio.example", app_secret="s" * 64,
            admin_password="correct-horse-battery", digest_email="me@studio.example", alert_email="me@studio.example",
            twilio_account_sid="AC1", twilio_auth_token="t", twilio_from="+15125550000", pexels_api_key="px")


@pytest.fixture
def cfg(tmp_path):
    return replace(Config(), db_path=tmp_path / "d" / "t.db", build_dir=tmp_path / "d" / "sites",
                   senders_file=tmp_path / "none.json", **GOOD)


def doctor(cfg, http=None, **kw):
    anth = SimpleNamespace(models=SimpleNamespace(retrieve=lambda m: SimpleNamespace(id=m)))
    return Doctor(cfg, http=http or FakeHTTP(), smtp_factory=FakeSMTP, imap_factory=FakeIMAP,
                  anthropic_client=anth, **kw)


def test_fully_configured_is_ready(cfg):
    d = doctor(cfg)
    d.run()
    assert d.r.blocking == [], d.render()
    assert all(st == OK for _, st in d.verdicts()), d.verdicts()
    assert "Nothing blocks going live" in d.render()


def test_failures_are_reported_with_fixes(cfg):
    bad = replace(cfg, smtp_password="wrong", company_address="123 Main St, Anytown, USA",
                  stripe_webhook_secret="oops", twilio_from="")
    d = doctor(bad, http=FakeHTTP(fail=("netlify",)))
    d.run()
    fails = {c.name: c for c in d.r.blocking}
    assert "Identity" in fails and "Netlify" in fails and "Webhook secret" in fails and "Twilio" in fails
    assert any("sending" in n for n in fails) and "535" in next(c.detail for n, c in fails.items() if "sending" in n)
    assert all(c.fix for c in d.r.blocking)
    assert dict(d.verdicts())["Email outreach"] == FAIL


def test_test_key_in_live_mode_warns(cfg):
    d = doctor(replace(cfg, stripe_key="sk_test_x"))
    d.run()
    assert any(c.name == "Stripe mode" and c.status == WARN for c in d.r.checks)


def test_offline_makes_no_network_calls(cfg):
    http = FakeHTTP()
    d = doctor(cfg, http=http, offline=True)
    d.run()
    assert http.urls == [] and any(c.status == SKIP for c in d.r.checks)


def test_cli_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("agency.__main__.config", replace(
        Config(), db_path=tmp_path / "t.db", build_dir=tmp_path / "s", senders_file=tmp_path / "none.json",
        smtp_host="", stripe_key="", netlify_token=""))
    assert main(["doctor", "--offline"]) == 1
    assert "BLOCKED" in capsys.readouterr().out
