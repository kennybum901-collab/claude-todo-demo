"""Client self-service site edits, with human approval.

  1. A client replies to any email ("we're open Saturdays now, new phone 555-0199").
  2. EditBot drafts the exact change to their site content (Claude with a strict
     JSON schema; without an API key the request is routed to you as 'needs_human').
  3. You review the before/after in Obsidian (`Edits/` folder) and set
     `approve: true` (or `reject: true`). The CLI works too: `edit-approve <id>`.
  4. The bot rebuilds and redeploys the site and emails the client that it's live.

Nothing is ever published without approval. Only whitelisted text fields can
change (headline, tagline, about, services, call to action, hours, phone,
address); photos, logos and design changes are routed to you.
"""
from __future__ import annotations

import json
import logging
from email.message import EmailMessage
from typing import Any

from .config import Config
from .db import DB, now
from .mailer import Mailer

log = logging.getLogger(__name__)

TEXT_FIELDS = ("headline", "tagline", "about", "cta", "hours")
LEAD_FIELDS = ("phone", "address")
MAX_LEN = {"headline": 120, "tagline": 200, "about": 1500, "cta": 80, "hours": 300,
           "phone": 40, "address": 200}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_change_request": {"type": "boolean"},
        "needs_human": {"type": "boolean"},
        "summary": {"type": "string"},
        "headline": {"type": "string"},
        "tagline": {"type": "string"},
        "about": {"type": "string"},
        "cta": {"type": "string"},
        "hours": {"type": "string"},
        "phone": {"type": "string"},
        "address": {"type": "string"},
        "services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "description": {"type": "string"}},
                "required": ["name", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["is_change_request", "needs_human", "summary", "headline", "tagline", "about",
                 "cta", "hours", "phone", "address", "services"],
    "additionalProperties": False,
}

PROMPT = """You maintain a small business's one-page website. The owner emailed us.
Decide whether the email asks to change the website text, and if so return the
COMPLETE updated content (copy every field you are not changing exactly as-is).

Rules:
- Change only what the owner asked for. Never invent facts, prices, awards or claims.
- Editable: headline, tagline, about, cta (button text), hours, phone, address, services
  (each with name + description). Use "" for hours if the site has none and none was given.
- Set needs_human=true if the request involves anything else (photos, logo, colors,
  layout, new pages, domain, billing) or is ambiguous; still apply the parts you can.
- Set is_change_request=false for thanks, questions or chit-chat with no change.
- summary: one short sentence describing the change, for the reviewer.

Current website content (JSON):
{current}

Owner's email:
<<<
{message}
>>>"""


def current_content(lead, cfg: Config) -> dict[str, Any]:
    path = cfg.build_dir / f"{lead['id']}-copy.json"
    copy = json.loads(path.read_text()) if path.exists() else {}
    return {
        **{f: copy.get(f, "") for f in TEXT_FIELDS},
        "services": copy.get("services", []),
        "phone": lead["phone"] or "",
        "address": lead["address"] or "",
    }


def sanitize(proposed: dict[str, Any], before: dict[str, Any]) -> dict[str, Any]:
    """Keep only whitelisted fields with sane lengths; fall back to current values."""
    out: dict[str, Any] = {}
    for f in TEXT_FIELDS + LEAD_FIELDS:
        v = proposed.get(f, before.get(f, ""))
        out[f] = str(v).strip()[:MAX_LEN[f]] if v is not None else ""
    services = proposed.get("services")
    if not isinstance(services, list) or not services:
        services = before.get("services", [])
    out["services"] = [
        {"name": str(s.get("name", "")).strip()[:80], "description": str(s.get("description", "")).strip()[:300]}
        for s in services[:8] if isinstance(s, dict) and str(s.get("name", "")).strip()
    ]
    return out


def diff(before: dict[str, Any], after: dict[str, Any]) -> list[tuple[str, str, str]]:
    def show(v):
        if isinstance(v, list):
            return "; ".join(f"{s['name']}: {s['description']}" for s in v)
        return v or "(empty)"
    return [(k, show(before.get(k)), show(after.get(k)))
            for k in TEXT_FIELDS + LEAD_FIELDS + ("services",) if before.get(k) != after.get(k)]


def draft_with_ai(before: dict[str, Any], message: str, cfg: Config) -> dict[str, Any]:
    import anthropic

    response = anthropic.Anthropic().beta.messages.create(
        model=cfg.anthropic_model,
        max_tokens=8000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": EDIT_SCHEMA}},
        messages=[{"role": "user", "content": PROMPT.format(
            current=json.dumps(before, indent=2), message=message[:6000])}],
    )
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"edit draft stopped: {response.stop_reason}")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


