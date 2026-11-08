"""Runs the whole business pipeline, once or forever on a schedule.

    find leads -> payment links -> build sites -> deploy previews -> email
    -> read replies -> detect payments -> publish final site -> monitor clients
    -> sync everything into the Obsidian vault (CRM notes, dashboard, daily log)
"""
from __future__ import annotations

import logging
import time

from .bots.billing_bot import BillingBot
from .bots.builder_bot import BuilderBot
from .bots.deploy_bot import DeployBot
from .bots.lead_bot import LeadBot
from .bots.monitor_bot import MonitorBot
from .bots.outreach_bot import OutreachBot
from .config import Config
from .db import DB
from .obsidian import ObsidianSync

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, db: DB, cfg: Config):
        self.db, self.cfg = db, cfg
        self.leads = LeadBot(db, cfg)
        self.billing = BillingBot(db, cfg)
        self.builder = BuilderBot(db, cfg)
        self.deployer = DeployBot(db, cfg)
        self.outreach = OutreachBot(db, cfg)
        self.monitor = MonitorBot(db, cfg)
        self.obsidian = ObsidianSync(db, cfg)

    def onboard_paid_clients(self) -> int:
        """Rebuild paid sites without the preview banner / noindex, redeploy, mark client."""
        paid = self.db.leads_by_status("paid")
        for row in paid:
            self.builder.build(dict(row), preview=False)
        if paid:
            self.deployer.run(statuses=("paid",), next_status="client")
            for row in paid:
                self.db.log("orchestrator", "onboarded", row["business_name"], row["id"])
        return len(paid)

    def _step(self, name: str, fn, *args):
        try:
            result = fn(*args)
            log.info("%-10s -> %s", name, result)
            return result
        except Exception as exc:  # one failing bot must never stop the others
            log.exception("%s failed", name)
            self.db.log("orchestrator", "error", f"{name}: {exc}")
            return None

    def run_once(self) -> dict:
        summary = {}
        if self.cfg.obsidian_enabled:  # apply do_not_contact flags BEFORE any email goes out
            summary["obsidian_pull"] = self._step("obsidian<", self.obsidian.pull)
        summary |= {
            "leads": self._step("leads", self.leads.run),
            "links": self._step("links", self.billing.create_links),
            "built": self._step("build", self.builder.run),
            "deployed": self._step("deploy", self.deployer.run),
            "replies": self._step("inbox", self.outreach.check_inbox),
            "emailed": self._step("outreach", self.outreach.run),
            "paid": self._step("payments", self.billing.check_payments),
            "onboarded": self._step("onboard", self.onboard_paid_clients),
            "down": self._step("monitor", self.monitor.run),
        }
        if self.cfg.obsidian_enabled:
            summary["obsidian_notes"] = self._step("obsidian>", self.obsidian.push)
        summary["metrics"] = self.db.metrics(self.cfg.monthly_fee_cents)
        return summary

    def run_forever(self, interval_minutes: int = 60) -> None:
        log.info("Orchestrator started (dry_run=%s, every %d min)", self.cfg.dry_run, interval_minutes)
        while True:
            self.run_once()
            time.sleep(interval_minutes * 60)
