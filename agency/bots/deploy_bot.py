"""Deploy bot: publishes built sites to Netlify as live preview URLs.

Uses Netlify's zip-deploy API: create a site, then POST a zip of the folder.
In DRY_RUN (or with no NETLIFY_TOKEN) it records a local file:// preview instead.
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path

import requests

from ..config import Config
from ..db import DB

log = logging.getLogger(__name__)
API = "https://api.netlify.com/api/v1"


def zip_dir(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in folder.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(folder).as_posix())
    return buf.getvalue()


class DeployBot:
    name = "deploy"

    def __init__(self, db: DB, cfg: Config, session: requests.Session | None = None):
        self.db, self.cfg = db, cfg
        self.http = session or requests.Session()

    @property
    def live(self) -> bool:
        return not self.cfg.dry_run and bool(self.cfg.netlify_token)

    def _headers(self, extra: dict | None = None) -> dict:
        return {"Authorization": f"Bearer {self.cfg.netlify_token}", **(extra or {})}

    def deploy(self, lead: dict) -> tuple[str, str | None]:
        folder = self.cfg.build_dir / lead["site_slug"]
        if not self.live:
            return folder.joinpath("index.html").resolve().as_uri(), None
        site_id = lead.get("netlify_site_id")
        if not site_id:
            r = self.http.post(f"{API}/sites", headers=self._headers(),
                               json={"name": f"preview-{lead['site_slug']}"[:63]}, timeout=30)
            r.raise_for_status()
            site_id = r.json()["id"]
        r = self.http.post(
            f"{API}/sites/{site_id}/deploys",
            headers=self._headers({"Content-Type": "application/zip"}),
            data=zip_dir(folder), timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        url = data.get("ssl_url") or data.get("url") or f"https://preview-{lead['site_slug']}.netlify.app"
        return url, site_id

    def run(self, statuses: tuple[str, ...] = ("built",), next_status: str = "deployed") -> int:
        n = 0
        for row in self.db.leads_by_status(*statuses):
            lead = dict(row)
            try:
                url, site_id = self.deploy(lead)
            except (requests.RequestException, OSError) as exc:
                self.db.log(self.name, "error", str(exc), lead["id"])
                continue
            fields = {"status": next_status, "preview_url": url}
            if site_id:
                fields["netlify_site_id"] = site_id
            self.db.update_lead(lead["id"], **fields)
            self.db.log(self.name, "deployed" if self.live else "dry_deploy", url, lead["id"])
            n += 1
        log.info("deploy bot: %d deployed (%s)", n, "live" if self.live else "dry run")
        return n

    def cleanup(self) -> int:
        """Take down previews for prospects who opted out or never responded, so
        we don't leave sites up for businesses that said no."""
        n = 0
        for row in self.db.leads_by_status("lost", "unsubscribed", "bounced"):
            if not row["netlify_site_id"]:
                continue
            if self.live:
                try:
                    r = self.http.delete(f"{API}/sites/{row['netlify_site_id']}",
                                         headers=self._headers(), timeout=30)
                    if r.status_code not in (200, 204, 404):
                        r.raise_for_status()
                except requests.RequestException as exc:
                    self.db.log(self.name, "error", f"teardown: {exc}", row["id"])
                    continue
            self.db.update_lead(row["id"], netlify_site_id=None, preview_url=None)
            self.db.log(self.name, "preview_removed", row["status"], row["id"])
            n += 1
        return n
