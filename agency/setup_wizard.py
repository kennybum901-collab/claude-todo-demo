"""`python -m agency setup`: guided setup that writes .env for you.

Asks plain-language questions, keeps anything already in .env as the default
(press Enter to keep it), generates secrets, backs up the old .env, writes the
new one readable only by you (chmod 600), and finishes by running the doctor.
Always leaves DRY_RUN=1: going live is a deliberate, separate step.
"""
from __future__ import annotations

import getpass
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import ROOT

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

EMAIL_PRESETS = {
    "1": ("Gmail / Google Workspace", "smtp.gmail.com", "587", "imap.gmail.com",
          "Use an App Password: Google Account > Security > 2-Step Verification > App passwords."),
    "2": ("Microsoft 365 / Outlook", "smtp.office365.com", "587", "outlook.office365.com",
          "SMTP AUTH must be enabled for the mailbox in the Microsoft 365 admin center."),
    "3": ("Zoho Mail", "smtp.zoho.com", "587", "imap.zoho.com", "Use an app-specific password if 2FA is on."),
    "4": ("Other / custom", "", "587", "", ""),
}

# (section title, [(KEY, ...)]) - the order .env is written in
LAYOUT = [
    ("Company (shown on every site and email)", ["COMPANY_NAME", "OWNER_NAME", "COMPANY_EMAIL", "COMPANY_ADDRESS", "COMPANY_URL"]),
    ("Pricing (cents)", ["SETUP_FEE_CENTS", "MONTHLY_FEE_CENTS", "CURRENCY"]),
    ("Safety", ["DRY_RUN", "DAILY_EMAIL_CAP"]),
    ("Leads", ["LEAD_QUERIES", "GOOGLE_PLACES_API_KEY"]),
    ("Sending email", ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "IMAP_HOST"]),
    ("Daily review & alerts", ["DIGEST_EMAIL", "ALERT_EMAIL", "ALERT_WEBHOOK_URL"]),
    ("Server", ["PUBLIC_URL", "APP_SECRET", "ADMIN_PASSWORD"]),
    ("Payments, hosting, AI", ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "NETLIFY_TOKEN", "ANTHROPIC_API_KEY"]),
    ("Optional extras", ["PEXELS_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM"]),
]
SECRET_KEYS = {"SMTP_PASSWORD", "APP_SECRET", "ADMIN_PASSWORD", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET",
               "NETLIFY_TOKEN", "ANTHROPIC_API_KEY", "GOOGLE_PLACES_API_KEY", "PEXELS_API_KEY", "TWILIO_AUTH_TOKEN"}


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def render_env(values: dict[str, str]) -> str:
    lines = ["# Written by `python -m agency setup` on " + datetime.now().strftime("%Y-%m-%d %H:%M"),
             "# Re-run setup any time; it keeps these values as defaults.", ""]
    written = set()
    for title, keys in LAYOUT:
        present = [k for k in keys if values.get(k) not in (None, "")]
        if not present:
            continue
        lines.append(f"# ---- {title} ----")
        for k in present:
            v = values[k]
            lines.append(f'{k}="{v}"' if (" " in v or "#" in v or "|" in v or ";" in v) else f"{k}={v}")
            written.add(k)
        lines.append("")
    extra = {k: v for k, v in values.items() if k not in written and v not in (None, "")}
    if extra:
        lines.append("# ---- Other settings (kept from your previous .env) ----")
        lines += [f'{k}="{v}"' if " " in v else f"{k}={v}" for k, v in sorted(extra.items())]
        lines.append("")
    return "\n".join(lines)


def mask(v: str) -> str:
    return "" if not v else ("•" * 4 + v[-4:] if len(v) > 8 else "•" * len(v))


