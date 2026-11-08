"""Obsidian sync: mirrors the agency database into an Obsidian vault.

The vault is plain Markdown, so it works with any Obsidian setup (Sync, iCloud,
git). Point OBSIDIAN_VAULT at your vault folder, or open ./vault in Obsidian.

Written on every pipeline run:
  Leads/<Business>.md   one CRM note per lead (YAML properties + activity timeline)
  Dashboard.md          MRR, pipeline, hot replies, sites down
  Daily/<YYYY-MM-DD>.md what every bot did today

Your edits are safe: the bots only rewrite the properties block and the region
between the AGENCY markers. Anything you write under "## My notes" is kept.

Two-way: set `do_not_contact: true` in a lead's properties and the next run
adds that email to the suppression list.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import DB

START, END = "<!-- AGENCY:START (managed by bots, edits here are overwritten) -->", "<!-- AGENCY:END -->"
NOTES_HEADER = "## My notes"
STATUS_EMOJI = {
    "new": "🆕", "built": "🛠️", "deployed": "🌐", "contacted": "✉️", "replied": "🔥",
    "paid": "💳", "client": "✅", "lost": "🪦", "unsubscribed": "🚫",
}


def note_name(row) -> str:
    safe = re.sub(r'[\\/:*?"<>|#^\[\]]', "", row["business_name"]).strip() or "Lead"
    return f"{safe} ({row['id']})"


def _yaml_value(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def parse_frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    out: dict[str, str] = {}
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep:
                out[k.strip()] = v.strip().strip('"')
    return out


def _money(cents: int) -> str:
    return f"${cents / 100:,.0f}"


class ObsidianSync:
    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        self.vault = Path(cfg.obsidian_vault)

    # ---------- two-way: read user intent from notes ----------
    def pull(self) -> int:
        """Apply do_not_contact flags set by hand in Obsidian."""
        n = 0
        for path in (self.vault / "Leads").glob("*.md"):
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))
            if fm.get("do_not_contact", "").lower() == "true" and fm.get("email"):
                if not self.db.is_suppressed(fm["email"]):
                    self.db.suppress(fm["email"], "obsidian do_not_contact")
                    if fm.get("lead_id", "").isdigit():
                        self.db.update_lead(int(fm["lead_id"]), status="unsubscribed")
                    n += 1
        return n

    # ---------- one-way: write notes ----------
    def _lead_note(self, row, existing: str) -> str:
        fm_old = parse_frontmatter(existing) if existing else {}
        props = {
            "lead_id": row["id"],
            "business": row["business_name"],
            "status": row["status"],
            "niche": row["niche"],
            "city": row["city"],
            "phone": row["phone"],
            "email": row["email"],
            "rating": row["rating"],
            "reviews": row["review_count"],
            "preview_url": row["preview_url"],
            "payment_url": row["payment_url"],
            "followups_sent": row["followups_sent"],
            "last_contacted": (row["last_contacted"] or "")[:10] or None,
            "source": row["source"],
            "do_not_contact": fm_old.get("do_not_contact", "false").lower() == "true"
                              or row["status"] == "unsubscribed",
            "tags": f"lead/{row['status']}",
            "updated": row["updated_at"][:10],
        }
        front = "---\n" + "\n".join(f"{k}: {_yaml_value(v)}" for k, v in props.items()) + "\n---\n"

        events = self.db.conn.execute(
            "SELECT * FROM events WHERE lead_id = ? ORDER BY id", (row["id"],)
        ).fetchall()
        timeline = "\n".join(
            f"- `{e['created_at'][:16].replace('T', ' ')}` **{e['bot']}** {e['kind']}"
            + (f": {e['detail'][:160]}" if e["detail"] else "")
            for e in events
        ) or "- (no activity yet)"
        action = {
            "replied": "> [!important] Replied: answer them personally today.",
            "paid": "> [!success] Paid: site goes live on the next run.",
            "client": "> [!success] Client: on the care plan.",
            "unsubscribed": "> [!warning] Opted out: never contact again.",
        }.get(row["status"], "")
        links = " · ".join(
            f"[{label}]({url})" for label, url in
            (("Preview site", row["preview_url"]), ("Payment link", row["payment_url"])) if url
        )
        managed = (
            f"{START}\n# {STATUS_EMOJI.get(row['status'], '')} {row['business_name']}\n\n"
            + (f"{action}\n\n" if action else "")
            + (f"{links}\n\n" if links else "")
            + f"**{row['niche'] or 'business'}** in {row['city'] or '?'}"
            + (f" · {row['rating']}★ ({row['review_count']} reviews)" if row["rating"] else "")
            + f"\n\n## Activity\n{timeline}\n\nBack to [[Dashboard]]\n{END}\n"
        )

        user_part = f"\n{NOTES_HEADER}\n\n"
        if existing and END in existing:
            tail = existing.split(END, 1)[1].lstrip("\n")
            if tail.strip():
                user_part = "\n" + tail
        return front + "\n" + managed + user_part

    def write_leads(self) -> int:
        folder = self.vault / "Leads"
        folder.mkdir(parents=True, exist_ok=True)
        n = 0
        for row in self.db.conn.execute("SELECT * FROM leads ORDER BY id"):
            path = folder / f"{note_name(row)}.md"
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            new = self._lead_note(row, existing)
            if new != existing:
                path.write_text(new, encoding="utf-8")
                n += 1
        return n

    def write_dashboard(self) -> None:
        m = self.db.metrics(self.cfg.monthly_fee_cents)
        rows = self.db.conn.execute("SELECT * FROM leads ORDER BY updated_at DESC").fetchall()
        hot = [r for r in rows if r["status"] == "replied"]
        down = self.db.conn.execute(
            "SELECT u.*, l.business_name, l.id lid FROM uptime u JOIN leads l ON l.id = u.lead_id "
            "WHERE u.id IN (SELECT MAX(id) FROM uptime GROUP BY lead_id) AND u.ok = 0"
        ).fetchall()
        order = ["new", "built", "deployed", "contacted", "replied", "paid", "client", "lost", "unsubscribed"]
        pipeline = "\n".join(
            f"| {STATUS_EMOJI[s]} {s} | {m['pipeline'].get(s, 0)} |" for s in order
        )
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"""---
tags: agency/dashboard
updated: "{stamp}"
---
# 📊 {self.cfg.company_name}: Dashboard
*Auto-updated by the bots · last run {stamp} · {"🧪 DRY RUN" if self.cfg.dry_run else "🟢 LIVE"}*

