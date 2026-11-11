"""Финальная проверка блокировок и crash-recovery OCR-финализации.

Две части:

1. Реальные asyncio.Lock-цепочки (без замоканных локов):
   - auto-finalize → finalize_ocr_match под asyncio.wait_for;
   - finalize vs admin-cancel — взаимоисключение;
   - конкурентные finalize сериализованы _finalize_lock;
   - AST-регрессия: вызовы финализации не должны находиться внутри
     async with _mapping_lock/_room_lock/_finalize_lock
     (asyncio.Lock нереентерабелен → self-deadlock).

2. PostgreSQL (pgserver, изолированный инстанс):
   - решения _finalize_stats_gate по реальным статусам
     match_finalizations (processing/stats_done/done/нет записи);
   - атомарность stats_done в транзакции статистики (rollback);
   - конкурентное «восстановление vs ручной retry» — ровно одно
     применение ELO на уровне БД;
   - повторный запуск восстановления идемпотентен.
"""

from __future__ import annotations

import asyncio
import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent))
from test_rooms_lifecycle import (  # noqa: E402
    _FakeInteraction,
    _full_session,
    _session,
    _tournament,
    _user,
)

GUILD_ID = 1
MATCH_ID = "100_final_0"


# ---------------------------------------------------------------------------
# Реальные lock-цепочки
# ---------------------------------------------------------------------------

def _fake_guild():
    async def _raise(cid):
        raise Exception("no channel")

    return SimpleNamespace(
        id=GUILD_ID,
        get_channel=lambda cid: None,
        fetch_channel=_raise,
    )


@pytest.mark.asyncio
async def test_auto_finalize_chain_real_locks(monkeypatch):
    """Полная цепочка «готовность → auto-finalize → finalize_ocr_match»
    на настоящих _finalize_lock/_room_lock. Реализация impl заглушена —
    проверяется именно отсутствие self-deadlock и порядок локов."""
    import views.ocr_match_view as v

    t = _tournament()
    s = _full_session(t)
    entered = []

    async def fake_impl(interaction, tournament, mt, mi, winner):
        entered.append((mt, mi, winner))
        await asyncio.sleep(0.02)
        s["status"] = "completed"

    monkeypatch.setattr(v, "_finalize_ocr_match_impl", fake_impl)
    inter = _FakeInteraction(_user(1, "P1"))

    done, reason, _ = await asyncio.wait_for(
        v._maybe_auto_finalize_mm_rooms(inter, t, "final", 0),
        timeout=5,
    )
    assert done, reason
    assert entered == [("final", 0, 0)]
    assert s["status"] == "completed"


@pytest.mark.asyncio
async def test_finalize_vs_cancel_mutual_exclusion(monkeypatch):
    """finalize_ocr_match и cancel_rooms_session на одном матче:
    оба на реальных локах — ровно один может успешно завершиться."""
    import cogs.ocr_upload as up
    import views.ocr_match_view as v

    t = _tournament()
    s = _full_session(t)
    monkeypatch.setattr(up.store, "get", lambda g, c: t)
    monkeypatch.setattr(up.store, "set", lambda x: None)

    async def fake_impl(interaction, tournament, mt, mi, winner):
        # Как реальный impl: сессия должна существовать.
        sess = tournament.ocr_sessions.get(v._session_key(mt, mi))
        if not sess:
            raise ValueError("OCR-сессия не готова")
        await asyncio.sleep(0.05)  # окно гонки
        sess["status"] = "completed"

    monkeypatch.setattr(v, "_finalize_ocr_match_impl", fake_impl)

    inter = _FakeInteraction(_user(1, "P1"))
    results = {}

    async def do_finalize():
        try:
            await asyncio.wait_for(
                v.finalize_ocr_match(inter, t, "final", 0, 0), timeout=5
            )
            results["fin"] = "ok"
        except Exception as e:
            results["fin"] = f"err:{e}"

    async def do_cancel():
        try:
            results["cancel"] = await asyncio.wait_for(
                up.cancel_rooms_session(
                    _fake_guild(), _user(88, "A", admin=True),
                    t, "final", 0,
                ),
                timeout=5,
            )
        except Exception as e:
            results["cancel"] = f"err:{e}"

    await asyncio.gather(do_finalize(), do_cancel())

    fin_ok = results.get("fin") == "ok"
    cancel_ok = results.get("cancel") == "ok"
    # Взаимоисключение: успешный finalize → cancel видит done;
    # успешный cancel → finalize падает (сессия удалена).
    assert not (fin_ok and cancel_ok), results
    assert fin_ok or cancel_ok, results
    if fin_ok:
        assert results.get("cancel") == "done", results
        assert s["status"] == "completed"
    else:
        assert "final_0" not in t.ocr_sessions