class Wizard:
    def __init__(self, env_path: Path | None = None, *, ask_fn: Callable[[str], str] = input,
                 secret_fn: Callable[[str], str] = getpass.getpass, out: Callable[[str], None] = print):
        self.path = env_path or (ROOT / ".env")
        self.ask_fn, self.secret_fn, self.out = ask_fn, secret_fn, out
        self.v = read_env(self.path)

    # ---------- prompting ----------
    def ask(self, key: str, question: str, *, default: str = "", validate=None, secret: bool = False,
            hint: str = "", optional: bool = False) -> str:
        current = self.v.get(key) or default
        if hint:
            self.out(f"  ({hint})")
        while True:
            shown = mask(current) if secret else current
            suffix = f" [{shown}]" if shown else (" [skip]" if optional else "")
            raw = (self.secret_fn if secret else self.ask_fn)(f"{question}{suffix}: ").strip()
            value = raw or current
            if not value and optional:
                self.v.pop(key, None)
                return ""
            if not value:
                self.out("  This one is needed.")
                continue
            err = validate(value) if validate else None
            if err:
                self.out(f"  {err}")
                continue
            self.v[key] = value
            return value

    def yes(self, question: str, default: bool = False) -> bool:
        raw = self.ask_fn(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not raw else raw.startswith("y")

    # ---------- validators ----------
    @staticmethod
    def _email(v):
        return None if EMAIL_RE.match(v) else "That doesn't look like an email address."

    @staticmethod
    def _https(v):
        return None if v.startswith("https://") else "Must start with https://"

    @staticmethod
    def _money(v):
        return None if re.fullmatch(r"\d+(\.\d{1,2})?", v) else "Enter a number like 499 or 49.00"

    @staticmethod
    def _real(v):
        return "Please enter your real details (not the example placeholder)." if (
            "example.com" in v or "123 Main St" in v or "Anytown" in v) else None

    # ---------- sections ----------
    def company(self):
        self.out("\n1/6  Your business")
        self.ask("COMPANY_NAME", "Business name")
        self.ask("OWNER_NAME", "Your first name (signs the emails)")
        self.ask("COMPANY_EMAIL", "Business email",
                 validate=lambda v: self._email(v) or self._real(v))
        self.ask("COMPANY_ADDRESS", "Postal address (required by anti-spam law, shown in email footers)",
                 validate=self._real)
        self.ask("COMPANY_URL", "Your website", validate=lambda v: self._https(v) or self._real(v))

    def pricing(self):
        self.out("\n2/6  Pricing")
        to_dollars = lambda c: f"{int(c) / 100:g}" if c and c.isdigit() else ""  # noqa: E731
        for key, q, d in (("SETUP_FEE_CENTS", "One-time setup fee in dollars", "499"),
                          ("MONTHLY_FEE_CENTS", "Monthly plan in dollars", "49")):
            cur = to_dollars(self.v.get(key, "")) or d
            self.v.pop(key, None)
            dollars = self.ask(key, q, default=cur, validate=self._money)
            self.v[key] = str(round(float(dollars) * 100))
        self.v.setdefault("CURRENCY", "usd")
        self.v["DRY_RUN"] = "1"   # always: going live is a separate, deliberate step
        self.v.setdefault("DAILY_EMAIL_CAP", "30")

    def leads(self):
        self.out("\n3/6  Who you'll sell to")
        current = self.v.get("LEAD_QUERIES", "")
        pairs = [q.split("|", 1) for q in current.split(";") if "|" in q]
        niches = list(dict.fromkeys(a for a, _ in pairs))   # keep order, drop duplicates
        cities = list(dict.fromkeys(b for _, b in pairs))
        n = self.ask_fn(f"Types of business, comma-separated [{', '.join(niches) or 'roofer, plumber'}]: ").strip()
        c = self.ask_fn(f"Cities, separated by ';' (e.g. Austin, TX; Dallas, TX) [{'; '.join(cities) or 'Austin, TX'}]: ").strip()
        if not n and not c and current:
            self.out(f"  → keeping your {len(pairs)} existing searches")   # exactly as they were
        else:
            niches = [x.strip() for x in n.split(",") if x.strip()] if n else (niches or ["roofer", "plumber"])
            cities = [x.strip() for x in c.split(";") if x.strip()] if c else (cities or ["Austin, TX"])
            self.v["LEAD_QUERIES"] = ";".join(f"{a}|{b}" for a in niches for b in cities)
            self.out(f"  → {len(niches) * len(cities)} searches")
        self.ask("GOOGLE_PLACES_API_KEY", "Google Places API key (finds businesses automatically)", secret=True,
                 optional=True, hint="console.cloud.google.com > enable 'Places API (New)' > Credentials. "
                                     "Skip to import leads from a CSV instead.")

    def email(self):
        self.out("\n4/6  Sending email (use a SEPARATE domain from your main one, e.g. get-yourbrand.com)")
        if self.v.get("SMTP_HOST") and not self.yes(f"Change email settings (currently {self.v['SMTP_HOST']})?"):
            pass
        else:
            for k, (label, *_r) in EMAIL_PRESETS.items():
                self.out(f"  {k}) {label}")
            choice = self.ask_fn("Provider [1]: ").strip() or "1"
            label, smtp, port, imap, tip = EMAIL_PRESETS.get(choice, EMAIL_PRESETS["4"])
            if tip:
                self.out(f"  Tip: {tip}")
            if smtp:
                self.v["SMTP_HOST"] = smtp   # preset fills it in; Enter accepts
            self.ask("SMTP_HOST", "SMTP server", default=smtp)
            self.ask("SMTP_PORT", "SMTP port", default=port, validate=lambda v: None if v.isdigit() else "A number, usually 587")
            self.ask("SMTP_USER", "Sending address / username", validate=self._email)
            self.ask("SMTP_PASSWORD", "Password or app password", secret=True)
            self.ask("IMAP_HOST", "IMAP server (reads replies, opt-outs, bounces)", default=imap, optional=True)
        self.ask("DIGEST_EMAIL", "Where should the daily review email go?",
                 default=self.v.get("COMPANY_EMAIL", ""), validate=self._email)
        self.v.setdefault("ALERT_EMAIL", self.v["DIGEST_EMAIL"])
        self.ask("ALERT_WEBHOOK_URL", "Phone alerts URL (Slack/Discord webhook or https://ntfy.sh/<secret-topic>)",
                 optional=True, validate=self._https)

    def server(self):
        self.out("\n5/6  Your server")
        self.ask("PUBLIC_URL", "Public address of the bots server (e.g. https://bots.yourbrand.com)",
                 optional=True, validate=self._https,
                 hint="Skip if you haven't set up the server yet; you can re-run setup later.")
        if len(self.v.get("APP_SECRET", "")) < 32:
            self.v["APP_SECRET"] = secrets.token_hex(32)
            self.out("  ✓ Generated APP_SECRET")
        if not self.v.get("ADMIN_PASSWORD"):
            self.v["ADMIN_PASSWORD"] = secrets.token_urlsafe(18)
            self.out(f"  ✓ Generated the /admin dashboard password: {self.v['ADMIN_PASSWORD']}  (user: admin) "
                     "- save it in your password manager")

    def services(self):
        self.out("\n6/6  Connections (Enter to skip any; doctor will tell you what each enables)")
        self.ask("STRIPE_SECRET_KEY", "Stripe secret key (start with sk_test_)", secret=True, optional=True,
                 hint="dashboard.stripe.com > Developers > API keys",
                 validate=lambda v: None if v.startswith(("sk_", "rk_")) else "Stripe keys start with sk_ or rk_")
        if self.v.get("STRIPE_SECRET_KEY") and self.v.get("PUBLIC_URL"):
            self.ask("STRIPE_WEBHOOK_SECRET", "Stripe webhook signing secret (whsec_...)", secret=True, optional=True,
                     hint=f"Stripe > Developers > Webhooks > Add endpoint {self.v['PUBLIC_URL']}/stripe/webhook "
                          "(events: checkout.session.completed, customer.subscription.deleted, invoice.payment_failed)",
                     validate=lambda v: None if v.startswith("whsec_") else "Starts with whsec_")
        self.ask("NETLIFY_TOKEN", "Netlify personal access token", secret=True, optional=True,
                 hint="app.netlify.com > User settings > Applications > Personal access tokens")
        self.ask("ANTHROPIC_API_KEY", "Claude API key (written copy, reply drafts, site edits)", secret=True,
                 optional=True, hint="Claude Console > API keys")
        if self.yes("Set up optional extras (stock photos, text messages)?"):
            self.ask("PEXELS_API_KEY", "Pexels API key (free stock photos)", secret=True, optional=True,
                     hint="pexels.com/api")
            if self.ask("TWILIO_ACCOUNT_SID", "Twilio Account SID (AC...)", optional=True,
                        hint="console.twilio.com; US texting also needs A2P 10DLC registration"):
                self.ask("TWILIO_AUTH_TOKEN", "Twilio Auth Token", secret=True)
                self.ask("TWILIO_FROM", "Twilio number (+1...) or Messaging Service SID (MG...)")

    # ---------- run ----------
    def save(self) -> Path | None:
        backup = None
        if self.path.exists():
            backup = self.path.with_name(f".env.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(self.path, backup)
        self.path.write_text(render_env(self.v))
        try:
            os.chmod(self.path, 0o600)  # secrets inside: owner read/write only
        except OSError:
            pass
        return backup

    def run(self) -> Path | None:
        self.out("Agency setup: answer a few questions; press Enter to keep the value in [brackets].")
        for step in (self.company, self.pricing, self.leads, self.email, self.server, self.services):
            step()
        backup = self.save()
        self.out(f"\n✓ Saved {self.path}" + (f" (previous version backed up to {backup.name})" if backup else ""))
        self.out("  DRY_RUN=1 is on: nothing will be sent, deployed or charged until you change it.")
        return backup
