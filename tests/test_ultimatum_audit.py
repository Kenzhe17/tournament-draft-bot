"""Release-аудит «Ультиматума» 2.0: гонки, рестарт, приватность.

Проверяем на реальном PostgreSQL (pgserver):
- конкурентная финализация одного матча — ровно один результат и один ELO;
- конкурентные голоса двух сторон — консенсус или спор, без зависаний;
- «рестарт» между фазами: состояние полностью восстанавливается из БД;
- идемпотентность повторных нажатий (join, start, finalize);
- приватность: пароли комнат не попадают в текст сетки/PNG/публичные данные;
- отмена/удаление дуэта до старта.
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent.parent))

import pgserver  # noqa: E402

from ultimatum import ULTIMATUM_SCHEMA, ELO_SCHEMA  # noqa: E402
from ultimatum.bracket import Format  # noqa: E402
from ultimatum.service import ult_service  # noqa: E402
from ultimatum.store import ultimatum_store  # noqa: E402
from ultimatum.elo import ult_elo_store  # noqa: E402

GUILD = 999000222


def _user(uid: int, name: str = ""):
    return SimpleNamespace(id=uid, display_name=name or f"U{uid}")


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_audit")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    import storage.db as dbmod

    pool = await asyncpg.create_pool(pg_uri, min_size=4, max_size=12)
    async with pool.acquire() as conn:
        await conn.execute(ULTIMATUM_SCHEMA)
        await conn.execute(ELO_SCHEMA)

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
        ):
            await conn.execute(f"DELETE FROM {t}")
    yield


async def _mk(fmt=Format.SOLO, base=1000, is_test=False):
    t = await ultimatum_store.create_tournament(
        fmt, GUILD, f"AUDIT-{fmt.value}", 1, is_test=is_test,
    )
    need = 16 if fmt is Format.SOLO else 8
    for i in range(need):
        if fmt is Format.SOLO:
            await ultimatum_store.add_entrant(
                t.id, [base + i], [f"P{base+i}"]
            )
        else:
            await ultimatum_store.add_entrant(
                t.id, [base + 2 * i, base + 2 * i + 1],
                [f"A{base+i}", f"B{base+i}"]
            )
    return await ultimatum_store.get_tournament(t.id)


async def _ready_match(t, key="U1-1"):
    return next(
        m for m in await ultimatum_store.get_matches(t.id) if m.key == key
    )


# ---------------------------------------------------------------------------
# Конкурентность
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_finalize_single_result(pg_pool):
    """Два одновременных завершения матча → один результат, один ELO."""
    t = await _mk()
    await ult_service.start(t)
    m = await _ready_match(t)
    winner = m.entrant_a
    uid = (await ultimatum_store.get_entrant(winner)).member_ids[0]

    async def _fin():
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                return await ult_service._finalize_match(
                    conn, m.id, winner, by_admin=False
                )

    results = await asyncio.gather(_fin(), _fin(), _fin())
    done = [r for r in results if r is not None]
    assert len(done) == 1, f"finalize ran {len(done)} times"

    hist = await ult_elo_store.get_history(uid, "solo", 20)
    match_hist = [
        h for h in await _all_history(pg_pool) if h["match_id"] == m.id
    ]
    assert len(match_hist) == 2  # winner+loser, не задвоено


async def _all_history(pool):
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM ult_elo_history")


@pytest.mark.asyncio
async def test_concurrent_votes_consensus(pg_pool):
    """Параллельные голоса сторон: либо консенсус, либо спор —
    матч не зависает."""
    t = await _mk()
    await ult_service.start(t)
    m = await _ready_match(t)
    ea = await ultimatum_store.get_entrant(m.entrant_a)
    eb = await ultimatum_store.get_entrant(m.entrant_b)
    ua, ub = ea.member_ids[0], eb.member_ids[0]

    out1, out2 = await asyncio.gather(
        ult_service.cast_vote(m.id, _user(ua), "a"),
        ult_service.cast_vote(m.id, _user(ub), "a"),
    )
    kinds = {out1.kind, out2.kind}
    m2 = await ultimatum_store.get_match(m.id)
    assert m2.status in ("COMPLETED", "DISPUTED", "AWAITING_CONFIRMATION")
    if m2.status == "COMPLETED":
        assert m2.winner_entrant == m.entrant_a
    # Ни один исход не должен быть 'error' для валидных голосов
    assert "error" not in kinds


@pytest.mark.asyncio
async def test_double_click_join(pg_pool):
    t = await ultimatum_store.create_tournament(
        Format.SOLO, GUILD, "DUP", 1,
    )
    u = _user(123456)
    msgs = await asyncio.gather(
        *[ult_service.register_solo(t, u) for _ in range(5)]
    )
    ents = await ultimatum_store.get_entrants(t.id)
    assert len(ents) == 1
    assert sum("уже участвуете" in m or "в турнире" in m for m in msgs) >= 4


@pytest.mark.asyncio
async def test_double_click_start(pg_pool):
    t = await _mk()
    results = await asyncio.gather(
        *[ult_service.start(t) for _ in range(4)]
    )
    oks = [r for r, _ in results if r]
    assert len(oks) == 1
    matches = await ultimatum_store.get_matches(t.id)
    assert len(matches) == 30


# ---------------------------------------------------------------------------
# Рестарт-устойчивость (состояние только в БД)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_vs_start_race(pg_pool):
    """Регистрация и старт конкурируют за reg-lock — финальное
    состояние всегда консистентно (нет «призрака» вне сетки)."""
    t = await ultimatum_store.create_tournament(
        Format.SOLO, GUILD, "RACE", 1,
    )
    for i in range(15):
        await ultimatum_store.add_entrant(t.id, [3000 + i], [f"P{i}"])
    t = await ultimatum_store.get_tournament(t.id)
    reg_res, start_res = await asyncio.gather(
        ult_service.register_solo(t, _user(9999)),
        ult_service.start(t),
    )
    t_end = await ultimatum_store.get_tournament(t.id)
    ents = await ultimatum_store.get_entrants(t.id)
    matches = await ultimatum_store.get_matches(t.id)
    if t_end.status == "IN_PROGRESS":
        # Регистрация успела до/вместе со стартом — состав полный,
        # сетка создана, никто не «повис» вне.matches
        assert len(ents) == 16 and len(matches) == 30
        seeded = {
            e.id for e in ents if e.seed is not None
        }
        assert len(seeded) == 16
    else:
        # Старт выиграл lock → try_start → 15/16 → откат в
        # REGISTRATION. Заблокированная регистрация могла
        # дождаться lock и записаться ПОСЛЕ отката — тогда
        # 16 участников и повторный «Старт» завершит набор.
        assert t_end.status == "REGISTRATION"
        assert len(ents) in (15, 16) and matches == []


@pytest.mark.asyncio
async def test_restart_mid_tournament(pg_pool):
    """Сыграть часть сетки, «перезапустить» (просто перечитать БД),
    доиграть до конца — состояние не теряется."""
    t = await _mk()
    await ult_service.start(t)

    # Сыграть U1-1..U1-4 (половина первого раунда)
    rng = random.Random(2)
    for i in range(1, 5):
        m = next(
            x for x in await ultimatum_store.get_matches(t.id)
            if x.key == f"U1-{i}"
        )
        w = rng.choice([m.entrant_a, m.entrant_b])
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await ult_service._finalize_match(conn, m.id, w)

    # «Рестарт»: новый объект сервиса, чистое чтение из БД
    svc2 = ult_service.__class__()
    t_re = await svc2.store.get_tournament(t.id)
    matches_re = await svc2.store.get_matches(t.id)
    ents_re = await svc2.store.get_entrants(t.id)

    done = sum(1 for m in matches_re if m.status == "COMPLETED")
    assert done == 4
    # U2-1 и U2-2 уже READY (оба U1 сыграны), L1-1/L1-2 READY
    ready_keys = {m.key for m in matches_re if m.status == "READY"}
    assert {"U2-1", "U2-2", "L1-1", "L1-2"} <= ready_keys
    # Ни один матч не «завис» с пустыми слотами после рестарта
    waiting_empty = [
        m.key for m in matches_re
        if m.status == "WAITING" and m.entrant_a and m.entrant_b
    ]
    assert not waiting_empty

    # Доиграть
    played = 0
    guard = 0
    while guard < 300:
        guard += 1
        ms = await ultimatum_store.get_matches(t.id)
        ready = [
            m for m in ms
            if m.status in ("READY", "IN_PROGRESS",
                            "AWAITING_CONFIRMATION", "DISPUTED")
        ]
        if not ready:
            break
        m = ready[0]
        w = rng.choice([m.entrant_a, m.entrant_b])
        async with pg_pool.acquire() as conn:
            async with conn.transaction():
                await svc2._finalize_match(conn, m.id, w)
        played += 1
    t_end = await ultimatum_store.get_tournament(t.id)
    assert t_end.status == "COMPLETED"
    assert played + 4 in (30, 31)


@pytest.mark.asyncio
async def test_restart_during_registration_and_invite(pg_pool):
    """Регистрация и неотвеченное приглашение переживают рестарт."""
    t = await ultimatum_store.create_tournament(Format.DUO, GUILD, "DUO-R", 1)
    cap, part = _user(5001), _user(5002)
    inv_id, _ = await ult_service.duo_create_invite(t, cap, part)
    # «Рестарт» — перечитать приглашение из БД
    inv = await ultimatum_store.get_invite(inv_id)
    assert inv.status == "pending"
    ok, msg = await ult_service.duo_respond(inv, part, accept=True)
    assert ok
    ents = await ultimatum_store.get_entrants(t.id)
    assert len(ents) == 1 and sorted(ents[0].member_ids) == [5001, 5002]


@pytest.mark.asyncio
async def test_restart_during_dispute(pg_pool):
    """DISPUTED-матч после рестарта разрешается админом."""
    t = await _mk()
    await ult_service.start(t)
    m = await _ready_match(t)
    ea = await ultimatum_store.get_entrant(m.entrant_a)
    eb = await ultimatum_store.get_entrant(m.entrant_b)
    await ult_service.cast_vote(m.id, _user(ea.member_ids[0]), "a")
    await ult_service.cast_vote(m.id, _user(eb.member_ids[0]), "b")
    m2 = await ultimatum_store.get_match(m.id)
    assert m2.status == "DISPUTED"
    # «Рестарт»
    ok, msg = await ult_service.admin_set_result(
        m.id, m.entrant_b, _user(1)
    )
    assert ok
    m3 = await ultimatum_store.get_match(m.id)
    assert m3.status == "COMPLETED" and m3.winner_entrant == m.entrant_b


@pytest.mark.asyncio
async def test_completed_match_cannot_revotecast(pg_pool):
    t = await _mk()
    await ult_service.start(t)
    m = await _ready_match(t)
    ea = await ultimatum_store.get_entrant(m.entrant_a)
    eb = await ultimatum_store.get_entrant(m.entrant_b)
    await ult_service.cast_vote(m.id, _user(ea.member_ids[0]), "a")
    await ult_service.cast_vote(m.id, _user(eb.member_ids[0]), "a")
    assert (await ultimatum_store.get_match(m.id)).status == "COMPLETED"
    out = await ult_service.cast_vote(m.id, _user(ea.member_ids[0]), "b")
    assert out.kind == "error"


# ---------------------------------------------------------------------------
# Приватность комнат
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_room_password_not_in_public_text(pg_pool):
    from views.ultimatum_view import (
        build_bracket_text, build_match_detail,
    )
    t = await _mk()
    await ult_service.start(t)
    m = await _ready_match(t)
    # Участник ставит комнату с паролем
    ea = await ultimatum_store.get_entrant(m.entrant_a)
    ok, _ = await ult_service.set_room(
        m.id, _user(ea.member_ids[0]), "SECRET_ROOM_42", "hunter2"
    )
    assert ok
    # Публичный текст сетки — без комнаты вообще
    matches = await ultimatum_store.get_matches(t.id)
    ents = {e.id: e for e in await ultimatum_store.get_entrants(t.id)}
    text = build_bracket_text(t, matches, ents)
    assert "hunter2" not in text and "SECRET_ROOM_42" not in text
    # PNG не содержит пароля (рендер не использует room_*)
    from ultimatum.bracket_png import render_bracket_png
    png = render_bracket_png(t, matches, ents)
    assert b"hunter2" not in png
    # Карточка матча для постороннего — без пароля
    m2 = await ultimatum_store.get_match(m.id)
    outsider = build_match_detail(
        t, m2, ents, [], viewer_id=777777, is_org=False
    )
    blob = str(outsider.to_components())
    assert "hunter2" not in blob and "SECRET_ROOM_42" not in blob
    # Для участника — виден
    insider = build_match_detail(
        t, m2, ents, [], viewer_id=ea.member_ids[0], is_org=False
    )
    assert "hunter2" in str(insider.to_components())


@pytest.mark.asyncio
async def test_room_completed_match_locked(pg_pool):
    t = await _mk()
    await ult_service.start(t)
    m = await _ready_match(t)
    ea = await ultimatum_store.get_entrant(m.entrant_a)
    eb = await ultimatum_store.get_entrant(m.entrant_b)
    await ult_service.cast_vote(m.id, _user(ea.member_ids[0]), "a")
    await ult_service.cast_vote(m.id, _user(eb.member_ids[0]), "a")
    ok, msg = await ult_service.set_room(
        m.id, _user(ea.member_ids[0]), "X", "Y"
    )
    assert not ok and "заверш" in msg.lower()


# ---------------------------------------------------------------------------
# Дуо: выход и слоты
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duo_leave_frees_slot(pg_pool):
    t = await ultimatum_store.create_tournament(Format.DUO, GUILD, "DUO-L", 1)
    a, b = _user(6001), _user(6002)
    inv_id, _ = await ult_service.duo_create_invite(t, a, b)
    await ult_service.duo_respond(await ultimatum_store.get_invite(inv_id), b, True)
    msg = await ult_service.duo_leave(
        await ultimatum_store.get_tournament(t.id), a
    )
    assert "освобождён" in msg or "распущен" in msg
    ents = await ultimatum_store.get_entrants(t.id)
    assert ents == []


@pytest.mark.asyncio
async def test_duo_locked_after_start(pg_pool):
    t = await _mk(Format.DUO, base=6100)
    await ult_service.start(t)
    msg = await ult_service.duo_leave(t, _user(6100))
    assert "заблокирован" in msg


# ---------------------------------------------------------------------------
# ELO: раздельные рейтинги и идемпотентность
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_elo_solo_duo_independent(pg_pool):
    ts = await _mk(Format.SOLO, base=7000)
    await ult_service.start(ts)
    m = await _ready_match(ts)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await ult_service._finalize_match(conn, m.id, m.entrant_a)
    uid = (await ultimatum_store.get_entrant(m.entrant_a)).member_ids[0]
    rs = await ult_elo_store.get_rating(uid, "solo")
    rd = await ult_elo_store.get_rating(uid, "duo")
    assert rs and rs["rated_matches"] == 1 and rs["elo"] != 1000
    assert rd is None

    td = await _mk(Format.DUO, base=7100)
    await ult_service.start(td)
    md = await _ready_match(td)
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await ult_service._finalize_match(conn, md.id, md.entrant_a)
    ud = (await ultimatum_store.get_entrant(md.entrant_a)).member_ids[0]
    rdd = await ult_elo_store.get_rating(ud, "duo")
    rsd = await ult_elo_store.get_rating(ud, "solo")
    assert rdd and rdd["rated_matches"] == 1
    assert rsd is None


@pytest.mark.asyncio
async def test_elo_double_finalize_protection(pg_pool):
    """Повторный _finalize_match не начисляет ELO дважды."""
    t = await _mk()
    await ult_service.start(t)
    m = await _ready_match(t)
    winner = m.entrant_a
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            await ult_service._finalize_match(conn, m.id, winner)
    uid = (await ultimatum_store.get_entrant(winner)).member_ids[0]
    before = (await ult_elo_store.get_rating(uid, "solo"))["elo"]
    hist_n = len(await ult_elo_store.get_history(uid, "solo", 50))
    async with pg_pool.acquire() as conn:
        async with conn.transaction():
            res = await ult_service._finalize_match(conn, m.id, winner)
    assert res is None
    after = (await ult_elo_store.get_rating(uid, "solo"))["elo"]
    assert before == after
    assert len(await ult_elo_store.get_history(uid, "solo", 50)) == hist_n
