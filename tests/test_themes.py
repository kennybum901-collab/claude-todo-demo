import json
from dataclasses import replace

import pytest

from agency.analytics import Analytics
from agency.bots.builder_bot import THEMES, BuilderBot, pick_theme
from agency.config import Config
from agency.db import DB
from agency.qa import check_site


@pytest.fixture
def env(tmp_path):
    cfg = replace(Config(), dry_run=True, use_ai_copy=False, db_path=tmp_path / "t.db", build_dir=tmp_path / "sites",
                  senders_file=tmp_path / "none.json", public_url="https://bots.example", app_secret="s", site_theme="")
    return DB(cfg.db_path), cfg


@pytest.mark.parametrize("niche,theme", [("roofer", "bold"), ("Plumbing", "bold"), ("hair salon", "elegant"),
                                         ("Nail Spa", "elegant"), ("landscaper", "fresh"), ("house cleaning", "fresh"),
                                         ("bakery", "classic"), (None, "classic")])
def test_pick_theme(niche, theme):
    assert pick_theme(niche) == theme
    assert pick_theme(niche, forced="bold") == "bold"


def add(db, i, niche):
    return db.add_lead(business_name=f"Biz {i}", niche=niche, city="Austin, TX", phone="(512) 555-0100",
                       address="1 Main St", rating=4.6, review_count=50, source="t", source_id=str(i), status="new")


def test_every_theme_renders_cleanly_with_and_without_form(env):
    db, cfg = env
    for i, theme in enumerate(THEMES):
        lid = add(db, i, "bakery")
        b = BuilderBot(db, replace(cfg, site_theme=theme))
        for preview in (True, False):
            out = b.build(dict(db.get_lead(lid)), preview=preview)
            html = (out / "index.html").read_text()
            assert f"theme-{theme}" in html and ("Free preview" in html) == preview
            assert ("/quote/" in html) == (not preview)
            db.update_lead(lid, site_slug=out.name)
            assert check_site(db.get_lead(lid), cfg) == [], theme


def test_theme_is_sticky_across_rebuilds(env):
    db, cfg = env
    lid = add(db, 1, "roofer")
    b = BuilderBot(db, cfg)
    b.build(dict(db.get_lead(lid)))
    assert db.get_lead(lid)["theme"] == "bold"
    assert json.loads((cfg.build_dir / f"{lid}-copy.json").read_text())["theme"] == "bold"
    db.update_lead(lid, niche="hair salon")                 # niche edited later...
    out = BuilderBot(db, replace(cfg, site_theme="fresh")).build(dict(db.get_lead(lid)), preview=False)
    assert "theme-bold" in (out / "index.html").read_text()  # ...design doesn't jump around


def test_no_fake_five_stars(env):
    db, cfg = env
    lid = add(db, 1, "hair salon")
    db.update_lead(lid, rating=4.0)
    html = (BuilderBot(db, cfg).build(dict(db.get_lead(lid))) / "index.html").read_text()
    assert "★★★★★" not in html and "4.0★" in html


def test_performance_by_design(env):
    db, cfg = env
    for i, niche in enumerate(("roofer", "hair salon", "landscaper")):
        BuilderBot(db, cfg).build(dict(db.get_lead(add(db, i, niche))))
    themes = {r["segment"] for r in Analytics(db, cfg).snapshot().by_theme}
    assert themes == {"bold", "elegant", "fresh"}
