import json
from dataclasses import replace

import pytest

from agency.bots.lead_bot import LeadBot
from agency.config import Config
from agency.db import DB
from agency.edits import EditBot, diff, sanitize
from agency.mailer import Mailer, Sender
from agency.obsidian import ObsidianSync
from agency.orchestrator import Orchestrator
from tests.test_pipeline import SAMPLE

COPY = {"headline": "Rivera Plumbing", "tagline": "Trusted plumber", "about": "Family owned.",
        "cta": "Call today", "services": [{"name": "Drain Cleaning", "description": "Fast."}],
        "email_opener": "hi"}


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    cfg.build_dir.mkdir(parents=True)
    (cfg.build_dir / "1-copy.json").write_text(json.dumps(COPY))
    db.update_lead(1, status="client", site_slug="rivera-plumbing-1", preview_url="https://rivera.example")
    return db, cfg


def fake_ai(result):
    def drafter(before, message, cfg):
        return {**before, **result}
    return drafter


def test_client_reply_creates_edit_request(env):
    db, cfg = env
    orch = Orchestrator(db, cfg)
    assert orch.outreach.classify_reply("owner@riveraplumbing.example", "Please add Saturday hours 9-2") == "client_reply"
    assert db.conn.execute("SELECT status FROM edits").fetchone()[0] == "new"
    assert orch.outreach.classify_reply("owner@riveraplumbing.example", "STOP") == "client_opt_out"
    assert db.conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1


def test_full_flow_draft_approve_in_obsidian_apply(env):
    db, cfg = env
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, m): sent.append(m)
        def quit(self): pass

    live = replace(cfg, dry_run=False)
    mailer = Mailer(db, live, [Sender(email="a@one.example", from_name="A", smtp_host="h")], smtp_factory=FakeSMTP)
    orch = Orchestrator(db, live)
    orch.mailer = mailer
    bot = EditBot(db, live, mailer, drafter=fake_ai({
        "is_change_request": True, "needs_human": False, "summary": "Add Saturday hours and new phone",
        "hours": "Mon-Fri 8-6, Sat 9-2", "phone": "(512) 555-0199"}))
    eid = bot.submit(1, "We're open Saturdays 9-2 now. New number 512-555-0199.")
    bot.run(orch.builder, orch.deployer)
    assert db.conn.execute("SELECT status FROM edits WHERE id=?", (eid,)).fetchone()[0] == "drafted"

    sync = ObsidianSync(db, live, mailer)
    sync.push()
    note = next((cfg.obsidian_vault / "Edits").glob("*.md"))
    text = note.read_text()
    assert "| **hours** | (empty) | Mon-Fri 8-6, Sat 9-2 |" in text and "| **phone** |" in text
    assert "Add Saturday hours" in (cfg.obsidian_vault / "Dashboard.md").read_text()
    note.write_text(text.replace("approve: false", "approve: true"))
    sync.push()                                            # must not clobber the approval
    assert "approve: true" in note.read_text()
    assert sync.pull_edit_decisions() == 1

    orch.deployer.cfg = replace(cfg)                       # dry deploy (no Netlify in tests)
    assert bot.run(orch.builder, orch.deployer)["applied"] == 1
    html = (cfg.build_dir / "rivera-plumbing-1" / "index.html").read_text()
    assert "Mon-Fri 8-6, Sat 9-2" in html and "(512) 555-0199" in html
    assert db.get_lead(1)["phone"] == "(512) 555-0199"
    confirm = [m for m in sent if "update is live" in m["Subject"]]
    assert confirm and "Add Saturday hours" in confirm[0].get_content()


def test_not_a_request_and_needs_human(env):
    db, cfg = env
    bot = EditBot(db, cfg, drafter=fake_ai({"is_change_request": False, "needs_human": False, "summary": "Just thanks"}))
    e1 = bot.submit(1, "Thanks, looks great!")
    bot.draft(db.conn.execute("SELECT * FROM edits WHERE id=?", (e1,)).fetchone())
    assert db.conn.execute("SELECT status FROM edits WHERE id=?", (e1,)).fetchone()[0] == "not_request"
    assert not bot.decide(e1, approve=True)

    no_ai = EditBot(db, cfg)                               # no API key -> human handles it
    e2 = no_ai.submit(1, "Can you swap the photo on top?")
    no_ai.draft(db.conn.execute("SELECT * FROM edits WHERE id=?", (e2,)).fetchone())
    row = db.conn.execute("SELECT * FROM edits WHERE id=?", (e2,)).fetchone()
    assert row["status"] == "needs_human" and row["proposed_json"] is None
    assert not no_ai.decide(e2, approve=True)              # nothing to auto-apply
    assert no_ai.decide(e2, approve=False)                 # close it after doing it by hand


def test_sanitize_whitelists_and_limits():
    before = {"headline": "a", "tagline": "b", "about": "c", "cta": "d", "hours": "", "phone": "1",
              "address": "x", "services": [{"name": "S", "description": "D"}]}
    out = sanitize({"headline": "H" * 500, "script": "<script>", "services": "bad"}, before)
    assert len(out["headline"]) == 120 and "script" not in out and out["services"] == before["services"]
    assert [f for f, _, _ in diff(before, out)] == ["headline"]


def test_ai_failure_routes_to_human(env):
    db, cfg = env
    def boom(*a):
        raise RuntimeError("api down")
    bot = EditBot(db, cfg, drafter=boom)
    eid = bot.submit(1, "change hours")
    assert bot.draft(db.conn.execute("SELECT * FROM edits WHERE id=?", (eid,)).fetchone()) == "needs_human"
