"""Generates website copy and outreach openers.

Uses Claude when ANTHROPIC_API_KEY is set (USE_AI_COPY=1); otherwise falls back to
solid niche-aware templates so the pipeline always works offline.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .config import Config

log = logging.getLogger(__name__)

COPY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "tagline": {"type": "string"},
        "about": {"type": "string"},
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["name", "description"],
                "additionalProperties": False,
            },
        },
        "cta": {"type": "string"},
        "email_opener": {"type": "string"},
    },
    "required": ["headline", "tagline", "about", "services", "cta", "email_opener"],
    "additionalProperties": False,
}

NICHE_SERVICES: dict[str, list[tuple[str, str]]] = {
    "plumber": [
        ("Emergency Repairs", "Burst pipes and leaks fixed fast, day or night."),
        ("Drain Cleaning", "Clogged sinks, showers and main lines cleared."),
        ("Water Heaters", "Repair and replacement of tank and tankless units."),
    ],
    "roofer": [
        ("Roof Repair", "Leaks, missing shingles and storm damage handled quickly."),
        ("Roof Replacement", "Durable new roofs with a workmanship warranty."),
        ("Free Inspections", "Honest assessments with photos, no pressure."),
    ],
    "hair salon": [
        ("Cuts & Styling", "Precision cuts tailored to you."),
        ("Color", "Balayage, highlights and full color by experienced stylists."),
        ("Treatments", "Deep conditioning and repair for healthy hair."),
    ],
}
DEFAULT_SERVICES = [
    ("Quality Service", "Professional work done right the first time."),
    ("Fair Pricing", "Clear quotes with no surprises."),
    ("Local & Trusted", "Proudly serving our neighbors."),
]


def template_copy(lead: dict[str, Any]) -> dict[str, Any]:
    name = lead["business_name"]
    niche = (lead.get("niche") or "local business").lower()
    city = lead.get("city") or "your area"
    services = NICHE_SERVICES.get(niche, DEFAULT_SERVICES)
    rating = lead.get("rating")
    reviews = lead.get("review_count")
    social = (
        f"Rated {rating}★ by {reviews} customers. " if rating and reviews else ""
    )
    return {
        "headline": f"{name}",
        "tagline": f"Trusted {niche} serving {city}.",
        "about": (
            f"{social}{name} is a locally owned {niche} in {city}. "
            "We take pride in honest work, clear communication and treating every "
            "customer like a neighbor."
        ),
        "services": [{"name": n, "description": d} for n, d in services],
        "cta": "Call today for a free quote",
        "email_opener": (
            f"I came across {name} while looking for a {niche} in {city} and noticed "
            "you don't have a website yet"
            + (f" - which is surprising given your {rating}★ rating." if rating else ".")
        ),
    }


def ai_copy(lead: dict[str, Any], cfg: Config) -> dict[str, Any]:
    import anthropic  # imported lazily so the package is optional

    client = anthropic.Anthropic()
    facts = {k: lead.get(k) for k in ("business_name", "niche", "city", "address", "phone",
                                       "rating", "review_count")}
    prompt = (
        "Write homepage copy for a small local business website, plus a one-sentence "
        "friendly, specific opener for a cold email offering them the site. Use only the "
        "facts provided; do not invent awards, years in business, licenses or prices. "
        "Keep the about section under 70 words and give exactly 3 services.\n\n"
        f"Business facts (JSON):\n{json.dumps(facts)}"
    )
    response = client.beta.messages.create(
        model=cfg.anthropic_model,
        max_tokens=4000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": COPY_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"copy generation stopped: {response.stop_reason}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def generate_copy(lead: dict[str, Any], cfg: Config) -> dict[str, Any]:
    if cfg.use_ai_copy:
        try:
            return ai_copy(lead, cfg)
        except Exception as exc:  # never let copywriting stall the pipeline
            log.warning("AI copy failed for %s (%s); using template", lead["business_name"], exc)
    return template_copy(lead)


# ---------------------------------------------------------------------------
# Reply drafting: when a prospect answers, prepare a suggested response for the
# human to review in Obsidian. Drafts are NEVER sent automatically.
# ---------------------------------------------------------------------------
REPLY_PROMPT = """You help a small web-design studio answer prospects who replied to an
email offering them a free preview website. Draft a short, warm, plain-text reply
(under 120 words) from {owner} at {company}. Answer their actual question. Facts you
may use: setup is {setup} one-time, care plan is {monthly}/month (hosting, security,
small edits), cancel anytime, preview: {preview}, payment link: {payment}.
Do not invent anything else (no discounts, guarantees, timelines or features).
If they ask for changes, say you will update the preview. Return only the email body.

Their business: {business}
Their reply:
<<<
{reply}
>>>"""


def template_reply(lead: dict[str, Any], reply: str, cfg: Config) -> str:
    text = reply.lower()
    setup, monthly = f"${cfg.setup_fee_cents / 100:,.0f}", f"${cfg.monthly_fee_cents / 100:,.0f}"
    if any(w in text for w in ("price", "cost", "how much", "$", "charge")):
        middle = (f"It's {setup} one-time to put it on your own domain with any changes you "
                  f"want, then {monthly}/month for hosting, security and small edits. "
                  "Cancel anytime.")
    elif any(w in text for w in ("change", "edit", "photo", "logo", "update", "add")):
        middle = ("Happy to. Send over the changes (photos, logo, hours, wording) and I'll "
                  "update the preview so you can see it before deciding.")
    elif any(w in text for w in ("scam", "catch", "legit", "who are you")):
        middle = (f"Fair question. I'm {cfg.owner_name}, I run {cfg.company_name}. I build "
                  "the site first so owners can see exactly what they'd get. No obligation.")
    else:
        middle = ("Thanks for getting back to me! Happy to answer any questions or make "
                  "changes to the preview.")
    return (f"Hi,\n\n{middle}\n\nPreview: {lead.get('preview_url')}\n"
            f"When you're ready: {lead.get('payment_url')}\n\n{cfg.owner_name}\n{cfg.company_name}")


def draft_reply(lead: dict[str, Any], reply: str, cfg: Config) -> str:
    if cfg.use_ai_copy:
        try:
            import anthropic

            prompt = REPLY_PROMPT.format(
                owner=cfg.owner_name, company=cfg.company_name,
                setup=f"${cfg.setup_fee_cents / 100:,.0f}",
                monthly=f"${cfg.monthly_fee_cents / 100:,.0f}",
                preview=lead.get("preview_url"), payment=lead.get("payment_url"),
                business=lead.get("business_name"), reply=reply[:4000],
            )
            response = anthropic.Anthropic().beta.messages.create(
                model=cfg.anthropic_model,
                max_tokens=2000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                output_config={"effort": "low"},
                messages=[{"role": "user", "content": prompt}],
            )
            if response.stop_reason not in ("refusal", "max_tokens"):
                text = "".join(b.text for b in response.content if b.type == "text").strip()
                if text:
                    return text
        except Exception as exc:
            log.warning("AI reply draft failed (%s); using template", exc)
    return template_reply(lead, reply, cfg)
