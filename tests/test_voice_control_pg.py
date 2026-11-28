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
from storage import moderation_store as modstore
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
    await modstore.moderation_store.ensure()
    async with pool.acquire() as conn:
        for t in ("voice_control_events", "voice_control_ops",
                  "voice_control_overwrites", "voice_control_restrictions",
                  "voice_control_exceptions", "voice_control_speakers",
                  "voice_control_modes", "voice_control_settings",
                  "voice_control_panels",
                  "moderation_events", "moderation_cases",
                  "moderation_reports", "moderation_appeals",
                  "moderation_settings"):
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

    # due_restrictions — только выборка кандидатов; переход в
    # pending_release выполняет сервис под замком через CAS.
    due = await pg_pool.due_restrictions()
    assert any(r["id"] == r1["id"] for r in due)
    row = await pg_pool.set_state(
        1, r1["id"], "pending_release",
        from_states=("active", "pending_apply"))
    assert row["state"] == "pending_release"
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


@pytest.mark.asyncio
async def test_delivery_state_lifecycle_pg(pg_pool):
    """P0.2: delivery_state персистится и меняется mark_delivery."""
    r = await pg_pool.add_restriction(
        1, 10, 98, "server_mute", "manual", created_by=2)
    assert r["delivery_state"] == "not_dispatched"
    await pg_pool.mark_delivery(1, r["id"], "dispatch_started")
    row = await pg_pool.restriction_by_id(1, r["id"])
    assert row["delivery_state"] == "dispatch_started"
    await pg_pool.mark_delivery(1, r["id"], "outcome_unknown")
    await pg_pool.mark_delivery(1, r["id"], "confirmed_applied")
    row = await pg_pool.restriction_by_id(1, r["id"])
    assert row["delivery_state"] == "confirmed_applied"
    assert row["state"] == "pending_apply"


@pytest.mark.asyncio
async def test_claim_token_cas_pg(pg_pool):
    """P0.5: set_state/release_restriction с expected_claim_token —
    чужой token не завершает переход; release_claim по token."""
    r = await pg_pool.add_restriction(
        1, 10, 99, "server_mute", "manual", created_by=2,
        state="pending_release")
    batch = await pg_pool.claim_pending("pending_release", "w1", limit=10)
    assert any(x["id"] == r["id"] for x in batch)
    token1 = next(x["claim_token"] for x in batch if x["id"] == r["id"])
    # Уникальный token на каждое получение lease: повторный claim
    # тем же worker'ом даёт ДРУГОЙ token.
    batch_again = await pg_pool.claim_pending("pending_apply", "w1",
                                              limit=10)
    token_other = next((x["claim_token"] for x in batch_again
                        if x["id"] == r["id"]), None)
    assert token_other != token1 or token_other is None
    # Неверный token — переход и mark_delivery отклонены.
    assert await pg_pool.set_state(
        1, r["id"], "released", expected_claim_token="w2") is None
    assert await pg_pool.release_restriction(
        1, r["id"], expected_claim_token="w2") is None
    assert await pg_pool.mark_delivery(
        1, r["id"], "confirmed_applied",
        expected_claim_token="w2") is False
    # Верный token — переход и mark_delivery проходят.
    row = await pg_pool.set_state(
        1, r["id"], "pending_apply",
        from_states=("pending_release",),
        expected_claim_token=token1)
    assert row is not None and row["state"] == "pending_apply"
    assert await pg_pool.mark_delivery(
        1, r["id"], "dispatch_started",
        expected_claim_token=token1) is False  # set_state сбросил claim
    # Повторный claim — новый уникальный token.
    batch3 = await pg_pool.claim_pending("pending_apply", "w3", limit=10)
    token3 = next(x["claim_token"] for x in batch3 if x["id"] == r["id"])
    assert token3 != token1 and token3.startswith("w3:")
    # release_claim чужим token не снимает claim.
    await pg_pool.release_claim(r["id"], token1)
    row = await pg_pool.restriction_by_id(1, r["id"])
    assert row["claim_token"] == token3
    await pg_pool.release_claim(r["id"], token3)
    row = await pg_pool.restriction_by_id(1, r["id"])
    assert row["claim_token"] is None


@pytest.mark.asyncio
async def test_moderation_voice_state_survives_flood_pg(pg_pool):
    """P0.1/R1: >100 событий ДРУГИХ пользователей не вытесняют
    действующее модерационное наказание target'а."""
    db = modstore.moderation_store
    target, other = 500, 501
    # Действующий мут target'а.
    await db.event(1, 1, "voice.mute.applied",
                   detail={"target_id": target, "value": True})
    # 150 событий мута других пользователей — позже по id.
    for i in range(150):
        await db.event(1, 1, "voice.mute.applied",
                       detail={"target_id": other + i, "value": True})
    st = await db.voice_state(1, target, "mute")
    assert st["status"] == "active"
    # Запрошенная, но не подтверждённая операция → unknown.
    await db.event(1, 1, "voice.mute.requested",
                   detail={"target_id": target, "value": False})
    st = await db.voice_state(1, target, "mute")
    assert st["status"] == "unknown"
    # Подтверждённое снятие → inactive.
    await db.event(1, 1, "voice.mute.applied",
                   detail={"target_id": target, "value": False})
    st = await db.voice_state(1, target, "mute")
    assert st["status"] == "inactive"
    # Другой field не смешивается.
    st = await db.voice_state(1, target, "deafen")
    assert st["status"] == "none"


