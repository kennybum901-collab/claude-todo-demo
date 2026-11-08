"""Report bot: a monthly "here's what you're paying for" email to every client.

Retention is the cheapest growth: a client who sees uptime, work done and their
plan each month cancels far less often than one who only sees a charge.

  * goes out on/after REPORT_DAY each month, covering the previous calendar month
  * one report per client per month (tracked in the `reports` table)
  * only for clients who were already clients before that month ended
  * a copy is saved to the Obsidian vault under Reports/
  * respects the suppression list (a client who opted out gets no reports)
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timezone

from jinja2 import Environment, FileSystemLoader

from ..config import Config
from ..db import DB, now
from ..mailer import Mailer
from ..quotes import counts as quote_counts
from ..unsubscribe import url as unsubscribe_url
from .builder_bot import TEMPLATES
from .outreach_bot import money
from .upsell_bot import CATALOG_BY_KEY, best_offers

log = logging.getLogger(__name__)

WORK_LABELS = {
    "deployed": "Published updates to your website",
    "dry_deploy": "Published updates to your website",
    "built": "Rebuilt your site with the latest content",
    "addon_active": "Set up {detail}",
    "onboarded": "Launched your website on the care plan",
}


def previous_period(today: date) -> tuple[str, date, date]:
    first_this = today.replace(day=1)
    last_prev = date.fromordinal(first_this.toordinal() - 1)
    first_prev = last_prev.replace(day=1)
    return first_prev.strftime("%Y-%m"), first_prev, last_prev


class ReportBot:
    name = "report"

    def __init__(self, db: DB, cfg: Config, mailer: Mailer | None = None):
        self.db, self.cfg = db, cfg
        self.mailer = mailer or Mailer(db, cfg)
        self.env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)

    # ---------- data ----------
    def stats(self, lead, start: date, end: date) -> dict:
        lo, hi = start.isoformat(), end.isoformat()
        row = self.db.conn.execute(
            "SELECT COUNT(*) n, SUM(ok) ok, AVG(CASE WHEN ok THEN ms END) ms FROM uptime "
            "WHERE lead_id = ? AND substr(created_at,1,10) BETWEEN ? AND ?", (lead["id"], lo, hi),
        ).fetchone()
        checks, ok = row["n"] or 0, row["ok"] or 0
        # an incident = a run of consecutive failed checks
        incidents, prev_ok = 0, True
        for r in self.db.conn.execute(
            "SELECT ok FROM uptime WHERE lead_id = ? AND substr(created_at,1,10) BETWEEN ? AND ? "
            "ORDER BY id", (lead["id"], lo, hi)):
            if not r["ok"] and prev_ok:
                incidents += 1
            prev_ok = bool(r["ok"])
        work: list[str] = []
        for e in self.db.conn.execute(
            "SELECT kind, detail FROM events WHERE lead_id = ? AND substr(created_at,1,10) BETWEEN ? AND ? "
            "ORDER BY id", (lead["id"], lo, hi)):
            label = WORK_LABELS.get(e["kind"])
            if label:
                item = label.format(detail=(e["detail"] or "").split(" +")[0])
                if item not in work:
                    work.append(item)
        addons = [
            {"name": CATALOG_BY_KEY[a["addon"]].name if a["addon"] in CATALOG_BY_KEY else a["addon"],
             "price": money(a["price_cents"])}
            for a in self.db.addons_for(lead["id"]) if a["status"] == "active"
        ]
        offered = {a["addon"] for a in self.db.addons_for(lead["id"])}
        suggestion = next((a for a in best_offers(lead["niche"]) if a.key not in offered), None)
        return {
            "quotes": quote_counts(self.db, lead["id"], lo, hi),
            "quotes_total": quote_counts(self.db, lead["id"]),
            "checks": checks,
            "uptime_pct": 100.0 * ok / checks if checks else 0.0,
            "avg_ms": int(row["ms"] or 0),
            "incidents": incidents,
            "work": work,
            "addons": addons,
            "suggestion": suggestion,
        }

    def eligible(self, lead, period: str, end: date) -> bool:
        if not lead["email"] or self.db.is_suppressed(lead["email"]):
            return False
        since = self.db.client_since(lead["id"])
        if not since or datetime.fromisoformat(since).date() > end:
            return False
        return self.db.conn.execute(
            "SELECT 1 FROM reports WHERE lead_id = ? AND period = ?", (lead["id"], period)
        ).fetchone() is None

    def compose(self, lead, period: str, start: date, end: date):
        from email.message import EmailMessage

        data = self.stats(lead, start, end)
        body = self.env.get_template("email_report.txt.j2").render(
            lead=lead, cfg=self.cfg, month_name=f"{calendar.month_name[start.month]} {start.year}",
            base_price=money(self.cfg.monthly_fee_cents),
            unsub=unsubscribe_url(lead["email"], self.cfg), **data,
        )
        subject, _, text = body.partition("\n")
        msg = EmailMessage()
        msg["Subject"] = subject.removeprefix("Subject:").strip()
        msg["To"] = lead["email"]
        msg.set_content(text.strip() + "\n")
        return msg, data

    # ---------- obsidian copy ----------
    def save_to_vault(self, lead, period: str, msg) -> None:
        if not self.cfg.obsidian_enabled:
            return
        from ..obsidian import note_name

        folder = self.cfg.obsidian_vault / "Reports"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{note_name(lead)} {period}.md").write_text(
            f"---\ntags: agency/report\nlead_id: {lead['id']}\nperiod: \"{period}\"\n---\n"
            f"# {msg['Subject']}\n\nClient: [[{note_name(lead)}]]\n\n```\n{msg.get_content().strip()}\n```\n",
            encoding="utf-8",
        )

    # ---------- run ----------
    def run(self, today: date | None = None) -> int:
        today = today or datetime.now(timezone.utc).date()
        if today.day < self.cfg.report_day:
            return 0
        period, start, end = previous_period(today)
        sent = 0
        try:
            for lead in self.db.leads_by_status("client"):
                if not self.eligible(lead, period, end):
                    continue
                sender = self.mailer.pick(lead)
                if sender is None:
                    continue  # inboxes full today; it goes out on the next run
                msg, _ = self.compose(lead, period, start, end)
                self.mailer.send(sender, msg, bot=self.name, lead_id=lead["id"],
                                 detail=f"monthly report {period}")
                with self.db.tx() as c:
                    c.execute("INSERT OR IGNORE INTO reports (lead_id, period, sent_at) VALUES (?,?,?)",
                              (lead["id"], period, now()))
                self.save_to_vault(lead, period, msg)
                sent += 1
        finally:
            self.mailer.close()
        log.info("report bot: %d monthly reports for %s", sent, period)
        return sent
