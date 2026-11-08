from dataclasses import replace

import pytest

from agency.bots.lead_bot import LeadBot
from agency.config import Config
from agency.db import DB
from agency.obsidian import ObsidianSync, parse_frontmatter
from agency.orchestrator import Orchestrator
from tests.test_pipeline import SAMPLE


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    return db, cfg


def lead_note(cfg, lead_id):
    return next((cfg.obsidian_vault / "Leads").glob(f"* ({lead_id}).md"))


def test_run_writes_vault(env):
    db, cfg = env
    Orchestrator(db, cfg).run_once()
    v = cfg.obsidian_vault
    assert len(list((v / "Leads").glob("*.md"))) == 4
    dash = (v / "Dashboard.md").read_text()
    assert "MRR" in dash and "[[Rivera Plumbing (1)]]" in dash
    assert list((v / "Daily").glob("*.md"))
    fm = parse_frontmatter(lead_note(cfg, 1).read_text())
    assert fm["status"] == "contacted" and fm["email"] == "owner@riveraplumbing.example"


def test_user_notes_survive_resync(env):
    db, cfg = env
    sync = ObsidianSync(db, cfg)
    sync.run()
    path = lead_note(cfg, 1)
    path.write_text(path.read_text() + "Owner is Maria, call after 3pm.\n")
    db.update_lead(1, status="replied")
    sync.run()
    text = path.read_text()
    assert "Owner is Maria, call after 3pm." in text
    assert 'status: "replied"' in text and "answer them personally" in text
    assert "[[Bella's Hair Studio (3)]]" not in (cfg.obsidian_vault / "Dashboard.md").read_text().split("Replies waiting")[1].split("##")[0]


def test_do_not_contact_flag_in_obsidian_blocks_email(env):
    db, cfg = env
    orch = Orchestrator(db, cfg)
    ObsidianSync(db, cfg).run()
    path = lead_note(cfg, 2)
    path.write_text(path.read_text().replace("do_not_contact: false", "do_not_contact: true"))
    summary = orch.run_once()
    assert summary["obsidian_pull"] == 1
    assert summary["emailed"] == 2  # lead 2 skipped
    assert db.is_suppressed("info@lonestarroof.example")
    assert db.get_lead(2)["status"] == "unsubscribed"
    assert "do_not_contact: true" in path.read_text()  # flag preserved after re-sync
