import base64
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date
from http.server import ThreadingHTTPServer

import pytest

from agency.analytics import MIN_SAMPLE, Analytics, month_ends
from agency.config import Config
from agency.dashboard import render
from agency.db import DB
from agency.obsidian import ObsidianSync
from agency.server import make_handler


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, db_path=tmp_path / "t.db",
                  build_dir=tmp_path / "sites", obsidian_vault=tmp_path / "vault",
                  senders_file=tmp_path / "none.json", admin_password="pw")
    return DB(cfg.db_path), cfg


def lead(db, i, niche="plumber", city="Austin", status="contacted", emailed=True):
    lid = db.add_lead(business_name=f"B{i}", niche=niche, city=city, email=f"b{i}@x.example",
                      status=status, source="t", source_id=f"s{i}", site_slug=f"b{i}")
    if emailed:
        db.log("outreach", "dry_send", "step 0", lid)
    return lid


def pay(db, lid, day, kind="setup+first_month"):
    db.conn.execute("INSERT INTO payments VALUES (?,?,?,?,?,?)", (f"p{lid}{day}", lid, 54800, "usd", kind, f"{day}T10:00:00+00:00"))
    db.conn.commit()


def event(db, lid, kind, day, detail=""):
    db.log("billing", kind, detail, lid)
    db.conn.execute("UPDATE events SET created_at=? WHERE id=(SELECT MAX(id) FROM events)", (f"{day}T10:00:00+00:00",))
    db.conn.commit()


def test_month_ends():
    m = month_ends(date(2026, 3, 15), 3)
    assert [k for k, _, _ in m] == ["2026-01", "2026-02", "2026-03"]
    assert m[-1][2] == date(2026, 3, 15) and m[1][2] == date(2026, 2, 28)


def test_mrr_history_with_churn_winback_and_addons(env):
    db, cfg = env
    a, b = lead(db, 1, status="client"), lead(db, 2, status="client")
    pay(db, a, "2026-07-10")
    pay(db, b, "2026-08-05")
    event(db, b, "churned", "2026-09-20")
    pay(db, b, "2026-10-15", kind="winback")                  # came back
    db.conn.execute("INSERT INTO addons (lead_id, addon, price_cents, status, offered_at, activated_at) "
                    "VALUES (?,?,?,?,?,?)", (a, "seo", 19900, "canceled", "x", "2026-08-01T00:00:00+00:00"))
    db.conn.commit()
    event(db, a, "addon_canceled", "2026-10-02", detail="seo")
    hist = {h["month"]: h for h in Analytics(db, cfg).mrr_history(date(2026, 11, 8))}
    fee = cfg.monthly_fee_cents
    assert hist["2026-06"]["base"] == 0
    assert hist["2026-07"]["base"] == fee and hist["2026-07"]["addons"] == 0
    assert hist["2026-08"]["base"] == 2 * fee and hist["2026-08"]["addons"] == 19900
    assert hist["2026-09"]["base"] == fee                       # b churned
    assert hist["2026-10"]["base"] == 2 * fee and hist["2026-10"]["addons"] == 0   # b back, add-on cancelled
    k = Analytics(db, cfg).kpis(date(2026, 11, 8))
    assert k["clients"] == 2 and k["arpu_cents"] == fee


def test_funnel_and_segments(env):
    db, cfg = env
    for i in range(30):
        lead(db, i, niche="roofer", city="Austin")
    for i in range(30, 60):
        lead(db, i, niche="plumber", city="Dallas")
    lead(db, 99, emailed=False, status="new")
    for lid in (31, 32, 33, 34):                              # 4 plumber clients
        db.update_lead(lid, status="client")
        db.log("outreach", "replied", "", lid)
        pay(db, lid, "2026-10-01")
    db.log("outreach", "replied", "", 1)                       # roofer replied, no sale
    a = Analytics(db, cfg)
    f = {s["stage"]: s for s in a.funnel()}
    assert f["Leads found"]["count"] == 61 and f["Emailed"]["count"] == 60
    assert f["Replied"]["count"] == 5 and f["Became client"]["count"] == 4
    assert f["Became client"]["rate_from_prev"] == pytest.approx(0.8)
    niches = {r["segment"]: r for r in a.segments("niche", date(2026, 11, 8))}
    assert niches["plumber"]["clients"] == 4 and niches["plumber"]["close_rate"] == pytest.approx(4 / 30)
    assert niches["plumber"]["mrr_cents"] == 4 * cfg.monthly_fee_cents
    assert niches["roofer"]["enough_data"] and niches["roofer"]["emailed"] >= MIN_SAMPLE
    snap = a.snapshot(date(2026, 11, 8))
    assert any("Double down on niche **plumber**" in r for r in snap.recommendations)
    assert list(niches)[0] == "plumber"                         # sorted by MRR


def test_no_data_is_safe(env):
    db, cfg = env
    snap = Analytics(db, cfg).snapshot()
    assert snap.kpis["mrr_cents"] == 0 and snap.kpis["ltv_cents"] is None
    assert "Not enough data" in snap.recommendations[0]
    html = render(snap, "Test Co")
    assert "<svg" in html and "Test Co: Performance" in html and "http" not in html.split("<body>")[1].split("</main>")[0].replace("https://", "")


def test_obsidian_performance_note(env):
    db, cfg = env
    lid = lead(db, 1, status="client")
    pay(db, lid, "2026-10-01")
    ObsidianSync(db, cfg).push()
    note = (cfg.obsidian_vault / "Performance.md").read_text()
    assert "xychart-beta" in note and "## By niche" in note and "| plumber |" in note


def test_admin_requires_password(env):
    db, cfg = env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cfg))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_port}/admin"
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(url)
        assert e.value.code == 401
        bad = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"admin:nope").decode()})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(bad)
        ok = urllib.request.Request(url, headers={"Authorization": "Basic " + base64.b64encode(b"admin:pw").decode()})
        with urllib.request.urlopen(ok) as r:
            assert r.status == 200 and "Monthly recurring revenue" in r.read().decode()
    finally:
        httpd.shutdown()
    off = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(replace(cfg, admin_password="")))
    threading.Thread(target=off.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(f"http://127.0.0.1:{off.server_port}/admin")
        assert e.value.code == 404                              # disabled without a password
    finally:
        off.shutdown()
