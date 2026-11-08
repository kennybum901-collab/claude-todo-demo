"""Monitor bot: uptime checks for every paying client's site (part of the care plan)."""
from __future__ import annotations

import logging
import time

import requests

from ..config import Config
from ..db import DB

log = logging.getLogger(__name__)


class MonitorBot:
    name = "monitor"

    def __init__(self, db: DB, cfg: Config, session: requests.Session | None = None):
        self.db, self.cfg = db, cfg
        self.http = session or requests.Session()

    def run(self) -> list[dict]:
        down = []
        for row in self.db.leads_by_status("client"):
            url = row["preview_url"]
            if not url or not url.startswith("http"):
                continue
            start = time.monotonic()
            try:
                r = self.http.get(url, timeout=15)
                ok, status = r.ok, r.status_code
            except requests.RequestException:
                ok, status = False, None
            ms = int((time.monotonic() - start) * 1000)
            self.db.record_uptime(row["id"], url, ok, status, ms)
            if not ok:
                down.append({"lead_id": row["id"], "url": url, "status": status})
                self.db.log(self.name, "down", f"{url} -> {status}", row["id"])
        if down:
            log.warning("monitor bot: %d client sites DOWN: %s", len(down), down)
        return down
