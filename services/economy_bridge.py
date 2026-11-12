"""Internal economy bridge for r!z3 Arcade.

Read-only balance lookup plus transactional pooled-stake endpoints
(escrow / settle / refund) over Railway private networking or an
authenticated HTTPS domain. The bot database remains the single source
of truth; balances are only mutated inside explicit, idempotent,
all-or-nothing transactions recorded in arcade_ledger.

Enabled only when ECONOMY_BRIDGE_TOKEN and ECONOMY_BRIDGE_PORT are set.
Without them no listener is started at all.
"""

from __future__ import annotations

import hmac
import json
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

# Mirrors config.MAX_BALANCE — winnings are clamped, never destroyed
# silently: the clamped remainder is reported back as `overflow`.
_MAX_BALANCE = 100_000
_MAX_STAKE = 1_000_000
_MAX_STAKES = 6


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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS arcade_escrow (
    op_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    entries JSONB NOT NULL,
    payouts JSONB,
    state TEXT NOT NULL DEFAULT 'reserved',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS arcade_ledger (
    id BIGSERIAL PRIMARY KEY,
    op_id TEXT NOT NULL,
    guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    kind TEXT NOT NULL,
    amount INTEGER NOT NULL,
    balance_after INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS arcade_ledger_op_idx ON arcade_ledger (op_id);
"""


def _authorized(request: web.Request) -> web.Response | None:
    token = os.getenv(_TOKEN_ENV, "")
    if not token:
        return web.json_response({"error": "disabled"}, status=503)
    if not hmac.compare_digest(request.headers.get("X-Bridge-Token", ""), token):
        return web.json_response({"error": "unauthorized"}, status=401)
    if _rate_limited(request.remote or "unknown"):
        return web.json_response({"error": "rate_limited"}, status=429)
    return None


def _snowflake_map(raw: object) -> int | None:
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 20:
        return None
    value = int(raw)
    return value if 0 < value < 2**63 else None


async def _stakes_payload(request: web.Request) -> tuple[int, list[dict], str] | web.Response:
    guild_id = _snowflake(request.query.get("guild_id"))
    if guild_id is None:
        return web.json_response({"error": "invalid_parameters"}, status=400)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_parameters"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid_parameters"}, status=400)
    op_id = body.get("op_id")
    stakes = body.get("stakes")
    if not isinstance(op_id, str) or not 1 <= len(op_id) <= 120 or not isinstance(stakes, list) or not 1 <= len(stakes) <= _MAX_STAKES:
        return web.json_response({"error": "invalid_parameters"}, status=400)
    seen: set[int] = set()
    clean: list[dict] = []
    for entry in stakes:
        if not isinstance(entry, dict):
            return web.json_response({"error": "invalid_parameters"}, status=400)
        user_id = _snowflake_map(str(entry.get("user_id", "")))
        amount = entry.get("amount")
        if user_id is None or user_id in seen or not isinstance(amount, int) or isinstance(amount, bool) or not 0 < amount <= _MAX_STAKE:
            return web.json_response({"error": "invalid_parameters"}, status=400)
        seen.add(user_id)
        clean.append({"user_id": user_id, "amount": amount})
    return guild_id, clean, op_id


async def _ensure_schema(pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA)


async def escrow(request: web.Request) -> web.Response:
    denied = _authorized(request)
    if denied is not None:
        return denied
    parsed = await _stakes_payload(request)
    if isinstance(parsed, web.Response):
        return parsed
    guild_id, stakes, op_id = parsed

    from storage.db import get_pool
    pool = await get_pool()
    await _ensure_schema(pool)

    async with pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT state, entries FROM arcade_escrow WHERE op_id = $1", op_id)
        if existing is not None:
            # Idempotent replay: return the recorded outcome, never re-debit.
            return web.json_response({"ok": existing["state"] == "reserved", "op_id": op_id, "state": existing["state"], "replayed": True})
        try:
            async with conn.transaction():
                # Wallets are created lazily with the bot's default balance of
                # 100, exactly like the first wallet operation in the bot.
                for stake in stakes:
                    await conn.execute(
                        "INSERT INTO user_balance (guild_id, user_id, balance) VALUES ($1, $2, 100) ON CONFLICT (guild_id, user_id) DO NOTHING",
                        guild_id, stake["user_id"],
                    )
                for stake in stakes:
                    updated = await conn.execute(
        "UPDATE user_balance SET balance = balance - $3 WHERE guild_id = $1 AND user_id = $2 AND balance >= $3",
                        guild_id, stake["user_id"], stake["amount"],
                    )
                    if not updated.endswith(" 1"):
                        raise _Insufficient(stake["user_id"])
                    row = await conn.fetchrow(
                        "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                        guild_id, stake["user_id"],
                    )
                    await conn.execute(
                        "INSERT INTO arcade_ledger (op_id, guild_id, user_id, kind, amount, balance_after) VALUES ($1,$2,$3,'hold',$4,$5)",
                        op_id, guild_id, stake["user_id"], -stake["amount"], int(row["balance"]) if row else None,
                    )
                await conn.execute(
                    "INSERT INTO arcade_escrow (op_id, guild_id, entries, state) VALUES ($1,$2,$3::jsonb,'reserved')",
                    op_id, guild_id, json.dumps(stakes),
                )
        except _Insufficient as exc:
            return web.json_response({"ok": False, "error": "insufficient", "user_id": str(exc.user_id)}, status=409)
    return web.json_response({"ok": True, "op_id": op_id, "state": "reserved", "replayed": False})


class _Insufficient(Exception):
    def __init__(self, user_id: int) -> None:
        super().__init__("insufficient")
        self.user_id = user_id


async def _body(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _escrow_row(conn, op_id: str, guild_id: int):
    return await conn.fetchrow("SELECT * FROM arcade_escrow WHERE op_id = $1 AND guild_id = $2", op_id, guild_id)


async def settle(request: web.Request) -> web.Response:
    denied = _authorized(request)
    if denied is not None:
        return denied
    guild_id = _snowflake(request.query.get("guild_id"))
    body = await _body(request)
    if guild_id is None or body is None:
        return web.json_response({"error": "invalid_parameters"}, status=400)
    op_id = body.get("op_id")
    payouts = body.get("payouts")
    if not isinstance(op_id, str) or not isinstance(payouts, list) or not payouts or len(payouts) > _MAX_STAKES:
        return web.json_response({"error": "invalid_parameters"}, status=400)
    clean: list[dict] = []
    seen: set[int] = set()
    for entry in payouts:
        if not isinstance(entry, dict):
            return web.json_response({"error": "invalid_parameters"}, status=400)
        user_id = _snowflake_map(str(entry.get("user_id", "")))
        amount = entry.get("amount")
        if user_id is None or user_id in seen or not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
            return web.json_response({"error": "invalid_parameters"}, status=400)
        seen.add(user_id)
        clean.append({"user_id": user_id, "amount": amount})

    from storage.db import get_pool
    pool = await get_pool()
    await _ensure_schema(pool)
    async with pool.acquire() as conn:
        escrow_row = await _escrow_row(conn, op_id, guild_id)
        if escrow_row is None:
            return web.json_response({"error": "not_found"}, status=404)
        if escrow_row["state"] == "settled":
            return web.json_response({"ok": True, "op_id": op_id, "state": "settled", "replayed": True})
        if escrow_row["state"] != "reserved":
            return web.json_response({"error": "conflict", "state": escrow_row["state"]}, status=409)
        held = sum(int(e["amount"]) for e in json.loads(escrow_row["entries"]))
        paid = sum(p["amount"] for p in clean)
        if paid > held:
            return web.json_response({"error": "payout_exceeds_pool"}, status=400)
        results: list[dict] = []
        try:
            async with conn.transaction():
                for payout in clean:
                    await conn.execute(
                        "INSERT INTO user_balance (guild_id, user_id, balance) VALUES ($1, $2, 100) ON CONFLICT (guild_id, user_id) DO NOTHING",
                        guild_id, payout["user_id"],
                    )
                    before_row = await conn.fetchrow(
                        "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                        guild_id, payout["user_id"],
                    )
                    before = int(before_row["balance"])
                    after_row = await conn.fetchrow(
                        "UPDATE user_balance SET balance = LEAST(balance + $3, $4) WHERE guild_id = $1 AND user_id = $2 RETURNING balance",
                        guild_id, payout["user_id"], payout["amount"], _MAX_BALANCE,
                    )
                    after = int(after_row["balance"])
                    actual = max(0, after - before)
                    await conn.execute(
                        "INSERT INTO arcade_ledger (op_id, guild_id, user_id, kind, amount, balance_after) VALUES ($1,$2,$3,'payout',$4,$5)",
                        op_id, guild_id, payout["user_id"], actual, after,
                    )
                    results.append({"user_id": str(payout["user_id"]), "requested": payout["amount"], "credited": actual, "overflow": payout["amount"] - actual})
                await conn.execute(
                    "UPDATE arcade_escrow SET state = 'settled', payouts = $2::jsonb, settled_at = now() WHERE op_id = $1",
                    op_id, json.dumps(clean),
                )
        except Exception:
            logger.exception("Arcade settle failed for op %s", op_id)
            return web.json_response({"error": "settle_failed"}, status=500)
    return web.json_response({"ok": True, "op_id": op_id, "state": "settled", "replayed": False, "payouts": results})


async def refund(request: web.Request) -> web.Response:
    denied = _authorized(request)
    if denied is not None:
        return denied
    guild_id = _snowflake(request.query.get("guild_id"))
    body = await _body(request)
    if guild_id is None or body is None or not isinstance(body.get("op_id"), str):
        return web.json_response({"error": "invalid_parameters"}, status=400)
    op_id = body["op_id"]

    from storage.db import get_pool
    pool = await get_pool()
    await _ensure_schema(pool)
    async with pool.acquire() as conn:
        escrow_row = await _escrow_row(conn, op_id, guild_id)
        if escrow_row is None:
            return web.json_response({"error": "not_found"}, status=404)
        if escrow_row["state"] == "refunded":
            return web.json_response({"ok": True, "op_id": op_id, "state": "refunded", "replayed": True})
        if escrow_row["state"] != "reserved":
            return web.json_response({"error": "conflict", "state": escrow_row["state"]}, status=409)
        try:
            async with conn.transaction():
                for entry in json.loads(escrow_row["entries"]):
                    user_id, amount = int(entry["user_id"]), int(entry["amount"])
                    await conn.execute(
                        "INSERT INTO user_balance (guild_id, user_id, balance) VALUES ($1, $2, 100) ON CONFLICT (guild_id, user_id) DO NOTHING",
                        guild_id, user_id,
                    )
                    before_row = await conn.fetchrow(
                        "SELECT balance FROM user_balance WHERE guild_id = $1 AND user_id = $2",
                        guild_id, user_id,
                    )
                    before = int(before_row["balance"])
                    after_row = await conn.fetchrow(
                        "UPDATE user_balance SET balance = LEAST(balance + $3, $4) WHERE guild_id = $1 AND user_id = $2 RETURNING balance",
                        guild_id, user_id, amount, _MAX_BALANCE,
                    )
                    after = int(after_row["balance"])
                    await conn.execute(
                        "INSERT INTO arcade_ledger (op_id, guild_id, user_id, kind, amount, balance_after) VALUES ($1,$2,$3,'refund',$4,$5)",
                        op_id, guild_id, user_id, max(0, after - before), after,
                    )
                await conn.execute("UPDATE arcade_escrow SET state = 'refunded', settled_at = now() WHERE op_id = $1", op_id)
        except Exception:
            logger.exception("Arcade refund failed for op %s", op_id)
            return web.json_response({"error": "refund_failed"}, status=500)
    return web.json_response({"ok": True, "op_id": op_id, "state": "refunded", "replayed": False})


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
    app.router.add_post("/internal/economy/escrow", escrow)
    app.router.add_post("/internal/economy/settle", settle)
    app.router.add_post("/internal/economy/refund", refund)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    logger.info("Economy bridge listening on %s:%s (internal only)", host, port)
    return runner
