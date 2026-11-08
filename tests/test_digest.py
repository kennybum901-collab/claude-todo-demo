import random
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from email import message_from_bytes
from http.server import ThreadingHTTPServer

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.outreach_bot import OutreachBot
from agency.config import Config
from agency.db import DB
from agency.digest import Digest, control_url
from agency.mailer import Mailer, Sender
from agency.orchestrator import Orchestrator
from agency.qa import check_email, check_site
from agency.quotes import submit
from agency.server import make_handler
from tests.test_pipeline import SAMPLE


class FakeSMTP:
    sent = []

    def __init__(self, *a, **k): pass
    def starttls(self): pass
    def login(self, *a): pass
    def send_message(self, m): FakeSMTP.sent.append(m)
    def quit(self): pass


@pytest.fixture
def env(tmp_path):
    FakeSMTP.sent = []
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites", obsidian_vault=tmp_path / "vault",
                  senders_file=tmp_path / "none.json", digest_email="me@owner.example", digest_hour=0,
                  public_url="https://bots.example", app_secret="s3cret")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    return db, cfg


def real_inbox(db, cfg):
    return Mailer(db, cfg, [Sender(email="a@one.example", from_name="A", smtp_host="smtp.x")], smtp_factory=FakeSMTP)


def test_email_checks(env):
    _, cfg = env
    good = f"Hi there,\n\nI built a site.\n\n--\n{cfg.company_name}, {cfg.company_address}\nReply STOP to opt out."
    assert check_email("I built a website for Rivera Plumbing", good, cfg) == []
    probs = check_email("ACT NOW!!! FREE WEBSITE FOR YOU", "Hi {{ name }}, click here", cfg)
    text = " ".join(probs)
    for expected in ("CAPITALS", "exclamation", "placeholder", "postal address", "opt out", "spam-trigger"):
        assert expected in text
    assert check_email("We're second to none", good, cfg) == []
    assert any("placeholder" in p for p in check_email("Hi None", good, cfg))
    assert any("file://" in p for p in check_email("s", good + " file:///tmp/x", replace(cfg, dry_run=False)))


def test_site_checks(env):
    db, cfg = env
    Orchestrator(db, cfg).run_once()
    lead = db.get_lead(1)
    assert check_site(lead, cfg) == []
    assert check_site(db.get_lead(3), cfg) == []        # "Bella's" is HTML-escaped on the page
    path = cfg.build_dir / lead["site_slug"] / "index.html"
    path.write_text(path.read_text().replace("<h1>", "<h1>{{ headline }} ").replace("tel:", "x:"))
    probs = " ".join(check_site(lead, cfg))
    assert "placeholder" in probs and "tap-to-call" in probs


def test_digest_in_dry_run_goes_only_to_owner(env):
    db, cfg = env
    mailer = real_inbox(db, cfg)
    orch = Orchestrator(db, cfg)
    orch.outreach = OutreachBot(db, cfg, mailer)
    orch.run_once()                                       # dry run: prospects get nothing
    assert FakeSMTP.sent == [] or all(m["To"] == "me@owner.example" for m in FakeSMTP.sent)
    FakeSMTP.sent = []
    d = Digest(db, cfg, mailer=mailer, rng=random.Random(1))
    assert d.send(force=True)
    (msg,) = FakeSMTP.sent
    assert msg["To"] == "me@owner.example" and "List-Unsubscribe" not in msg
    body = msg.get_body(("plain",)).get_content()
    assert "mode: DRY RUN" in body and "Emails sent" in body and "SAMPLE OF EMAILS SENT (3)" in body
    assert "I built a website for" in body or "website" in body.lower()
    assert "/control/pause?t=" in body
    names = [p.get_filename() for p in msg.iter_attachments()]
    assert len(names) == 3 and all(n.endswith(".html") for n in names)
    # digest itself is not archived as a prospect email
    assert db.conn.execute("SELECT COUNT(*) FROM messages WHERE bot='digest'").fetchone()[0] == 0


def test_digest_once_per_day_after_hour(env):
    db, cfg = env
    d = Digest(db, replace(cfg, digest_hour=13), mailer=real_inbox(db, cfg))
    morning = datetime(2026, 11, 9, 9, tzinfo=timezone.utc)
    afternoon = datetime(2026, 11, 9, 14, tzinfo=timezone.utc)
    assert not d.send(now=morning)
    assert d.send(now=afternoon) and not d.send(now=afternoon)
    assert d.send(now=datetime(2026, 11, 10, 14, tzinfo=timezone.utc))


def test_issues_flagged_in_subject(env):
    db, cfg = env
    Orchestrator(db, cfg).run_once()
    db.conn.execute("UPDATE messages SET body = body || ' {{ oops }}' WHERE id = 1")
    db.log("outreach", "error", "smtp timeout")
    db.conn.commit()
    msg, n = Digest(db, cfg, rng=random.Random(0)).compose()
    body = msg.get_body(("plain",)).get_content()
    assert n >= 2 and msg["Subject"].startswith("⚠️") and "NEEDS A LOOK" in body and "smtp timeout" in body


def test_kill_switch_stops_marketing_not_quotes(env):
    db, cfg = env
    db.set_setting("sending_paused", "1")
    assert Orchestrator(db, cfg).run_once()["emailed"] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM messages WHERE kind IN ('sent','dry_send')").fetchone()[0] == 0
    db.update_lead(1, status="client")
    live = replace(cfg, dry_run=False)
    submit(db, live, 1, {"name": "Jo", "phone": "5125550100"}, real_inbox(db, live))
    assert any("quote request" in m["Subject"] for m in FakeSMTP.sent)   # transactional still flows
    db.set_setting("sending_paused", None)
    assert Orchestrator(db, cfg).run_once()["emailed"] == 2   # lead 1 is now a client


def test_one_click_pause_link(env):
    db, cfg = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        link = control_url("pause", cfg).replace("https://bots.example", base)
        with urllib.request.urlopen(link) as r:              # GET = confirm page only
            assert "Pause all marketing email?" in r.read().decode()
        assert not DB(cfg.db_path).sending_paused()
        urllib.request.urlopen(urllib.request.Request(link, data=b"", method="POST"))
        assert DB(cfg.db_path).sending_paused()
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(f"{base}/control/pause?t=forged", data=b""))
        resume = control_url("resume", cfg).replace("https://bots.example", base)
        urllib.request.urlopen(urllib.request.Request(resume, data=b"", method="POST"))
        assert not DB(cfg.db_path).sending_paused()
    finally:
        httpd.shutdown()
