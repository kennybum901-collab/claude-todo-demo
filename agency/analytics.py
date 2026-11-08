"""Business analytics: MRR history, funnel, segments, unit economics.

Pure read-only queries over the database. Rendered by `dashboard.py` (HTML,
served at /admin) and by the Obsidian sync (`Performance.md`).
"""
from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .config import Config
from .db import DB

MIN_SAMPLE = 20          # emailed leads needed before a segment is judged
START_KINDS = ("setup+first_month", "winback")


@dataclass
class Snapshot:
    generated_at: str
    kpis: dict[str, Any]
    mrr_history: list[dict[str, Any]]          # [{month, label, base, addons}]
    funnel: list[dict[str, Any]]               # [{stage, count, rate_from_prev}]
    by_niche: list[dict[str, Any]]
    by_city: list[dict[str, Any]]
    by_source: list[dict[str, Any]] = field(default_factory=list)
    by_theme: list[dict[str, Any]] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


def _d(ts: str) -> date:
    return datetime.fromisoformat(ts).date()


def month_ends(today: date, months: int = 12) -> list[tuple[str, str, date]]:
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        last = date(y, m, calendar.monthrange(y, m)[1])
        out.append((f"{y:04d}-{m:02d}", f"{calendar.month_abbr[m]} {str(y)[2:]}", min(last, today)))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return list(reversed(out))


