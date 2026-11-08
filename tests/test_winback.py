import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.winback_bot import WinbackBot
from agency.config import Config
from agency.db import DB
from agency.orchestrator import Orchestrator
from agency.server import StripeEvents
from tests.test_pipeline import SAMPLE, FakeResp


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    return db, cfg


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def set_event(db, lead_id, kind, days):
    db.log("billing" if kind == "churned" else "outreach", kind, "", lead_id)
    db.conn.execute("UPDATE events SET created_at=? WHERE lead_id=? AND kind=?", (ago(days), lead_id, kind))
    db.conn.commit()


def age_winbacks(db, days):
    db.conn.execute("UPDATE winbacks SET sent_at=?", (ago(days),))
    db.conn.commit()


def test_churned_client_gets_two_emails_then_stops(env):
    db, cfg = env
    db.update_lead(1, status="churned")
    set_event(db, 1, "churned", 10)
    bot = WinbackBot(db, cfg)
    assert bot.send() == 0                                    # too soon (30 days)
    set_event(db, 1, "churned", 31)
    db.conn.execute("DELETE FROM events WHERE kind='churned' AND created_at > ?", (ago(20),))
    db.conn.commit()
    assert bot.send() == 1
    assert bot.send() == 0                                    # 30-day gap
    age_winbacks(db, 31)
    assert bot.send() == 1
    age_winbacks(db, 31)
    assert bot.send() == 0                                    # max 2
    steps = [r[0] for r in db.conn.execute("SELECT step FROM winbacks WHERE lead_id=1 ORDER BY step")]
    assert steps == [1, 2]
    links = {r[0] for r in db.conn.execute("SELECT payment_link_id FROM winbacks")}
    assert len(links) == 1                                    # same link reused


def test_lost_prospect_gets_exactly_one_email(env):
    db, cfg = env
    db.update_lead(2, status="lost")
    set_event(db, 2, "lost", 91)
    bot = WinbackBot(db, cfg)
    assert bot.send() == 1
    age_winbacks(db, 400)
    assert bot.send() == 0
    msg = bot.compose(dict(db.get_lead(2)), "lost", 1, "https://pay")
    body = msg.get_content()
    assert "last time I'll reach out" in body and "no setup fee" in body and "$49/month" in body
    assert cfg.company_address in body and "STOP" in body


def test_suppressed_and_bounced_never_contacted(env):
    db, cfg = env
    db.update_lead(1, status="churned")
    set_event(db, 1, "churned", 40)
    db.suppress("owner@riveraplumbing.example", "x")
    db.update_lead(2, status="bounced")
    assert WinbackBot(db, cfg).send() == 0


def test_client_email_mentions_quotes(env):
    db, cfg = env
    db.update_lead(3, status="churned")
    for _ in range(4):
        db.conn.execute("INSERT INTO quotes (lead_id, name, created_at) VALUES (3, 'x', ?)", (ago(100),))
    db.conn.commit()
    body = WinbackBot(db, cfg).compose(dict(db.get_lead(3)), "churned", 1, "https://pay").get_content()
    assert "brought in 4 quote requests" in body and "no setup fee" in body


def test_recovery_via_webhook_relaunches_site(env):
    db, cfg = env
    cfg.build_dir.mkdir(parents=True)
    (cfg.build_dir / "1-copy.json").write_text(json.dumps({
        "headline": "Rivera", "tagline": "t", "about": "a", "cta": "c",
        "services": [{"name": "S", "description": "d"}], "email_opener": ""}))
    db.update_lead(1, status="churned", site_slug="rivera-1", subscription_id="sub_old")
    db.conn.execute("INSERT INTO winbacks (lead_id, audience, step, sent_at) VALUES (1,'churned',1,?)", (ago(1),))
    db.conn.commit()
    ev = StripeEvents(db, cfg)
    result = ev.handle({"type": "checkout.session.completed", "data": {"object": {
        "id": "cs_wb", "payment_status": "paid", "amount_total": 4900, "currency": "usd",
        "subscription": "sub_new", "metadata": {"lead_id": "1", "winback": "1"}}}})
    assert result == "winback_recovered"
    assert db.get_lead(1)["status"] == "paid" and db.get_lead(1)["subscription_id"] == "sub_new"
    assert db.conn.execute("SELECT kind FROM payments WHERE id='cs_wb'").fetchone()[0] == "winback"
    Orchestrator(db, cfg).onboard_paid_clients()
    assert db.get_lead(1)["status"] == "client"
    # a late cancellation notice for the OLD subscription must not churn them again
    assert ev.handle({"type": "customer.subscription.deleted", "data": {"object": {
        "id": "sub_old", "metadata": {"lead_id": "1"}}}}) == "old_subscription"
    assert db.get_lead(1)["status"] == "client"
    assert WinbackBot(db, cfg).stats() == {"contacted": 1, "recovered": 1}


class StripeFake:
    def __init__(self):
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw["data"]))
        return FakeResp({"id": "price_w"} if url.endswith("/prices") else {"id": "plink_w", "url": "https://buy/w"})

    def get(self, url, **kw):
        return FakeResp({"data": [{"id": "cs_w", "status": "complete", "payment_status": "paid",
                                   "amount_total": 4900, "currency": "usd", "subscription": "sub_w"}]})


def test_live_link_has_no_setup_fee_and_polling_recovers(env):
    db, cfg = env
    live = replace(cfg, dry_run=False, stripe_key="sk_test")
    db.update_lead(2, status="lost")
    set_event(db, 2, "lost", 100)
    http = StripeFake()
    bot = WinbackBot(db, live, http)
    assert bot.send() == 1
    price = http.posts[0][1]
    assert price["unit_amount"] == cfg.monthly_fee_cents and price["recurring[interval]"] == "month"
    link = http.posts[1][1]
    assert "line_items[1][price]" not in link and link["metadata[winback]"] == "1"
    assert bot.check_purchases() == 1 and db.get_lead(2)["status"] == "paid"
