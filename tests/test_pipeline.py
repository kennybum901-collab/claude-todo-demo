from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agency.bots.billing_bot import BillingBot
from agency.bots.deploy_bot import DeployBot
from agency.bots.lead_bot import LeadBot
from agency.bots.outreach_bot import OutreachBot
from agency.config import Config
from agency.db import DB
from agency.orchestrator import Orchestrator

SAMPLE = Path(__file__).resolve().parent.parent / "sample_leads.csv"


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json",
                  imap_host="", daily_email_cap=30)
    db = DB(cfg.db_path)
    return db, cfg


def test_full_dry_run_pipeline(env):
    db, cfg = env
    assert LeadBot(db, cfg).import_csv(SAMPLE) == 4
    assert LeadBot(db, cfg).import_csv(SAMPLE) == 0  # dedupe
    summary = Orchestrator(db, cfg).run_once()
    assert summary["built"] == 4 and summary["deployed"] == 4
    assert summary["emailed"] == 3  # one lead is phone-only
    lead = db.get_lead(1)
    assert lead["status"] == "contacted" and lead["payment_url"]
    html = (cfg.build_dir / lead["site_slug"] / "index.html").read_text()
    assert lead["business_name"].replace("'", "&#39;") in html and "Free preview" in html


def test_email_has_compliance_footer(env):
    db, cfg = env
    LeadBot(db, cfg).import_csv(SAMPLE)
    Orchestrator(db, cfg).run_once()
    msg = OutreachBot(db, cfg).compose(dict(db.get_lead(1)), 0)
    body = msg.get_content()
    assert cfg.company_address in body and "STOP" in body
    assert msg["List-Unsubscribe"]


def test_followups_and_cap(env):
    db, cfg = env
    LeadBot(db, cfg).import_csv(SAMPLE)
    orch = Orchestrator(db, cfg)
    orch.run_once()
    assert orch.outreach.run() == 0  # not due yet
    old = (datetime.now(timezone.utc) - timedelta(days=cfg.followup_days + 1)).isoformat()
    db.conn.execute("UPDATE leads SET last_contacted=?", (old,))
    db.conn.commit()
    assert orch.outreach.run() == 3
    assert db.get_lead(1)["followups_sent"] == 1

    capped = OutreachBot(db, replace(cfg, daily_email_cap=6))
    db.conn.execute("UPDATE leads SET last_contacted=?", (old,))
    db.conn.commit()
    assert capped.run() == 0  # 6 already sent today


def test_opt_out_is_honored(env):
    db, cfg = env
    LeadBot(db, cfg).import_csv(SAMPLE)
    orch = Orchestrator(db, cfg)
    orch.run_once()
    email = db.get_lead(1)["email"]
    assert orch.outreach.classify_reply(f"Bob <{email}>", "Please STOP emailing me") == "unsubscribed"
    assert db.is_suppressed(email)
    assert db.get_lead(1)["status"] == "unsubscribed"
    assert orch.outreach.classify_reply(db.get_lead(2)["email"], "How much for a logo too?") == "replied"


def test_payment_turns_lead_into_client(env):
    db, cfg = env
    LeadBot(db, cfg).import_csv(SAMPLE)
    orch = Orchestrator(db, cfg)
    orch.run_once()
    BillingBot(db, cfg).mark_paid(1, "pay_1", 54_800, "usd")
    BillingBot(db, cfg).mark_paid(1, "pay_1", 54_800, "usd")  # idempotent
    summary = orch.run_once()
    lead = db.get_lead(1)
    assert lead["status"] == "client" and summary["onboarded"] == 1
    html = (cfg.build_dir / lead["site_slug"] / "index.html").read_text()
    assert "Free preview" not in html
    m = db.metrics(cfg.monthly_fee_cents)
    assert m["clients"] == 1 and m["mrr_cents"] == cfg.monthly_fee_cents
    assert m["revenue_collected_cents"] == 54_800


class FakeResp:
    def __init__(self, data):
        self._d = data
        self.ok, self.status_code = True, 200

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        if url.endswith("/prices"):
            return FakeResp({"id": f"price_{len(self.calls)}"})
        if url.endswith("/payment_links"):
            return FakeResp({"id": "plink_1", "url": "https://buy.stripe.com/x"})
        if url.endswith("/sites"):
            return FakeResp({"id": "site_1"})
        return FakeResp({"ssl_url": "https://preview-x.netlify.app"})

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return FakeResp({"data": [{"id": "cs_1", "status": "complete",
                                   "payment_status": "paid", "amount_total": 54800,
                                   "currency": "usd"}]})


def test_live_stripe_and_netlify_calls(env):
    db, cfg = env
    live = replace(cfg, dry_run=False, stripe_key="sk_test_x", netlify_token="tok")
    LeadBot(db, live).import_csv(SAMPLE)
    http = FakeSession()
    billing = BillingBot(db, live, http)
    assert billing.create_links() == 4
    link_call = next(c for c in http.calls if c[1].endswith("/payment_links"))
    assert link_call[2]["data"]["metadata[lead_id]"] == "1"

    Orchestrator(db, live).builder.run()
    assert DeployBot(db, live, http).run() == 4
    assert db.get_lead(1)["preview_url"] == "https://preview-x.netlify.app"
    zip_call = next(c for c in http.calls if c[1].endswith("/deploys"))
    assert zip_call[2]["headers"]["Content-Type"] == "application/zip"

    assert billing.check_payments() == 4
    assert db.get_lead(1)["status"] == "paid"


def test_live_email_never_sends_local_or_fake_links(env, monkeypatch):
    db, cfg = env
    LeadBot(db, cfg).import_csv(SAMPLE)
    Orchestrator(db, cfg).run_once()  # dry run: file:// previews, dry_ payment links
    db.conn.execute("UPDATE leads SET status='deployed'")
    db.conn.commit()
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("must not connect"))
    live = replace(cfg, dry_run=False, smtp_host="smtp.example.com")
    assert OutreachBot(db, live).run() == 0
