"""Lead bot: finds local businesses that have NO website.

Sources:
  * Google Places API (New) Text Search - official, ToS-compliant API. Needs
    GOOGLE_PLACES_API_KEY. Places doesn't return emails, so these leads get a
    phone number and are routed to the call list until you add an email.
  * CSV import - any list you have the right to use (columns: business_name,
    niche, city, address, phone, email, rating, review_count).
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

import requests

from ..config import Config
from ..db import DB

log = logging.getLogger(__name__)
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join(
    f"places.{f}"
    for f in ("id", "displayName", "formattedAddress", "nationalPhoneNumber",
              "websiteUri", "rating", "userRatingCount", "businessStatus")
)


class LeadBot:
    name = "lead"

    def __init__(self, db: DB, cfg: Config, session: requests.Session | None = None):
        self.db, self.cfg = db, cfg
        self.http = session or requests.Session()

    def search_places(self, niche: str, city: str, max_pages: int = 3) -> list[dict]:
        headers = {
            "X-Goog-Api-Key": self.cfg.google_places_key,
            "X-Goog-FieldMask": FIELD_MASK + ",nextPageToken",
        }
        body: dict = {"textQuery": f"{niche} in {city}", "pageSize": 20}
        results: list[dict] = []
        for _ in range(max_pages):
            resp = self.http.post(PLACES_URL, json=body, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            results.extend(data.get("places", []))
            token = data.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        return results

    def run(self) -> int:
        if not self.cfg.google_places_key:
            log.info("lead bot: no GOOGLE_PLACES_API_KEY; skipping search (use `import-csv`)")
            return 0
        added = 0
        for query in self.cfg.lead_queries:
            niche, _, city = query.partition("|")
            try:
                places = self.search_places(niche.strip(), city.strip())
            except requests.RequestException as exc:
                self.db.log(self.name, "error", f"{query}: {exc}")
                continue
            for p in places:
                if p.get("websiteUri") or p.get("businessStatus", "OPERATIONAL") != "OPERATIONAL":
                    continue  # already has a site, or closed
                lead_id = self.db.add_lead(
                    business_name=p.get("displayName", {}).get("text", "Unknown"),
                    niche=niche.strip(), city=city.strip(),
                    address=p.get("formattedAddress"), phone=p.get("nationalPhoneNumber"),
                    rating=p.get("rating"), review_count=p.get("userRatingCount"),
                    source="google_places", source_id=f"gp:{p['id']}",
                )
                if lead_id:
                    added += 1
                    self.db.log(self.name, "lead_added", query, lead_id)
        log.info("lead bot: %d new leads", added)
        return added

    def import_csv(self, path: Path | str) -> int:
        added = 0
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                name = (row.get("business_name") or "").strip()
                if not name:
                    continue
                key = f"csv:{name.lower()}|{(row.get('phone') or row.get('email') or '').lower()}"
                lead_id = self.db.add_lead(
                    business_name=name,
                    niche=(row.get("niche") or "").strip() or None,
                    city=(row.get("city") or "").strip() or None,
                    address=row.get("address") or None,
                    phone=row.get("phone") or None,
                    email=(row.get("email") or "").strip().lower() or None,
                    rating=float(row["rating"]) if row.get("rating") else None,
                    review_count=int(row["review_count"]) if row.get("review_count") else None,
                    source="csv", source_id=key,
                )
                if lead_id:
                    added += 1
                    self.db.log(self.name, "lead_added", f"csv {path}", lead_id)
        return added
