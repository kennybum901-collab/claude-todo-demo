from dataclasses import replace
from datetime import date, timedelta
from email import message_from_string

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.outreach_bot import OutreachBot
from agency.config import Config
from agency.db import DB
from agency.mailer import Mailer, Sender, load_senders
from agency.obsidian import ObsidianSync
from tests.test_pipeline import SAMPLE


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json")
    db = DB(cfg.db_path)
    return db, cfg


def add_leads(db, n):
    for i in range(n):
        db.add_lead(business_name=f"Biz {i}", niche="plumber", city="Austin", email=f"o{i}@biz{i}.example",
                    status="deployed", preview_url="https://p.example", payment_url="https://pay",
                    payment_link_id="plink", source="t", source_id=f"t{i}")


def test_warmup_ramp():
    s = Sender(email="a@x", from_name="A", daily_cap=30, warmup_start="2026-01-01")
    d0 = date(2026, 1, 1)
    assert s.cap_on(d0 - timedelta(days=1)) == 0
    assert s.cap_on(d0) == 5 and s.cap_on(d0 + timedelta(days=5)) == 15
    assert s.cap_on(d0 + timedelta(days=100)) == 30


def test_rotation_respects_each_cap_and_is_sticky(env):
    db, cfg = env
    add_leads(db, 12)
    senders = [Sender(email="a@one.example", from_name="A", daily_cap=4),
               Sender(email="b@two.example", from_name="B", daily_cap=6)]
    mailer = Mailer(db, cfg, senders)
    assert OutreachBot(db, cfg, mailer).run() == 10       # 4 + 6, two leads wait
    assert mailer.sent_today(senders[0]) == 4 and mailer.sent_today(senders[1]) == 6
    first = {r["id"]: r["sender"] for r in db.leads_by_status("contacted")}
    assert set(first.values()) == {"a@one.example", "b@two.example"}
    # the lead's inbox is reused for follow-ups
    lead = db.leads_by_status("contacted")[0]
    fresh = Mailer(db, cfg, [Sender(email="a@one.example", from_name="A", daily_cap=100),
                             Sender(email="b@two.example", from_name="B", daily_cap=100)])
    assert fresh.pick(lead).email == lead["sender"]


def test_from_header_uses_inbox(env):
    db, cfg = env
    add_leads(db, 1)
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, m): sent.append(m)
        def quit(self): pass

    live = replace(cfg, dry_run=False)
    mailer = Mailer(db, live, [Sender(email="sam@two.example", from_name="Sam", smtp_host="smtp.x")],
                    smtp_factory=FakeSMTP)
    assert OutreachBot(db, live, mailer).run() == 1
    assert sent[0]["From"] == "Sam <sam@two.example>"
    assert "sam@two.example" in sent[0]["List-Unsubscribe"]


def test_bounce_suppresses_and_pauses_inbox(env):
    db, cfg = env
    add_leads(db, 25)
    s = Sender(email="a@one.example", from_name="A", daily_cap=100)
    mailer = Mailer(db, cfg, [s])
    bot = OutreachBot(db, cfg, mailer)
    assert bot.run() == 25
    raw = ("From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
           "Subject: Delivery Status Notification (Failure)\n\n"
           "Your message wasn't delivered to o3@biz3.example because the address couldn't be found.\n")
    assert bot.process_message(message_from_string(raw), "a@one.example") == "bounce"
    assert db.is_suppressed("o3@biz3.example") and db.get_lead(4)["status"] == "bounced"
    assert not mailer.is_paused(s)                      # 1/25 = 4% < 5%
    bot.handle_bounce("o5@biz5.example", "a@one.example")
    assert mailer.is_paused(s) and mailer.total_remaining() == 0   # 2/25 = 8%
    ObsidianSync(db, cfg, mailer).push()
    assert "⏸️ paused" in (cfg.obsidian_vault / "Dashboard.md").read_text()


def test_normal_reply_not_treated_as_bounce(env):
    db, cfg = env
    LeadBot(db, cfg).import_csv(SAMPLE)
    db.conn.execute("UPDATE leads SET status='contacted'")
    db.conn.commit()
    raw = "From: owner@riveraplumbing.example\nSubject: Re: your site\n\nLooks good, what's next?\n"
    assert OutreachBot(db, cfg).process_message(message_from_string(raw)) == "replied"


def test_senders_file_loading(env, tmp_path, monkeypatch):
    db, cfg = env
    f = tmp_path / "senders.json"
    f.write_text('[{"email": "a@x.example", "smtp_host": "h", "password_env": "PW1"}]')
    monkeypatch.setenv("PW1", "secret")
    (s,) = load_senders(replace(cfg, senders_file=f))
    assert s.password == "secret" and s.smtp_user == "a@x.example" and s.daily_cap == 30
    (fallback,) = load_senders(cfg)
    assert fallback.email == cfg.company_email and fallback.daily_cap == cfg.daily_email_cap
