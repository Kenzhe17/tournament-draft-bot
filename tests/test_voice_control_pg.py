"""PG-интеграция Voice Control Store: схема, идемпотентность,
гильдейская изоляция, таймеры и журнал.

Требует pgserver (embedded PostgreSQL). Без него — skip.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from storage import voice_control_store as vstore


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_vc")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    prev_url = dbmod.DATABASE_URL
    prev_pool = dbmod._pool
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    store = vstore.VoiceControlStore()
    await store.ensure()
    async with pool.acquire() as conn:
        for t in ("voice_control_events", "voice_control_ops",
                  "voice_control_overwrites", "voice_control_restrictions",
                  "voice_control_exceptions", "voice_control_speakers",
                  "voice_control_modes", "voice_control_settings"):
            await conn.execute(f"DELETE FROM {t}")
    try:
        yield store
    finally:
        dbmod.DATABASE_URL = prev_url
        dbmod._pool = prev_pool
        await pool.close()


@pytest.mark.asyncio
async def test_settings_roundtrip(pg_pool):
    await pg_pool.put_settings(1, allowed_role_ids=[5, 2],
                               protected_user_ids=[7])
    conf = await pg_pool.settings(1)
    assert conf["allowed_role_ids"] == [2, 5]
    assert conf["protected_user_ids"] == [7]
    # Изоляция гильдий.
    assert (await pg_pool.settings(2)).get("allowed_role_ids") == []


@pytest.mark.asyncio
async def test_mode_and_speakers(pg_pool):
    await pg_pool.put_mode(1, 10, speaker_mode=True, auto_mute=True)
    mode = await pg_pool.mode(1, 10)
    assert mode["speaker_mode"] and mode["auto_mute"]
    await pg_pool.add_speaker(1, 10, 42, 1)
    assert await pg_pool.speakers(1, 10) == [42]
    await pg_pool.remove_speaker(1, 10, 42)
    assert await pg_pool.speakers(1, 10) == []


@pytest.mark.asyncio
async def test_restriction_idempotent_and_expiry(pg_pool):
    past = datetime.now(UTC) - timedelta(seconds=5)
    r1 = await pg_pool.add_restriction(
        1, 10, 70, "server_mute", "timer", expires_at=past,
        request_key="k1", created_by=2)
    assert r1 and r1["state"] == "active"
    # Тот же request_key — не дублирует.
    r2 = await pg_pool.add_restriction(
        1, 10, 70, "server_mute", "timer", expires_at=past,
        request_key="k1", created_by=2)
    assert r2 is None or r2["id"] == r1["id"]

    expired = await pg_pool.expire_due_restrictions()
    assert any(r["id"] == r1["id"] for r in expired)
    active = await pg_pool.restrictions(1, channel_id=10, user_id=70)
    assert active == []


@pytest.mark.asyncio
async def test_has_other_active_and_release(pg_pool):
    a = await pg_pool.add_restriction(1, 10, 71, "server_mute", "bulk_mute")
    b = await pg_pool.add_restriction(1, 10, 71, "server_mute", "speaker_mode")
    assert await pg_pool.has_other_active(1, 10, 71, "server_mute",
                                          exclude_id=a["id"])
    await pg_pool.release_restriction(1, a["id"], actor_id=2)
    # b ещё активен → has_other(exclude a) True; has_other(exclude b) False.
    assert await pg_pool.has_other_active(1, 10, 71, "server_mute",
                                          exclude_id=a["id"])
    assert not await pg_pool.has_other_active(1, 10, 71, "server_mute",
                                              exclude_id=b["id"])


@pytest.mark.asyncio
async def test_overwrites_and_events(pg_pool):
    ids = await pg_pool.save_overwrites(
        1, 10, [{"target_type": "role", "target_id": 1,
                 "before": {"connect": True},
                 "applied": {"connect": False}}], actor_id=2)
    assert len(ids) == 1
    rows = await pg_pool.active_overwrites(1, 10)
    assert rows[0]["state"] == "applied"
    await pg_pool.resolve_overwrite(1, ids[0], state="restored")
    assert await pg_pool.active_overwrites(1, 10) == []

    await pg_pool.event(1, 2, "voice.bulk_mute", channel_id=10,
                        detail={"done": 3})
    events = await pg_pool.events(1)
    assert events[0]["action"] == "voice.bulk_mute"


@pytest.mark.asyncio
async def test_guild_isolation_pg(pg_pool):
    await pg_pool.add_restriction(2, 20, 80, "server_mute", "manual")
    assert await pg_pool.restrictions(1) == [] or all(
        r["guild_id"] == 1 for r in await pg_pool.restrictions(1))
    assert len(await pg_pool.restrictions(2)) == 1
