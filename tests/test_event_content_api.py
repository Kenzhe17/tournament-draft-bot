"""API-тесты Control Center — Event Content (Events Content
Expansion).

Проверяем на реальном PostgreSQL (pgserver) + переопределении
current_user:
- доступ: BOT_OWNER_ID → 200; обычный пользователь и
  admin/moderator без BOT_OWNER_ID → 403 на запись;
- CSRF: запись без X-CC-Request → 403;
- невалидный импорт → 422 и данные не меняются;
- merge-import не создаёт дубликатов;
- CRUD валидирует итоговый каталог (анaграмма/дубликат → 422);
- редактирование записи не влияет на уже выданный снимок
  (активный ивент хранит config-снимок).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

pgserver = pytest.importorskip("pgserver")
asyncpg = pytest.importorskip("asyncpg")
httpx = pytest.importorskip("httpx")
pytest_asyncio = pytest.importorskip("pytest_asyncio")

from control_center.auth import PanelUser, current_user  # noqa: E402
from control_center.main import create_app  # noqa: E402
from control_center.routers import event_content_api  # noqa: E402
from services import event_content  # noqa: E402
from storage.event_content_store import EventContentStore  # noqa: E402

OWNER_ID = 424242
GUILD = 555
H = {"X-CC-Request": "1"}


@pytest.fixture(scope="module")
def pg_uri(tmp_path_factory):
    pgdata = tmp_path_factory.mktemp("pgdata_cc_content")
    srv = pgserver.get_server(str(pgdata))
    return srv.get_uri()


@pytest_asyncio.fixture
async def content_store(pg_uri):
    pool = await asyncpg.create_pool(pg_uri, min_size=1, max_size=3)
    s = EventContentStore(pool)
    await s.ensure_schema()
    async with pool.acquire() as conn:
        for t in ("event_content_entries", "event_content_catalog",
                  "event_content_cycle_state",
                  "event_content_draws",
                  "event_content_reservations"):
            await conn.execute(f"DELETE FROM {t}")
    yield s
    await pool.close()


@pytest_asyncio.fixture
async def app(content_store, monkeypatch):
    """ASGI-приложение с реальным store и подменённым владельцем."""
    monkeypatch.setattr(event_content_api, "event_content_store",
                        content_store)
    monkeypatch.setattr("config.BOT_OWNER_ID", OWNER_ID)
    monkeypatch.setattr("control_center.config.PANEL_GUILD_ID",
                        GUILD, raising=False)
    async def _noop_audit(*a, **kw):
        return None
    monkeypatch.setattr(
        "storage.admin_config_store.insert_audit", _noop_audit)
    return create_app()


def _user(uid=OWNER_ID, role="owner", perms=None):
    payload = {"sub": str(uid), "username": "t",
               "avatar": "", "guild_id": GUILD, "role": role}
    return PanelUser(payload, set(perms or {"*"}))


def _auth(app, user):
    app.dependency_overrides[current_user] = lambda: user


@pytest_asyncio.fixture
async def client(app):
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test") as c:
        yield c


def _entries(n=5, game="word"):
    src = event_content.bundled_entries(game)[:n]
    out = []
    for i, e in enumerate(src):
        e = dict(e)
        e["id"] = f"api_{i:03d}"
        e["category"] = "Тест"
        out.append(e)
    return out


# ------------------------------------------------------------ доступ

@pytest.mark.asyncio
async def test_owner_can_list_games(client, app):
    _auth(app, _user())
    r = await client.get("/api/event-content/games")
    assert r.status_code == 200
    games = {g["game"] for g in r.json()["games"]}
    assert games == {"word", "krokodil"}


@pytest.mark.asyncio
async def test_non_owner_denied(client, app):
    for uid, role in ((1, "viewer"), (2, "admin"),
                      (3, "moderator"), (4, "organizer")):
        _auth(app, _user(uid=uid, role=role, perms={"*"}))
        # Содержимое каталога (записи с ответами) — только
        # BOT_OWNER_ID, даже с полным perm-бандлом
        r = await client.get("/api/event-content/word/entries")
        assert r.status_code == 403, role
        r = await client.get("/api/event-content/word/categories")
        assert r.status_code == 403, role
        r = await client.get(
            "/api/event-content/word/entries/api_000")
        assert r.status_code == 403, role
        # Обезличенная статистика доступна (settings.view)
        r = await client.get("/api/event-content/games")
        assert r.status_code == 200, role
        body = r.json()
        # только счётчики/версии — никаких контентных полей
        def _keys(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    yield k
                    yield from _keys(v)
            elif isinstance(o, list):
                for v in o:
                    yield from _keys(v)
        keys = set(_keys(body))
        assert not ({"answer", "accepted", "clue", "word",
                     "items"} & keys), keys
        # Запись — только BOT_OWNER_ID
        r = await client.post("/api/event-content/word/entries",
                              json={"word": "глобус"}, headers=H)
        assert r.status_code == 403, role
        r = await client.post("/api/event-content/word/import",
                              json={"entries": []}, headers=H)
        assert r.status_code == 403, role
        r = await client.delete("/api/event-content/word/entries/x1",
                                headers=H)
        assert r.status_code == 403, role


@pytest.mark.asyncio
async def test_admin_without_owner_id_denied(client, app):
    """admin-роль панели без BOT_OWNER_ID: чтение доступно
    (settings.view), запись — 403."""
    _auth(app, _user(uid=999, role="admin",
                     perms={"settings.view", "settings.manage",
                            "panel.view"}))
    r = await client.get("/api/event-content/games")
    assert r.status_code == 200
    # Записи каталога — только owner
    r = await client.get("/api/event-content/word/entries")
    assert r.status_code == 403
    r = await client.post("/api/event-content/word/entries",
                          json={"word": "глобус"}, headers=H)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_csrf_required_for_writes(client, app):
    _auth(app, _user())
    r = await client.post("/api/event-content/word/entries",
                          json={"word": "глобус"})
    assert r.status_code == 403
    r = await client.post("/api/event-content/word/import",
                          json={"entries": []})
    assert r.status_code == 403
    r = await client.delete("/api/event-content/word/entries/x1")
    assert r.status_code == 403


# ------------------------------------------------------------ CRUD

@pytest.mark.asyncio
async def test_owner_crud_flow(client, app):
    _auth(app, _user())
    entries = _entries(5)
    r = await client.post("/api/event-content/word/import",
                          json={"entries": entries, "version": "t1"},
                          headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["inserted"] == 5

    r = await client.get("/api/event-content/word/entries")
    assert r.status_code == 200
    assert r.json()["total"] == 5

    r = await client.post(
        "/api/event-content/word/entries/api_000/toggle", headers=H)
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = await client.patch("/api/event-content/word/entries/api_000",
                           json={"enabled": True,
                                 "difficulty": "hard"}, headers=H)
    assert r.status_code == 200

    r = await client.delete(
        "/api/event-content/word/entries/api_000", headers=H)
    assert r.status_code == 200
    r = await client.get("/api/event-content/word/entries/api_000")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_invalid_import_rejected_atomically(
        client, app, content_store):
    _auth(app, _user())
    good = _entries(5)
    await client.post("/api/event-content/word/import",
                      json={"entries": good}, headers=H)
    bad = good + [{"id": "bad", "word": "ab",
                   "category": "x", "difficulty": "easy",
                   "enabled": True}]
    r = await client.post("/api/event-content/word/import",
                          json={"entries": bad, "replace": True},
                          headers=H)
    assert r.status_code == 422
    stats = await content_store.catalog_stats("word")
    assert stats["total"] == 5


@pytest.mark.asyncio
async def test_merge_import_no_duplicates(client, app,
                                          content_store):
    _auth(app, _user())
    entries = _entries(5)
    await client.post("/api/event-content/word/import",
                      json={"entries": entries, "replace": True},
                      headers=H)
    r = await client.post("/api/event-content/word/import",
                          json={"entries": entries, "replace": False},
                          headers=H)
    assert r.status_code == 200
    stats = await content_store.catalog_stats("word")
    assert stats["total"] == 5


@pytest.mark.asyncio
async def test_merge_import_anagram_conflict_422(
        client, app, content_store):
    _auth(app, _user())
    entries = _entries(5)
    await client.post("/api/event-content/word/import",
                      json={"entries": entries, "replace": True},
                      headers=H)
    base = entries[0]["word"]
    ana = "".join(sorted(base, reverse=True))
    if ana == base:
        ana = base[1:] + base[0]
    r = await client.post(
        "/api/event-content/word/import",
        json={"entries": [{"id": "conflict1", "word": ana,
                           "category": "X", "difficulty": "easy",
                           "enabled": True}],
              "replace": False},
        headers=H)
    assert r.status_code == 422
    stats = await content_store.catalog_stats("word")
    assert stats["total"] == 5


@pytest.mark.asyncio
async def test_edit_does_not_affect_picked_snapshot(
        client, app, content_store):
    """Запись, уже выданная событию (pick → config-снимок),
    не меняется при редактировании каталога."""
    import random
    _auth(app, _user())
    entries = _entries(5)
    await client.post("/api/event-content/word/import",
                      json={"entries": entries, "replace": True},
                      headers=H)
    picked = await content_store.pick(
        "word", guild_id="g1", event_id="evt1",
        rng=random.Random(0))
    assert picked is not None
    old_word = picked["word"]
    r = await client.patch(
        f"/api/event-content/word/entries/{picked['id']}",
        json={"enabled": False}, headers=H)
    assert r.status_code == 200
    assert picked["word"] == old_word
    assert picked["enabled"] is True
    for _ in range(10):
        e = await content_store.pick(
            "word", guild_id="g2", event_id="x",
            rng=random.Random(1))
        assert e is None or e["id"] != picked["id"]


@pytest.mark.asyncio
async def test_export_roundtrip(client, app):
    _auth(app, _user())
    entries = _entries(5, game="krokodil")
    await client.post("/api/event-content/krokodil/import",
                      json={"entries": entries}, headers=H)
    r = await client.get("/api/event-content/krokodil/export")
    assert r.status_code == 200
    body = r.json()
    assert body["game"] == "krokodil"
    assert len(body["entries"]) == 5
    assert body["entries"][0]["answer"]


@pytest.mark.asyncio
async def test_unknown_game_404(client, app):
    _auth(app, _user())
    r = await client.get("/api/event-content/chess/entries")
    assert r.status_code == 404
