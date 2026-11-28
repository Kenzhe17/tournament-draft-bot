"""Интеграционные тесты «Ультиматума» 2.0 на реальном PostgreSQL.

pgserver поднимает изолированный инстанс; схема ult2_*/ult_elo_*
применяется из ultimatum/__init__.py. Проверяем:
- регистрацию Соло/Дуо (включая подтверждение дуэта);
- идемпотентный старт и построение сетки;
- голосование и фиксацию результата;
- спор и решение организатора;
- GF1/GF2 и итоговые места;
- независимость рейтингов Соло/Дуо и защиту от дабл-ELO;
- тестовые турниры без ELO.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

import pgserver  # noqa: E402

from storage.vac_store import _DDL as VAC_SCHEMA  # noqa: E402
from ultimatum import ULTIMATUM_SCHEMA, ELO_SCHEMA  # noqa: E402
from ultimatum.bracket import Format  # noqa: E402
from ultimatum.service import ult_service  # noqa: E402
from ultimatum.store import ultimatum_store  # noqa: E402
from ultimatum.elo import ult_elo_store  # noqa: E402

GUILD = 999000111


def _user(uid: int, name: str = ""):
    return SimpleNamespace(id=uid, display_name=name or f"U{uid}", name=name or f"u{uid}")


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_ult")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    import storage.db as dbmod

    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=8)
    async with pool.acquire() as conn:
        await conn.execute(ULTIMATUM_SCHEMA)
        await conn.execute(ELO_SCHEMA)
        await conn.execute(VAC_SCHEMA)

    prev_url = dbmod.DATABASE_URL
    dbmod.DATABASE_URL = pg_uri
    dbmod._pool = pool
    try:
        yield pool
    finally:
        dbmod._pool = None
        dbmod.DATABASE_URL = prev_url
        await pool.close()


@pytest_asyncio.fixture(autouse=True)
async def clean(pg_pool):
    async with pg_pool.acquire() as conn:
        for t in (
            "ult_elo_history", "ult_elo_ratings",
            "ult2_audit", "ult2_votes", "ult2_duo_invites",
            "ult2_matches", "ult2_entrants", "ult2_tournaments",
            "vac_bans", "vac_profiles",
        ):
            await conn.execute(f"DELETE FROM {t}")
    yield


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

async def _mk_tournament(fmt=Format.SOLO, is_test=False):
    return await ultimatum_store.create_tournament(
        fmt, GUILD, f"TEST-{fmt.value}", 1, is_test=is_test,
    )


async def _fill_solo(t, n=16, base=100):
    for i in range(n):
        await ultimatum_store.add_entrant(
            t.id, [base + i], [f"P{base+i}"],
        )


async def _fill_duo(t, n=8, base=500):
    for i in range(n):
        await ultimatum_store.add_entrant(
            t.id, [base + 2 * i, base + 2 * i + 1],
            [f"A{base+i}", f"B{base+i}"],
        )


async def _play_ready(t, rng):
    """Сыграть все READY-матчи случайными победителями через
    внутреннюю фиксацию сервиса (как test_sim)."""
    played = 0
    guard = 0
    while guard < 300:
        guard += 1
        matches = await ultimatum_store.get_matches(t.id)
        ready = [m for m in matches if m.status in (
            "READY", "IN_PROGRESS", "AWAITING_CONFIRMATION", "DISPUTED",
        )]
        if not ready:
            break
        m = ready[0]
        winner = rng.choice([m.entrant_a, m.entrant_b])
        from storage.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ult_service._finalize_match(
                    conn, m.id, winner, by_admin=False,
                )
        played += 1
    return played


# ---------------------------------------------------------------------------
# Соло
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_solo_full_tournament(pg_pool):
    t = await _mk_tournament(Format.SOLO)
    await _fill_solo(t)
    ok, msg = await ult_service.start(await ultimatum_store.get_tournament(t.id))
    assert ok, msg

    matches = await ultimatum_store.get_matches(t.id)
    assert len(matches) == 30
    ready = [m for m in matches if m.status == "READY"]
    assert len(ready) == 8 and all(m.key.startswith("U1-") for m in ready)

    rng = random.Random(7)
    played = await _play_ready(t, rng)
    assert played == 30 or played == 31  # GF2 возможен

    t2 = await ultimatum_store.get_tournament(t.id)
    assert t2.status == "COMPLETED"
    assert t2.champion_entrant_id

    entrants = await ultimatum_store.get_entrants(t.id)
    places = [e for e in entrants if e.final_place]
    assert {e.final_place for e in places} >= {1, 2, 3, 4}
    groups = {e.place_group for e in entrants if e.place_group and not e.final_place}
    assert groups <= {"5–6", "7–8", "9–12", "13–16"}

    # ELO: все 16 игроков получили solo-рейтинг
    for i in range(16):
        r = await ult_elo_store.get_rating(100 + i, "solo")
        assert r is not None and r["rated_matches"] > 0
        # Дуо-рейтинг не создан
        assert await ult_elo_store.get_rating(100 + i, "duo") is None


@pytest.mark.asyncio
async def test_solo_start_idempotent(pg_pool):
    t = await _mk_tournament(Format.SOLO)
    await _fill_solo(t)
    ok1, _ = await ult_service.start(await ultimatum_store.get_tournament(t.id))
    ok2, msg2 = await ult_service.start(await ultimatum_store.get_tournament(t.id))
    assert ok1 and not ok2
    matches = await ultimatum_store.get_matches(t.id)
    assert len(matches) == 30  # сетка не продублировалась


@pytest.mark.asyncio
async def test_solo_vote_flow(pg_pool):
    t = await _mk_tournament(Format.SOLO)
    await _fill_solo(t, base=200)
    await ult_service.start(await ultimatum_store.get_tournament(t.id))
    m = next(x for x in await ultimatum_store.get_matches(t.id)
             if x.key == "U1-1")
    ent_a = await ultimatum_store.get_entrant(m.entrant_a)
    ent_b = await ultimatum_store.get_entrant(m.entrant_b)
    ua, ub = ent_a.member_ids[0], ent_b.member_ids[0]

    # Один голос — матч ждёт второго
    out = await ult_service.cast_vote(m.id, _user(ua), "a")
    assert out.kind == "voted"
    # Противоположные голоса — спор
    out = await ult_service.cast_vote(m.id, _user(ub), "b")
    assert out.kind == "disputed"
    m2 = await ultimatum_store.get_match(m.id)
    assert m2.status == "DISPUTED"

    # Организатор решает
    ok, msg = await ult_service.admin_set_result(m.id, m.entrant_a, _user(1))
    assert ok, msg
    m3 = await ultimatum_store.get_match(m.id)
    assert m3.status == "COMPLETED"
    assert m3.winner_entrant == m.entrant_a


@pytest.mark.asyncio
async def test_vote_requires_participant(pg_pool):
    t = await _mk_tournament(Format.SOLO)
    await _fill_solo(t, base=300)
    await ult_service.start(await ultimatum_store.get_tournament(t.id))
    m = next(x for x in await ultimatum_store.get_matches(t.id)
             if x.key == "U1-1")
    out = await ult_service.cast_vote(m.id, _user(999999), "a")
    assert out.kind == "error"


@pytest.mark.asyncio
async def test_finalize_idempotent_no_double_elo(pg_pool):
    t = await _mk_tournament(Format.SOLO)
    await _fill_solo(t, base=400)
    await ult_service.start(await ultimatum_store.get_tournament(t.id))
    m = next(x for x in await ultimatum_store.get_matches(t.id)
             if x.status == "READY")
    winner = m.entrant_a
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await ult_service._finalize_match(conn, m.id, winner)
    # Повторная фиксация — не должна менять ELO
    hist_before = await ult_elo_store.get_history(
        (await ultimatum_store.get_entrant(winner)).member_ids[0], "solo", 50
    )
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            res = await ult_service._finalize_match(conn, m.id, winner)
    assert res is None
    hist_after = await ult_elo_store.get_history(
        (await ultimatum_store.get_entrant(winner)).member_ids[0], "solo", 50
    )
    assert len(hist_after) == len(hist_before)


@pytest.mark.asyncio
async def test_test_tournament_no_elo(pg_pool):
    t = await _mk_tournament(Format.SOLO, is_test=True)
    await _fill_solo(t, base=600)
    await ult_service.start(await ultimatum_store.get_tournament(t.id))
    await _play_ready(t, random.Random(3))
    r = await ult_elo_store.get_rating(600, "solo")
    assert r is None or r["rated_matches"] == 0


# ---------------------------------------------------------------------------
# Дуо
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duo_invite_flow(pg_pool):
    t = await _mk_tournament(Format.DUO)
    cap, part = _user(700), _user(701, "Partner")
    inv_id, msg = await ult_service.duo_create_invite(
        await ultimatum_store.get_tournament(t.id), cap, part
    )
    assert inv_id and "отправлено" in msg
    inv = await ultimatum_store.get_invite(inv_id)
    ok, msg = await ult_service.duo_respond(inv, part, accept=True)
    assert ok, msg
    ents = await ultimatum_store.get_entrants(t.id)
    assert len(ents) == 1
    assert sorted(ents[0].member_ids) == [700, 701]

    # Повторное принятие — уже обработано
    ok2, _ = await ult_service.duo_respond(inv, part, accept=True)
    assert not ok2


@pytest.mark.asyncio
async def test_duo_decline_and_reinvite(pg_pool):
    t = await _mk_tournament(Format.DUO)
    cap, part = _user(710), _user(711)
    inv_id, _ = await ult_service.duo_create_invite(
        await ultimatum_store.get_tournament(t.id), cap, part
    )
    inv = await ultimatum_store.get_invite(inv_id)
    ok, _ = await ult_service.duo_respond(inv, part, accept=False)
    assert ok
    # Капитан может пригласить снова
    inv2, msg = await ult_service.duo_create_invite(
        await ultimatum_store.get_tournament(t.id), cap, part
    )
    assert inv2 and "отправлено" in msg


@pytest.mark.asyncio
async def test_duo_cannot_multi_team(pg_pool):
    t = await _mk_tournament(Format.DUO)
    a, b, c = _user(720), _user(721), _user(722)
    i1, _ = await ult_service.duo_create_invite(
        await ultimatum_store.get_tournament(t.id), a, b
    )
    await ult_service.duo_respond(await ultimatum_store.get_invite(i1), b, True)
    # a уже в команде — не может создать новую
    i2, msg = await ult_service.duo_create_invite(
        await ultimatum_store.get_tournament(t.id), a, c
    )
    assert i2 is None and "уже в дуэте" in msg


@pytest.mark.asyncio
async def test_duo_full_tournament_elo(pg_pool):
    t = await _mk_tournament(Format.DUO)
    # 8 дуэтов через service
    base = 800
    for i in range(8):
        cap, part = _user(base + 2 * i), _user(base + 2 * i + 1)
        inv_id, _ = await ult_service.duo_create_invite(
            await ultimatum_store.get_tournament(t.id), cap, part
        )
        await ult_service.duo_respond(
            await ultimatum_store.get_invite(inv_id), part, True
        )
    ents = await ultimatum_store.get_entrants(t.id)
    assert len(ents) == 8

    ok, msg = await ult_service.start(await ultimatum_store.get_tournament(t.id))
    assert ok, msg
    matches = await ultimatum_store.get_matches(t.id)
    assert len(matches) == 14

    await _play_ready(t, random.Random(11))
    t2 = await ultimatum_store.get_tournament(t.id)
    assert t2.status == "COMPLETED"

    # Дуо ELO создан у всех 16 игроков, Соло — нет
    for i in range(16):
        rd = await ult_elo_store.get_rating(base + i, "duo")
        rs = await ult_elo_store.get_rating(base + i, "solo")
        assert rd is not None and rd["rated_matches"] > 0
        assert rs is None


@pytest.mark.asyncio
async def test_duo_vote_team_consensus(pg_pool):
    """В Дуо достаточно согласия сторон; голоса напарников одной
    команды учитываются как голоса этой команды."""
    t = await _mk_tournament(Format.DUO)
    base = 900
    for i in range(8):
        cap, part = _user(base + 2 * i), _user(base + 2 * i + 1)
        inv_id, _ = await ult_service.duo_create_invite(
            await ultimatum_store.get_tournament(t.id), cap, part
        )
        await ult_service.duo_respond(
            await ultimatum_store.get_invite(inv_id), part, True
        )
    await ult_service.start(await ultimatum_store.get_tournament(t.id))
    m = next(x for x in await ultimatum_store.get_matches(t.id)
             if x.key == "U1-1")
    ea = await ultimatum_store.get_entrant(m.entrant_a)
    eb = await ultimatum_store.get_entrant(m.entrant_b)
    # Оба члена команды A голосуют за A; один член B — за A → консенсус
    for uid in ea.member_ids:
        out = await ult_service.cast_vote(m.id, _user(uid), "a")
    out = await ult_service.cast_vote(m.id, _user(eb.member_ids[0]), "a")
    assert out.kind == "completed", out.message
    m2 = await ultimatum_store.get_match(m.id)
    assert m2.status == "COMPLETED"
    assert m2.winner_entrant == m.entrant_a
