import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from agency.bots.builder_bot import BuilderBot
from agency.bots.lead_bot import LeadBot
from agency.bots.report_bot import ReportBot
from agency.config import Config
from agency.db import DB
from agency.mailer import Mailer, Sender
from agency.obsidian import ObsidianSync
from agency.quotes import QuoteError, clean, form_action, submit
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
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json",
                  public_url="https://bots.example", app_secret="s3cret")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    db.update_lead(1, status="client", preview_url="https://rivera.example", site_slug="rivera-1")
    return db, cfg


GOOD = {"name": "Jane Doe", "phone": "(512) 555-0100", "email": "jane@home.example",
        "service": "Drain Cleaning", "message": "Kitchen sink is backed up.", "website": ""}


def test_form_only_on_client_sites(env):
    db, cfg = env
    b = BuilderBot(db, cfg)
    lead = dict(db.get_lead(1))
    copy = {"headline": "h", "tagline": "t", "about": "a", "cta": "c",
            "services": [{"name": "Drain Cleaning", "description": "d"}]}
    live = b.render(lead, copy, preview=False)
    assert form_action(1, cfg) in live and 'name="website"' in live and "Drain Cleaning</option>" in live
    assert "/quote/" not in b.render(lead, copy, preview=True)
    no_server = BuilderBot(db, replace(cfg, public_url=""))
    assert "/quote/" not in no_server.render(lead, copy, preview=False)


def test_validation():
    assert clean(GOOD)["name"] == "Jane Doe"
    for bad, msg in [({**GOOD, "name": ""}, "name"), ({**GOOD, "phone": "", "email": ""}, "phone number or email"),
                     ({**GOOD, "email": "nope"}, "email"), ({**GOOD, "phone": "12"}, "phone")]:
        with pytest.raises(QuoteError, match=msg):
            clean(bad)
    with pytest.raises(QuoteError, match="spam"):
        clean({**GOOD, "website": "http://spam"})
    assert len(clean({**GOOD, "message": "x" * 10000})["message"]) == 3000


def test_submit_forwards_to_owner_even_when_cold_cap_is_full(env):
    db, cfg = env
    live = replace(cfg, dry_run=False)
    mailer = Mailer(db, live, [Sender(email="a@one.example", from_name="A", smtp_host="h", daily_cap=0)],
                    smtp_factory=FakeSMTP)
    qid = submit(db, live, 1, GOOD, mailer)
    (msg,) = FakeSMTP.sent
    assert msg["To"] == "owner@riveraplumbing.example" and msg["Reply-To"] == "jane@home.example"
    assert "Kitchen sink is backed up." in msg.get_content() and "List-Unsubscribe" not in msg
    assert db.conn.execute("SELECT notified FROM quotes WHERE id=?", (qid,)).fetchone()[0] == 1
    assert mailer.sent_today(mailer.senders[0]) == 0      # doesn't eat cold-email capacity


def test_quote_for_non_client_rejected(env):
    db, cfg = env
    with pytest.raises(QuoteError, match="not active"):
        submit(db, cfg, 2, GOOD)


def test_phone_only_client_quote_flagged_in_obsidian(env):
    db, cfg = env
    db.update_lead(4, status="client")                     # Mike's Lawn Care has no email
    submit(db, cfg, 4, GOOD)
    ObsidianSync(db, cfg).push()
    dash = (cfg.obsidian_vault / "Dashboard.md").read_text()
    assert "**1** this month" in dash and "not forwarded" in dash
    note = next((cfg.obsidian_vault / "Leads").glob("* (4).md")).read_text()
    assert "## Quote requests (1 total)" in note and "Jane Doe" in note


def test_report_counts_quotes(env):
    db, cfg = env
    db.conn.execute("INSERT INTO payments VALUES ('p1',1,54800,'usd','x','2026-09-01T00:00:00+00:00')")
    for day in ("2026-09-20", "2026-10-03", "2026-10-21"):
        db.conn.execute("INSERT INTO quotes (lead_id, name, created_at) VALUES (1, 'x', ?)", (f"{day}T10:00:00+00:00",))
    db.conn.commit()
    msg, data = ReportBot(db, cfg).compose(db.get_lead(1), "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    assert data["quotes"] == 2
    assert "2 quote requests came in through your website (3 since launch)" in msg.get_content()


@pytest.fixture
def server(env):
    db, cfg = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", db, cfg
    httpd.shutdown()


def post(url, form):
    data = urllib.parse.urlencode(form).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data)) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_http_form_flow(server):
    base, db, cfg = server
    action = form_action(1, cfg).replace("https://bots.example", base)
    code, html = post(action, GOOD)
    assert code == 200 and "your request was sent" in html and "https://rivera.example" in html
    assert post(action, {**GOOD, "email": "bad", "phone": ""})[0] == 400
    code, html = post(action, {**GOOD, "website": "spam"})
    assert code == 200                                     # honeypot: fake success...
    assert DB(cfg.db_path).conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1  # ...not stored
    assert post(f"{base}/quote/1/forged", GOOD)[0] == 404
    for _ in range(3):
        post(action, GOOD)
    assert post(action, GOOD)[0] == 429                   # 5 per IP per site per hour