class Analytics:
    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        q = self.db.conn.execute
        self.starts: dict[int, list[date]] = defaultdict(list)
        for r in q("SELECT lead_id, created_at FROM payments WHERE kind IN (?,?)", START_KINDS):
            self.starts[r["lead_id"]].append(_d(r["created_at"]))
        self.stops: dict[int, list[date]] = defaultdict(list)
        for r in q("SELECT lead_id, created_at FROM events WHERE kind = 'churned' AND lead_id IS NOT NULL"):
            self.stops[r["lead_id"]].append(_d(r["created_at"]))
        self.addon_cancels: dict[tuple[int, str], date] = {}
        for r in q("SELECT lead_id, detail, created_at FROM events WHERE kind = 'addon_canceled'"):
            self.addon_cancels[(r["lead_id"], (r["detail"] or "").strip())] = _d(r["created_at"])
        self.addons = q("SELECT lead_id, addon, price_cents, activated_at FROM addons "
                        "WHERE activated_at IS NOT NULL").fetchall()

    # ---------- point-in-time ----------
    def active_at(self, lead_id: int, day: date) -> bool:
        started = [d for d in self.starts.get(lead_id, []) if d <= day]
        if not started:
            return False
        stopped = [d for d in self.stops.get(lead_id, []) if d <= day]
        return not stopped or max(started) > max(stopped)

    def clients_at(self, day: date) -> list[int]:
        return [lid for lid in self.starts if self.active_at(lid, day)]

    def mrr_at(self, day: date) -> tuple[int, int]:
        active = set(self.clients_at(day))
        base = len(active) * self.cfg.monthly_fee_cents
        addons = 0
        for a in self.addons:
            if a["lead_id"] not in active or _d(a["activated_at"]) > day:
                continue
            cancel = self.addon_cancels.get((a["lead_id"], a["addon"]))
            if cancel and cancel <= day:
                continue
            addons += a["price_cents"]
        return base, addons

    # ---------- sections ----------
    def mrr_history(self, today: date, months: int = 12) -> list[dict[str, Any]]:
        rows = []
        for key, label, end in month_ends(today, months):
            base, addons = self.mrr_at(end)
            rows.append({"month": key, "label": label, "base": base, "addons": addons})
        return rows

    def _lead_sets(self) -> dict[str, set[int]]:
        q = self.db.conn.execute
        emailed = {r[0] for r in q("SELECT DISTINCT lead_id FROM events WHERE bot = 'outreach' "
                                    "AND kind IN ('sent','dry_send') AND lead_id IS NOT NULL")}
        replied = {r[0] for r in q("SELECT DISTINCT lead_id FROM events WHERE kind = 'replied'")}
        replied |= {r[0] for r in q("SELECT id FROM leads WHERE status IN ('replied','paid','client','churned')")}
        won = {r[0] for r in q("SELECT DISTINCT lead_id FROM payments WHERE kind IN (?,?)", START_KINDS)}
        built = {r[0] for r in q("SELECT id FROM leads WHERE site_slug IS NOT NULL")}
        return {"all": {r[0] for r in q("SELECT id FROM leads")}, "built": built,
                "emailed": emailed | won, "replied": replied | won, "won": won}

    def funnel(self) -> list[dict[str, Any]]:
        s = self._lead_sets()
        stages = [("Leads found", s["all"]), ("Site built", s["built"] | s["emailed"]),
                  ("Emailed", s["emailed"]), ("Replied", s["replied"]), ("Became client", s["won"])]
        out, prev = [], None
        for name, ids in stages:
            n = len(ids)
            out.append({"stage": name, "count": n,
                        "rate_from_prev": (n / prev) if prev else None})
            prev = n or None
        return out

    def segments(self, column: str, today: date) -> list[dict[str, Any]]:
        s = self._lead_sets()
        active = set(self.clients_at(today))
        addon_mrr: dict[int, int] = defaultdict(int)
        for a in self.addons:
            cancel = self.addon_cancels.get((a["lead_id"], a["addon"]))
            if a["lead_id"] in active and not (cancel and cancel <= today):
                addon_mrr[a["lead_id"]] += a["price_cents"]
        groups: dict[str, dict[str, Any]] = {}
        for r in self.db.conn.execute(f"SELECT id, {column} AS seg FROM leads"):
            key = (r["seg"] or "unknown").strip() or "unknown"
            g = groups.setdefault(key, {"segment": key, "leads": 0, "emailed": 0, "replied": 0,
                                        "won": 0, "clients": 0, "mrr_cents": 0})
            lid = r["id"]
            g["leads"] += 1
            g["emailed"] += lid in s["emailed"]
            g["replied"] += lid in s["replied"]
            g["won"] += lid in s["won"]
            if lid in active:
                g["clients"] += 1
                g["mrr_cents"] += self.cfg.monthly_fee_cents + addon_mrr[lid]
        rows = list(groups.values())
        for g in rows:
            g["close_rate"] = g["won"] / g["emailed"] if g["emailed"] else 0.0
            g["reply_rate"] = g["replied"] / g["emailed"] if g["emailed"] else 0.0
            g["enough_data"] = g["emailed"] >= MIN_SAMPLE
        return sorted(rows, key=lambda g: (-g["mrr_cents"], -g["won"], -g["leads"], g["segment"]))

    def kpis(self, today: date) -> dict[str, Any]:
        base, addons = self.mrr_at(today)
        clients = len(self.clients_at(today))
        month_ago = today - timedelta(days=30)
        clients_then = len(self.clients_at(month_ago))
        churned_30 = sum(1 for days in self.stops.values() for d in days if month_ago < d <= today)
        churn = churned_30 / clients_then if clients_then else None
        mrr = base + addons
        arpu = mrr / clients if clients else 0
        revenue_30 = self.db.conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM payments WHERE substr(created_at,1,10) > ?",
            (month_ago.isoformat(),)).fetchone()[0]
        prev_base, prev_addons = self.mrr_at(month_ago)
        return {
            "mrr_cents": mrr, "base_mrr_cents": base, "addon_mrr_cents": addons,
            "mrr_change_cents": mrr - (prev_base + prev_addons),
            "clients": clients, "arpu_cents": int(arpu),
            "churn_rate_30d": churn,
            # LTV only once churn is observable; never divide by an imaginary rate
            "ltv_cents": int(arpu / churn) if churn else None,
            "revenue_30d_cents": revenue_30,
            "revenue_total_cents": self.db.conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0],
        }

    def recommendations(self, by_niche, by_city, funnel) -> list[str]:
        recs: list[str] = []
        for label, rows in (("niche", by_niche), ("city", by_city)):
            judged = [r for r in rows if r["enough_data"]]
            if len(judged) >= 2:
                best = max(judged, key=lambda r: r["close_rate"])
                worst = min(judged, key=lambda r: r["close_rate"])
                if best["close_rate"] > 0 and best["close_rate"] >= 2 * max(worst["close_rate"], 0.001):
                    recs.append(f"Double down on {label} **{best['segment']}**: {best['close_rate']:.1%} close "
                                f"rate vs {worst['close_rate']:.1%} for {worst['segment']}.")
            for r in judged:
                if r["won"] == 0 and r["emailed"] >= 2 * MIN_SAMPLE:
                    recs.append(f"Consider pausing {label} **{r['segment']}**: {r['emailed']} emailed, no clients yet.")
        emailed = next(f for f in funnel if f["stage"] == "Emailed")
        replied = next(f for f in funnel if f["stage"] == "Replied")
        won = next(f for f in funnel if f["stage"] == "Became client")
        if emailed["count"] >= 100 and (replied["rate_from_prev"] or 0) < 0.01:
            recs.append("Reply rate is under 1%: test new subject lines and check inbox placement.")
        if replied["count"] >= 10 and (won["rate_from_prev"] or 0) < 0.2:
            recs.append("Fewer than 1 in 5 replies close: answer replies faster and use the closing playbook.")
        if not recs:
            recs.append(f"Not enough data yet for recommendations (each segment needs {MIN_SAMPLE}+ emailed leads).")
        return recs

    def snapshot(self, today: date | None = None) -> Snapshot:
        today = today or datetime.now(timezone.utc).date()
        funnel = self.funnel()
        by_niche, by_city = self.segments("niche", today), self.segments("city", today)
        return Snapshot(
            generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            kpis=self.kpis(today), mrr_history=self.mrr_history(today), funnel=funnel,
            by_niche=by_niche, by_city=by_city, by_source=self.segments("source", today),
            by_theme=self.segments("theme", today),
            recommendations=self.recommendations(by_niche, by_city, funnel),
        )
