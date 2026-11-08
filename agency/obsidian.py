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

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import DB
from .mailer import Mailer

START, END = "<!-- AGENCY:START (managed by bots, edits here are overwritten) -->", "<!-- AGENCY:END -->"
NOTES_HEADER = "## My notes"
STATUS_EMOJI = {
    "new": "🆕", "built": "🛠️", "deployed": "🌐", "contacted": "✉️", "replied": "🔥",
    "paid": "💳", "client": "✅", "churned": "💔", "lost": "🪦", "unsubscribed": "🚫", "bounced": "📭",
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
    def __init__(self, db: DB, cfg: Config, mailer: Mailer | None = None):
        self.db, self.cfg = db, cfg
        self.mailer = mailer
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

    def pull_edit_decisions(self) -> int:
        """`approve: true` / `reject: true` in an Edits/ note -> decision recorded."""
        from .edits import EditBot

        folder = self.vault / "Edits"
        if not folder.exists():
            return 0
        bot, n = EditBot(self.db, self.cfg, mailer=self.mailer), 0
        for path in folder.glob("*.md"):
            fm = parse_frontmatter(path.read_text(encoding="utf-8"))
            if not fm.get("edit_id", "").isdigit():
                continue
            approve = fm.get("approve", "").lower() == "true"
            reject = fm.get("reject", "").lower() == "true"
            if approve != reject and bot.decide(int(fm["edit_id"]), approve=approve):
                n += 1
        return n

    def write_edits(self) -> None:
        folder = self.vault / "Edits"
        rows = self.db.conn.execute(
            "SELECT e.*, l.business_name, l.id lid FROM edits e JOIN leads l ON l.id = e.lead_id "
            "WHERE e.status != 'new' ORDER BY e.id").fetchall()
        if not rows:
            return
        folder.mkdir(parents=True, exist_ok=True)
        from .edits import diff

        for e in rows:
            lead = {"business_name": e["business_name"], "id": e["lid"]}
            path = folder / f"{note_name(lead)} edit {e['id']}.md"
            if path.exists():
                # only rewrite when the status changed, so an `approve: true` you just
                # set (and any notes you add) survive until the next run reads them
                fm = parse_frontmatter(path.read_text(encoding="utf-8"))
                if fm.get("status") == e["status"]:
                    continue
            changes = ""
            if e["proposed_json"]:
                rows_md = "\n".join(
                    f"| **{f}** | {b.replace('|', '/')[:300]} | {a.replace('|', '/')[:300]} |"
                    for f, b, a in diff(json.loads(e["before_json"]), json.loads(e["proposed_json"])))
                changes = f"## Proposed change\n| Field | Now | After |\n|---|---|---|\n{rows_md}\n"
            how = {
                "drafted": "> [!todo] Review the change, then set **approve: true** (or **reject: true**) above. It goes live on the next run.",
                "needs_human": ("> [!warning] Needs you: part of this can't be done automatically. "
                                + ("Approve to apply the drafted text changes, then handle the rest by hand." if e["proposed_json"]
                                   else "Make the change by hand, then set **reject: true** to close it.")),
                "not_request": "> [!info] Not a change request (no action needed).",
                "approved": "> [!success] Approved: going live on the next run.",
                "applied": "> [!success] Live. The client was emailed.",
                "rejected": "> [!failure] Rejected.",
            }.get(e["status"], "")
            quoted = "\n".join(f"> {line}" for line in e["message"].strip().splitlines())
            path.write_text(
                f"---\nedit_id: {e['id']}\nlead_id: {e['lid']}\nstatus: \"{e['status']}\"\n"
                f"approve: false\nreject: false\ntags: agency/edit\n---\n"
                f"# ✏️ {e['business_name']}: {e['summary'] or 'site edit'}\n\n{how}\n\n"
                f"Client: [[{note_name(lead)}]] · requested {e['created_at'][:10]}\n\n"
                f"## Their message\n{quoted}\n\n{changes}",
                encoding="utf-8",
            )

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
            "score": row["score"],
            "followups_sent": row["followups_sent"],
            "subject_variant": row["variant"],
            "last_contacted": (row["last_contacted"] or "")[:10] or None,
            "source": row["source"],
            "referred_by": row["referred_by"],
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
            "churned": "> [!failure] Churned: subscription ended. Worth a win-back call?",
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
            + self._reply_section(row)
            + self._addon_section(row)
            + self._quote_section(row)
            + f"\n\n## Activity\n{timeline}\n\nBack to [[Dashboard]]\n{END}\n"
        )

        user_part = f"\n{NOTES_HEADER}\n\n"
        if existing and END in existing:
            tail = existing.split(END, 1)[1].lstrip("\n")
            if tail.strip():
                user_part = "\n" + tail
        return front + "\n" + managed + user_part

    @staticmethod
    def _quote(text: str) -> str:
        return "\n".join(f"> {line}" for line in text.strip().splitlines())

    def _reply_section(self, row) -> str:
        if not row["last_reply"]:
            return ""
        out = f"\n\n## Their latest reply\n> [!quote]\n{self._quote(row['last_reply'])}"
        if row["reply_draft"] and row["status"] == "replied":
            out += ("\n\n## Suggested answer (review, edit, send yourself)\n"
                    f"> [!tip]- Draft\n{self._quote(row['reply_draft'])}")
        return out

    def _addon_section(self, row) -> str:
        addons = self.db.addons_for(row["id"])
        if not addons:
            return ""
        icon = {"offered": "📨", "active": "✅", "canceled": "❌"}
        lines = "\n".join(
            f"- {icon.get(a['status'], '')} **{a['addon']}** ${a['price_cents'] / 100:,.0f}/mo · {a['status']}"
            + (f" · [link]({a['payment_url']})" if a["payment_url"] and a["status"] == "offered" else "")
            for a in addons
        )
        return f"\n\n## Add-ons\n{lines}"

    def _quote_section(self, row) -> str:
        quotes = self.db.conn.execute(
            "SELECT * FROM quotes WHERE lead_id = ? ORDER BY id DESC LIMIT 10", (row["id"],)
        ).fetchall()
        if not quotes:
            return ""
        total = self.db.conn.execute("SELECT COUNT(*) FROM quotes WHERE lead_id = ?", (row["id"],)).fetchone()[0]
        lines = "\n".join(
            f"- `{q['created_at'][:10]}` **{q['name']}** · {q['phone'] or q['email']} · {q['service'] or ''}"
            + ("" if q["notified"] else " · ⚠️ not forwarded, send it to the client")
            for q in quotes
        )
        return f"\n\n## Quote requests ({total} total)\n{lines}"

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
        order = ["new", "built", "deployed", "contacted", "replied", "paid", "client", "churned",
                 "lost", "unsubscribed", "bounced"]
        at_risk = self.db.conn.execute(
            "SELECT DISTINCT l.* FROM events e JOIN leads l ON l.id = e.lead_id WHERE e.kind = 'past_due' "
            "AND l.status = 'client' AND substr(e.created_at,1,10) >= date('now','-3 day')"
        ).fetchall()
        addon_rows = self.db.conn.execute(
            "SELECT addon, COUNT(*) offered, SUM(status='active') active, "
            "SUM(CASE WHEN status='active' THEN price_cents ELSE 0 END) mrr FROM addons GROUP BY addon"
        ).fetchall()
        addon_table = "\n".join(
            f"| {r['addon']} | {r['offered']} | {r['active']} | {_money(r['mrr'])} |" for r in addon_rows
        ) or "| - | 0 | 0 | $0 |"
        client_msgs = self.db.conn.execute(
            "SELECT DISTINCT l.* FROM events e JOIN leads l ON l.id = e.lead_id "
            "WHERE e.kind = 'client_reply' AND substr(e.created_at,1,10) >= date('now','-7 day')"
        ).fetchall()
        inbox_rows = "\n".join(
            f"| {i['email']} | {i['sent_today']}/{i['cap_today']} | {i['bounce_rate']:.1%} ({i['recent_sends']} sends) | "
            + ("⏸️ paused" if i["paused"] else "🌱 warming up" if i["warming"] else "🟢 ok") + " |"
            for i in (self.mailer or Mailer(self.db, self.cfg)).status()
        )
        pending_edits = self.db.conn.execute(
            "SELECT e.*, l.business_name, l.id lid FROM edits e JOIN leads l ON l.id = e.lead_id "
            "WHERE e.status IN ('drafted','needs_human') ORDER BY e.id").fetchall()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        quotes_month = self.db.conn.execute(
            "SELECT COUNT(*) FROM quotes WHERE substr(created_at,1,7) = ?", (month,)).fetchone()[0]
        quotes_all = self.db.conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        unforwarded = self.db.conn.execute(
            "SELECT l.*, COUNT(*) n FROM quotes q JOIN leads l ON l.id = q.lead_id "
            "WHERE q.notified = 0 GROUP BY l.id").fetchall()
        from .referrals import ReferralBot
        ref = ReferralBot(self.db, self.cfg, mailer=self.mailer).stats()
        from .bots.winback_bot import WinbackBot
        wb = WinbackBot(self.db, self.cfg, mailer=self.mailer).stats()
        top_leads = self.db.leads_by_status("new", "built", "deployed", limit=10, by_score=True)
        ab = self.db.variant_stats()
        ab_table = "\n".join(
            f"| {v['variant']} | {v['sent']} | {v['replies']} ({v['reply_rate']:.1%}) | {v['wins']} ({v['win_rate']:.1%}) |"
            for v in ab
        ) or "| - | 0 | 0 | 0 |"
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

