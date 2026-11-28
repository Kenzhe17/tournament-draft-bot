"""PG-интеграция: хранилище контента событийных игр.

Проверяем на реальном PostgreSQL (pgserver):
- ensure_schema идемпотентна;
- seed_bundled не затирает существующие записи;
- pick(): shuffle-bag без повторов внутри цикла, следующий
  цикл после исчерпания, исключение немедленного повтора,
  per-guild независимость, резервирование блокирует
  параллельную выдачу, release возвращает запись;
- фильтры: disabled_categories, difficulty weights
  (все нули → равные веса fallback), enabled=false;
- пустой каталог → None (игра недоступна без падения);
- import_entries: невалидный JSON отклоняется атомарно,
  рабочий каталог не меняется.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")
pytest_asyncio = pytest.importorskip("pytest_asyncio")

from storage.event_content_store import EventContentStore

TABLES = (
    "event_content_entries", "event_content_catalog",
    "event_content_cycle_state", "event_content_draws",
    "event_content_reservations",
)


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_content")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def store(pg_uri, monkeypatch):
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=4)
    s = EventContentStore(pool)
    await s.ensure_schema()
    async with pool.acquire() as conn:
        for t in TABLES:
            await conn.execute(f"DELETE FROM {t}")
    yield s
    await pool.close()


def _entries(prefix, n, difficulty="easy", enabled=True,
             category="Тест"):
    return [{"id": f"{prefix}_{i:04d}",
             "word" if prefix.startswith("w") else "answer":
                 f"{prefix}{i}",
             "category": category, "difficulty": difficulty,
             "enabled": enabled}
            | ({"accepted": [f"{prefix}{i}"]}
               if not prefix.startswith("w") else {})
            for i in range(n)]


# ------------------------------------------------------------- schema

@pytest.mark.asyncio
async def test_schema_idempotent(store):
    await store.ensure_schema()
    await store.ensure_schema()


@pytest.mark.asyncio
async def test_seed_bundled_no_overwrite(store):
    entries = _entries("w", 5)
    r1 = await store.seed_bundled("word", entries, "v1")
    assert r1["inserted"] == 5
    # повторный сид с другим содержимым — no-op
    entries2 = _entries("w", 3) + [{"id": "w_new", "word": "новый",
                                    "category": "x",
                                    "difficulty": "easy",
                                    "enabled": True}]
    r2 = await store.seed_bundled("word", entries2, "v2")
    assert r2["skipped"] == 5
    stats = await store.catalog_stats("word")
    assert stats["total"] == 5
    assert stats["content_version"] == "v1"


# ------------------------------------------------------------- pick

@pytest.mark.asyncio
async def test_pick_no_repeat_within_cycle(store):
    await store.import_catalog("word", _entries("w", 20),
                               replace=True, source="test")
    rng = random.Random(1)
    seen = []
    for i in range(20):
        e = await store.pick("word", guild_id="g1",
                             event_id=f"e{i}", rng=rng)
        assert e is not None
        seen.append(e["id"])
        await store.release_event(f"e{i}")
    assert len(set(seen)) == 20  # ни одного повтора
    # 21-й pick — новый цикл, цикл_count=1
    e21 = await store.pick("word", guild_id="g1",
                           event_id="e20", rng=rng)
    assert e21 is not None
    await store.release_event("e20")
    st = await store.state("word", "g1")
    assert st["cycle_count"] == 1
    assert len(st["used"]) == 1


@pytest.mark.asyncio
async def test_pick_cycle_excludes_last_repeat(store):
    """На границе цикла последнее задание не повторяется
    немедленно, если есть альтернатива."""
    await store.import_catalog("krokodil", _entries("k", 3),
                               replace=True, source="test")
    rng = random.Random(0)
    ids = []
    for i in range(3):
        e = await store.pick("krokodil", guild_id="g9",
                             event_id=f"k{i}", rng=rng)
        ids.append(e["id"])
        await store.release_event(f"k{i}")
    # 4-й pick — новый цикл; last_id исключается при наличии
    # альтернатив (здесь 2 из 3) → детерминированно не равен
    e4 = await store.pick("krokodil", guild_id="g9",
                          event_id="k3", rng=rng)
    assert e4 is not None
    assert e4["id"] != ids[-1]
    await store.release_event("k3")


@pytest.mark.asyncio
async def test_pick_reservation_blocks_parallel(store):
    """Запись с активной резервацией не выдаётся повторно —
    в т.ч. другому guild."""
    await store.import_catalog("word", _entries("w", 3),
                               replace=True, source="test")
    rng = random.Random(2)
    # Резервируем все три вручную
    for i in range(3):
        await store.pick("word", guild_id="ga", event_id=f"a{i}",
                         rng=rng)
    # Все записи использованы в цикле ga — для gb цикл свой,
    # но резервации глобальны → пул gb пуст → None
    e = await store.pick("word", guild_id="gb", event_id="b0",
                         rng=rng)
    assert e is None
    # Снимаем резервации — gb может брать (свой цикл)
    await store.release_event("a0")
    await store.release_event("a1")
    await store.release_event("a2")
    e2 = await store.pick("word", guild_id="gb", event_id="b1",
                          rng=rng)
    assert e2 is not None


@pytest.mark.asyncio
async def test_pick_guild_isolation(store):
    await store.import_catalog("word", _entries("w", 10),
                               replace=True, source="test")
    rng = random.Random(3)
    # gA и gB имеют независимые циклы — оба могут взять
    # «одну и ту же» запись? Нет: резервация глобальна,
    # но used — per-guild. gB получит ДРУГУЮ запись пока
    # резервация gA активна.
    a = await store.pick("word", guild_id="gA", event_id="eA",
                         rng=rng)
    b = await store.pick("word", guild_id="gB", event_id="eB",
                         rng=rng)
    assert a is not None and b is not None
    assert a["id"] != b["id"]  # резервация не даёт дубль
    stA = await store.state("word", "gA")
    stB = await store.state("word", "gB")
    assert stA["used"] != stB["used"] or len(stA["used"]) == 1


@pytest.mark.asyncio
async def test_pick_disabled_categories(store):
    entries = _entries("w", 6, category="Еда") + \
        _entries("w", 4, category="Спорт")
    # уникальные id
    for i, e in enumerate(entries):
        e["id"] = f"w_{i:04d}"
    await store.import_catalog("word", entries, replace=True,
                               source="test")
    rng = random.Random(4)
    for _ in range(6):
        e = await store.pick("word", guild_id="gc",
                             disabled_categories={"Еда"},
                             rng=rng)
        assert e is not None
        assert e["category"] == "Спорт"
        await store.release("word", e["id"])


@pytest.mark.asyncio
async def test_pick_difficulty_weights_zero_fallback(store):
    """Все веса <=0 → равные веса (задокументированный
    fallback), игра не блокируется."""
    entries = _entries("w", 4, difficulty="easy") + \
        _entries("w", 4, difficulty="hard")
    for i, e in enumerate(entries):
        e["id"] = f"wz_{i:04d}"
    await store.import_catalog("word", entries, replace=True,
                               source="test")
    rng = random.Random(5)
    e = await store.pick("word", guild_id="gz",
                         difficulty_weights={"easy": 0,
                                             "medium": 0,
                                             "hard": 0},
                         rng=rng)
    assert e is not None  # fallback сработал


@pytest.mark.asyncio
async def test_pick_empty_catalog_returns_none(store):
    await store.import_catalog("word", [], replace=True,
                               source="test")
    # import пустого — ок; pick → None
    e = await store.pick("word", guild_id="g0",
                         event_id="e0", rng=random.Random(0))
    assert e is None


@pytest.mark.asyncio
async def test_pick_all_disabled_returns_none(store):
    await store.import_catalog(
        "word", _entries("w", 5, enabled=False), replace=True,
        source="test")
    e = await store.pick("word", guild_id="g0",
                         event_id="e0", rng=random.Random(0))
    assert e is None


@pytest.mark.asyncio
async def test_pick_1000_no_repeat(store):
    """TZ §8.4: 1000 выборов без повтора при полной базе."""
    entries = [{"id": f"w_{i:04d}", "word": f"слово{i}",
                "category": "Тест", "difficulty": "easy",
                "enabled": True} for i in range(1000)]
    await store.import_catalog("word", entries, replace=True,
                               source="test")
    rng = random.Random(123)
    seen = set()
    for i in range(1000):
        e = await store.pick("word", guild_id="gbig",
                             event_id=f"ev{i}", rng=rng)
        assert e is not None, f"pick {i} failed"
        assert e["id"] not in seen, f"repeat at {i}: {e['id']}"
        seen.add(e["id"])
        await store.release_event(f"ev{i}")
    assert len(seen) == 1000
    # следующий цикл начинается — cycle_count=1
    e = await store.pick("word", guild_id="gbig",
                         event_id="ev1000", rng=rng)
    assert e is not None
    await store.release_event("ev1000")
    st = await store.state("word", "gbig")
    assert st["cycle_count"] == 1


@pytest.mark.asyncio
async def test_release_frees_reservation(store):
    await store.import_catalog("word", _entries("w", 2),
                               replace=True, source="test")
    rng = random.Random(6)
    e1 = await store.pick("word", guild_id="gr", event_id="r1",
                          rng=rng)
    # entry зарезервирована → для другого guild недоступна,
    # но у gr used содержит её — pick для gr берёт вторую
    e2 = await store.pick("word", guild_id="gr", event_id="r2",
                          rng=rng)
    assert e1["id"] != e2["id"]
    # после release_event обе свободны для других guild
    await store.release_event("r1")
    await store.release_event("r2")
    other = await store.pick("word", guild_id="gr2",
                             event_id="r3", rng=rng)
    assert other is not None


@pytest.mark.asyncio
async def test_import_rejects_invalid_atomically(store):
    await store.import_catalog("word", _entries("w", 5),
                               replace=True, source="test")
    bad = [{"id": "bad1", "word": "ab",  # слишком короткое
            "category": "x", "difficulty": "easy",
            "enabled": True}]
    with pytest.raises(ValueError):
        await store.import_entries("word", bad, replace=True)
    stats = await store.catalog_stats("word")
    assert stats["total"] == 5  # каталог не изменился


@pytest.mark.asyncio
async def test_catalog_change_does_not_break_cycle(store):
    """Отключение записи на лету: used-записи исчезают из
    пула, цикл продолжается без сброса."""
    entries = _entries("w", 5)
    await store.import_catalog("word", entries, replace=True,
                               source="test")
    rng = random.Random(7)
    for i in range(3):
        await store.pick("word", guild_id="gch",
                             event_id=f"c{i}", rng=rng)
        await store.release_event(f"c{i}")
    st = await store.state("word", "gch")
    used_before = set(st["used"])
    assert len(used_before) == 3
    # Отключаем одну использованную запись
    victim = min(used_before)
    await store.upsert_entry("word", {
        "id": victim, "word": "изменённое",
        "category": "Тест", "difficulty": "easy",
        "enabled": False})
    # pick продолжает — used не сброшен
    st2 = await store.state("word", "gch")
    assert set(st2["used"]) >= used_before - {victim} or \
        set(st2["used"]) == used_before


@pytest.mark.asyncio
async def test_recent_draws_history(store):
    await store.import_catalog("krokodil", _entries("k", 4),
                               replace=True, source="test")
    rng = random.Random(8)
    for i in range(4):
        await store.pick("krokodil", guild_id="gh",
                         event_id=f"h{i}", rng=rng)
        await store.release_event(f"h{i}")
    draws = await store.recent_draws("krokodil", "gh", limit=10)
    assert len(draws) == 4
    assert {d["event_id"] for d in draws} == \
        {"h0", "h1", "h2", "h3"}
