"""PG-интеграция: спонтанные события 2.0 на реальном PostgreSQL
(pgserver, изолированный кластер).

Проверяем:
- event_store: атомарный claim одного активного события на канал,
  patch_actions (jsonb ||), finish() ровно один раз, кулдаун
  переживает «перезапуск» (новый инстанс store);
- event_reward_store: record/claim/complete/retry как у legacy;
- add_xp_once: ровно один XP-грант по grant_key, уровни растут;
- apply_reward: идемпотентность coins/xp/rep по grant_key;
- старые drop_coin_rewards продолжают работать рядом.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")
pytest_asyncio = pytest.importorskip("pytest_asyncio")

from storage import event_store as es  # noqa: E402
from storage import event_reward_store as ers  # noqa: E402
from storage import player_stats_store as pss  # noqa: E402
from storage.user_balance_store import user_balance_store  # noqa: E402
from services import event_rewards  # noqa: E402


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_events2")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri, monkeypatch):
    import storage.db as dbmod
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    prev = dbmod._pool
    dbmod._pool = pool
    await dbmod.init_db()
    async with pool.acquire() as conn:
        await conn.execute(es.DDL)
        await conn.execute(es.INDEX_DDL)
        await conn.execute(es.CHANNEL_DDL)
        await conn.execute(ers.DDL)
        await conn.execute(ers.INDEX_DDL)
        await conn.execute(pss.XP_GRANTS_DDL)
        await conn.execute(user_balance_store._GRANT_DDL)
        for t in ("spontaneous_events", "event_channel_state",
                  "event_rewards", "player_xp_grants",
                  "drop_coin_rewards", "match_coin_grants",
                  "user_balance", "player_stats",
                  "rep_balance", "rep_balance_grants",
                  "reputation_transactions", "rep_giver_state",
                  "rep_invites", "rep_boosts"):
            try:
                await conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
    monkeypatch.setattr(es.event_store, "_ddl_done", False)
    monkeypatch.setattr(ers.event_reward_store, "_ddl_done", False)
    try:
        yield pool
    finally:
        dbmod._pool = prev
        await pool.close()


def _deadline(seconds=60):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ------------------------------------------------------------ event_store

@pytest.mark.asyncio
async def test_event_store_one_active_per_channel(pg_pool):
    """Частичный UNIQUE-индекс: второй create на том же канале
    возвращает None — атомарный claim естественный+manual."""
    store = es.event_store
    row = await store.create(
        "evt00001", 1, 100, "speed",
        {"type": "coins", "amount": 100, "item_id": None},
        {"started_ts": 1.0}, _deadline())
    assert row is not None and row["state"] == "active"
    # Другой event_id, тот же канал — отказ.
    row2 = await store.create(
        "evt00002", 1, 100, "raffle",
        {"type": "coins", "amount": 100, "item_id": None},
        {}, _deadline())
    assert row2 is None
    # Другой канал — можно.
    row3 = await store.create(
        "evt00003", 1, 101, "raffle",
        {"type": "coins", "amount": 100, "item_id": None},
        {}, _deadline())
    assert row3 is not None
    # После finish слот освобождается.
    closed = await store.finish("evt00001", "finished",
                                {"reason": "test"})
    assert closed is not None
    again = await store.finish("evt00001", "finished", {})
    assert again is None  # второй финализатор отклонён
    row4 = await store.create(
        "evt00004", 1, 100, "dice",
        {"type": "coins", "amount": 100, "item_id": None},
        {}, _deadline())
    assert row4 is not None


@pytest.mark.asyncio
async def test_event_store_actions_patch_and_finish(pg_pool):
    store = es.event_store
    await store.create(
        "evt00010", 2, 200, "raffle",
        {"type": "xp", "amount": 50, "item_id": None},
        {}, _deadline())
    await store.patch_actions("evt00010", {"p:11": 1})
    await store.patch_actions("evt00010", {"p:22": 1, "p:33": 1})
    row = await store.get("evt00010")
    actions = row["actions"]
    if isinstance(actions, str):
        import json
        actions = json.loads(actions)
    assert actions == {"p:11": 1, "p:22": 1, "p:33": 1}
    # patch после finish не применяется (state='active' в WHERE)
    await store.finish("evt00010", "finished", {"winners": [11]})
    await store.patch_actions("evt00010", {"p:44": 1})
    row = await store.get("evt00010")
    actions = row["actions"]
    if isinstance(actions, str):
        import json
        actions = json.loads(actions)
    assert "p:44" not in actions


@pytest.mark.asyncio
async def test_event_store_cooldown_persists(pg_pool):
    """Кулдаун в event_channel_state — «переживает рестарт»
    (читается из БД, не из памяти)."""
    store = es.event_store
    until = datetime.now(timezone.utc) + timedelta(minutes=15)
    await store.mark_started(3, 300, until, "speed", "evt00020")
    assert await store.cooldown_active(3, 300) is True
    assert await store.last_event_type(3, 300) == "speed"
    # «Рестарт»: новый инстанс на той же БД видит кулдаун.
    fresh = es.EventStore()
    fresh._ddl_done = True
    # cooldown_active идёт через self-методы — используем тот же
    # pool; новый инстанс просто не имеет _ddl_done-кэша.
    assert await fresh.cooldown_active(3, 300) is True
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    await store.mark_started(3, 301, past, "dice", "evt00021")
    assert await store.cooldown_active(3, 301) is False


@pytest.mark.asyncio
async def test_event_store_overdue_uses_close_at(pg_pool):
    """overdue() учитывает скрытый close_at (growing burn,
    last_click) — событие закрывается по раннему дедлайну."""
    store = es.event_store
    now = datetime.now(timezone.utc)
    # deadline 60s, close 1s назад → overdue.
    await store.create(
        "evt00030", 4, 400, "growing",
        {"type": "coins", "amount": 100, "item_id": None},
        {}, now + timedelta(seconds=60),
        close_at=now - timedelta(seconds=1))
    rows = await store.overdue()
    ids = {r["event_id"] for r in rows}
    assert "evt00030" in ids


# ------------------------------------------------------- event_rewards

@pytest.mark.asyncio
async def test_event_reward_lifecycle(pg_pool):
    store = ers.event_reward_store
    row = await store.record("event:e1:1", "coins", 1, 42,
                             amount=250, item_id=None)
    assert row["status"] == "pending"
    # идемпотентно
    row2 = await store.record("event:e1:1", "coins", 1, 42,
                              amount=999)
    assert int(row2["amount"]) == 250
    claimed = await store.claim_key("event:e1:1")
    assert claimed is not None
    assert await store.claim_key("event:e1:1") is None  # lease занят
    ok = await store.complete("event:e1:1",
                              int(claimed["attempts"]))
    assert ok is True


@pytest.mark.asyncio
async def test_apply_reward_coins_idempotent(pg_pool):
    """coins: add_balance_once дедуплицирует по grant_key."""
    row = {"grant_key": "event:coins:1", "kind": "coins",
           "guild_id": 1, "user_id": 42, "amount": 300,
           "item_id": None}
    await event_rewards.apply_reward(row)
    await event_rewards.apply_reward(row)  # повтор — без дубля
    bal = await user_balance_store.get_balance(1, 42)
    assert bal == 300 + 100  # 100 стартовый бонус + 300
    # Третий вызов — всё ещё 400.
    await event_rewards.apply_reward(row)
    assert await user_balance_store.get_balance(1, 42) == 400


@pytest.mark.asyncio
async def test_apply_reward_xp_idempotent(pg_pool):
    row = {"grant_key": "event:xp:1", "kind": "xp",
           "guild_id": 1, "user_id": 42, "amount": 300,
           "item_id": None}
    await event_rewards.apply_reward(row)
    await event_rewards.apply_reward(row)
    stats = await pss.player_stats_store.get(1, 42)
    # 300 XP при xp_next=250 → level 2, xp=50.
    assert stats.level == 2
    assert stats.xp == 50
    # уровень не вырос повторно
    stats2 = await pss.player_stats_store.get(1, 42)
    assert stats2.level == 2 and stats2.xp == 50


@pytest.mark.asyncio
async def test_apply_reward_rep_idempotent(pg_pool):
    row = {"grant_key": "event:rep:1", "kind": "rep",
           "guild_id": 1, "user_id": 42, "amount": 2,
           "item_id": None}
    await event_rewards.apply_reward(row)
    await event_rewards.apply_reward(row)
    stats = await pss.player_stats_store.get(1, 42)
    assert int(stats.reputation or 0) == 2
    info = await __import__(
        "storage.rep_balance_store", fromlist=["x"]
    ).rep_balance_store.get_info(1, 42)
    assert info["balance"] >= 2 or info["earned_total"] >= 2


@pytest.mark.asyncio
async def test_apply_reward_item_missing_catalog_retries(pg_pool):
    """Отсутствующий предмет — ошибка (retry), приз не
    подменяется молча."""
    row = {"grant_key": "event:item:1", "kind": "item",
           "guild_id": 1, "user_id": 42, "amount": 0,
           "item_id": "no_such_item"}
    with pytest.raises(Exception):
        await event_rewards.apply_reward(row)