| MRR | Clients | Revenue collected | Close rate | Churn | Emails today |
|---|---|---|---|---|---|
| **{_money(m['mrr_cents'])}**<br>base {_money(m['base_mrr_cents'])} + add-ons {_money(m['addon_mrr_cents'])} | {m['clients']} | {_money(m['revenue_collected_cents'])} | {m['close_rate']:.1%} | {m['churn_rate']:.1%} | {m['emails_today']}/{self.cfg.daily_email_cap} |

## 🔥 Replies waiting for you
{chr(10).join(f"- [[{note_name(r)}]] ({r['email']}): " + (r['last_reply'] or '').strip().splitlines()[0][:80] if (r['last_reply'] or '').strip() else f"- [[{note_name(r)}]] ({r['email']})" for r in hot) or "- None right now"}

## ✏️ Site edits waiting for your approval
{chr(10).join(f"- [[{note_name({'business_name': r['business_name'], 'id': r['lid']})} edit {r['id']}]]: {r['summary']}" + (" ⚠️ needs you" if r['status'] == 'needs_human' else "") for r in pending_edits) or "- None"}

## 📝 Quote requests (client websites)
**{quotes_month}** this month · {quotes_all} all time
{chr(10).join(f"- ⚠️ [[{note_name(r)}]]: {r['n']} request(s) not forwarded (no client email or send failed)" for r in unforwarded) or ""}

