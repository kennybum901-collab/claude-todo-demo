"""Text messages via Twilio (used for review requests).

Compliance built in:
  * texts only go to customers the business says agreed to receive one
    (consent checkbox on the jobs page, stored with the request)
  * quiet hours: only between SMS_QUIET_START and SMS_QUIET_END in SMS_TIMEZONE
  * every text names the business and ends with "Reply STOP to opt out";
    STOP replies arrive at /twilio/sms (signature-verified) and suppress the number
  * US business texting needs A2P 10DLC registration in your Twilio console
In DRY_RUN, or without TWILIO_* settings, texts are logged and never sent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from .config import Config

STOP_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "optout", "revoke"}


def normalize_phone(raw: str | None, default_country: str = "1") -> str | None:
    """Best-effort E.164 (+15125550100). Returns None if it can't be a phone number."""
    if not raw:
        return None
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return f"+{digits}" if 8 <= len(digits) <= 15 else None
    if len(digits) == 10:
        return f"+{default_country}{digits}"
    if len(digits) == 11 and digits.startswith(default_country):
        return f"+{digits}"
    return None


def suppression_key(phone: str) -> str:
    """Phones share the suppression table with emails, prefixed so they can't collide."""
    return f"tel:{phone}"


def in_quiet_hours(cfg: Config, now: datetime | None = None) -> bool:
    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(cfg.sms_timezone))
    return not (cfg.sms_quiet_start <= local.hour < cfg.sms_quiet_end)


def twilio_signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's documented scheme: base64(HMAC-SHA1(token, url + sorted key/value pairs))."""
    data = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()).decode()


def verify_twilio(auth_token: str, url: str, params: dict[str, str], signature: str) -> bool:
    return bool(auth_token and signature) and hmac.compare_digest(twilio_signature(auth_token, url, params), signature)


class SMS:
    def __init__(self, cfg: Config, http: requests.Session | None = None):
        self.cfg = cfg
        self.http = http or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.cfg.twilio_account_sid and self.cfg.twilio_auth_token and self.cfg.twilio_from)

    @property
    def live(self) -> bool:
        return self.configured and not self.cfg.dry_run

    def send(self, to: str, body: str) -> str:
        """Returns the Twilio message SID ('dry' when not live)."""
        if not self.live:
            return "dry"
        sender_field = "MessagingServiceSid" if self.cfg.twilio_from.startswith("MG") else "From"
        r = self.http.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self.cfg.twilio_account_sid}/Messages.json",
            data={"To": to, sender_field: self.cfg.twilio_from, "Body": body},
            auth=(self.cfg.twilio_account_sid, self.cfg.twilio_auth_token), timeout=30)
        r.raise_for_status()
        return r.json().get("sid", "")
