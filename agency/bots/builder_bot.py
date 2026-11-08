"""Builder bot: turns each new lead into a ready-to-ship one-page website."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import Config
from ..copywriter import generate_copy
from ..db import DB
from ..photos import Photos
from ..quotes import form_action

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
# brand colors per design; every one keeps white button text at >= 4.5:1 contrast
PALETTES = {
    "classic": ["#1f6feb", "#0f766e", "#b45309", "#7c3aed", "#be123c", "#15803d", "#0e7490"],
    "bold": ["#c2410c", "#b91c1c", "#1d4ed8", "#a16207", "#047857"],
    "elegant": ["#8a5a44", "#6b4f7a", "#3f6f6a", "#9c4f63", "#5b5f97"],
    "fresh": ["#15803d", "#0f766e", "#0e7490", "#4d7c0f", "#166534"],
}
PALETTE = PALETTES["classic"]
THEMES = tuple(PALETTES)
# niche keywords -> design (first match wins); anything else gets "classic"
THEME_BY_NICHE = (
    ("bold", ("plumb", "roof", "electric", "hvac", "heating", "air condition", "mechanic", "auto repair",
              "contractor", "construction", "concrete", "garage door", "locksmith", "towing", "pest", "welding")),
    ("elegant", ("salon", "hair", "barber", "nail", "spa", "massage", "beauty", "lash", "brow", "esthetic",
                 "tattoo", "florist", "bridal", "boutique", "dentist", "photograph")),
    ("fresh", ("landscap", "lawn", "garden", "tree", "clean", "maid", "pressure wash", "pool", "window",
               "moving", "pet", "dog", "groom", "carpet", "junk", "solar")),
)


def pick_theme(niche: str | None, forced: str = "") -> str:
    if forced in THEMES:
        return forced
    n = (niche or "").lower()
    for theme, words in THEME_BY_NICHE:
        if any(w in n for w in words):
            return theme
    return "classic"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "site"


class BuilderBot:
    name = "builder"

    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        self.env = Environment(
            loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"])
        )

    def render(self, lead: dict, copy: dict, preview: bool = True, photos: list | None = None) -> str:
        schema = {
            "@context": "https://schema.org",
            "@type": "LocalBusiness",
            "name": lead["business_name"],
            "telephone": lead.get("phone"),
            "address": lead.get("address"),
        }
        theme = copy.get("theme") if copy.get("theme") in THEMES else pick_theme(lead.get("niche"), self.cfg.site_theme)
        palette = PALETTES[theme]
        color = palette[int(hashlib.md5(lead["business_name"].encode()).hexdigest(), 16) % len(palette)]
        return self.env.get_template(f"site/{theme}.html.j2").render(
            lead=lead, copy=copy, cfg=self.cfg, color=color, preview=preview, theme=theme,
            photos=photos or [], photo_credits=sorted({p["credit"] for p in (photos or []) if p.get("credit")}),
            payment_url=lead.get("payment_url"), year=date.today().year,
            # the quote form only goes on paying clients' sites, never on previews
            quote_action=None if preview else form_action(lead["id"], self.cfg),
            schema_json=json.dumps({k: v for k, v in schema.items() if v}).replace("</", "<\\/"),
        )

    def build(self, lead: dict, preview: bool = True) -> Path:
        copy_path = self.cfg.build_dir / f"{lead['id']}-copy.json"
        if copy_path.exists():  # reuse copy so rebuilds (e.g. after payment) are free & stable
            copy = json.loads(copy_path.read_text())
        else:
            copy = generate_copy(lead, self.cfg)
        slug = lead.get("site_slug") or f"{slugify(lead['business_name'])}-{lead['id']}"
        out = self.cfg.build_dir / slug
        out.mkdir(parents=True, exist_ok=True)
        # the design is chosen once and saved with the copy, so rebuilds never change it
        copy.setdefault("theme", pick_theme(lead.get("niche"), self.cfg.site_theme))
        copy_path.write_text(json.dumps(copy, indent=2))
        if lead.get("theme") != copy["theme"]:
            self.db.update_lead(lead["id"], theme=copy["theme"])
        photos = Photos(self.db, self.cfg)
        if self.cfg.pexels_api_key:
            photos.ensure_stock(lead)
        site_photos = photos.copy_into(lead["id"], out)  # only approved photos are ever published
        (out / "index.html").write_text(self.render(lead, copy, preview, site_photos), encoding="utf-8")
        (out / "robots.txt").write_text(
            "User-agent: *\nDisallow: /\n" if preview else "User-agent: *\nAllow: /\n"
        )
        return out

    def run(self, limit: int = 50) -> int:
        built = 0
        for row in self.db.leads_by_status("new", limit=limit, by_score=True):  # best leads first
            lead = dict(row)
            try:
                out = self.build(lead)
            except Exception as exc:
                self.db.log(self.name, "error", str(exc), lead["id"])
                continue
            self.db.update_lead(lead["id"], status="built", site_slug=out.name)
            self.db.log(self.name, "built", str(out), lead["id"])
            built += 1
        log.info("builder bot: built %d sites", built)
        return built