## 💬 Client messages (last 7 days)
{chr(10).join(f"- [[{note_name(r)}]]: " + (r['last_reply'] or '').strip().splitlines()[0][:80] for r in client_msgs if (r['last_reply'] or '').strip()) or "- None"}

## 💳 Payment problems (card failing: call them before they cancel)
{chr(10).join(f"- [[{note_name(r)}]] · {r['phone'] or r['email']}" for r in at_risk) or "- None"}

## 🚨 Client sites down
{chr(10).join(f"- [[{note_name(r)}]]: {r['url']} ({r['status']})" for r in down) or "- All client sites up"}

## Pipeline
| Stage | Leads |
|---|---|
{pipeline}

## 📮 Sending inboxes
| Inbox | Sent today | Bounce rate | Health |
|---|---|---|---|
{inbox_rows}

*Inboxes auto-pause above {self.cfg.bounce_pause_rate:.0%} bounces. Configure them in `senders.json`.*

## 📈 Add-on upsells
| Add-on | Offered | Active | MRR |
|---|---|---|---|
{addon_table}

## 🤝 Referrals
{ref['signups']} referred sign-ups · **{ref['won']} became clients** · {ref['rewards']} free months given

## 🔁 Win-back
{wb['contacted']} people contacted · **{wb['recovered']} won back**

