"""Your own company website: `python -m agency company-site [--deploy]`.

Generated from your settings (name, prices, add-ons, target niches and city,
address, email) into data/company-site/:
  index.html             - pitch, how it works, example designs, pricing, FAQ,
                           and a "get my free preview" form
  examples/<design>/     - one sample site per design, for made-up businesses,
                           clearly labelled as examples
The form posts to {PUBLIC_URL}/get-started, which creates a consented
'inbound' lead (it jumps the outreach queue) and emails you. Without
PUBLIC_URL the form is replaced by a mailto link.

No invented testimonials, client counts or ratings: only true statements.
"""
from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .bots.builder_bot import PALETTES, TEMPLATES, BuilderBot
from .bots.deploy_bot import API as NETLIFY_API
from .bots.deploy_bot import zip_dir
from .bots.outreach_bot import money
from .bots.upsell_bot import CATALOG
from .config import Config
from .copywriter import template_copy
from .db import DB

EXAMPLES = [
    # (design, fictional business, niche, title, blurb, swatch)
    ("bold", "Example Roofing Co.", "roofer", "Bold", "For roofers, plumbers, electricians and other trades.",
     "linear-gradient(135deg,#15191e 60%,#c2410c 60%)"),
    ("elegant", "Example Hair Studio", "hair salon", "Elegant", "For salons, barbers, spas and beauty.",
     "linear-gradient(135deg,#fbf8f4 55%,#8a5a44 55%)"),
    ("fresh", "Example Lawn Care", "landscaper", "Fresh", "For lawn care, landscaping, cleaning and home services.",
     "linear-gradient(180deg,#15803d 65%,#f1f7f2 65%)"),
    ("classic", "Example Bakery", "bakery", "Classic", "Clean and simple, for everyone else.",
     "linear-gradient(135deg,#f4f6f8 60%,#1f6feb 60%)"),
]
EXAMPLE_BANNER = ('<div style="background:#14231a;color:#fff;text-align:center;padding:10px 16px;'
                  'font:14px/1.4 system-ui,sans-serif">Example design by {company}: not a real business. '
                  '<a href="../../index.html#examples" style="color:#9be2b0;font-weight:700">← Back</a></div>')


def niches_from(cfg: Config) -> list[str]:
    seen = []
    for q in cfg.lead_queries:
        n = q.split("|")[0].strip()
        if n and n not in seen:
            seen.append(n)
    return seen or ["local businesses"]


def city_from(cfg: Config) -> str:
    for q in cfg.lead_queries:
        if "|" in q:
            return q.split("|", 1)[1].strip()
    return "your area"


def plural(n: str) -> str:
    n = n.strip()
    if n.lower().endswith(("care", "cleaning", "repair", "service", "services", "removal", "washing", "control")):
        return n + " companies"          # "lawn care" -> "lawn care companies"
    if n.endswith(("s", "sh", "ch")):
        return n + "es"
    return n[:-1] + "ies" if n.endswith("y") and n[-2:-1] not in "aeiou" else n + "s"


def build(cfg: Config, out: Path | None = None) -> Path:
    out = out or Path(cfg.db_path).parent / "company-site"
    if out.exists():
        shutil.rmtree(out)
    (out / "examples").mkdir(parents=True)

    db = DB(":memory:")  # examples are rendered from fake leads; nothing touches your real database
    builder = BuilderBot(db, cfg)
    examples = []
    for design, name, niche, title, blurb, swatch in EXAMPLES:
        lead = {"id": 0, "business_name": name, "niche": niche, "city": city_from(cfg),
                "phone": "(512) 555-0100", "address": None, "rating": None, "review_count": None}
        copy = {**template_copy(lead), "theme": design}
        html = builder.render(lead, copy, preview=False)
        html = html.replace(f'<body class="theme-{design}">',
                            f'<body class="theme-{design}">' + EXAMPLE_BANNER.format(company=cfg.company_name), 1)
        html = html.replace("<head>", '<head>\n<meta name="robots" content="noindex">', 1)
        (out / "examples" / design).mkdir()
        (out / "examples" / design / "index.html").write_text(html, encoding="utf-8")
        examples.append({"href": f"examples/{design}/index.html", "title": title, "blurb": blurb,
                         "preview_css": swatch, "color": PALETTES[design][0]})

    niches = niches_from(cfg)
    pretty = [plural(n) for n in niches]
    niches_text = pretty[0] if len(pretty) == 1 else ", ".join(pretty[:-1]) + " and " + pretty[-1]
    name = cfg.company_name
    split = next((i for i in range(1, len(name)) if name[i].isupper()), len(name))  # "SiteSprout" -> Site|Sprout
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    page = env.get_template("company/index.html.j2").render(
        cfg=cfg, city=city_from(cfg), niches_text=niches_text, niche_list=niches,
        setup=money(cfg.setup_fee_cents), monthly=money(cfg.monthly_fee_cents),
        addons=[{"name": a.name, "price": money(a.price_cents), "pitch": a.pitch} for a in CATALOG],
        examples=examples, year=date.today().year,
        form_action=f"{cfg.public_url}/get-started" if cfg.public_url else None,
        name_head=name[:split], name_tail=name[split:],
    )
    (out / "index.html").write_text(page, encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nAllow: /\nDisallow: /examples/\n")
    return out


def deploy(cfg: Config, folder: Path, db: DB, http: requests.Session | None = None) -> str:
    """Publish to Netlify (needs NETLIFY_TOKEN and DRY_RUN=0). Reuses the same site every time."""
    if cfg.dry_run or not cfg.netlify_token:
        raise RuntimeError("deploying needs NETLIFY_TOKEN and DRY_RUN=0 (or drag the folder onto app.netlify.com/drop)")
    http = http or requests.Session()
    headers = {"Authorization": f"Bearer {cfg.netlify_token}"}
    site_id = db.get_setting("company_site_netlify_id")
    if not site_id:
        r = http.post(f"{NETLIFY_API}/sites", headers=headers, timeout=30,
                      json={"name": f"{cfg.company_name.lower().replace(' ', '-')}-home"[:63]})
        r.raise_for_status()
        site_id = r.json()["id"]
        db.set_setting("company_site_netlify_id", site_id)
    r = http.post(f"{NETLIFY_API}/sites/{site_id}/deploys", headers={**headers, "Content-Type": "application/zip"},
                  data=zip_dir(folder), timeout=120)
    r.raise_for_status()
    return r.json().get("ssl_url") or r.json().get("url") or ""
