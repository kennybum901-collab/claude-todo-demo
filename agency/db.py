"""SQLite storage: the single source of truth for the whole pipeline.

Lead lifecycle (``leads.status``):
    new -> built -> deployed -> contacted -> replied -> client
                                         \-> lost / unsubscribed
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
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER,
    bot        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    detail     TEXT,
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
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

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

    def leads_by_status(self, *statuses: str, limit: int = 1000) -> list[sqlite3.Row]:
        marks = ", ".join("?" for _ in statuses)
        return self.conn.execute(
            f"SELECT * FROM leads WHERE status IN ({marks}) ORDER BY id LIMIT ?",
            (*statuses, limit),
        ).fetchall()

    # ---------- events / audit log ----------
    def log(self, bot: str, kind: str, detail: str = "", lead_id: int | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events (lead_id, bot, kind, detail, created_at) VALUES (?,?,?,?,?)",
                (lead_id, bot, kind, detail, now()),
            )

    def emails_sent_today(self) -> int:
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE bot='outreach' AND kind IN ('sent','dry_send') "
            "AND substr(created_at,1,10)=?",
            (today,),
        ).fetchone()
        return row[0]

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
        contacted = sum(counts.get(s, 0) for s in ("contacted", "replied", "client", "lost"))
        return {
            "pipeline": counts,
            "clients": clients,
            "mrr_cents": clients * monthly_fee_cents,
            "revenue_collected_cents": revenue,
            "close_rate": (clients / contacted) if contacted else 0.0,
            "emails_today": self.emails_sent_today(),
        }
