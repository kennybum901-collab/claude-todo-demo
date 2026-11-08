import json
import sqlite3
import sys
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agency.config import Config
from agency.db import DB
from agency.supervisor import Alerter, Supervisor, backup_database


@pytest.fixture
def cfg(tmp_path):
    c = replace(Config(), db_path=tmp_path / "data" / "t.db", senders_file=tmp_path / "none.json",
                alert_webhook_url="", alert_email="", run_every_minutes=1, log_dir=tmp_path / "logs")
    DB(c.db_path)
    return c


def quiet_alert(cfg):
    return Alerter(cfg, cooldown=0)


def health_server(payload):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            body = json.dumps(payload()).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}/health"


SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
CRASHER = [sys.executable, "-c", "raise SystemExit(3)"]


def test_crash_is_restarted_with_backoff(cfg):
    alert = quiet_alert(cfg)
    sup = Supervisor(cfg, CRASHER, check_every=0.05, startup_grace=0, max_backoff=0.2, alert=alert)
    sup.start_child()
    sup.child.wait()
    assert sup.check() == "exited"
    sup.restart()
    sup.child.wait()
    sup.restart()
    assert sup.restarts == 2 and sup.backoff == 0.2      # 1 -> capped at max_backoff
    sup.stop_child()
    assert any("exited with code 3" in m for m in alert.sent)


def test_hung_server_detected_after_repeated_health_failures(cfg):
    sup = Supervisor(cfg, SLEEPER, health_url="http://127.0.0.1:9/health", startup_grace=0,
                     health_failures=3, alert=quiet_alert(cfg))
    sup.start_child()
    try:
        assert [sup.check() for _ in range(3)] == ["health_miss", "health_miss", "unhealthy"]
    finally:
        sup.stop_child()
    assert sup.child.poll() is not None                   # killed


def test_stale_heartbeat_triggers_restart(cfg):
    beat = {"v": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()}
    httpd, url = health_server(lambda: {"ok": True, "last_heartbeat": beat["v"]})
    sup = Supervisor(cfg, SLEEPER, health_url=url, startup_grace=0, alert=quiet_alert(cfg))
    sup.start_child()
    try:
        assert sup.check() == "stale"                     # 10 min old > 3 x 1 min
        beat["v"] = datetime.now(timezone.utc).isoformat()
        assert sup.check() == "ok"
    finally:
        sup.stop_child()
        httpd.shutdown()


def test_startup_grace_period(cfg):
    sup = Supervisor(cfg, SLEEPER, health_url="http://127.0.0.1:9/health", startup_grace=60,
                     alert=quiet_alert(cfg))
    sup.start_child()
    try:
        assert sup.check() == "starting"
    finally:
        sup.stop_child()


def test_run_loop_restarts_and_stops(cfg):
    sup = Supervisor(cfg, CRASHER, check_every=0.05, startup_grace=0, max_backoff=0.01,
                     alert=quiet_alert(cfg))
    sup.backoff = 0.01
    sup.run(max_ticks=4)
    assert sup.restarts >= 2
    assert list((cfg.db_path.parent / "backups").glob("agency-*.db"))   # housekeeping ran on tick 1


def test_alert_cooldown_and_webhook(cfg):
    posted = []
    alert = Alerter(replace(cfg, alert_webhook_url="https://hooks.slack.example/x"), cooldown=900,
                    opener=lambda req, timeout: posted.append(json.loads(req.data)))
    assert alert("crash", "boom") and not alert("crash", "boom again")   # deduplicated
    assert alert("disk", "low disk")
    assert len(posted) == 2 and "boom" in posted[0]["text"] and posted[0]["content"] == posted[0]["text"]
    ntfy = []
    Alerter(replace(cfg, alert_webhook_url="https://ntfy.sh/topic"),
            opener=lambda req, timeout: ntfy.append(req.data))("x", "hello")
    assert ntfy and ntfy[0].endswith(b"hello")


def test_backup_and_retention(cfg):
    db = DB(cfg.db_path)
    db.add_lead(business_name="Kept", source_id="k")
    folder = cfg.db_path.parent / "backups"
    old = backup_database(cfg, today="2026-01-01")
    new = backup_database(cfg, today="2026-02-01")       # 31 days later, keep 14
    assert new.exists() and not old.exists()
    with sqlite3.connect(new) as c:
        assert c.execute("SELECT business_name FROM leads").fetchone()[0] == "Kept"
    assert backup_database(cfg, today="2026-02-01") == new and len(list(folder.glob("*.db"))) == 1