class EditBot:
    name = "edits"

    def __init__(self, db: DB, cfg: Config, mailer: Mailer | None = None, drafter=None):
        self.db, self.cfg = db, cfg
        self.mailer = mailer or Mailer(db, cfg)
        self.drafter = drafter or (draft_with_ai if cfg.use_ai_copy else None)

    # ---------- intake ----------
    def submit(self, lead_id: int, message: str) -> int:
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO edits (lead_id, message, status, created_at) VALUES (?,?,?,?)",
                (lead_id, message[:6000], "new", now()),
            )
        self.db.log(self.name, "edit_requested", message[:120], lead_id)
        return cur.lastrowid

    # ---------- drafting ----------
    def draft(self, edit) -> str:
        lead = self.db.get_lead(edit["lead_id"])
        before = current_content(lead, self.cfg)
        status, summary, proposed = "needs_human", "Review this request and edit the site by hand.", None
        if self.drafter is not None:
            try:
                result = self.drafter(before, edit["message"], self.cfg)
                if not result.get("is_change_request"):
                    status, summary = "not_request", result.get("summary") or "No change requested."
                else:
                    proposed = sanitize(result, before)
                    summary = (result.get("summary") or "Site update").strip()[:300]
                    if not diff(before, proposed):
                        status = "needs_human"
                        summary += " (no text change could be drafted)"
                        proposed = None
                    else:
                        status = "needs_human" if result.get("needs_human") else "drafted"
            except Exception as exc:
                log.warning("edit #%s draft failed: %s", edit["id"], exc)
                summary = f"Automatic draft failed ({exc}); review by hand."
        with self.db.tx() as c:
            c.execute(
                "UPDATE edits SET status=?, summary=?, before_json=?, proposed_json=? WHERE id=?",
                (status, summary, json.dumps(before), json.dumps(proposed) if proposed else None, edit["id"]),
            )
        self.db.log(self.name, f"edit_{status}", summary[:160], edit["lead_id"])
        return status

    # ---------- decisions ----------
    def decide(self, edit_id: int, approve: bool) -> bool:
        row = self.db.conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
        if row is None or row["status"] not in ("drafted", "needs_human"):
            return False
        if approve and not row["proposed_json"]:
            return False  # nothing to apply automatically; edit the site by hand
        with self.db.tx() as c:
            c.execute("UPDATE edits SET status=?, decided_at=? WHERE id=?",
                      ("approved" if approve else "rejected", now(), edit_id))
        self.db.log(self.name, "edit_approved" if approve else "edit_rejected", f"#{edit_id}", row["lead_id"])
        return True

    # ---------- applying ----------
    def apply(self, edit, builder, deployer) -> None:
        lead = self.db.get_lead(edit["lead_id"])
        proposed = json.loads(edit["proposed_json"])
        path = self.cfg.build_dir / f"{lead['id']}-copy.json"
        copy = json.loads(path.read_text()) if path.exists() else {}
        for f in TEXT_FIELDS:
            copy[f] = proposed[f]
        copy["services"] = proposed["services"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(copy, indent=2))
        self.db.update_lead(lead["id"], phone=proposed["phone"] or None, address=proposed["address"] or None)
        builder.build(dict(self.db.get_lead(lead["id"])), preview=False)
        if lead["status"] == "client" and lead["site_slug"]:
            url, site_id = deployer.deploy(dict(self.db.get_lead(lead["id"])))  # just this site
            fields = {"preview_url": url}
            if site_id:
                fields["netlify_site_id"] = site_id
            self.db.update_lead(lead["id"], **fields)
            lead = self.db.get_lead(lead["id"])
        with self.db.tx() as c:
            c.execute("UPDATE edits SET status='applied', applied_at=? WHERE id=?", (now(), edit["id"]))
        self.db.log(self.name, "edit_applied", edit["summary"] or "", lead["id"])
        self.confirm(lead, edit)

    def confirm(self, lead, edit) -> None:
        if not lead["email"]:
            return
        msg = EmailMessage()
        msg["Subject"] = f"Your website update is live: {lead['business_name']}"
        msg["To"] = lead["email"]
        msg.set_content(
            f"Hi {lead['business_name']} team,\n\nDone! Your website has been updated:\n"
            f"  - {edit['summary']}\n\nTake a look: {lead['preview_url']}\n\n"
            f"Anything else? Just reply to this email.\n\n{self.cfg.owner_name}\n{self.cfg.company_name}\n"
        )
        sender = self.mailer.notify_sender()
        if sender is not None:
            try:
                self.mailer.send(sender, msg, bot=self.name, lead_id=lead["id"],
                                 detail="edit live confirmation", kind="notify")
            finally:
                self.mailer.close()

    def run(self, builder, deployer) -> dict:
        drafted = applied = 0
        for edit in self.db.conn.execute("SELECT * FROM edits WHERE status = 'new' ORDER BY id").fetchall():
            self.draft(edit)
            drafted += 1
        for edit in self.db.conn.execute("SELECT * FROM edits WHERE status = 'approved' ORDER BY id").fetchall():
            try:
                self.apply(edit, builder, deployer)
                applied += 1
            except Exception as exc:
                self.db.log(self.name, "error", f"apply #{edit['id']}: {exc}", edit["lead_id"])
        return {"drafted": drafted, "applied": applied}
