"""Интеграционные тесты cashcup_test_sessions на отдельной тестовой
PostgreSQL (pgserver, временный кластер — production не используется).

Покрывают: create_snapshot → start → simulate → restart → archive,
идемпотентность/гонки mutate, и ГЛАВНОЕ — изоляцию: cashcup_tournaments
полностью неизменна до и после всего тестового прогона.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from services.cashcup_sim import simulate_to_final
from services.cashcup_store import cashcup_store
from services.cashcup_test_store import (
    CashCupTestError,
    cashcup_test_store,
)

G = 555000


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_cctest")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    cashcup_store._ready = False
    cashcup_test_store._ready = False
    await cashcup_store.ensure_schema()
    await cashcup_test_store.ensure_schema()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cashcup_audit")
        await conn.execute("DELETE FROM cashcup_tournaments")
        await conn.execute("DELETE FROM cashcup_test_audit")
        await conn.execute("DELETE FROM cashcup_test_sessions")
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


def _teams(n: int = 6) -> list[dict]:
    teams = []
    for i in range(n):
        dids = [9000 + i * 10 + j for j in range(5)]
        members = [{"discord_id": d, "ff_uid": str(700000 + i * 10 + j),
                    "nick": f"p{i}-{j}", "region": "ru", "device": "pc",
                    "ff_ok": True, "verified": True,
                    "joined_at": "2026-01-01T00:00:00+00:00"}
                   for j, d in enumerate(dids)]
        teams.append({"id": f"tm{i:02d}", "name": f"Team{i}",
                      "captain": dids[0], "members": members,
                      "pending": [], "lineup": dids[:4]})
    return teams


def _snapshot_state(n: int = 6) -> dict:
    return {"status": "registration", "prize": "TEST PRIZE",
            "pool": 0, "teams": _teams(n), "bracket": None,
            "payouts": []}


async def _prod_state(pg_pool) -> str:
    """Контрольная сумма настоящего Cash Cup (read-only)."""
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COALESCE(jsonb_agg(t ORDER BY t.id), '[]'::jsonb) "
            "FROM cashcup_tournaments t")
    return json.dumps(row[0], sort_keys=True, default=str)


@pytest.mark.asyncio
async def test_full_test_session_lifecycle(pg_pool):
    # 1) Создаём «настоящий» Cash Cup и регистрируем команды.
    real = await cashcup_store.create(G, 111, 999, "REAL PRIZE")

    def add_teams(st):
        st["teams"] = _teams(6)
        return len(st["teams"])
    await cashcup_store.mutate(G, 999, "seed", add_teams,
                               tournament_id=real["tournament_id"])
    before = await _prod_state(pg_pool)

    # 2) Импорт snapshot → тестовая сессия.
    sess = await cashcup_test_store.create_snapshot(
        G, 222, 999, _snapshot_state(6))
    assert sess["status"] == "snapshot"
    assert sess["session_id"] != real["tournament_id"]

    # 3) Старт теста → bracket, симуляция до финала.
    def start(st):
        assert st["status"] == "snapshot"
        from services.cashcup_bracket import create_bracket
        st["bracket"] = create_bracket([t["id"] for t in st["teams"]],
                                       seed=1)
        st["status"] = "running"
        return True
    await cashcup_test_store.mutate(G, 999, "start", start,
                                    session_id=sess["session_id"])

    def sim_change(st):
        summary = simulate_to_final(st, seed=1)
        st["last_sim"] = {"places": summary["places"]}
        return summary
    _, summary = await cashcup_test_store.mutate(
        G, 999, "simulate", sim_change,
        session_id=sess["session_id"])
    assert summary["places"]["champion"]

    mid = await _prod_state(pg_pool)
    assert mid == before, "настоящий Cash Cup изменился!"

    # 4) Заново → snapshot; удалить → archived.
    def restart(st):
        fresh = json.loads(json.dumps(st["base"]))
        fresh.update({"status": "snapshot", "bracket": None,
                      "guild_id": st["guild_id"], "test": True,
                      "base": st["base"]})
        st.clear(); st.update(fresh)
        return True
    await cashcup_test_store.mutate(G, 999, "restart", restart,
                                    session_id=sess["session_id"])
    got = await cashcup_test_store.get(G, sess["session_id"])
    assert got["status"] == "snapshot" and got["bracket"] is None

    def arch(st):
        st["status"] = "archived"
    await cashcup_test_store.mutate(G, 999, "delete", arch,
                                    session_id=sess["session_id"])
    assert await cashcup_test_store.latest(G) is None or \
        (await cashcup_test_store.latest(G))["status"] != "archived"

    after = await _prod_state(pg_pool)
    assert after == before


@pytest.mark.asyncio
async def test_mutate_version_conflict(pg_pool):
    sess = await cashcup_test_store.create_snapshot(
        G, 222, 999, _snapshot_state(4))
    sid = sess["session_id"]

    def bump(st):
        st["payouts"] = list(st.get("payouts") or []) + [1]
    await cashcup_test_store.mutate(G, 999, "a", bump, session_id=sid)
    # Повторный mutate на свежем состоянии работает (version растёт).
    def bump2(st):
        st["payouts"] = list(st.get("payouts") or []) + [2]
    await cashcup_test_store.mutate(G, 999, "b", bump2, session_id=sid)
    got = await cashcup_test_store.get(G, sid)
    assert got["payouts"] == [1, 2]


@pytest.mark.asyncio
async def test_concurrent_mutations_serialized(pg_pool):
    sess = await cashcup_test_store.create_snapshot(
        G, 222, 999, _snapshot_state(4))
    sid = sess["session_id"]

    async def worker(n):
        def change(st):
            st["payouts"] = sorted(
                (st.get("payouts") or []) + [n])
        try:
            await cashcup_test_store.mutate(G, 999, f"w{n}", change,
                                            session_id=sid)
            return True
        except CashCupTestError:
            return False

    results = await asyncio.gather(*(worker(i) for i in range(8)))
    # FOR UPDATE сериализует; при экстремальной гонке version-check
    # отклоняет — это безопасно, но в рамках одной сессии gather
    # на одном event loop выполняется последовательно внутри
    # транзакций, поэтому все должны пройти.
    assert all(results)
    got = await cashcup_test_store.get(G, sid)
    assert got["payouts"] == list(range(8))


@pytest.mark.asyncio
async def test_archived_session_rejects_mutation(pg_pool):
    sess = await cashcup_test_store.create_snapshot(
        G, 222, 999, _snapshot_state(4))
    sid = sess["session_id"]

    def arch(st):
        st["status"] = "archived"
    await cashcup_test_store.mutate(G, 999, "delete", arch, session_id=sid)

    def no(st):
        st["payouts"] = [1]
    with pytest.raises(CashCupTestError):
        await cashcup_test_store.mutate(G, 999, "late", no, session_id=sid)


@pytest.mark.asyncio
async def test_manual_match_flow_checkin_room_result_dispute(pg_pool):
    """Check-in → комната → результат → следующий раунд → спор →
    решение — тот же путь, что у кнопок панели, через store.mutate."""
    from services.cashcup_bracket import (
        create_bracket,
        ready_matches,
        record_result,
    )
    sess = await cashcup_test_store.create_snapshot(
        G, 222, 999, _snapshot_state(4))
    sid = sess["session_id"]

    def start(st):
        st["bracket"] = create_bracket(
            [t["id"] for t in st["teams"]], seed=1)
        st["status"] = "running"
    await cashcup_test_store.mutate(G, 999, "start", start, session_id=sid)

    def find(st, mid):
        b = st["bracket"]
        active = set(b.get("active_keys") or [])
        return next((m for m in b["matches"]
                     if m["id"] == mid and m["id"] in active
                     and m.get("status") in ("ready", "disputed")), None)

    # Первый активный матч.
    state = await cashcup_test_store.get(G, sid)
    mid = ready_matches(state["bracket"])[0]["id"]

    # 1) Виртуальный check-in обоих капитанов.
    def checkin(st):
        m = find(st, mid)
        caps = [int(t["captain"]) for t in st["teams"]
                if t["id"] in (m["a"], m["b"])]
        for c in caps:
            if c not in m["checkins"]:
                m["checkins"].append(c)
        m["test_checkin"] = True
    await cashcup_test_store.mutate(G, 999, "checkin", checkin,
                                    session_id=sid)

    # 2) Фиктивная комната.
    def room(st):
        m = find(st, mid)
        assert len(m["checkins"]) == 2
        m["room_id"] = f"TEST-{mid}"
        m["test_room"] = True
    await cashcup_test_store.mutate(G, 999, "room", room, session_id=sid)

    # 3) Спор блокирует результат.
    def dispute(st):
        m = find(st, mid)
        m["status"] = "disputed"
        m["dispute"] = {"reason": "test", "evidence": []}
    await cashcup_test_store.mutate(G, 999, "dispute", dispute,
                                    session_id=sid)

    def blocked(st):
        m = find(st, mid)
        assert m is not None and m["status"] == "disputed"
        # record_result не принимает disputed: ready_matches его
        # фильтрует — продвижение результата заблокировано.
        from services.cashcup_bracket import ready_matches as rm
        assert all(x["id"] != mid for x in rm(st["bracket"]))
    await cashcup_test_store.mutate(G, 999, "assert_blocked", blocked,
                                    session_id=sid)

    # 4) Решение спора записывает результат.
    def resolve(st):
        m = find(st, mid)
        m["status"] = "ready"
        record_result(st["bracket"], mid, 1, 0,
                      evidence="TEST:dispute")
        if st["bracket"]["champion"]:
            st["status"] = "completed"
    await cashcup_test_store.mutate(G, 999, "resolve", resolve,
                                    session_id=sid)

    # 5) Матч завершён, сетка перешла дальше.
    state = await cashcup_test_store.get(G, sid)
    done = next(m for m in state["bracket"]["matches"] if m["id"] == mid)
    assert done["status"] == "completed" and done["winner"]


@pytest.mark.asyncio
async def test_session_isolated_per_guild(pg_pool):
    s1 = await cashcup_test_store.create_snapshot(
        G, 1, 1, _snapshot_state(3))
    s2 = await cashcup_test_store.create_snapshot(
        G + 1, 1, 1, _snapshot_state(3))
    assert await cashcup_test_store.get(G, s2["session_id"]) is None
    assert await cashcup_test_store.get(G + 1, s1["session_id"]) is None