| MRR | Clients | Revenue collected | Close rate | Emails today |
|---|---|---|---|---|
| **{_money(m['mrr_cents'])}** | {m['clients']} | {_money(m['revenue_collected_cents'])} | {m['close_rate']:.1%} | {m['emails_today']}/{self.cfg.daily_email_cap} |

## 🔥 Replies waiting for you
{chr(10).join(f"- [[{note_name(r)}]] ({r['email']})" for r in hot) or "- None right now"}

## 🚨 Client sites down
{chr(10).join(f"- [[{note_name(r)}]]: {r['url']} ({r['status']})" for r in down) or "- All client sites up"}

## Pipeline
| Stage | Leads |
|---|---|
{pipeline}

## Recently updated
{chr(10).join(f"- {STATUS_EMOJI.get(r['status'], '')} [[{note_name(r)}]] · {r['status']}" for r in rows[:15]) or "- No leads yet"}

## Live views (needs the Dataview community plugin)
```dataview
TABLE status, niche, city, rating, followups_sent AS "follow-ups", last_contacted
FROM "Leads"
SORT updated DESC
```

See also: [[Home]] · [[Business Plan]] · [[Playbooks/Closing a Reply]]
"""
        (self.vault / "Dashboard.md").write_text(text, encoding="utf-8")

    def write_daily(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        events = self.db.conn.execute(
            "SELECT e.*, l.business_name, l.id lid FROM events e LEFT JOIN leads l ON l.id = e.lead_id "
            "WHERE substr(e.created_at,1,10) = ? ORDER BY e.id", (today,)
        ).fetchall()
        folder = self.vault / "Daily"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [
            f"- `{e['created_at'][11:16]}` **{e['bot']}** {e['kind']}"
            + (f" · [[{note_name({'business_name': e['business_name'], 'id': e['lid']})}]]"
               if e["business_name"] else "")
            + (f": {e['detail'][:120]}" if e["detail"] and not e["business_name"] else "")
            for e in events
        ]
        (folder / f"{today}.md").write_text(
            f"---\ntags: agency/daily\n---\n# Bot activity {today}\n\n"
            + ("\n".join(lines) or "- Nothing yet") + "\n\n[[Dashboard]]\n",
            encoding="utf-8",
        )

    def push(self) -> int:
        self.vault.mkdir(parents=True, exist_ok=True)
        written = self.write_leads()
        self.write_dashboard()
        self.write_daily()
        return written

    def run(self) -> dict:
        pulled = self.pull()
        return {"do_not_contact_applied": pulled, "notes_written": self.push()}
