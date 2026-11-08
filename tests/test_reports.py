from dataclasses import replace
from datetime import date

import pytest

from agency.bots.lead_bot import LeadBot
from agency.bots.report_bot import ReportBot, previous_period
from agency.bots.upsell_bot import UpsellBot
from agency.config import Config
from agency.db import DB
from agency.mailer import Mailer, Sender
from tests.test_pipeline import SAMPLE


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json", report_day=1)
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    return db, cfg


def client(db, lead_id, since="2026-09-15T10:00:00+00:00"):
    db.conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)", (f"p{lead_id}", lead_id, 54800, "usd", "x", since))
    db.update_lead(lead_id, status="client", preview_url="https://site.example")
    db.conn.commit()


def uptime(db, lead_id, day, oks):
    for ok in oks:
        db.conn.execute("INSERT INTO uptime (lead_id, url, ok, status, ms, created_at) VALUES (?,?,?,?,?,?)",
                        (lead_id, "https://site.example", int(ok), 200 if ok else 503, 300, f"{day}T12:00:00+00:00"))
    db.conn.commit()


def test_previous_period():
    assert previous_period(date(2026, 11, 8)) == ("2026-10", date(2026, 10, 1), date(2026, 10, 31))
    assert previous_period(date(2026, 1, 3)) == ("2025-12", date(2025, 12, 1), date(2025, 12, 31))


def test_report_content(env):
    db, cfg = env
    client(db, 3)
    uptime(db, 3, "2026-10-10", [True] * 8 + [False, False] + [True] * 10)
    db.log("deploy", "deployed", "https://site.example", 3)
    db.conn.execute("UPDATE events SET created_at='2026-10-12T09:00:00+00:00' WHERE kind='deployed'")
    db.conn.commit()
    UpsellBot(db, cfg).activate(3, "booking")
    db.conn.execute("UPDATE events SET created_at='2026-10-20T09:00:00+00:00' WHERE kind='addon_active'")
    db.conn.commit()
    msg, data = ReportBot(db, cfg).compose(db.get_lead(3), "2026-10", date(2026, 10, 1), date(2026, 10, 31))
    body = msg.get_content()
    assert msg["Subject"] == "Bella's Hair Studio website report: October 2026"
    assert data["checks"] == 20 and data["incidents"] == 1 and round(data["uptime_pct"]) == 90
    assert "Uptime: 90.00%" in body and "1 brief outage handled" in body
    assert "Published updates to your website" in body and "Set up Online Booking" in body
    assert "Online Booking: $79/month" in body and "$49/month" in body
    assert "Local SEO" in body          # next suggested add-on
    assert cfg.company_address in body


def test_once_per_month_and_eligibility(env):
    db, cfg = env
    client(db, 1)                                   # client since September -> gets October report
    client(db, 2, since="2026-11-02T10:00:00+00:00")  # joined in November -> no October report
    client(db, 4)                                   # phone-only, no email
    db.suppress("owner@riveraplumbing.example", "x")
    client(db, 3)
    bot = ReportBot(db, cfg)
    assert bot.run(date(2026, 11, 8)) == 1          # only lead 3 (1 suppressed, 2 too new, 4 no email)
    assert bot.run(date(2026, 11, 9)) == 0          # already sent for 2026-10
    assert bot.run(date(2026, 12, 1)) == 2          # November report: 2 and 3
    assert list((cfg.obsidian_vault / "Reports").glob("*2026-10.md"))


def test_waits_for_report_day_and_inbox_capacity(env):
    db, cfg = env
    client(db, 3)
    assert ReportBot(db, replace(cfg, report_day=10)).run(date(2026, 11, 8)) == 0
    full = Mailer(db, cfg, [Sender(email="a@x.example", from_name="A", daily_cap=0)])
    assert ReportBot(db, cfg, full).run(date(2026, 11, 8)) == 0
    assert db.conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0  # retried later
