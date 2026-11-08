import io
import json
from dataclasses import replace
from email.message import EmailMessage

import pytest
from PIL import Image

from agency.bots.builder_bot import BuilderBot
from agency.bots.lead_bot import LeadBot
from agency.bots.outreach_bot import OutreachBot
from agency.config import Config
from agency.db import DB
from agency.edits import EditBot
from agency.obsidian import ObsidianSync
from agency.orchestrator import Orchestrator
from agency.photos import PhotoError, Photos, process_image
from agency.qa import check_site
from tests.test_pipeline import SAMPLE


def jpeg_with_gps(w=3000, h=2000) -> bytes:
    im = Image.new("RGB", (w, h), (30, 120, 200))
    exif = Image.Exif()
    exif[0x8825] = {1: "N", 2: (30.0, 16.0, 0.0), 3: "W", 4: (97.0, 44.0, 0.0)}  # GPS IFD: Austin, TX
    exif[0x010F] = "PhoneMaker"
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, google_places_key="", imap_host="",
                  db_path=tmp_path / "data" / "t.db", build_dir=tmp_path / "data" / "sites",
                  obsidian_vault=tmp_path / "vault", senders_file=tmp_path / "none.json", pexels_api_key="")
    db = DB(cfg.db_path)
    LeadBot(db, cfg).import_csv(SAMPLE)
    db.update_lead(3, status="client")
    out = BuilderBot(db, cfg).build(dict(db.get_lead(3)), preview=False)
    db.update_lead(3, site_slug=out.name)
    return db, cfg


def test_process_strips_gps_and_resizes():
    raw = jpeg_with_gps()
    assert Image.open(io.BytesIO(raw)).getexif().get(0x8825)               # GPS present before
    out, w, h = process_image(raw)
    im = Image.open(io.BytesIO(out))
    assert (w, h) == (1600, 1067) and im.format == "JPEG"
    assert not im.getexif() and b"PhoneMaker" not in out                   # all metadata gone
    with pytest.raises(PhotoError):
        process_image(b"<?php system($_GET['x']); ?>")
    with pytest.raises(PhotoError, match="too large"):
        process_image(b"0" * 16_000_000)


def client_email(attachments):
    msg = EmailMessage()
    msg["From"] = "Bella <bella@bellashair.example>"
    msg["Subject"] = "Photos for the site"
    msg.set_content("Here are some pictures of the salon!")
    for name, data, sub in attachments:
        msg.add_attachment(data, maintype="image", subtype=sub, filename=name)
    return msg


def test_emailed_photos_need_approval_then_go_live(env):
    db, cfg = env
    bot = OutreachBot(db, cfg)
    bot.process_message(client_email([("salon_front.jpg", jpeg_with_gps(), "jpeg"),
                                      ("chair.jpg", jpeg_with_gps(800, 600), "jpeg"),
                                      ("virus.jpg", b"MZ\x90\x00not an image", "jpeg")]))
    edit = db.conn.execute("SELECT * FROM edits WHERE proposed_json LIKE '%photos%'").fetchone()
    assert edit["status"] == "drafted" and "Add 2 photos" in edit["summary"] and "weren't usable" in edit["summary"]
    site = cfg.build_dir / db.get_lead(3)["site_slug"]
    orch = Orchestrator(db, cfg)
    orch.builder.build(dict(db.get_lead(3)), preview=False)
    assert "<img" not in (site / "index.html").read_text()                  # pending photos never published

    ObsidianSync(db, cfg).push()
    note = next((cfg.obsidian_vault / "Edits").glob("*.md")).read_text()
    assert "![[attachments/photo-" in note
    assert list((cfg.obsidian_vault / "Edits" / "attachments").glob("photo-*.jpg"))

    eb = EditBot(db, cfg)
    assert eb.decide(edit["id"], approve=True)
    eb.run(orch.builder, orch.deployer)
    html = (site / "index.html").read_text()
    assert 'class="hero-photo"' in html and 'alt="salon front"' in html and "Our Work" in html
    assert len(list((site / "img").glob("*.jpg"))) == 2
    db.update_lead(3, site_slug=site.name)
    assert check_site(db.get_lead(3), cfg) == []


def test_rejected_photos_stay_off_the_site(env):
    db, cfg = env
    eid = Photos(db, cfg).intake(3, [("a.jpg", jpeg_with_gps(400, 300))])
    EditBot(db, cfg).decide(eid, approve=False)
    assert db.conn.execute("SELECT status FROM photos").fetchone()[0] == "rejected"
    assert Photos(db, cfg).for_site(3) == []


def test_non_clients_cannot_add_photos(env):
    db, cfg = env
    msg = client_email([("x.jpg", jpeg_with_gps(400, 300), "jpeg")])
    msg.replace_header("From", "owner@riveraplumbing.example")            # a prospect, not a client
    OutreachBot(db, cfg).process_message(msg)
    assert db.conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 0


class FakePexels:
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)

        class R:
            def __init__(s, data=None, content=b""):
                s._d, s.content = data, content
            def raise_for_status(s): pass
            def json(s): return s._d
        if "api.pexels.com" in url:
            return R({"photos": [{"src": {"large": f"https://images.pexels.com/{i}.jpg"}, "photographer": f"Ann {i}",
                                  "alt": "Hair salon interior"} for i in range(3)]})
        return R(content=jpeg_with_gps(900, 600))


def test_stock_photos_with_credit_replaced_by_client_photos(env):
    db, cfg = env
    stock_cfg = replace(cfg, pexels_api_key="key")
    http = FakePexels()
    assert Photos(db, stock_cfg, http).ensure_stock(db.get_lead(2)) == 3
    assert Photos(db, stock_cfg, http).ensure_stock(db.get_lead(2)) == 0   # only once
    lead = dict(db.get_lead(2))
    out = BuilderBot(db, cfg).build(lead)
    html = (out / "index.html").read_text()
    assert "Photos: Ann 0 / Pexels, Ann 1 / Pexels, Ann 2 / Pexels" in html
    eid = Photos(db, cfg).intake(2, [("van.jpg", jpeg_with_gps(500, 400))])
    Photos(db, cfg).approve(json.loads(db.conn.execute("SELECT proposed_json FROM edits WHERE id=?", (eid,)).fetchone()[0])["photos"])
    assert [p["source"] for p in Photos(db, cfg).for_site(2)] == ["client"]  # client photos win
