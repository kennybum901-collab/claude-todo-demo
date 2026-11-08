import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.report_bot import ReportBot
from agency.bots.upsell_bot import UpsellBot
from agency.config import Config
from agency.db import DB
from agency.mailer import Mailer, Sender
from agency.reviews import ReviewBot, ReviewError, click_url, jobs_url, request_review, review_url_for
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
    cfg = replace(Config(), dry_run=False, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites", obsidian_vault=tmp_path / "vault",
                  senders_file=tmp_path / "none.json", public_url="https://bots.example", app_secret="s3cret")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    db.update_lead(3, status="client", source_id="gp:ChIJbella")            # Bella's, found via Google Places
    UpsellBot(db, cfg).activate(3, "reviews")
    mailer = Mailer(db, cfg, [Sender(email="a@one.example", from_name="Agency", smtp_host="h")], smtp_factory=FakeSMTP)
    return db, cfg, mailer


def test_review_link_auto_from_places(env):
    db, cfg, _ = env
    assert review_url_for(db.get_lead(3)) == "https://search.google.com/local/writereview?placeid=ChIJbella"
    assert review_url_for(db.get_lead(2)) is None
    db.update_lead(2, review_url="https://g.page/r/abc/review")
    assert review_url_for(db.get_lead(2)) == "https://g.page/r/abc/review"


def test_only_booster_clients_and_one_request_per_customer(env):
    db, cfg, _ = env
    with pytest.raises(ReviewError, match="isn't active"):
        request_review(db, 1, "Jo", "jo@x.example")
    request_review(db, 3, "Jane Doe", "Jane@Home.example")
    with pytest.raises(ReviewError, match="already been asked"):
        request_review(db, 3, "Jane", "jane@home.example")
    with pytest.raises(ReviewError, match="email"):
        request_review(db, 3, "X", "nope")


def test_welcome_request_and_reminder(env):
    db, cfg, mailer = env
    bot = ReviewBot(db, cfg, mailer)
    rid = request_review(db, 3, "Jane Doe", "jane@home.example")
    out = bot.run()
    assert out == {"welcomed": 1, "sent": 1, "reminded": 0}
    welcome, req = FakeSMTP.sent
    assert jobs_url(3, cfg) in welcome.get_content() and welcome["To"] == "bella@bellashair.example"
    assert req["To"] == "jane@home.example" and req["From"].startswith("Bella's Hair Studio <")
    assert req["Reply-To"] == "bella@bellashair.example" and "List-Unsubscribe" in req
    body = req.get_content()
    # never review-gated: the public review link is in every request
    assert "Hi Jane," in body and f"Leave a review: {click_url(rid, cfg)}" in body
    assert click_url(rid, cfg, feedback=True) in body and cfg.company_address in body
    assert bot.run() == {"welcomed": 0, "sent": 0, "reminded": 0}      # nothing due yet
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    db.conn.execute("UPDATE review_requests SET sent_at=?", (old,))
    db.conn.commit()
    assert bot.run()["reminded"] == 1
    assert "Leave a review:" in FakeSMTP.sent[-1].get_content()       # reminder carries the link too
    db.conn.execute("UPDATE review_requests SET reminded_at=NULL, sent_at=?", (old,))
    db.conn.commit()
    db.suppress("jane@home.example", "x")
    assert bot.run()["reminded"] == 0                                 # opt-outs respected


def test_no_review_link_means_no_send(env):
    db, cfg, mailer = env
    db.update_lead(1, status="client")
    UpsellBot(db, cfg).activate(1, "reviews")
    request_review(db, 1, "A", "a@x.example")
    assert ReviewBot(db, cfg, mailer).run()["sent"] == 0               # Rivera has no review link yet
    assert db.conn.execute("SELECT sent_at FROM review_requests WHERE lead_id=1").fetchone()[0] is None
    assert db.conn.execute("SELECT 1 FROM events WHERE kind='skipped' AND detail='no review link set'").fetchone()
    db.update_lead(1, review_url="https://g.page/r/rivera/review")
    assert ReviewBot(db, cfg, mailer).run()["sent"] == 1               # sent once the link is set


def test_report_shows_review_stats(env):
    db, cfg, mailer = env
    db.conn.execute("INSERT INTO payments VALUES ('p3',3,54800,'usd','setup+first_month','2026-09-01T00:00:00+00:00')")
    for i, clicked in enumerate((True, False, True)):
        db.conn.execute("INSERT INTO review_requests (lead_id, customer_email, created_at, sent_at, clicked_at) "
                        "VALUES (3, ?, 'x', '2026-10-05T10:00:00+00:00', ?)", (f"c{i}@x.example", "x" if clicked else None))
    db.conn.commit()
    msg, _ = ReportBot(db, cfg).compose(db.get_lead(3), "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    assert "3 review requests sent, 2 customers opened your review page" in msg.get_content()


@pytest.fixture
def server(env):
    db, cfg, _ = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}", db, cfg
    httpd.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def test_http_jobs_page_click_and_feedback(server):
    base, db, cfg = server
    db.conn.execute("INSERT INTO quotes (lead_id, name, email, created_at) VALUES (3, 'Sam', 'sam@x.example', 'x')")
    db.conn.commit()
    jobs = jobs_url(3, cfg).replace("https://bots.example", base)
    with urllib.request.urlopen(jobs) as r:
        page = r.read().decode()
    assert "Ask for a review" in page and "sam@x.example" in page        # recent quote offered
    data = urllib.parse.urlencode({"name": "Sam", "email": "sam@x.example"}).encode()
    with urllib.request.urlopen(urllib.request.Request(jobs, data=data)) as r:
        assert "will get a review request" in r.read().decode()
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(f"{base}/jobs/3/forged")
    rid = DB(cfg.db_path).conn.execute("SELECT id FROM review_requests").fetchone()[0]
    click = click_url(rid, cfg).replace("https://bots.example", base)
    opener = urllib.request.build_opener(NoRedirect)
    with pytest.raises(urllib.error.HTTPError) as e:
        opener.open(click)
    assert e.value.code == 302 and "writereview?placeid=ChIJbella" in e.value.headers["Location"]
    assert DB(cfg.db_path).conn.execute("SELECT clicked_at FROM review_requests").fetchone()[0]
    fb = click_url(rid, cfg, feedback=True).replace("https://bots.example", base)
    with urllib.request.urlopen(fb) as r:
        assert "Leave a review" in r.read().decode()                     # public option on the feedback page
    with urllib.request.urlopen(urllib.request.Request(fb, data=b"message=Great+cut%2C+a+bit+late")) as r:
        assert "has your message" in r.read().decode()
    assert DB(cfg.db_path).conn.execute("SELECT feedback FROM review_requests").fetchone()[0] == "Great cut, a bit late"
