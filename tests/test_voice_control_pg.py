"""PG-интеграция Voice Control Store: схема, durable lifecycle,
идемпотентность, гильдейская изоляция, claims/CAS, таймеры,
журнал и реальная конкурентность.

Требует pgserver (embedded PostgreSQL). Без него — skip.
"""
from __future__ import annotations

import asyncio
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
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=8)
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
                               protected_user_ids=[7],
                               protected_channel_ids=[11])
    conf = await pg_pool.settings(1)
    assert conf["allowed_role_ids"] == [2, 5]
    assert conf["protected_user_ids"] == [7]
    assert conf["protected_channel_ids"] == [11]
    assert (await pg_pool.settings(2)).get("allowed_role_ids") == []


@pytest.mark.asyncio
async def test_mode_columns_and_speakers(pg_pool):
    await pg_pool.put_mode(1, 10, speaker_mode=True, auto_mute=True,
                           name_override="VC", name_before="Old",
                           user_limit=5, user_limit_before=0)
    mode = await pg_pool.mode(1, 10)
    assert mode["speaker_mode"] and mode["auto_mute"]
    assert mode["name_before"] == "Old"
    assert mode["user_limit_before"] == 0
    await pg_pool.add_speaker(1, 10, 42, 1)
    assert await pg_pool.speakers(1, 10) == [42]
    await pg_pool.remove_speaker(1, 10, 42)
    assert await pg_pool.speakers(1, 10) == []


@pytest.mark.asyncio
async def test_restriction_lifecycle_states(pg_pool):
    """pending_apply → active → pending_release → released."""
    r = await pg_pool.add_restriction(
        1, 10, 70, "server_mute", "manual", created_by=2)
    assert r["state"] == "pending_apply"
    # active_only включает pending_apply.
    assert any(x["id"] == r["id"]
               for x in await pg_pool.restrictions(1, user_id=70))

    row = await pg_pool.set_state(1, r["id"], "active")
    assert row["state"] == "active"
    # CAS: переход из неверного состояния отклоняется.
    assert await pg_pool.set_state(
        1, r["id"], "released", from_states=("pending_apply",)) is None
    row = await pg_pool.set_state(
        1, r["id"], "pending_release",
        from_states=("active", "pending_apply", "pending_release"))
    assert row["state"] == "pending_release"
    row = await pg_pool.release_restriction(1, r["id"], actor_id=2,
                                            final_state="released")
    assert row["state"] == "released"
    assert row["released_at"] is not None
    # Терминальные записи не трогаем повторно.
    assert await pg_pool.release_restriction(1, r["id"]) is None


@pytest.mark.asyncio
async def test_restriction_idempotent_and_expiry(pg_pool):
    past = datetime.now(UTC) - timedelta(seconds=5)
    r1 = await pg_pool.add_restriction(
        1, 10, 71, "server_mute", "timer", expires_at=past,
        request_key="k1", created_by=2, state="active")
    assert r1 is not None
    r2 = await pg_pool.add_restriction(
        1, 10, 71, "server_mute", "timer", expires_at=past,
        request_key="k1", created_by=2)
    # Активный дубль request_key возвращает существующую запись —
    # новой строки не создаётся.
    assert r2 is None or r2["id"] == r1["id"]

    expired = await pg_pool.expire_due_restrictions()
    assert any(r["id"] == r1["id"] for r in expired)
    # pending_release не входит в active_only.
    active = await pg_pool.restrictions(1, channel_id=10, user_id=71)
    assert active == []


@pytest.mark.asyncio
async def test_has_other_active_guild_wide(pg_pool):
    """has_other_active — по (guild,user,kind), НЕЗАВИСИМО от канала:
    server mute — флаг уровня участника."""
    a = await pg_pool.add_restriction(1, 10, 72, "server_mute",
                                      "bulk_mute", state="active")
    b = await pg_pool.add_restriction(1, 20, 72, "server_mute",
                                      "speaker_mode", state="active")
    assert await pg_pool.has_other_active(1, 72, "server_mute",
                                          exclude_id=a["id"])
    # Другой kind не считается.
    assert not await pg_pool.has_other_active(1, 72, "server_deafen",
                                              exclude_id=a["id"])
    await pg_pool.release_restriction(1, a["id"], actor_id=2)
    assert await pg_pool.has_other_active(1, 72, "server_mute",
                                          exclude_id=a["id"])
    assert not await pg_pool.has_other_active(1, 72, "server_mute",
                                              exclude_id=b["id"])


