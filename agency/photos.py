"""Photos for client websites.

Two sources:
  * client photos - a client replies to any email with images attached. They're
    processed, held as 'pending' and turned into an edit request; you approve it
    in Obsidian (the photos show inside the note) or with `edit-approve <id>`.
  * stock photos - with PEXELS_API_KEY set, sites without client photos get
    up to 3 free Pexels photos matched to the niche, credited in the footer.
    Client photos always replace stock ones once approved.

Every image is opened and re-encoded with Pillow: non-images are rejected, it's
resized to max 1600px, saved as a fresh JPEG, and EXIF metadata (including GPS
location) is dropped, so a client's phone photo can't leak where they live.
"""
from __future__ import annotations

import io
import json
import logging
import shutil
from pathlib import Path

import requests

from .config import Config
from .db import DB, now

log = logging.getLogger(__name__)
MAX_BYTES = 15_000_000
MAX_PER_EMAIL = 10
MAX_SIDE = 1600
PEXELS = "https://api.pexels.com/v1/search"


class PhotoError(ValueError):
    pass


def process_image(data: bytes) -> tuple[bytes, int, int]:
    """Validate + resize + re-encode as JPEG without metadata. Returns (jpeg, w, h)."""
    if len(data) > MAX_BYTES:
        raise PhotoError("image too large")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise PhotoError("Pillow is not installed (pip install pillow)") from exc
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()                      # cheap structural check first
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > 60_000_000:
                raise PhotoError("image dimensions too large")
            im = ImageOps.exif_transpose(im)  # keep phone photos the right way up
            im = im.convert("RGB")
            im.thumbnail((MAX_SIDE, MAX_SIDE))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=82, optimize=True, progressive=True)  # no exif= -> metadata dropped
            return out.getvalue(), im.width, im.height
    except PhotoError:
        raise
    except Exception as exc:
        raise PhotoError(f"not a usable image ({type(exc).__name__})") from exc


class Photos:
    def __init__(self, db: DB, cfg: Config, http: requests.Session | None = None):
        self.db, self.cfg = db, cfg
        self.http = http or requests.Session()
        self.root = Path(cfg.build_dir).parent / "photos"

    def _store(self, lead_id: int, data: bytes, *, source: str, status: str, alt: str = "",
               credit: str | None = None) -> int:
        jpeg, w, h = process_image(data)
        folder = self.root / str(lead_id)
        folder.mkdir(parents=True, exist_ok=True)
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO photos (lead_id, filename, source, status, alt, credit, width, height, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (lead_id, "", source, status, alt[:200], credit, w, h, now()))
            pid = cur.lastrowid
            name = f"photo-{pid}.jpg"
            (folder / name).write_bytes(jpeg)
            c.execute("UPDATE photos SET filename = ? WHERE id = ?", (name, pid))
        return pid

    def path(self, photo) -> Path:
        return self.root / str(photo["lead_id"]) / photo["filename"]

    # ---------- client photos ----------
    def intake(self, lead_id: int, attachments: list[tuple[str, bytes]], message: str = "") -> int | None:
        """Store client-sent images as pending and open an edit request. Returns the edit id."""
        ids, rejected = [], 0
        for name, data in attachments[:MAX_PER_EMAIL]:
            try:
                ids.append(self._store(lead_id, data, source="client", status="pending",
                                       alt=Path(name or "").stem.replace("_", " ").replace("-", " ")))
            except PhotoError as exc:
                rejected += 1
                log.info("photo from lead %s rejected: %s", lead_id, exc)
        if not ids:
            return None
        lead = self.db.get_lead(lead_id)
        summary = f"Add {len(ids)} photo{'s' if len(ids) != 1 else ''} to {lead['business_name']}'s site"
        if rejected:
            summary += f" ({rejected} attachment(s) weren't usable images)"
        with self.db.tx() as c:
            cur = c.execute(
                "INSERT INTO edits (lead_id, message, status, summary, before_json, proposed_json, created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (lead_id, message[:6000] or "(photos attached)", "drafted", summary, json.dumps({}),
                 json.dumps({"photos": ids}), now()))
        self.db.log("photos", "photos_received", summary, lead_id)
        return cur.lastrowid

    def approve(self, photo_ids: list[int]) -> None:
        if not photo_ids:
            return
        marks = ",".join("?" * len(photo_ids))
        with self.db.tx() as c:
            c.execute(f"UPDATE photos SET status = 'approved' WHERE id IN ({marks})", photo_ids)

    def reject(self, photo_ids: list[int]) -> None:
        if not photo_ids:
            return
        marks = ",".join("?" * len(photo_ids))
        with self.db.tx() as c:
            c.execute(f"UPDATE photos SET status = 'rejected' WHERE id IN ({marks})", photo_ids)

    # ---------- what a site shows ----------
    def for_site(self, lead_id: int) -> list:
        """Approved client photos win; otherwise approved stock photos."""
        rows = self.db.conn.execute(
            "SELECT * FROM photos WHERE lead_id = ? AND status = 'approved' ORDER BY id", (lead_id,)).fetchall()
        client = [r for r in rows if r["source"] == "client"]
        return client or [r for r in rows if r["source"] == "stock"][:3]

    def copy_into(self, lead_id: int, site_dir: Path) -> list[dict]:
        """Copy the site's photos into <site>/img/ and return template data."""
        img = site_dir / "img"
        if img.exists():
            shutil.rmtree(img)
        out = []
        for p in self.for_site(lead_id):
            src = self.path(p)
            if not src.is_file():
                continue
            img.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, img / p["filename"])
            out.append({"src": f"img/{p['filename']}", "alt": p["alt"] or "", "width": p["width"],
                        "height": p["height"], "credit": p["credit"]})
        return out

    # ---------- stock ----------
    def ensure_stock(self, lead) -> int:
        """Fetch up to 3 Pexels photos for a lead that has none. Needs PEXELS_API_KEY."""
        if not self.cfg.pexels_api_key:
            return 0
        have = self.db.conn.execute("SELECT COUNT(*) FROM photos WHERE lead_id = ? AND status != 'rejected'",
                                    (lead["id"],)).fetchone()[0]
        if have:
            return 0
        query = lead["niche"] or "small business"
        try:
            r = self.http.get(PEXELS, params={"query": query, "per_page": 3, "orientation": "landscape"},
                              headers={"Authorization": self.cfg.pexels_api_key}, timeout=20)
            r.raise_for_status()
            n = 0
            for ph in r.json().get("photos", [])[:3]:
                img = self.http.get(ph["src"]["large"], timeout=30)
                img.raise_for_status()
                self._store(lead["id"], img.content, source="stock", status="approved",
                            alt=ph.get("alt") or query, credit=f"{ph.get('photographer', 'Pexels')} / Pexels")
                n += 1
            return n
        except (requests.RequestException, KeyError, PhotoError) as exc:
            log.info("stock photos for lead %s failed: %s", lead["id"], exc)
            return 0
