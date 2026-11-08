from dataclasses import replace

import pytest

from agency.bots.outreach_bot import OutreachBot
from agency.config import Config
from agency.db import DB
from agency.mailer import Mailer, Sender
from agency.orchestrator import Orchestrator
from agency.scoring import DEFAULT_RATE, LeadScorer, reputation_factor, to_score


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json")
    return DB(cfg.db_path), cfg


def add(db, i, niche="plumber", city="Austin", rating=4.6, reviews=40, status="deployed", emailed=False, won=False):
    lid = db.add_lead(business_name=f"B{i}", niche=niche, city=city, email=f"b{i}@x.example", rating=rating,
                      review_count=reviews, status=status, source="t", source_id=f"s{i}",
                      preview_url="https://p.example", payment_url="https://pay", payment_link_id="pl")
    if emailed or won:
        db.log("outreach", "dry_send", "step 0", lid)
    if won:
        db.conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)", (f"p{i}", lid, 1, "usd", "setup+first_month", "2026-01-01"))
        db.conn.commit()
    return lid


def test_reputation_and_curve():
    assert reputation_factor(4.8, 150)[0] > reputation_factor(4.0, 30)[0] > reputation_factor(3.0, 2)[0]
    assert to_score(0) == 0 and to_score(0.03) == 38 and to_score(1.0) == 95
    assert to_score(0.10) > to_score(0.06) > to_score(0.03)


def test_no_history_uses_default_rate(env):
    db, cfg = env
    lid = add(db, 1)
    s, p, why = LeadScorer(db, cfg).score(db.get_lead(lid))
    assert 0 < p < 0.06 and "(early)" in why
    assert LeadScorer(db, cfg).overall == pytest.approx(DEFAULT_RATE)


def test_learns_from_results_with_smoothing(env):
    db, cfg = env
    i = 0
    for niche, wins in (("roofer", 12), ("landscaper", 0)):
        for k in range(60):
            add(db, i, niche=niche, status="contacted", emailed=True, won=k < wins)
            i += 1
    roofer = add(db, 900, niche="roofer")
    land = add(db, 901, niche="landscaper")
    lucky = add(db, 902, niche="baker")
    add(db, 903, niche="baker", status="client", won=True)   # 1 sale out of 1 emailed
    sc = LeadScorer(db, cfg)
    s_roof, s_land, s_lucky = (sc.score(db.get_lead(x))[0] for x in (roofer, land, lucky))
    assert s_roof > s_lucky > s_land        # one lucky baker sale doesn't beat 12/60 roofers
    assert sc.score_all() >= 3 and db.get_lead(roofer)["score"] == s_roof
    assert "roofer" in db.get_lead(roofer)["score_reason"]


def test_outreach_sends_best_first_and_respects_minimum(env):
    db, cfg = env
    weak = add(db, 1, rating=3.0, reviews=2)
    strong = add(db, 2, rating=4.9, reviews=200)
    mid = add(db, 3, rating=4.2, reviews=30)
    LeadScorer(db, cfg).score_all()
    one = Mailer(db, cfg, [Sender(email="a@x.example", from_name="A", daily_cap=1)])
    assert OutreachBot(db, cfg, one).run() == 1
    assert db.get_lead(strong)["status"] == "contacted"     # the single send went to the best lead
    picky = replace(cfg, min_lead_score=db.get_lead(mid)["score"])
    assert OutreachBot(db, picky).run() == 1                 # mid sent, weak skipped
    assert db.get_lead(weak)["status"] == "deployed" and db.get_lead(mid)["status"] == "contacted"


def test_pipeline_scores_and_dashboard_lists_top_leads(env):
    db, cfg = env
    add(db, 1, status="new")
    Orchestrator(db, cfg).run_once()
    assert db.get_lead(1)["score"] is not None
    dash = (cfg.obsidian_vault / "Dashboard.md").read_text()
    assert "Best leads not yet emailed" in dash