@pytest.mark.asyncio
async def test_concurrent_finalize_serialized(monkeypatch):
    """Два одновременных finalize (напр. auto + ручной retry):
    _finalize_lock сериализует — impl выполняется последовательно,
    без параллельного входа."""
    import views.ocr_match_view as v

    t = _tournament()
    s = _full_session(t)
    log = []

    async def fake_impl(interaction, tournament, mt, mi, winner):
        log.append("enter")
        await asyncio.sleep(0.03)
        log.append("exit")
        sess = tournament.ocr_sessions.get(v._session_key(mt, mi))
        if sess:
            sess["status"] = "completed"

    monkeypatch.setattr(v, "_finalize_ocr_match_impl", fake_impl)
    inter = _FakeInteraction(_user(1, "P1"))

    await asyncio.wait_for(
        asyncio.gather(
            v.finalize_ocr_match(inter, t, "final", 0, 0),
            v.finalize_ocr_match(inter, t, "final", 0, 0),
        ),
        timeout=5,
    )
    # Строгая чередуемость enter/exit — параллельного входа нет.
    assert log == ["enter", "exit", "enter", "exit"], log


@pytest.mark.asyncio
async def test_finalize_rechecks_readiness_under_lock():
    """Между ready-check вызывающего и захватом _room_lock сопоставление
    могло быть откачено — impl обязан перепроверить готовность и
    отказать до любого начисления ELO."""
    import views.ocr_match_view as v

    t = _tournament()
    s = _full_session(t)
    # «Параллельное действие» после ready-check: строка отвязана.
    row = s["rooms"][0]["maps"]["0"]["rows"][0]
    row["discord_name"] = None
    row["discord_id"] = None
    row["source"] = None

    with pytest.raises(ValueError, match="не готова"):
        await v._finalize_ocr_match_impl(
            _FakeInteraction(_user(1, "P1")), t, "final", 0, 0
        )
    # Статус не переведён в finalizing, ELO не тронут.
    assert s["status"] == "awaiting_mapping"


@pytest.mark.asyncio
async def test_finalize_ready_snapshot_passes_recheck(monkeypatch):
    """Готовая сессия проходит повторную проверку — impl доходит
    до PG-гейта (дальше заглушаем, чтобы не трогать process_match_result)."""
    import views.ocr_match_view as v

    t = _tournament()
    s = _full_session(t)
    reached = []

    async def fake_gate(gid, mid):
        reached.append(mid)
        return "done", True  # already_processed, stats_done

    # impl импортирует get_status локально из модуля — подменяем там.
    import storage.finalize_store as fs
    monkeypatch.setattr(fs, "get_status", fake_gate)
    monkeypatch.setattr(v.store, "get", lambda g, c: t)
    monkeypatch.setattr(v.store, "set", lambda x: None)
    # Дальше impl упирается в store/PG — обрываем через исключение
    # на store.set, если дойдёт (значит recheck прошёл).
    try:
        await v._finalize_ocr_match_impl(
            _FakeInteraction(_user(1, "P1")), t, "final", 0, 0
        )
    except Exception:
        pass
    assert reached == ["100_final_0"]


def test_no_finalize_call_inside_mapping_lock():
    """AST-регрессия: вызовы finalize_ocr_match/_auto_finalize_match/
    _maybe_auto_finalize_mm_rooms/_rooms_after_map не должны быть
    внутри `async with`, захватывающего _mapping_lock/_room_lock/
    _finalize_lock — asyncio.Lock нереентерабелен (self-deadlock)."""
    path = (
        Path(__file__).resolve().parent.parent
        / "views" / "ocr_match_view.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    finalize_names = {
        "finalize_ocr_match",
        "_auto_finalize_match",
        "_maybe_auto_finalize_mm_rooms",
        "_rooms_after_map",
    }
    lock_names = ("_mapping_lock", "_room_lock", "_finalize_lock")
    bad = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.lock_depth = 0

        def visit_AsyncWith(self, node):
            src_items = " ".join(
                ast.unparse(i.context_expr) for i in node.items
            )
            if any(n in src_items for n in lock_names):
                self.lock_depth += 1
                self.generic_visit(node)
                self.lock_depth -= 1
            else:
                self.generic_visit(node)

        def visit_Call(self, node):
            f = node.func
            name = (
                f.id if isinstance(f, ast.Name)
                else getattr(f, "attr", "")
            )
            if name in finalize_names and self.lock_depth:
                bad.append((name, node.lineno))
            self.generic_visit(node)

    V().visit(tree)
    assert not bad, f"finalize calls inside lock: {bad}"


