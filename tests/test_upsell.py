from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.upsell_bot import CATALOG_BY_KEY, UpsellBot, best_offers
from agency.config import Config
from agency.db import DB
from agency.obsidian import ObsidianSync
from agency.orchestrator import Orchestrator
from tests.test_pipeline import SAMPLE, FakeResp


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", upsell_after_days=60, upsell_gap_days=30)
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    return db, cfg


def make_client(db, lead_id, days_ago):
    ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    db.conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)",
                    (f"p{lead_id}", lead_id, 54_800, "usd", "setup+first_month", ts))
    db.conn.commit()
    db.update_lead(lead_id, status="client")


def age_offers(db, days):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db.conn.execute("UPDATE addons SET offered_at=?", (ts,))
    db.conn.commit()


def test_niche_specific_offer_first():
    assert best_offers("hair salon")[0].key == "booking"
    assert best_offers("roofer")[0].key == "reviews"
    assert [a.key for a in best_offers("bakery")] == ["seo"]


def test_offer_timing_gap_and_no_repeats(env):
    db, cfg = env
    bot = UpsellBot(db, cfg)
    make_client(db, 1, days_ago=10)            # too new
    make_client(db, 3, days_ago=90)            # hair salon, eligible
    assert bot.send_offers() == 1
    assert [a["addon"] for a in db.addons_for(3)] == ["booking"]
    assert bot.send_offers() == 0              # gap not reached
    age_offers(db, 31)
    assert bot.send_offers() == 1
    assert [a["addon"] for a in db.addons_for(3)] == ["booking", "seo"]
    age_offers(db, 31)
    assert bot.send_offers() == 0              # catalog exhausted for this niche


def test_opted_out_client_gets_no_offers_but_stays_client(env):
    db, cfg = env
    make_client(db, 3, days_ago=90)
    orch = Orchestrator(db, cfg)
    assert orch.outreach.classify_reply("bella@bellashair.example", "STOP sending offers") == "client_opt_out"
    assert db.get_lead(3)["status"] == "client"
    assert UpsellBot(db, cfg).send_offers() == 0


def test_activation_adds_mrr_and_shows_in_obsidian(env):
    db, cfg = env
    make_client(db, 3, days_ago=90)
    bot = UpsellBot(db, cfg)
    bot.send_offers()
    assert bot.activate(3, "booking") and not bot.activate(3, "booking")
    m = db.metrics(cfg.monthly_fee_cents)
    booking = CATALOG_BY_KEY["booking"].price_cents
    assert m["addon_mrr_cents"] == booking
    assert m["mrr_cents"] == cfg.monthly_fee_cents + booking
    ObsidianSync(db, cfg).push()
    note = next((cfg.obsidian_vault / "Leads").glob("* (3).md")).read_text()
    assert "## Add-ons" in note and "booking" in note
    assert "Add-on upsells" in (cfg.obsidian_vault / "Dashboard.md").read_text()
    db.update_lead(3, status="churned")       # churned clients' add-ons don't count
    assert db.metrics(cfg.monthly_fee_cents)["addon_mrr_cents"] == 0


def test_upsell_email_content(env):
    db, cfg = env
    make_client(db, 3, days_ago=90)
    msg = UpsellBot(db, cfg).compose(dict(db.get_lead(3)), CATALOG_BY_KEY["booking"], "https://pay")
    body = msg.get_content()
    assert "Online Booking" in msg["Subject"] and "$79/month" in body
    assert cfg.company_address in body and "STOP" in body and "https://pay" in body


class StripeFake:
    def __init__(self):
        self.posts = []

    def post(self, url, **kw):
        self.posts.append((url, kw))
        if url.endswith("/prices"):
            return FakeResp({"id": "price_a"})
        return FakeResp({"id": "plink_a", "url": "https://buy.stripe.com/a"})

    def get(self, url, **kw):
        if "/subscriptions/" in url:
            return FakeResp({"status": "canceled"})
        return FakeResp({"data": [{"id": "cs_a", "status": "complete", "payment_status": "paid",
                                   "amount_total": 7900, "currency": "usd", "subscription": "sub_a"}]})


def test_live_stripe_addon_flow(env):
    db, cfg = env
    live = replace(cfg, dry_run=False, stripe_key="sk_test")  # no SMTP: emails stay dry
    make_client(db, 3, days_ago=90)
    http = StripeFake()
    bot = UpsellBot(db, live, http)
    assert bot.send_offers() == 1
    price_call = http.posts[0][1]["data"]
    assert price_call["recurring[interval]"] == "month" and price_call["unit_amount"] == 7900
    assert bot.check_purchases() == 1
    a = db.addons_for(3)[0]
    assert a["status"] == "active" and a["subscription_id"] == "sub_a"
    assert bot.check_cancellations() == 1
    assert db.addons_for(3)[0]["status"] == "canceled"