@pytest.mark.asyncio
async def test_claim_token_unique_per_lease_pg(pg_pool):
    """RC3/P0.3: каждый lease — уникальный token; повторный claim
    тем же worker'ом НЕ переиспользует token."""
    r = await pg_pool.add_restriction(
        1, 10, 110, "server_mute", "manual", created_by=2,
        state="pending_release")
    b1 = await pg_pool.claim_pending("pending_release", "w1",
                                     limit=10, lease_seconds=0)
    t1 = next(x["claim_token"] for x in b1 if x["id"] == r["id"])
    assert t1.startswith("w1:") and len(t1) > 4
    # lease_seconds=0 → claim_until=now → сразу доступен для re-claim.
    b2 = await pg_pool.claim_pending("pending_release", "w1",
                                     limit=10, lease_seconds=60)
    t2 = next(x["claim_token"] for x in b2 if x["id"] == r["id"])
    assert t2 != t1 and t2.startswith("w1:")
    # Старый token не проходит fencing.
    assert await pg_pool.set_state(
        1, r["id"], "released", expected_claim_token=t1) is None
    assert await pg_pool.mark_delivery(
        1, r["id"], "confirmed_applied",
        expected_claim_token=t1) is False
    # renew_claim продлевает только свой lease.
    assert await pg_pool.renew_claim(r["id"], t2, lease_seconds=300)
    assert not await pg_pool.renew_claim(r["id"], t1)
    row = await pg_pool.restriction_by_id(1, r["id"])
    assert row["claim_token"] == t2
    assert row["claim_until"] > datetime.now(UTC)


@pytest.mark.asyncio
async def test_set_state_atomic_delivery_pg(pg_pool):
    """RC3/P0.2: delivery_state обновляется АТОМАРНО с lifecycle state;
    проигранный CAS не меняет delivery."""
    r = await pg_pool.add_restriction(
        1, 10, 111, "server_mute", "manual", created_by=2,
        state="pending_apply", delivery_state="dispatch_started")
    row = await pg_pool.set_state(
        1, r["id"], "active", from_states=("pending_apply",),
        delivery_state="confirmed_applied")
    assert row["state"] == "active"
    assert row["delivery_state"] == "confirmed_applied"
    # CAS проигран → delivery остаётся прежним.
    row = await pg_pool.set_state(
        1, r["id"], "pending_release",
        from_states=("pending_apply",), delivery_state="not_dispatched")
    assert row is None
    fresh = await pg_pool.restriction_by_id(1, r["id"])
    assert fresh["state"] == "active"
    assert fresh["delivery_state"] == "confirmed_applied"


@pytest.mark.asyncio
async def test_due_restrictions_readonly_pg(pg_pool):
    """RC3/P0.2: due_restrictions — только выборка кандидатов,
    состояние не меняется (expiry под замком через CAS в сервисе)."""
    past = datetime.now(UTC) - timedelta(seconds=5)
    r = await pg_pool.add_restriction(
        1, 10, 112, "server_mute", "timer", expires_at=past,
        created_by=2, state="active")
    due = await pg_pool.due_restrictions()
    assert any(x["id"] == r["id"] for x in due)
    fresh = await pg_pool.restriction_by_id(1, r["id"])
    assert fresh["state"] == "active"  # не тронуто
    # CAS expiry: активная → pending_release атомарно.
    row = await pg_pool.set_state(
        1, r["id"], "pending_release",
        from_states=("active", "pending_apply"))
    assert row["state"] == "pending_release"
    # Проигранный CAS: повторный expiry не ломает pending_release.
    assert await pg_pool.set_state(
        1, r["id"], "pending_release",
        from_states=("active", "pending_apply")) is None


@pytest.mark.asyncio
async def test_public_panel_roundtrip_pg(pg_pool):
    """Публичная карточка: одна запись на guild, upsert при
    повторной публикации."""
    assert await pg_pool.panel(1) is None
    await pg_pool.put_panel(1, 50, 9001)
    p = await pg_pool.panel(1)
    assert p["channel_id"] == 50 and p["message_id"] == 9001
    # Повторная публикация — обновление, не дубликат.
    await pg_pool.put_panel(1, 51, 9002)
    p = await pg_pool.panel(1)
    assert p["channel_id"] == 51 and p["message_id"] == 9002
    # Изоляция guild.
    assert await pg_pool.panel(2) is None


@pytest.mark.asyncio
async def test_mod_state_real_store_no_import_substitution(pg_pool):
    """Regression 4d39ddb: services.voice_control обязан вызывать
    voice_state() на ЭКЗЕМПЛЯРЕ ModerationStore. Импорт модуля
    вместо экземпляра давал AttributeError -> status 'unknown'
    на каждом чтении. Тест идёт по реальному PG-пути без моков."""
    from services import voice_control as vcsvc

    db = modstore.moderation_store
    target = 424242
    # Модерация подтверждённо держит mute.
    await db.event(1, 1, "voice.mute.applied",
                   detail={"target_id": target, "value": True})
    st = await vcsvc._mod_state(1, target, "server_mute")
    assert st["status"] == "active"
    # Deafen не трогали — 'none', а не 'unknown' (значит вызов
    # реально дошёл до журнала, а не упал в except).
    st = await vcsvc._mod_state(1, target, "server_deafen")
    assert st["status"] == "none"
    # Подтверждённое снятие -> inactive; _mod_cleared_after видит его.
    await db.event(1, 1, "voice.mute.applied",
                   detail={"target_id": target, "value": False})
    st = await vcsvc._mod_state(1, target, "server_mute")
    assert st["status"] == "inactive"
    assert st["at"] is not None
    assert await vcsvc._mod_cleared_after(
        1, target, "server_mute", st["at"] - timedelta(seconds=1))
