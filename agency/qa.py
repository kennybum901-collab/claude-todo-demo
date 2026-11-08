"""Automatic quality checks for generated sites and sent emails.

Used by the daily digest (and `python -m agency qa`). Each check returns a list
of human-readable problems; an empty list means it passed.
"""
from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import requests

from .config import Config

# template syntax and programming nulls are case-sensitive ("second to none" is fine English)
LEFTOVERS = re.compile(r"\{\{|\}\}|\{%|%\}|\bNone\b|\bnull\b|\bundefined\b|\bTODO\b|(?i:lorem ipsum)|(?i:\[insert)")
SPAM_PHRASES = ("act now", "100% free", "guaranteed", "risk-free", "no obligation whatsoever", "$$$",
                "click here", "winner", "urgent", "limited time", "cash bonus", "earn money")


def check_site(lead, cfg: Config, *, http=None, preview: bool | None = None) -> list[str]:
    problems: list[str] = []
    if not lead["site_slug"]:
        return ["no site has been built"]
    path = Path(cfg.build_dir) / lead["site_slug"] / "index.html"
    if not path.exists():
        return [f"site file missing: {path.name}"]
    html = path.read_text(encoding="utf-8", errors="ignore")
    visible = unescape(re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ", html, flags=re.S))
    if lead["business_name"] and lead["business_name"].split()[0].lower() not in visible.lower():
        problems.append("business name not on the page")
    m = LEFTOVERS.search(visible)
    if m:
        problems.append(f"leftover placeholder text: “{m.group(0)}”")
    if lead["phone"] and "tel:" not in html:
        problems.append("phone number has no tap-to-call link")
    if "<title>" not in html or "<title></title>" in html:
        problems.append("missing page title")
    if 'name="viewport"' not in html:
        problems.append("not set up for phones (no viewport tag)")
    if len(html) > 300_000:
        problems.append(f"page is large ({len(html) // 1000} KB)")
    url = lead["preview_url"] or ""
    if url.startswith("https://") and http is not None:
        try:
            r = http.get(url, timeout=15)
            if r.status_code != 200:
                problems.append(f"live page returns HTTP {r.status_code}")
        except requests.RequestException as exc:
            problems.append(f"live page unreachable ({type(exc).__name__})")
    return problems


def check_email(subject: str, body: str, cfg: Config, *, bot: str = "outreach") -> list[str]:
    problems: list[str] = []
    subject, body = subject or "", body or ""
    if not subject.strip():
        problems.append("empty subject line")
    elif len(subject) > 78:
        problems.append(f"subject is long ({len(subject)} chars)")
    letters = [c for c in subject if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6 and len(letters) > 8:
        problems.append("subject is mostly CAPITALS")
    if "!!" in subject + body:
        problems.append("multiple exclamation marks")
    m = LEFTOVERS.search(subject + "\n" + body)
    if m:
        problems.append(f"leftover placeholder text: “{m.group(0)}”")
    marketing = bot in ("outreach", "upsell", "winback", "report")
    if marketing:
        if cfg.company_address and cfg.company_address not in body:
            problems.append("missing postal address (required by CAN-SPAM)")
        if "stop" not in body.lower() and "unsubscribe" not in body.lower():
            problems.append("no way to opt out")
    lowered = (subject + " " + body).lower()
    hits = [p for p in SPAM_PHRASES if p in lowered]
    if hits:
        problems.append("spam-trigger wording: " + ", ".join(hits))
    if not cfg.dry_run and "file://" in body:
        problems.append("contains a local file:// link")
    return problems
