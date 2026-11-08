r"""SQLite storage: the single source of truth for the whole pipeline.

Lead lifecycle (``leads.status``):
    new -> built -> deployed -> contacted -> replied -> paid -> client -> churned
                                         \-> lost / unsubscribed / bounced
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    business_name   TEXT NOT NULL,
    niche           TEXT,
    city            TEXT,
    address         TEXT,
    phone           TEXT,
    email           TEXT,
    rating          REAL,
    review_count    INTEGER,
    source          TEXT,
    source_id       TEXT UNIQUE,
    status          TEXT NOT NULL DEFAULT 'new',
    site_slug       TEXT,
    preview_url     TEXT,
    netlify_site_id TEXT,
    payment_url     TEXT,
    payment_link_id TEXT,
    followups_sent  INTEGER NOT NULL DEFAULT 0,
    last_contacted  TEXT,
    notes           TEXT,
    variant         TEXT,
    sender          TEXT,
    score           INTEGER,
    score_reason    TEXT,
    referred_by     INTEGER,
    theme           TEXT,
    last_reply      TEXT,
    reply_draft     TEXT,
    subscription_id TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER,
    bot        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
    sender     TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suppression (
    email      TEXT PRIMARY KEY,
    reason     TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id          TEXT PRIMARY KEY,
    lead_id     INTEGER NOT NULL,
    amount      INTEGER NOT NULL,
    currency    TEXT NOT NULL,
    kind        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS addons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL,
    addon           TEXT NOT NULL,
    price_cents     INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'offered',   -- offered | active | canceled
    payment_url     TEXT,
    payment_link_id TEXT,
    subscription_id TEXT,
    offered_at      TEXT NOT NULL,
    activated_at    TEXT,
    UNIQUE (lead_id, addon)
);
CREATE TABLE IF NOT EXISTS quotes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id      INTEGER NOT NULL,
    name         TEXT,
    phone        TEXT,
    email        TEXT,
    service      TEXT,
    message      TEXT,
    notified     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edits (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id       INTEGER NOT NULL,
    message       TEXT NOT NULL,
    status        TEXT NOT NULL,  -- new|drafted|needs_human|not_request|approved|rejected|applied
    summary       TEXT,
    before_json   TEXT,
    proposed_json TEXT,
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    applied_at    TEXT
);
CREATE TABLE IF NOT EXISTS winbacks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL,
    audience        TEXT NOT NULL,     -- churned | lost
    step            INTEGER NOT NULL,
    payment_url     TEXT,
    payment_link_id TEXT,
    sent_at         TEXT NOT NULL,
    recovered_at    TEXT,
    UNIQUE (lead_id, audience, step)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS messages (           -- copy of every email the bots send (audit + daily digest)
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER,
    bot        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    to_addr    TEXT,
    subject    TEXT,
    body       TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referral_rewards (
    referred_lead_id INTEGER PRIMARY KEY,
    referrer_id      INTEGER NOT NULL,
    coupon_id        TEXT,
    applied_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports (
    lead_id  INTEGER NOT NULL,
    period   TEXT NOT NULL,          -- YYYY-MM the report covers
    sent_at  TEXT NOT NULL,
    PRIMARY KEY (lead_id, period)
);
CREATE TABLE IF NOT EXISTS uptime (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL,
    url        TEXT NOT NULL,
    ok         INTEGER NOT NULL,
    status     INTEGER,
    ms         INTEGER,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # timeout + WAL: the web server and the scheduler thread write concurrently
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    # columns added after the first release; ALTER existing databases in place
    MIGRATIONS = {
        "leads": {"variant": "TEXT", "last_reply": "TEXT", "reply_draft": "TEXT",
                  "subscription_id": "TEXT", "sender": "TEXT", "score": "INTEGER",
                  "score_reason": "TEXT", "referred_by": "INTEGER", "theme": "TEXT"},
        "events": {"sender": "TEXT"},
    }

    def _migrate(self) -> None:
        for table, cols in self.MIGRATIONS.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---------- leads ----------
    def add_lead(self, **fields: Any) -> int | None:
        """Insert a lead; returns id, or None if it already exists (dedupe on source_id)."""
        fields.setdefault("status", "new")
        fields["created_at"] = fields["updated_at"] = now()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.tx() as c:
            cur = c.execute(
                f"INSERT OR IGNORE INTO leads ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            return cur.lastrowid if cur.rowcount else None

    def update_lead(self, lead_id: int, **fields: Any) -> None:
        fields["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE leads SET {sets} WHERE id = ?", (*fields.values(), lead_id))

    def get_lead(self, lead_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()

    def leads_by_status(self, *statuses: str, limit: int = 1000, by_score: bool = False) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in statuses)
        order = "COALESCE(score, -1) DESC, id" if by_score else "id"
        return self.conn.execute(
            f"SELECT * FROM leads WHERE status IN ({marks}) ORDER BY {order} LIMIT ?",
            (*statuses, limit),
        ).fetchall()

    # ---------- events / audit log ----------
    def log(self, bot: str, kind: str, detail: str = "", lead_id: int | None = None,
            sender: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (lead_id, bot, kind, detail, sender, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (lead_id, bot, kind, detail, sender, now()),
            )

    def emails_sent_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind IN ('sent','dry_send') "
            "AND substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
        return row[0]

    # ---------- settings (kill switch, digest bookkeeping) ----------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        with self.tx() as c:
            if value is None:
                c.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def sending_paused(self) -> bool:
        return self.get_setting("sending_paused") == "1"

    # ---------- suppression (unsubscribes / bounces) ----------
    def suppress(self, email: str, reason: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO suppression (email, reason, created_at) VALUES (?,?,?)",
                (email.lower().strip(), reason, now()),
            )

    def is_suppressed(self, email: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM suppression WHERE email = ?", (email.lower().strip(),)
            ).fetchone()
            is not None
        )

    # ---------- payments ----------
    def record_payment(self, pid: str, lead_id: int, amount: int, currency: str, kind: str) -> bool:
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO payments (id, lead_id, amount, currency, kind, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (pid, lead_id, amount, currency, kind, now()),
            )
            return bool(cur.rowcount)

    def record_uptime(self, lead_id: int, url: str, ok: bool, status: int | None, ms: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO uptime (lead_id, url, ok, status, ms, created_at) VALUES (?,?,?,?,?,?)",
                (lead_id, url, int(ok), status, ms, now()),
            )

    # ---------- reporting ----------
    def metrics(self, monthly_fee_cents: int) -> dict[str, Any]:
        counts = {
            r["status"]: r["n"]
            for r in self.conn.execute("SELECT status, COUNT(*) n FROM leads GROUP BY status")
        }
        revenue = self.conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments").fetchone()[0]
        clients = counts.get("client", 0)
        churned = counts.get("churned", 0)
        contacted = sum(counts.get(s, 0) for s in
                        ("contacted", "replied", "paid", "client", "churned", "lost", "unsubscribed",
                         "bounced"))
        return {
            "pipeline": counts,
            "clients": clients,
            "mrr_cents": clients * monthly_fee_cents + self.addon_mrr(),
            "base_mrr_cents": clients * monthly_fee_cents,
            "addon_mrr_cents": self.addon_mrr(),
            "revenue_collected_cents": revenue,
            "close_rate": ((clients + churned) / contacted) if contacted else 0.0,
            "churned": churned,
            "churn_rate": (churned / (clients + churned)) if (clients + churned) else 0.0,
            "emails_today": self.emails_sent_today(),
        }

    # ---------- add-ons (upsells) ----------
    def addon_mrr(self) -> int:
        return self.conn.execute(
            "SELECT COALESCE(SUM(a.price_cents),0) FROM addons a JOIN leads l ON l.id = a.lead_id "
            "WHERE a.status = 'active' AND l.status = 'client'"
        ).fetchone()[0]

    def addons_for(self, lead_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM addons WHERE lead_id = ? ORDER BY id", (lead_id,)
        ).fetchall()

    def client_since(self, lead_id: int) -> str | None:
        row = self.conn.execute(
            "SELECT MIN(created_at) FROM payments WHERE lead_id = ?", (lead_id,)
        ).fetchone()
        return row[0]

    def variant_stats(self) -> list[dict[str, Any]]:
        """Reply and close rate per outreach subject-line variant (A/B test)."""
        rows = self.conn.execute(
            """SELECT variant,
                      COUNT(*) AS sent,
                      SUM(status IN ('replied','paid','client','churned')) AS replies,
                      SUM(status IN ('paid','client','churned')) AS wins
               FROM leads WHERE variant IS NOT NULL GROUP BY variant ORDER BY variant"""
        ).fetchall()
        return [
            {"variant": r["variant"], "sent": r["sent"], "replies": r["replies"], "wins": r["wins"],
             "reply_rate": r["replies"] / r["sent"] if r["sent"] else 0.0,
             "win_rate": r["wins"] / r["sent"] if r["sent"] else 0.0}
            for r in rows
        ]
