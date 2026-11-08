"""Signed one-click unsubscribe links (RFC 8058).

Gmail and Yahoo require bulk senders to support one-click unsubscribe via the
`List-Unsubscribe` + `List-Unsubscribe-Post` headers. When PUBLIC_URL and
APP_SECRET are set, every email gets an https link served by `agency serve`;
otherwise only the mailto: fallback is used.
"""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

from .config import Config


def token(email: str, secret: str) -> str:
    return hmac.new(secret.encode(), email.lower().strip().encode(), hashlib.sha256).hexdigest()[:32]


def verify(email: str, tok: str, secret: str) -> bool:
    return bool(secret) and hmac.compare_digest(token(email, secret), tok or "")


def url(email: str, cfg: Config) -> str | None:
    if not (cfg.public_url and cfg.app_secret):
        return None
    return f"{cfg.public_url}/unsubscribe?" + urlencode({"e": email.lower(), "t": token(email, cfg.app_secret)})


def headers(recipient: str, mailbox: str, cfg: Config) -> dict[str, str]:
    link = url(recipient, cfg)
    mailto = f"<mailto:{mailbox}?subject=unsubscribe>"
    if link:
        return {"List-Unsubscribe": f"<{link}>, {mailto}",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return {"List-Unsubscribe": mailto}
