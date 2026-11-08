"""Central configuration, loaded from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    # --- Company identity (shown on every site footer and email) ---
    company_name: str = os.environ.get("COMPANY_NAME", "Northstar Web Studio")
    company_email: str = os.environ.get("COMPANY_EMAIL", "hello@example.com")
    company_address: str = os.environ.get("COMPANY_ADDRESS", "123 Main St, Anytown, USA")
    company_url: str = os.environ.get("COMPANY_URL", "https://example.com")
    owner_name: str = os.environ.get("OWNER_NAME", "Alex")

    # --- Pricing (cents) ---
    setup_fee_cents: int = _int("SETUP_FEE_CENTS", 49_900)       # $499 one-time
    monthly_fee_cents: int = _int("MONTHLY_FEE_CENTS", 4_900)    # $49/mo hosting + care
    currency: str = os.environ.get("CURRENCY", "usd")

    # --- Safety ---
    # DRY_RUN=1 (default): no emails are sent, nothing is deployed, nothing is charged.
    dry_run: bool = _bool("DRY_RUN", True)
    daily_email_cap: int = _int("DAILY_EMAIL_CAP", 30)
    followup_days: int = _int("FOLLOWUP_DAYS", 4)
    max_followups: int = _int("MAX_FOLLOWUPS", 2)
    # Multi-inbox sending (see senders.example.json); falls back to SMTP_* below
    senders_file: Path = Path(os.environ.get("SENDERS_FILE") or ROOT / "senders.json")
    bounce_pause_rate: float = float(os.environ.get("BOUNCE_PAUSE_RATE", "0.05"))
    reports_enabled: bool = _bool("REPORTS_ENABLED", True)
    report_day: int = _int("REPORT_DAY", 1)          # day of month monthly reports go out
    upsell_enabled: bool = _bool("UPSELL_ENABLED", True)
    upsell_after_days: int = _int("UPSELL_AFTER_DAYS", 60)
    upsell_gap_days: int = _int("UPSELL_GAP_DAYS", 30)

    # --- Storage ---
    db_path: Path = Path(os.environ.get("DB_PATH", ROOT / "data" / "agency.db"))
    build_dir: Path = Path(os.environ.get("BUILD_DIR", ROOT / "data" / "sites"))
    # Obsidian vault the bots write the CRM into (set to your own vault path)
    obsidian_vault: Path = Path(os.environ.get("OBSIDIAN_VAULT") or ROOT / "vault")
    obsidian_enabled: bool = _bool("OBSIDIAN_SYNC", True)

    # --- Server (python -m agency serve) ---
    public_url: str = os.environ.get("PUBLIC_URL", "").rstrip("/")    # e.g. https://bots.yourstudio.com
    port: int = _int("PORT", 8000)
    run_every_minutes: int = _int("RUN_EVERY_MINUTES", 60)
    stripe_webhook_secret: str = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    app_secret: str = os.environ.get("APP_SECRET", "")                 # signs unsubscribe links

    # --- 24/7 supervisor (python -m agency supervise) ---
    alert_webhook_url: str = os.environ.get("ALERT_WEBHOOK_URL", "")   # Slack / Discord / ntfy.sh
    alert_email: str = os.environ.get("ALERT_EMAIL", "")
    backup_keep_days: int = _int("BACKUP_KEEP_DAYS", 14)
    log_dir: Path = Path(os.environ.get("LOG_DIR") or ROOT / "data" / "logs")
    min_free_disk_mb: int = _int("MIN_FREE_DISK_MB", 500)

    # --- Integrations (all optional; bots degrade gracefully without them) ---
    google_places_key: str = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")
    use_ai_copy: bool = _bool("USE_AI_COPY", bool(os.environ.get("ANTHROPIC_API_KEY")))
    netlify_token: str = os.environ.get("NETLIFY_TOKEN", "")
    stripe_key: str = os.environ.get("STRIPE_SECRET_KEY", "")
    smtp_host: str = os.environ.get("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.environ.get("SMTP_USER", "")
    smtp_password: str = os.environ.get("SMTP_PASSWORD", "")
    imap_host: str = os.environ.get("IMAP_HOST", "")

    # Search targets for the lead bot: "niche|city" pairs separated by ';'
    lead_queries: list[str] = field(
        default_factory=lambda: [
            q.strip()
            for q in os.environ.get(
                "LEAD_QUERIES", "plumber|Austin, TX;roofer|Austin, TX;hair salon|Austin, TX"
            ).split(";")
            if q.strip()
        ]
    )


config = Config()
