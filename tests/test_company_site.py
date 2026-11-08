import threading
import urllib.parse
import urllib.request
from dataclasses import replace
from email.message import EmailMessage
from http.server import ThreadingHTTPServer

import pytest

from agency.company_site import build, deploy, plural
from agency.config import Config
from agency.db import DB
from agency.orchestrator import Orchestrator
from agency.server import make_handler


@pytest.fixture
def cfg(tmp_path):
    return replace(Config(), company_name="SiteSprout", company_email="hello@sitesprout.example",
                   company_address="PO Box 1, Austin, TX", company_url="https://sitesprout.example",
                   public_url="https://bots.sitesprout.example", app_secret="s",
                   lead_queries=["roofer|Austin, TX", "hair salon|Austin, TX", "lawn care|Austin, TX"],
                   db_path=tmp_path / "d" / "t.db", build_dir=tmp_path / "d" / "sites",
                   obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json", use_ai_copy=False)


def test_plural():
    assert [plural(n) for n in ("roofer", "hair salon", "lawn care", "pest control", "nail spa")] == \
        ["roofers", "hair salons", "lawn care companies", "pest control companies", "nail spas"]


def test_build_site_content_and_examples(cfg):
    out = build(cfg)
    html = (out / "index.html").read_text()
    assert "Site<span>Sprout</span>" in html
    assert "roofers, hair salons and lawn care companies in Austin, TX" in html
    assert "$499" in html and "$49" in html and "Review Booster" in html
    assert 'action="https://bots.sitesprout.example/get-started"' in html and 'name="consent"' in html
    assert "PO Box 1, Austin, TX" in html
    for claim in ("testimonial", "★", "trusted by", "clients served", "5-star"):
        assert claim.lower() not in html.lower()                      # no invented social proof
    for design in ("bold", "elegant", "fresh", "classic"):
        ex = (out / "examples" / design / "index.html").read_text()
        assert "not a real business" in ex and 'content="noindex"' in ex and f"theme-{design}" in ex
    assert not DB(cfg.db_path).conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]   # nothing written


def test_mailto_fallback_without_server(cfg):
    html = (build(replace(cfg, public_url="")) / "index.html").read_text()
    assert "mailto:hello@sitesprout.example" in html and "/get-started" not in html


def test_get_started_creates_warm_lead(cfg):
    DB(cfg.db_path)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        data = urllib.parse.urlencode({"business_name": "Ace Plumbing", "email": "ace@plumb.example", "niche": "plumber",
                                       "city": "Austin, TX", "consent": "on", "website": ""}).encode()
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}/get-started", data=data)) as r:
            assert "You're in!" in r.read().decode()
    finally:
        httpd.shutdown()
    db = DB(cfg.db_path)
    lead = db.conn.execute("SELECT * FROM leads").fetchone()
    assert lead["source"] == "inbound" and lead["referred_by"] is None and "consented" in lead["notes"]
    orch = Orchestrator(db, replace(cfg, min_lead_score=100))           # cold leads blocked by score...
    orch.run_once()
    assert db.get_lead(lead["id"])["status"] == "contacted"            # ...inbound still emailed first
    msg = orch.outreach.compose(dict(db.get_lead(lead["id"])), 0)
    assert msg["Subject"] == "Your free website preview for Ace Plumbing"
    assert "Thanks for requesting a free preview" in msg.get_content() and "came across" not in msg.get_content()


def test_deploy_requires_live_mode(cfg):
    with pytest.raises(RuntimeError, match="NETLIFY_TOKEN"):
        deploy(cfg, build(cfg), DB(cfg.db_path))