@pytest.mark.asyncio
async def test_claim_pending_concurrent_workers(pg_pool):
    """22. Реальная PG-конкурентность: два воркера через SKIP LOCKED
    не берут одну запись; после release_claim запись доступна."""
    for i in range(5):
        await pg_pool.add_restriction(
            1, 10, 80 + i, "server_mute", "manual",
            created_by=2, state="pending_release")

    b1, b2 = await asyncio.gather(
        pg_pool.claim_pending("pending_release", "w1", limit=10),
        pg_pool.claim_pending("pending_release", "w2", limit=10),
    )
    ids1 = {r["id"] for r in b1}
    ids2 = {r["id"] for r in b2}
    assert not (ids1 & ids2), "одна запись захвачена двумя воркерами"
    assert len(ids1) + len(ids2) == 5

    # Лиз истёкшего claim возвращает запись в пул (lease_seconds=0).
    await pg_pool.release_claim(next(iter(ids1)))
    again = await pg_pool.claim_pending("pending_release", "w3",
                                        limit=10)
    assert any(r["id"] in ids1 for r in again)


@pytest.mark.asyncio
async def test_set_state_cas_concurrent(pg_pool):
    """CAS-переходы: параллельные set_state не ломают состояние."""
    r = await pg_pool.add_restriction(
        1, 10, 90, "server_mute", "manual", created_by=2,
        state="pending_apply")
    results = await asyncio.gather(
        pg_pool.set_state(1, r["id"], "active",
                          from_states=("pending_apply",)),
        pg_pool.set_state(1, r["id"], "pending_release",
                          from_states=("pending_apply",)),
        pg_pool.set_state(1, r["id"], "conflict",
                          from_states=("pending_apply",)),
    )
    # CAS: ровно один переход из допустимого from_states побеждает.
    winners = [x for x in results if x is not None]
    assert len(winners) == 1
    final = await pg_pool.restrictions(1, user_id=90, active_only=False)
    assert final[0]["state"] == winners[0]["state"]


@pytest.mark.asyncio
async def test_overwrites_states_pg(pg_pool):
    ids = await pg_pool.save_overwrites(
        1, 10, [{"target_type": "role", "target_id": 1,
                 "before": {"allow": 1, "deny": 0, "connect": "none"},
                 "applied": {"allow": 1, "deny": 2, "connect": "deny"},
                 "state": "saved"}], actor_id=2)
    rows = await pg_pool.active_overwrites(1, 10)
    assert rows[0]["state"] == "saved"
    # 'saved' тоже виден в all_active_overwrites (startup-сверка).
    all_rows = await pg_pool.all_active_overwrites()
    assert any(o["id"] == ids[0] for o in all_rows)
    await pg_pool.resolve_overwrite(1, ids[0], state="applied")
    await pg_pool.resolve_overwrite(1, ids[0], state="restored")
    assert await pg_pool.active_overwrites(1, 10) == []


@pytest.mark.asyncio
async def test_scan_active_cursor_pg(pg_pool):
    """Курсорная выборка активных для sweeper'а."""
    for i in range(3):
        await pg_pool.add_restriction(
            1, 10, 95 + i, "server_mute", "manual",
            created_by=2, state="active")
    page1 = await pg_pool.scan_active(after_id=0, limit=2)
    assert len(page1) == 2
    page2 = await pg_pool.scan_active(after_id=page1[-1]["id"], limit=10)
    assert len(page2) >= 1
    assert all(r["id"] > page1[-1]["id"] for r in page2)


@pytest.mark.asyncio
async def test_pending_states_and_events(pg_pool):
    await pg_pool.add_restriction(1, 10, 96, "server_mute", "manual",
                                  created_by=2, state="pending_apply")
    counts = await pg_pool.pending_states()
    assert counts.get("pending_apply", 0) >= 1

    await pg_pool.event(1, 2, "voice.bulk_mute", channel_id=10,
                        detail={"done": 3})
    events = await pg_pool.events(1)
    assert events[0]["action"] == "voice.bulk_mute"


@pytest.mark.asyncio
async def test_guild_isolation_pg(pg_pool):
    await pg_pool.add_restriction(2, 20, 97, "server_mute", "manual",
                                  state="active")
    assert all(r["guild_id"] == 1
               for r in await pg_pool.restrictions(1))
    assert len(await pg_pool.restrictions(2)) >= 1
