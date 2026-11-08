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

log = logging.getLogger(__name__)
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
PALETTE = ["#1f6feb", "#0f766e", "#b45309", "#7c3aed", "#be123c", "#15803d", "#0e7490"]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "site"


class BuilderBot:
    name = "builder"

    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        self.env = Environment(
            loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"])
        )

    def render(self, lead: dict, copy: dict, preview: bool = True) -> str:
        schema = {
            "@context": "https://schema.org",
            "@type": "LocalBusiness",
            "name": lead["business_name"],
            "telephone": lead.get("phone"),
            "address": lead.get("address"),
        }
        color = PALETTE[int(hashlib.md5(lead["business_name"].encode()).hexdigest(), 16) % len(PALETTE)]
        return self.env.get_template("site.html.j2").render(
            lead=lead, copy=copy, cfg=self.cfg, color=color, preview=preview,
            payment_url=lead.get("payment_url"), year=date.today().year,
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
        copy_path.write_text(json.dumps(copy, indent=2))
        (out / "index.html").write_text(self.render(lead, copy, preview), encoding="utf-8")
        (out / "robots.txt").write_text(
            "User-agent: *\nDisallow: /\n" if preview else "User-agent: *\nAllow: /\n"
        )
        return out

    def run(self, limit: int = 50) -> int:
        built = 0
        for row in self.db.leads_by_status("new", limit=limit):
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