## 🎯 Best leads not yet emailed
{chr(10).join(f"- **{r['score']}** [[{note_name(r)}]] · {r['score_reason'] or ''}" for r in top_leads) or "- None waiting"}

## 🧪 Subject line A/B test
| Variant | Sent | Replies | Wins |
|---|---|---|---|
{ab_table}

*Variants are defined in `agency/bots/outreach_bot.py` (`SUBJECT_VARIANTS`). Wait for ~100 sends per variant before declaring a winner.*

## Recently updated
{chr(10).join(f"- {STATUS_EMOJI.get(r['status'], '')} [[{note_name(r)}]] · {r['status']}" for r in rows[:15]) or "- No leads yet"}

## Live views (needs the Dataview community plugin)
```dataview
TABLE score, status, niche, city, rating, followups_sent AS "follow-ups", last_contacted
FROM "Leads"
SORT score DESC
```

See also: [[Performance]] · [[Home]] · [[Business Plan]] · [[Playbooks/Closing a Reply]]
"""
        (self.vault / "Dashboard.md").write_text(text, encoding="utf-8")

    def write_performance(self) -> None:
        from .analytics import Analytics

        snap = Analytics(self.db, self.cfg).snapshot()
        k = snap.kpis
        hist = snap.mrr_history
        labels = ", ".join(f'"{h["label"]}"' for h in hist)
        totals = ", ".join(str(round((h["base"] + h["addons"]) / 100)) for h in hist)
        top = max([(h["base"] + h["addons"]) / 100 for h in hist] + [100])
        def rate(x):
            return "" if x is None else f"{x:.0%}"

        funnel = "\n".join(f"| {f['stage']} | {f['count']:,} | {rate(f['rate_from_prev'])} |" for f in snap.funnel)

        def seg(rows):
            lines = []
            for r in rows:
                close = f"{r['close_rate']:.1%}" if r["emailed"] else "–"
                flag = "" if r["enough_data"] else "*"
                lines.append(f"| {r['segment']} | {r['leads']} | {r['emailed']} | {close}{flag} | "
                             f"{r['clients']} | {_money(r['mrr_cents'])} |")
            return "\n".join(lines) or "| – | 0 | 0 | – | 0 | $0 |"

        churn = "–" if k["churn_rate_30d"] is None else f"{k['churn_rate_30d']:.1%}"
        ltv = "–" if k["ltv_cents"] is None else _money(k["ltv_cents"])
        text = f"""---
tags: agency/performance
updated: "{snap.generated_at}"
---
# 📈 Performance
*Auto-updated every run · interactive version: `/admin` on your server or `python -m agency dashboard`*

| MRR | Change (30d) | Clients | Revenue / client | Churn (30d) | Lifetime value |
|---|---|---|---|---|---|
| **{_money(k['mrr_cents'])}** | {'+' if k['mrr_change_cents'] >= 0 else '-'}{_money(abs(k['mrr_change_cents']))} | {k['clients']} | {_money(k['arpu_cents'])} | {churn} | {ltv} |

## What to do next
{chr(10).join('- ' + r for r in snap.recommendations)}

## Monthly recurring revenue
```mermaid
xychart-beta
    title "MRR, last 12 months ($)"
    x-axis [{labels}]
    y-axis "MRR ($)" 0 --> {int(top * 1.1) + 1}
    bar [{totals}]
```

## Sales funnel
| Stage | Leads | From previous |
|---|---|---|
{funnel}

## By niche
| Niche | Leads | Emailed | Close rate | Clients | MRR |
|---|---|---|---|---|---|
{seg(snap.by_niche)}

## By city
| City | Leads | Emailed | Close rate | Clients | MRR |
|---|---|---|---|---|---|
{seg(snap.by_city)}

Close rates marked * are based on fewer than 20 emailed leads: too early to judge. Back to [[Dashboard]]
"""
        (self.vault / "Performance.md").write_text(text, encoding="utf-8")

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
        self.write_edits()
        self.write_dashboard()
        self.write_performance()
        self.write_daily()
        return written

    def run(self) -> dict:
        self.pull_edit_decisions()
        pulled = self.pull()
        return {"do_not_contact_applied": pulled, "notes_written": self.push()}
