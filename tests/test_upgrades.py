import sqlite3
from dataclasses import replace

import pytest

from agency.bots.billing_bot import BillingBot
from agency.bots.deploy_bot import DeployBot
from agency.bots.lead_bot import LeadBot
from agency.bots.outreach_bot import SUBJECT_VARIANTS, OutreachBot
from agency.config import Config
from agency.db import DB
from agency.obsidian import ObsidianSync
from agency.orchestrator import Orchestrator
from tests.test_pipeline import SAMPLE, FakeResp


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    return db, cfg


def test_reply_gets_draft_and_shows_in_obsidian(env):
    db, cfg = env
    orch = Orchestrator(db, cfg)
    orch.run_once()
    status = orch.outreach.classify_reply("owner@riveraplumbing.example", "Looks great, how much does it cost?")
    assert status == "replied"
    lead = db.get_lead(1)
    assert "$499" in lead["reply_draft"] and lead["payment_url"] in lead["reply_draft"]
    ObsidianSync(db, cfg).push()
    note = next((cfg.obsidian_vault / "Leads").glob("* (1).md")).read_text()
    assert "how much does it cost" in note and "Suggested answer" in note
    dash = (cfg.obsidian_vault / "Dashboard.md").read_text()
    assert "how much does it cost" in dash.split("Replies waiting")[1]


def test_stop_word_needs_whole_word(env):
    db, cfg = env
    orch = Orchestrator(db, cfg)
    orch.run_once()
    assert orch.outreach.classify_reply("owner@riveraplumbing.example", "We work nonstop, any edits?") == "replied"
    assert not db.is_suppressed("owner@riveraplumbing.example")


def test_subject_variants_assigned_and_reported(env):
    db, cfg = env
    Orchestrator(db, cfg).run_once()
    variants = {db.get_lead(i)["variant"] for i in (1, 2, 3)}
    assert variants == set(SUBJECT_VARIANTS)
    lead = dict(db.get_lead(2))
    subject = OutreachBot(db, cfg).compose(lead, 0)["Subject"]
    assert subject == SUBJECT_VARIANTS[lead["variant"]].format(name=lead["business_name"])
    assert OutreachBot(db, cfg).compose(lead, 1)["Subject"] == f"Re: {subject}"
    db.update_lead(2, status="client")
    stats = {s["variant"]: s for s in db.variant_stats()}
    assert stats[lead["variant"]]["wins"] == 1 and stats[lead["variant"]]["win_rate"] == 1.0


class SubSession:
    def __init__(self, status):
        self.status, self.deleted = status, []

    def get(self, url, **kw):
        return FakeResp({"id": url.rsplit("/", 1)[1], "status": self.status})

    def delete(self, url, **kw):
        self.deleted.append(url)
        return FakeResp({})


@pytest.mark.parametrize("stripe_status,expected", [("canceled", "churned"), ("past_due", "client"),
                                                   ("active", "client")])
def test_subscription_churn(env, stripe_status, expected):
    db, cfg = env
    live = replace(cfg, dry_run=False, stripe_key="sk_test")
    billing = BillingBot(db, live, SubSession(stripe_status))
    billing.mark_paid(1, "cs_1", 54_800, "usd", subscription_id="sub_1")
    db.update_lead(1, status="client")
    billing.check_subscriptions()
    assert db.get_lead(1)["status"] == expected
    m = db.metrics(cfg.monthly_fee_cents)
    assert m["mrr_cents"] == (cfg.monthly_fee_cents if expected == "client" else 0)
    if stripe_status == "past_due":
        ObsidianSync(db, cfg).push()
        dash = (cfg.obsidian_vault / "Dashboard.md").read_text()
        assert "[[Rivera Plumbing (1)]]" in dash.split("Payment problems")[1].split("##")[0]


def test_preview_teardown_for_opt_outs(env):
    db, cfg = env
    live = replace(cfg, dry_run=False, netlify_token="tok")
    http = SubSession("x")
    db.update_lead(1, status="unsubscribed", netlify_site_id="site_9", preview_url="https://x.netlify.app")
    db.update_lead(2, status="contacted", netlify_site_id="site_8", preview_url="https://y.netlify.app")
    assert DeployBot(db, live, http).cleanup() == 1
    assert http.deleted == ["https://api.netlify.com/api/v1/sites/site_9"]
    assert db.get_lead(1)["preview_url"] is None and db.get_lead(2)["preview_url"]


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY, business_name TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'new', created_at TEXT, updated_at TEXT)")
    con.commit()
    con.close()
    db = DB(path)
    cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(leads)")}
    assert {"variant", "last_reply", "reply_draft", "subscription_id"} <= cols
