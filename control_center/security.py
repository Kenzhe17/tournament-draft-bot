"""Криптографические примитивы панели: HMAC-сессии и OAuth state.

Без внешних зависимостей: сессия — это подписанный токен
`v1.<b64(payload)>.<b64(hmac)>`, state — случайный nonce + подпись.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from control_center import config

_TOKEN_VERSION = "v1"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _secret() -> bytes:
    secret = config.SESSION_SECRET
    if not secret:
        raise RuntimeError(
            "CONTROL_CENTER_SESSION_SECRET is not configured"
        )
    return secret.encode()


def _sign(payload: bytes) -> str:
    return _b64e(hmac.new(_secret(), payload, hashlib.sha256).digest())


def issue_session(payload: dict[str, Any], ttl: int | None = None) -> str:
    """Подписанный сессионный токен."""
    body = dict(payload)
    now = int(time.time())
    body.setdefault("iat", now)
    body["exp"] = now + (ttl or config.SESSION_TTL_SECONDS)
    raw = json.dumps(body, separators=(",", ":")).encode()
    encoded = _b64e(raw)
    return f"{_TOKEN_VERSION}.{encoded}.{_sign(encoded.encode())}"


def verify_session(token: str) -> dict[str, Any] | None:
    """Проверить подпись и срок жизни. None — невалиден/просрочен."""
    try:
        version, encoded, sig = token.split(".", 2)
    except ValueError:
        return None
    if version != _TOKEN_VERSION:
        return None
    expected = _sign(encoded.encode())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        payload = json.loads(_b64d(encoded))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def issue_state(ttl: int = 600) -> tuple[str, str]:
    """OAuth state: (значение для callback, cookie-digest).

    Cookie хранит HMAC от nonce — сам nonce идёт в authorize URL.
    """
    nonce = secrets.token_urlsafe(24)
    digest = _b64e(
        hmac.new(_secret(), nonce.encode(), hashlib.sha256).digest()
    )
    return nonce, digest


def verify_state(nonce: str, digest: str) -> bool:
    expected = _b64e(
        hmac.new(_secret(), nonce.encode(), hashlib.sha256).digest()
    )
    return hmac.compare_digest(digest, expected)


def cookie_secure() -> bool:
    if config.COOKIE_SECURE == "true":
        return True
    if config.COOKIE_SECURE == "false":
        return False
    return config.PUBLIC_BASE_URL.startswith("https://")
