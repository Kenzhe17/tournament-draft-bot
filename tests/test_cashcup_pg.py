"""Интеграционные тесты CASH CUP на отдельной тестовой PostgreSQL.

Не используют production-БД: pgserver поднимает временный кластер.
Покрывают: create → регистрация двух команд → start (bracket) →
check-in → комната → результаты W/L/GF → completed → ручная
отметка выплаты (однократно) → аудит → stale tournament_id →
конкурентные mutate.
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")

import storage.db as dbmod
from services.cashcup_store import CashCupError, cashcup_store
from services.cashcup_bracket import (
    BracketError,
    create_bracket,
    ready_matches,
    record_final_game,
    record_result,
)

G = 777000


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_cashcup")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=10)
    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    cashcup_store._ready = False
    await cashcup_store.ensure_schema()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM cashcup_audit")
        await conn.execute("DELETE FROM cashcup_tournaments")
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


def _team(tid: str, name: str, captain: int) -> dict:
    return {
        "id": tid, "name": name, "captain": captain,
        "members": [{"discord_id": captain, "device": "mobile",
                     "ff_uid": "1", "nick": "n", "region": "ru",
                     "verified": True, "ff_ok": True,
                     "joined_at": "2026-11-18T00:00:00+00:00"}],
        "pending": [], "lineup": [captain],
        "sub_invites": [], "br_team_id": None,
    }


async def _mk_state(pg_pool):
    """create + две команды + start — состояние running с сеткой."""
    state = await cashcup_store.create(
        G, 100, 1, "тест", prize1=100000, currency="KZT")
    tid = state["tournament_id"]

    def add_teams(st):
        st["teams"] = [_team("tA", "Alpha", 11),
                       _team("tB", "Beta", 22)]

    await cashcup_store.mutate(G, 1, "add_teams", add_teams,
                               tournament_id=tid)

    def start(st):
        st["bracket"] = create_bracket([t["id"] for t in st["teams"]])
        st["status"] = "running"

    state, _ = await cashcup_store.mutate(
        G, 1, "start", start, tournament_id=tid)
    return tid, state


@pytest.mark.asyncio
async def test_two_team_tournament_full_flow_pg(pg_pool):
    tid, state = await _mk_state(pg_pool)
    assert state["status"] == "running"
    b = state["bracket"]
    w = ready_matches(b)
    assert len(w) == 1 and w[0]["bracket"] == "W"
    mid = w[0]["id"]

    # Check-in обоих капитанов (мутации под row lock).
    for cap in (11, 22):
        def checkin(st, _cap=cap):
            m = next(x for x in ready_matches(st["bracket"])
                     if x["id"] == mid)
            if _cap not in m["checkins"]:
                m["checkins"].append(_cap)
        await cashcup_store.mutate(G, cap, "checkin", checkin,
                                   tournament_id=tid)

    # Комната матча.
    def room(st):
        m = next(x for x in ready_matches(st["bracket"])
                 if x["id"] == mid)
        m["room_id"] = "123456"
        m["room_password"] = "pw"
    await cashcup_store.mutate(G, 1, "set_room", room, tournament_id=tid)

    # Результат W (BO1) → переход в нижнюю сетку → GF.
    def res_w(st):
        m = record_result(st["bracket"], mid, 1, 0,
                          evidence="https://discord.com/channels/1/2")
        return m["winner"]
    state, winner = await cashcup_store.mutate(
        G, 1, "result", res_w, tournament_id=tid)
    assert winner == state["bracket"]["upper"][0]

    # GF (BO3) — две игры record_final_game → чемпион → completed.
    gf = next(m for m in ready_matches(state["bracket"])
              if m["bracket"] == "GF")
    for _ in range(2):
        def gf_game(st):
            return record_final_game(
                st["bracket"], "a",
                evidence="https://discord.com/channels/1/3")
        state, _ = await cashcup_store.mutate(
            G, 1, "gf_game", gf_game, tournament_id=tid)
    assert state["status"] == "completed" or \
        state["bracket"]["champion"] is not None
    if state["bracket"]["champion"]:
        # имитируем перевод статуса как в cog.result
        def complete(st):
            if st["bracket"]["champion"]:
                st["status"] = "completed"
        state, _ = await cashcup_store.mutate(
            G, 1, "complete", complete,
            include_finished=True, tournament_id=tid)

    # Ручная отметка выплаты — однократно.
    champ_id = state["bracket"]["champion"]
    champ = next(t for t in state["teams"] if t["id"] == champ_id)
    recip = champ["members"][0]["discord_id"]

    def payout(st):
        if st["status"] != "completed":
            raise CashCupError("not completed")
        if st["payouts"]:
            raise CashCupError("already paid")
        st["payouts"].append({"recipient": recip, "amount": 100000,
                              "currency": "KZT", "place": 1,
                              "manual": True})
    state, _ = await cashcup_store.mutate(
        G, 1, "payout", payout,
        include_finished=True, tournament_id=tid)
    assert len(state["payouts"]) == 1

    # Повторная отметка — запрещена даже конкурентно.
    with pytest.raises(CashCupError):
        await cashcup_store.mutate(
            G, 1, "payout", payout,
            include_finished=True, tournament_id=tid)

    # Аудит: все действия записаны в cashcup_audit.
    async with pg_pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM cashcup_audit WHERE tournament_id=$1",
            tid)
    assert n >= 8

    # Перечитывание из PG подтверждает персистентность.
    fresh = await cashcup_store.latest(G)
    assert fresh["tournament_id"] == tid
    assert fresh["bracket"]["champion"] == champ_id
    assert len(fresh["payouts"]) == 1


@pytest.mark.asyncio
async def test_stale_tournament_id_rejected(pg_pool):
    tid, _ = await _mk_state(pg_pool)
    # Несуществующий tid → ошибка, состояние не тронуто.
    with pytest.raises(CashCupError):
        await cashcup_store.mutate(
            G, 1, "hack", lambda st: st.update(status="completed"),
            tournament_id="deadbeef0000")
    state = await cashcup_store.active(G)
    assert state["tournament_id"] == tid
    assert state["status"] == "running"


@pytest.mark.asyncio
async def test_concurrent_mutate_serialized(pg_pool):
    """Два mutate по одной строке: row-lock + version —
    оба применяются последовательно, без lost update."""
    tid, state = await _mk_state(pg_pool)

    def bump_a(st):
        st["counter_a"] = st.get("counter_a", 0) + 1

    def bump_b(st):
        st["counter_b"] = st.get("counter_b", 0) + 1

    await asyncio.gather(
        cashcup_store.mutate(G, 1, "a", bump_a, tournament_id=tid),
        cashcup_store.mutate(G, 1, "b", bump_b, tournament_id=tid),
    )
    state = await cashcup_store.active(G)
    assert state.get("counter_a") == 1
    assert state.get("counter_b") == 1
    assert state["version"] == 4  # create(0) + add_teams + start + a + b
