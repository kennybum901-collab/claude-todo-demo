"""24/7 supervisor: `python -m agency supervise`.

Keeps the whole business running unattended:

  * starts `python -m agency serve` as a child process and restarts it when it
    exits, with exponential backoff (1s -> 5 min) that resets once it's stable
  * restarts it when it *hangs*: /health stops answering, or the bot pipeline
    hasn't completed a run (heartbeat) in 3x RUN_EVERY_MINUTES
  * alerts you on crash, hang, recovery and low disk via ALERT_WEBHOOK_URL
    (Slack, Discord or ntfy.sh, i.e. push to your phone) and/or ALERT_EMAIL
  * backs the SQLite database up daily (online, consistent copy), keeping
    BACKUP_KEEP_DAYS days
  * writes rotating log files to LOG_DIR
  * forwards SIGTERM/SIGINT so `docker stop` / `systemctl stop` shut down cleanly

Run it under Docker (`restart: unless-stopped`) or systemd (`Restart=always`)
so the supervisor itself is restarted if the machine reboots.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config

log = logging.getLogger("agency.supervisor")


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(cfg.log_dir / "agency.log", maxBytes=5_000_000,
                                                  backupCount=5, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------
class Alerter:
    """Sends ops alerts; the same message is sent at most once per `cooldown` seconds."""

    def __init__(self, cfg: Config, cooldown: int = 900, opener=urllib.request.urlopen):
        self.cfg, self.cooldown, self.opener = cfg, cooldown, opener
        self.last_sent: dict[str, float] = {}
        self.sent: list[str] = []

    def __call__(self, key: str, message: str) -> bool:
        t = time.monotonic()
        if t - self.last_sent.get(key, -1e9) < self.cooldown:
            return False
        self.last_sent[key] = t
        text = f"[{self.cfg.company_name} bots] {message}"
        log.warning("ALERT: %s", message)
        self.sent.append(text)
        if self.cfg.alert_webhook_url:
            try:
                url = self.cfg.alert_webhook_url
                if "ntfy" in url:   # ntfy.sh takes a plain-text body
                    req = urllib.request.Request(url, data=text.encode(), method="POST")
                else:               # Slack reads "text", Discord reads "content"
                    req = urllib.request.Request(
                        url, data=json.dumps({"text": text, "content": text}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST")
                self.opener(req, timeout=10)
            except Exception as exc:
                log.error("alert webhook failed: %s", exc)
        if self.cfg.alert_email:
            try:
                self._email(text)
            except Exception as exc:
                log.error("alert email failed: %s", exc)
        return True

    def _email(self, text: str) -> None:
        from email.message import EmailMessage

        from .db import DB
        from .mailer import Mailer

        db = DB(self.cfg.db_path)
        mailer = Mailer(db, self.cfg)
        sender = mailer.notify_sender(to_owner=True)
        if sender is None:
            return
        msg = EmailMessage()
        msg["Subject"] = text[:120]
        msg["To"] = self.cfg.alert_email
        msg.set_content(text + "\n")
        try:
            mailer.send(sender, msg, bot="supervisor", lead_id=None, detail="alert", to_owner=True)
        finally:
            mailer.close()


# ---------------------------------------------------------------------------
# backups & disk
# ---------------------------------------------------------------------------
def backup_database(cfg: Config, today: str | None = None) -> Path | None:
    src = Path(cfg.db_path)
    if not src.exists():
        return None
    today = today or datetime.now(timezone.utc).date().isoformat()
    folder = src.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"agency-{today}.db"
    if not dest.exists():
        with sqlite3.connect(src, timeout=30) as s, sqlite3.connect(dest) as d:
            s.backup(d)   # online backup: consistent even while bots are writing
        log.info("database backed up to %s", dest)
    cutoff = (datetime.fromisoformat(today) - timedelta(days=cfg.backup_keep_days)).date().isoformat()
    for old in folder.glob("agency-*.db"):
        if old.stem.removeprefix("agency-") < cutoff:
            old.unlink()
    return dest


def free_disk_mb(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free // (1024 * 1024)


# ---------------------------------------------------------------------------
# supervisor
# ---------------------------------------------------------------------------
class Supervisor:
    def __init__(self, cfg: Config, cmd: list[str] | None = None, *, health_url: str | None = None,
                 check_every: float = 60, health_failures: int = 3, startup_grace: float = 120,
                 stable_after: float = 600, max_backoff: float = 300, alert: Alerter | None = None):
        self.cfg = cfg
        self.cmd = cmd or [sys.executable, "-m", "agency", "serve"]
        self.health_url = health_url or f"http://127.0.0.1:{cfg.port}/health"
        self.check_every, self.health_failures = check_every, health_failures
        self.startup_grace, self.stable_after, self.max_backoff = startup_grace, stable_after, max_backoff
        self.alert = alert or Alerter(cfg)
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.restarts = 0
        self.backoff = 1.0

    # ---- child process ----
    def start_child(self) -> None:
        log.info("starting: %s", " ".join(self.cmd))
        self.child = subprocess.Popen(self.cmd)
        self.started_at = time.monotonic()
        self.failures = 0

    def stop_child(self, timeout: float = 20) -> None:
        if self.child and self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout)
            except subprocess.TimeoutExpired:
                self.child.kill()
                self.child.wait()

    # ---- health ----
    def health(self) -> dict | None:
        try:
            with urllib.request.urlopen(self.health_url, timeout=10) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def heartbeat_stale(self, data: dict) -> bool:
        beat = data.get("last_heartbeat")
        if not beat:
            return time.monotonic() - self.started_at > 3 * self.cfg.run_every_minutes * 60 + self.startup_grace
        age = datetime.now(timezone.utc) - datetime.fromisoformat(beat)
        return age > timedelta(minutes=3 * self.cfg.run_every_minutes) and \
            time.monotonic() - self.started_at > self.startup_grace

    def check(self) -> str:
        """One supervision tick. Returns what happened (for logs/tests)."""
        assert self.child is not None
        code = self.child.poll()
        if code is not None:
            self.alert("crash", f"server exited with code {code}; restarting in {self.backoff:.0f}s")
            return "exited"
        if time.monotonic() - self.started_at < self.startup_grace:
            return "starting"
        data = self.health()
        if data is None:
            self.failures += 1
            if self.failures >= self.health_failures:
                self.alert("hang", f"/health failed {self.failures}x in a row; restarting server")
                return "unhealthy"
            return "health_miss"
        self.failures = 0
        if self.heartbeat_stale(data):
            self.alert("stale", "bot pipeline hasn't completed a run in "
                                f"{3 * self.cfg.run_every_minutes} min; restarting server")
            return "stale"
        if self.restarts and time.monotonic() - self.started_at > self.stable_after:
            if self.backoff > 1:
                self.alert("recovered", f"server stable again after {self.restarts} restart(s)")
            self.backoff = 1.0
        return "ok"

    def housekeeping(self) -> None:
        try:
            backup_database(self.cfg)
        except Exception as exc:
            self.alert("backup", f"database backup failed: {exc}")
        free = free_disk_mb(Path(self.cfg.db_path).parent)
        if free < self.cfg.min_free_disk_mb:
            self.alert("disk", f"low disk space: {free} MB free")

    def restart(self) -> None:
        self.stop_child()
        if self.stopping:
            return
        time.sleep(self.backoff)
        self.backoff = min(self.backoff * 2, self.max_backoff)
        self.restarts += 1
        self.start_child()

    def handle_signal(self, signum, _frame) -> None:
        log.info("signal %s: shutting down", signum)
        self.stopping = True

    def run(self, max_ticks: int | None = None) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.handle_signal)
            except ValueError:  # not in main thread (tests)
                pass
        self.alert("start", "supervisor started")
        self.start_child()
        last_housekeeping: float | None = None  # run on the first tick, then hourly
        ticks = 0
        try:
            while not self.stopping and (max_ticks is None or ticks < max_ticks):
                ticks += 1
                if last_housekeeping is None or time.monotonic() - last_housekeeping > 3600:
                    self.housekeeping()
                    last_housekeeping = time.monotonic()
                result = self.check()
                if result in ("exited", "unhealthy", "stale"):
                    self.restart()
                    continue
                deadline = time.monotonic() + self.check_every
                while not self.stopping and time.monotonic() < deadline:
                    if self.child and self.child.poll() is not None:
                        break  # react to a crash immediately
                    time.sleep(min(1.0, self.check_every))
        finally:
            self.stop_child()
            log.info("supervisor stopped")


def main(cfg: Config) -> None:
    setup_logging(cfg)
    Supervisor(cfg).run()
