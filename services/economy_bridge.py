"""Internal economy bridge for r!z3 Arcade.

Read-only balance lookup over Railway private networking. The bot database
remains the single source of truth; this endpoint never creates wallets,
never applies decay and never mutates balances.

Enabled only when ECONOMY_BRIDGE_TOKEN and ECONOMY_BRIDGE_PORT are set.
Without them no listener is started at all.
"""

from __future__ import annotations

import hmac
import logging
import os
import time

from aiohttp import web

logger = logging.getLogger(__name__)

_TOKEN_ENV = "ECONOMY_BRIDGE_TOKEN"
_HOST_ENV = "ECONOMY_BRIDGE_HOST"
_PORT_ENV = "ECONOMY_BRIDGE_PORT"

_RATE_LIMIT_ENV = "ECONOMY_BRIDGE_RATE_LIMIT"
_RATE_LIMIT_DEFAULT = 600     # requests per IP per window; all Arcade calls
                              # share the service's egress IP(s)
_RATE_WINDOW = 60.0           # seconds
_hits: dict[str, list[float]] = {}


def _rate_limited(ip: str) -> bool:
    try:
        limit = int(os.getenv(_RATE_LIMIT_ENV, "") or _RATE_LIMIT_DEFAULT)
    except ValueError:
        limit = _RATE_LIMIT_DEFAULT
    now = time.monotonic()
    hits = [t for t in _hits.get(ip, []) if now - t < _RATE_WINDOW]
    if len(hits) >= limit:
        _hits[ip] = hits
        return True
    hits.append(now)
    _hits[ip] = hits
    return False


def _snowflake(raw: str | None) -> int | None:
    if raw is None or not raw.isdigit() or len(raw) > 20:
        return None
    value = int(raw)
    return value if 0 < value < 2**63 else None


async def balance(request: web.Request) -> web.Response:
    token = os.getenv(_TOKEN_ENV, "")
    if not token:
        return web.json_response({"error": "disabled"}, status=503)
    if not hmac.compare_digest(request.headers.get("X-Bridge-Token", ""), token):
        return web.json_response({"error": "unauthorized"}, status=401)
    if _rate_limited(request.remote or "unknown"):
        return web.json_response({"error": "rate_limited"}, status=429)

    guild_id = _snowflake(request.query.get("guild_id"))
    user_id = _snowflake(request.query.get("user_id"))
    if guild_id is None or user_id is None:
        return web.json_response({"error": "invalid_parameters"}, status=400)

    # Deliberately a plain SELECT: user_balance_store.get_balance() can INSERT a
    # default wallet and apply daily decay — neither belongs in a read path.
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )
    return web.json_response({
        "balance": int(row["balance"]) if row else None,
        "exists": row is not None,
    })


async def start_bridge() -> web.AppRunner | None:
    """Start the internal HTTP listener. Returns None when not configured."""
    token = os.getenv(_TOKEN_ENV, "")
    port_raw = os.getenv(_PORT_ENV, "")
    if not token or not port_raw.isdigit():
        logger.info("Economy bridge disabled (ECONOMY_BRIDGE_TOKEN/PORT not set)")
        return None
    host = os.getenv(_HOST_ENV, "0.0.0.0")
    port = int(port_raw)
    app = web.Application()
    app.router.add_get("/internal/economy/balance", balance)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Economy bridge listening on %s:%s (internal only)", host, port)
    return runner