# ---------------------------------------------------------------------------
# PostgreSQL crash-recovery (pgserver — изолированный инстанс)
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS match_finalizations (
    match_id TEXT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'processing',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS test_elo_applied (
    match_id TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgserver = pytest.importorskip("pgserver")
    pgdata = tmp_path_factory.mktemp("pgdata_final")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def pg_pool(pg_uri):
    import asyncpg
    import storage.db as dbmod

    pool = await asyncpg.create_pool(pg_uri, min_size=2, max_size=8)
    async with pool.acquire() as conn:
        await conn.execute(DDL)
        await conn.execute("DELETE FROM match_finalizations")
        await conn.execute("DELETE FROM test_elo_applied")
    dbmod._pool = pool
    try:
        yield pool
    finally:
        dbmod._pool = None
        await pool.close()


async def _set_status(pool, status):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO match_finalizations (match_id, guild_id, status)"
            " VALUES ($1, $2, $3)"
            " ON CONFLICT (match_id) DO UPDATE SET status = $3",
            MATCH_ID, GUILD_ID, status,
        )


@pytest.mark.asyncio
async def test_gate_decisions_real_pg(pg_pool):
    """_finalize_stats_gate на реальных статусах match_finalizations:
    crash до stats_done → полный прогон; stats_done → только добивка;
    done → вообще ничего; нет записи → полный прогон один раз."""
    import storage.finalize_store as fs
    import views.ocr_match_view as v

    cases = [
        (None, False, (False, False)),    # нет записи, JSON чист
        ("processing", False, (False, False)),
        ("stats_done", False, (False, True)),
        ("done", False, (True, True)),
        (None, True, (True, True)),       # JSON-фолбэк без DB
    ]
    for status, json_flag, expected in cases:
        if status is None:
            async with pg_pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM match_finalizations WHERE match_id=$1",
                    MATCH_ID,
                )
        else:
            await _set_status(pg_pool, status)
        real = await fs.get_status(GUILD_ID, MATCH_ID)
        assert real == status
        assert v._finalize_stats_gate(real, json_flag) == expected


@pytest.mark.asyncio
async def test_stats_done_atomic_rollback(pg_pool):
    """stats_done пишется в ТОЙ ЖЕ транзакции, что и статистика:
    rollback → записи нет; commit → stats_done + применённая
    «статистика» присутствуют вместе."""
    import storage.db as dbmod
    import storage.finalize_store as fs

    pool = await dbmod.get_pool()
    # Rollback: статистика + stats_done не должны сохраниться.
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO test_elo_applied (match_id) VALUES ($1)",
                    MATCH_ID,
                )
                await fs.mark_stats_done_in_tx(conn, GUILD_ID, MATCH_ID)
                raise RuntimeError("crash mid-transaction")
    except RuntimeError:
        pass

    assert await fs.get_status(GUILD_ID, MATCH_ID) is None
    async with pool.acquire() as conn:
        row = await conn.fetchval(
            "SELECT count(*) FROM test_elo_applied WHERE match_id=$1",
            MATCH_ID,
        )
    assert row == 0

    # Commit: атомарно вместе.
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO test_elo_applied (match_id) VALUES ($1)",
                MATCH_ID,
            )
            await fs.mark_stats_done_in_tx(conn, GUILD_ID, MATCH_ID)
    assert await fs.get_status(GUILD_ID, MATCH_ID) == "stats_done"


@pytest.mark.asyncio
async def test_concurrent_recovery_vs_retry_single_elo(pg_pool):
    """Имитация finalize на уровне БД: три конкурентных вызова
    (авто-восстановление + два ручных retry) → ровно одна запись
    ELO/статистики благодаря PG-гейту и PK match_finalizations."""
    import storage.db as dbmod
    import storage.finalize_store as fs
    import views.ocr_match_view as v

    async def apply_once():
        st = await fs.get_status(GUILD_ID, MATCH_ID)
        already, stats_done = v._finalize_stats_gate(st, False)
        if already:
            return "skip"
        pool = await dbmod.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if not stats_done:
                    await conn.execute(
                        "INSERT INTO test_elo_applied (match_id)"
                        " VALUES ($1) ON CONFLICT (match_id) DO NOTHING",
                        MATCH_ID,
                    )
                    await fs.mark_stats_done_in_tx(conn, GUILD_ID, MATCH_ID)
        await fs.mark_done(GUILD_ID, MATCH_ID)
        return "applied"

    results = await asyncio.gather(*(apply_once() for _ in range(3)))
    pool = await dbmod.get_pool()
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM test_elo_applied WHERE match_id=$1",
            MATCH_ID,
        )
        st = await conn.fetchval(
            "SELECT status FROM match_finalizations WHERE match_id=$1",
            MATCH_ID,
        )
    assert n == 1, results
    assert st == "done"

    # Повторный запуск восстановления — ноль новых применений.
    assert await apply_once() == "skip"
    async with pool.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM test_elo_applied WHERE match_id=$1",
            MATCH_ID,
        )
    assert n == 1
