"""`python -m agency doctor`: pre-launch health check.

Checks configuration, then (unless --offline) tests every connection with a
harmless read-only call: SMTP/IMAP login, Stripe balance, Netlify user,
Anthropic model lookup, Google Places 1-result search, Twilio account,
Pexels 1-photo search, and your PUBLIC_URL /health. Nothing is sent, deployed
or charged. Ends with a ready/blocked verdict per feature and the fix for
every problem. Exit code 1 if anything blocks going live.
"""
from __future__ import annotations

import imaplib
import importlib.util
import json
import shutil
import smtplib
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .config import Config
from .mailer import load_senders

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
ICON = {OK: "✅", WARN: "⚠️ ", FAIL: "❌", SKIP: "➖"}
PLACEHOLDERS = ("example.com", "123 Main St", "Anytown")


@dataclass
class Check:
    area: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, *a, **k) -> Check:
        c = Check(*a, **k)
        self.checks.append(c)
        return c

    def status_of(self, area: str) -> str:
        sts = [c.status for c in self.checks if c.area == area]
        return FAIL if FAIL in sts else WARN if WARN in sts else OK if OK in sts else SKIP

    @property
    def blocking(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]


class Doctor:
    def __init__(self, cfg: Config, *, offline: bool = False, http: requests.Session | None = None,
                 smtp_factory=None, imap_factory=None, anthropic_client=None):
        self.cfg, self.offline = cfg, offline
        self.http = http or requests.Session()
        self.smtp_factory = smtp_factory or smtplib.SMTP
        self.imap_factory = imap_factory or imaplib.IMAP4_SSL
        self.anthropic_client = anthropic_client
        self.r = Report()

    # ---------- helpers ----------
    def _get(self, url: str, **kw):
        return self.http.get(url, timeout=15, **kw)

    def _net(self, area: str, name: str, fn, fix: str) -> None:
        if self.offline:
            self.r.add(area, name, SKIP, "skipped (--offline)")
            return
        try:
            detail = fn()
            self.r.add(area, name, OK, detail or "connected")
        except Exception as exc:  # any failure is reported, never raised
            msg = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
            self.r.add(area, name, FAIL, msg, fix)

    # ---------- checks ----------
    def basics(self) -> None:
        c = self.cfg
        bad = [n for n, v in (("COMPANY_EMAIL", c.company_email), ("COMPANY_ADDRESS", c.company_address),
                              ("COMPANY_URL", c.company_url)) if any(p in v for p in PLACEHOLDERS)]
        if bad:
            self.r.add("Company", "Identity", FAIL, f"placeholder values: {', '.join(bad)}",
                       "Set your real business email, postal address and website in .env (required in every email by CAN-SPAM).")
        else:
            self.r.add("Company", "Identity", OK, f"{c.company_name}, {c.company_address}")
        self.r.add("Company", "Mode", WARN if c.dry_run else OK,
                   "DRY_RUN=1: nothing is sent, deployed or charged" if c.dry_run else "LIVE (DRY_RUN=0)",
                   "Set DRY_RUN=0 when this report is clean and you've read a few daily reviews." if c.dry_run else "")
        for mod, needed in (("jinja2", True), ("requests", True), ("PIL", True), ("anthropic", False)):
            present = importlib.util.find_spec(mod) is not None
            self.r.add("System", f"Python package {mod}", OK if present else (FAIL if needed else WARN),
                       "installed" if present else "missing", "pip install -r requirements.txt")
        for label, path in (("Database folder", Path(c.db_path).parent), ("Sites folder", Path(c.build_dir))):
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".doctor"
                probe.write_text("ok")
                probe.unlink()
                self.r.add("System", label, OK, str(path))
            except OSError as exc:
                self.r.add("System", label, FAIL, str(exc), f"Make {path} writable.")
        free = shutil.disk_usage(Path(c.db_path).parent).free // (1024 * 1024)
        self.r.add("System", "Disk space", OK if free >= c.min_free_disk_mb else WARN, f"{free:,} MB free",
                   "Free up disk space or grow the volume.")

    def secrets(self) -> None:
        c = self.cfg
        if not c.public_url:
            self.r.add("Server", "PUBLIC_URL", WARN, "not set",
                       "Set PUBLIC_URL to https://your-domain to enable the quote form, unsubscribe links, "
                       "referrals, Review Booster and Stripe webhooks.")
        elif not c.public_url.startswith("https://"):
            self.r.add("Server", "PUBLIC_URL", FAIL, c.public_url, "PUBLIC_URL must start with https://")
        else:
            self._net("Server", "PUBLIC_URL /health", self._health, "Is `agency serve` running behind HTTPS at that address?")
        self.r.add("Server", "APP_SECRET", OK if len(c.app_secret) >= 32 else (FAIL if c.public_url else WARN),
                   "set" if len(c.app_secret) >= 32 else "missing or shorter than 32 characters",
                   'python -c "import secrets;print(secrets.token_hex(32))"  -> APP_SECRET')
        if c.admin_password:
            self.r.add("Server", "ADMIN_PASSWORD", OK if len(c.admin_password) >= 12 else WARN,
                       "set" if len(c.admin_password) >= 12 else "shorter than 12 characters", "Use a longer password for /admin.")
        if c.stripe_key:
            if not c.stripe_webhook_secret:
                self.r.add("Payments", "Webhook secret", WARN, "STRIPE_WEBHOOK_SECRET not set (hourly polling still works)",
                           "Stripe > Developers > Webhooks > add https://<PUBLIC_URL>/stripe/webhook, copy the signing secret.")
            elif not c.stripe_webhook_secret.startswith("whsec_"):
                self.r.add("Payments", "Webhook secret", FAIL, "doesn't start with whsec_", "Copy the endpoint's signing secret.")
            else:
                self.r.add("Payments", "Webhook secret", OK, "set")

    def _health(self) -> str:
        r = self._get(f"{self.cfg.public_url}/health")
        r.raise_for_status()
        data = r.json()
        return f"reachable, last heartbeat {data.get('last_heartbeat') or 'none yet'}"

    def email(self) -> None:
        senders = load_senders(self.cfg)
        live = [s for s in senders if s.smtp_host]
        if not live:
            self.r.add("Email", "Sending inbox", FAIL, "no SMTP settings or senders.json",
                       "Set SMTP_HOST/SMTP_USER/SMTP_PASSWORD (or senders.json) for a secondary sending domain.")
            return
        if Path(self.cfg.senders_file).is_file():
            self.r.add("Email", "senders.json", OK, f"{len(senders)} inbox(es)")
        for s in live:
            if any(p in s.email for p in PLACEHOLDERS):
                self.r.add("Email", s.email, FAIL, "placeholder address", "Use a real sending address.")
                continue
            if not s.password:
                self.r.add("Email", f"{s.email} password", FAIL, "empty",
                           "Set the password (or the env var named by password_env).")
                continue

            def smtp_login(s=s):
                conn = self.smtp_factory(s.smtp_host, s.smtp_port, timeout=15)
                try:
                    conn.starttls()
                    conn.login(s.smtp_user, s.password)
                finally:
                    try:
                        conn.quit()
                    except Exception:
                        pass
                return f"SMTP login ok ({s.smtp_host}), cap {s.daily_cap}/day"

            self._net("Email", f"{s.email} sending", smtp_login, "Check SMTP host/port/user/app-password.")
            if s.imap_host:
                def imap_login(s=s):
                    with self.imap_factory(s.imap_host) as imap:
                        imap.login(s.smtp_user, s.password)
                    return f"IMAP login ok ({s.imap_host})"
                self._net("Email", f"{s.email} inbox (replies)", imap_login, "Check IMAP host and credentials.")
            else:
                self.r.add("Email", f"{s.email} inbox (replies)", WARN, "no IMAP host: replies, opt-outs and bounces won't be read",
                           "Set IMAP_HOST (or imap_host in senders.json).")
        if not (self.cfg.digest_email or self.cfg.alert_email):
            self.r.add("Email", "Daily review / alerts recipient", WARN, f"falls back to {self.cfg.company_email}",
                       "Set DIGEST_EMAIL and ALERT_EMAIL (or ALERT_WEBHOOK_URL for phone push).")

    def services(self) -> None:
        c = self.cfg
        if c.stripe_key:
            mode = "TEST mode" if c.stripe_key.startswith("sk_test_") else "LIVE mode" if c.stripe_key.startswith(("sk_live_", "rk_live_")) else "unknown key type"

            def stripe():
                r = self._get("https://api.stripe.com/v1/balance", auth=(c.stripe_key, ""))
                r.raise_for_status()
                return f"key works ({mode})"
            self._net("Payments", "Stripe", stripe, "Check STRIPE_SECRET_KEY in Stripe > Developers > API keys.")
            if mode == "TEST mode" and not c.dry_run:
                self.r.add("Payments", "Stripe mode", WARN, "live outreach with a TEST key: nobody can actually pay",
                           "Switch to your sk_live_ key when you go live.")
        else:
            self.r.add("Payments", "Stripe", FAIL, "STRIPE_SECRET_KEY not set", "Add your Stripe secret key (start with sk_test_).")
        if c.netlify_token:
            def netlify():
                r = self._get("https://api.netlify.com/api/v1/user", headers={"Authorization": f"Bearer {c.netlify_token}"})
                r.raise_for_status()
                return f"token works ({r.json().get('email', 'account')})"
            self._net("Hosting", "Netlify", netlify, "Create a personal access token in Netlify user settings.")
        else:
            self.r.add("Hosting", "Netlify", FAIL, "NETLIFY_TOKEN not set: previews stay local files and no email will be sent",
                       "Add a Netlify personal access token.")
        if c.use_ai_copy:
            def claude():
                client = self.anthropic_client
                if client is None:
                    import anthropic
                    client = anthropic.Anthropic()
                m = client.models.retrieve(c.anthropic_model)
                return f"API key works, model {getattr(m, 'id', c.anthropic_model)} available"
            self._net("AI copy", "Anthropic", claude, "Check ANTHROPIC_API_KEY and ANTHROPIC_MODEL.")
        else:
            self.r.add("AI copy", "Anthropic", WARN, "no ANTHROPIC_API_KEY: template copy, edits need a human",
                       "Optional: add ANTHROPIC_API_KEY for written copy, reply drafts and automatic site edits.")
        if c.google_places_key:
            def places():
                r = self.http.post("https://places.googleapis.com/v1/places:searchText", timeout=15,
                                   json={"textQuery": "plumber in Austin, TX", "pageSize": 1},
                                   headers={"X-Goog-Api-Key": c.google_places_key, "X-Goog-FieldMask": "places.id"})
                r.raise_for_status()
                return f"key works; {len(c.lead_queries)} search(es) configured"
            self._net("Leads", "Google Places", places, "Enable 'Places API (New)' for the key in Google Cloud.")
        else:
            self.r.add("Leads", "Google Places", WARN, "not set: leads only via import-csv",
                       "Optional: add GOOGLE_PLACES_API_KEY to find leads automatically.")
        if c.twilio_account_sid or c.twilio_auth_token or c.twilio_from:
            if not (c.twilio_account_sid and c.twilio_auth_token and c.twilio_from):
                self.r.add("Texts", "Twilio", FAIL, "partially configured",
                           "Set all of TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM (or none).")
            else:
                def twilio():
                    r = self._get(f"https://api.twilio.com/2010-04-01/Accounts/{c.twilio_account_sid}.json",
                                  auth=(c.twilio_account_sid, c.twilio_auth_token))
                    r.raise_for_status()
                    return f"account {r.json().get('status', 'ok')}; remember A2P 10DLC registration"
                self._net("Texts", "Twilio", twilio, "Check the Account SID and Auth Token in the Twilio console.")
        if c.pexels_api_key:
            def pexels():
                r = self._get("https://api.pexels.com/v1/search", params={"query": "plumber", "per_page": 1},
                              headers={"Authorization": c.pexels_api_key})
                r.raise_for_status()
                return "key works"
            self._net("Photos", "Pexels", pexels, "Check PEXELS_API_KEY.")

    # ---------- verdict ----------
    FEATURES = {
        "Find leads": ("Leads",),
        "Build & host sites": ("System", "Hosting"),
        "Email outreach": ("Company", "Email", "Hosting", "Payments"),
        "Take payments": ("Payments",),
        "Quote form, referrals, unsubscribe links, webhooks": ("Server",),
        "Text messages": ("Texts",),
    }

    def run(self) -> Report:
        self.basics()
        self.secrets()
        self.email()
        self.services()
        return self.r

    def verdicts(self) -> list[tuple[str, str]]:
        out = []
        for feature, areas in self.FEATURES.items():
            sts = [self.r.status_of(a) for a in areas]
            st = FAIL if FAIL in sts else WARN if WARN in sts else SKIP if all(s == SKIP for s in sts) else OK
            out.append((feature, st))
        return out

    def render(self) -> str:
        lines, area = [], None
        for c in self.r.checks:
            if c.area != area:
                area = c.area
                lines.append(f"\n{area}")
            lines.append(f"  {ICON[c.status]} {c.name}: {c.detail}")
            if c.fix and c.status in (FAIL, WARN):
                lines.append(f"       → {c.fix}")
        lines.append("\nREADINESS")
        labels = {OK: "ready", WARN: "works, with warnings", FAIL: "BLOCKED", SKIP: "not checked / not set up"}
        for feature, st in self.verdicts():
            lines.append(f"  {ICON[st]} {feature}: {labels[st]}")
        n = len(self.r.blocking)
        lines.append("\n" + ("✅ Nothing blocks going live." if not n else
                             f"❌ {n} problem(s) to fix before going live (see → lines above)."))
        return "\n".join(lines).lstrip("\n")

    def as_json(self) -> str:
        return json.dumps({"checks": [c.__dict__ for c in self.r.checks],
                           "verdicts": dict(self.verdicts()), "blocking": len(self.r.blocking)}, indent=2)
