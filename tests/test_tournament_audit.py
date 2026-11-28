"""P1: журнал аудита турниров.

Покрывает:
- запись событий player.add/remove с before/after;
- исправление K/D с предыдущими и новыми значениями;
- идемпотентность подтверждения (dedup_key);
- дедупликация двойного нажатия;
- комната: событие без пароля;
- назначение/снятие организатора;
- персистентность после «перезапуска» (pending-файл/PG-мок);
- права: участник не видит журнал, чужой турнир — нет;
- недоступность БД → pending, не «ложный успех».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.tournament import Tournament, TournamentPhase, TournamentSize
from services import tournament_audit_service as audit
from storage import tournament_audit_store as audit_store


# ---------------------------------------------------------------------------
# Мок asyncpg-пула
# ---------------------------------------------------------------------------


class _FakeConn:
    """Минимальный asyncpg-Connection: хранит события в памяти."""

    def __init__(self):
        self.events: list[dict] = []
        self.dedup: set[str] = set()

    async def execute(self, sql, *args):
        # DDL/индексы — no-op
        if "INSERT INTO tournament_audit_log" in sql:
            (event_id, guild_id, channel_id, tournament_id, match_id,
             actor, target, action, source, before, after, status,
             iid, dedup_key) = args
            if dedup_key and dedup_key in self.dedup:
                return "INSERT 0 0"
            if dedup_key:
                self.dedup.add(dedup_key)
            import json as _j
            self.events.append({
                "event_id": event_id,
                "guild_id": guild_id,
                "channel_id": channel_id,
                "tournament_id": tournament_id,
                "match_id": match_id,
                "actor_user_id": actor,
                "target_user_id": target,
                "action": action,
                "source": source,
                "before_data": _j.loads(before) if before else None,
                "after_data": _j.loads(after) if after else None,
                "status": status,
                "interaction_id": iid,
                "dedup_key": dedup_key,
                "created_at": None,
                "discord_message_id": None,
            })
            return "INSERT 0 1"
        if "UPDATE tournament_audit_log" in sql:
            return "UPDATE 1"
        if "SELECT" in sql:
            return []
        return "OK"

    async def fetch(self, sql, *args):
        return []

    async def fetchval(self, sql, *args):
        return None

    async def fetchrow(self, sql, *args):
        return None


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


@pytest.fixture
def fake_pg(monkeypatch):
    """Подменить get_pool на фейк с памятью."""
    conn = _FakeConn()
    pool = _FakePool(conn)

    import storage.db as db_mod
    monkeypatch.setattr(db_mod, "_pool", pool)
    # get_pool возвращает _pool если не None
    return conn


@pytest.fixture
def pending_file(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "PENDING_FILE", tmp_path / "audit_pending.json")
    return tmp_path / "audit_pending.json"


# ---------------------------------------------------------------------------
# 1. Запись player.add/remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_player_add_remove_events(fake_pg):
    t = Tournament(guild_id=1, channel_id=100, size=TournamentSize.EIGHT)
    await audit.record(
        guild_id=1, channel_id=100, tournament_id=str(t.id),
        actor_user_id=10, target_user_id=77,
        action=audit.A.PLAYER_ADD,
        after={"player": "User77", "circle": 1},
    )
    await audit.record(
        guild_id=1, channel_id=100, tournament_id=str(t.id),
        actor_user_id=10, target_user_id=77,
        action=audit.A.PLAYER_REMOVE,
        before={"player": "User77"},
    )
    actions = [e["action"] for e in fake_pg.events]
    assert actions == [audit.A.PLAYER_ADD, audit.A.PLAYER_REMOVE]
    assert fake_pg.events[0]["target_user_id"] == 77
    assert fake_pg.events[0]["after_data"]["circle"] == 1


# ---------------------------------------------------------------------------
# 2. Исправление K/D: до/после по игроку
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kd_edit_keeps_before_after(fake_pg):
    await audit.record(
        guild_id=1, channel_id=100, tournament_id="t1",
        match_id="t1_qualifier_0", actor_user_id=10,
        action=audit.A.KD_ADMIN_EDIT,
        before={"PlayerA": "12/7"},
        after={"PlayerA": "14/7"},
    )
    ev = fake_pg.events[0]
    assert ev["before_data"] == {"PlayerA": "12/7"}
    assert ev["after_data"] == {"PlayerA": "14/7"}


# ---------------------------------------------------------------------------
# 3-4. Подтверждение победителя однократно / дедуп
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_dedup_key(fake_pg):
    kwargs = dict(
        guild_id=1, channel_id=100, tournament_id="t1",
        match_id="t1_final_0", actor_user_id=10,
        action=audit.A.RESULT_CONFIRM,
        after={"winner": 0},
        dedup_key="confirm:t1:final:0",
    )
    first = await audit.record(**kwargs)
    second = await audit.record(**kwargs)
    assert first is not None
    assert second is None  # дедупликация
    assert len(fake_pg.events) == 1


# ---------------------------------------------------------------------------
# 5. Комната: пароль не попадает в журнал
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_room_event_no_password(fake_pg):
    await audit.record(
        guild_id=1, channel_id=100, tournament_id="t1",
        match_id="t1:qualifier:0", actor_user_id=42,
        action=audit.A.ROOM_SUBMIT, source="room",
        # room_view не передаёт room_id/password — только метаданные
        after={
            "match": "МАТЧ #1", "room_no": 1,
            "password": "supersecret",
        },
    )
    ev = fake_pg.events[0]
    assert "password" not in str(ev["after_data"])
    assert "supersecret" not in str(ev["after_data"])
    assert ev["after_data"]["room_no"] == 1


@pytest.mark.asyncio
async def test_sanitize_drops_forbidden_keys():
    out = audit._sanitize({
        "room_id": "1", "password": "x", "nested": {
            "token": "abc", "ok": 1}})
    assert "password" not in out
    assert "token" not in out["nested"]
    assert out["nested"]["ok"] == 1


# ---------------------------------------------------------------------------
# 6. Назначение/снятие организатора
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_assign_remove(fake_pg):
    await audit.record(
        guild_id=1, channel_id=100, tournament_id="t1",
        actor_user_id=10, target_user_id=55,
        action=audit.A.ORG_ASSIGN, source="organizer",
    )
    await audit.record(
        guild_id=1, channel_id=100, tournament_id="t1",
        actor_user_id=10, target_user_id=55,
        action=audit.A.ORG_REMOVE, source="organizer",
    )
    assert [e["action"] for e in fake_pg.events] == [
        audit.A.ORG_ASSIGN, audit.A.ORG_REMOVE]
    assert all(e["target_user_id"] == 55 for e in fake_pg.events)


# ---------------------------------------------------------------------------
# 7-8. Перезапуск: события в PG не теряются; pending восстанавливается
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_flush_recovers(pending_file, monkeypatch):
    """Без PG событие → pending-файл; flush_pending → PG."""
    # БД «недоступна»: get_pool бросает
    import storage.db as db_mod

    real_get_pool = db_mod.get_pool

    async def _boom():
        raise RuntimeError("db down")

    # audit_store.add_event импортирует get_pool лениво —
    # ломаем сам get_pool
    monkeypatch.setattr(db_mod, "get_pool", _boom)

    ev = await audit.record(
        guild_id=1, channel_id=100, tournament_id="t1",
        action=audit.A.TOURNAMENT_CREATE,
        after={"size": "8"},
    )
    assert ev is not None and ev["persisted"] == "pending"
    assert pending_file.exists()

    # «Перезапуск»: БД вернулась — восстанавливаем get_pool
    # и подсовываем фейк-пул.
    conn = _FakeConn()
    pool = _FakePool(conn)
    db_mod._pool = pool
    monkeypatch.setattr(db_mod, "get_pool", real_get_pool)

    flushed = await audit.flush_pending()
    assert flushed == 1
    assert len(conn.events) == 1
    assert conn.events[0]["action"] == audit.A.TOURNAMENT_CREATE


@pytest.mark.asyncio
async def test_db_down_no_false_success(fake_pg, pending_file, monkeypatch):
    """Недоступность БД не даёт 'успех' — событие в pending."""
    import storage.db as db_mod

    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(db_mod, "get_pool", _boom)
    ev = await audit.record(
        guild_id=1, channel_id=100, action=audit.A.RESULT_CONFIRM,
    )
    # Статус события — честный 'success' действия, но persisted=pending
    assert ev["persisted"] == "pending"
    assert pending_file.exists()


# ---------------------------------------------------------------------------
# 9-10. Права просмотра журнала
# ---------------------------------------------------------------------------


def _member(uid, *, admin=False, roles=()):
    return SimpleNamespace(
        id=uid, roles=list(roles),
        guild_permissions=SimpleNamespace(administrator=admin),
    )


def _guild():
    return SimpleNamespace(id=1, roles=[], get_role=lambda r: None,
                           get_member=lambda u: None)


@pytest.mark.asyncio
async def test_audit_view_permission(monkeypatch):
    from views import tournament_audit_view as av

    t = Tournament(guild_id=1, channel_id=100, size=TournamentSize.EIGHT)
    t.organizer_id = 10

    import storage.json_store as js
    monkeypatch.setattr(js, "store", SimpleNamespace(
        get=lambda g, c: t if (g, c) == (1, 100) else None))
    # view импортирует store по имени модуля
    monkeypatch.setattr(av, "store", js.store)

    guild = _guild()

    def _ix(user):
        return SimpleNamespace(
            user=user, guild=guild, guild_id=1, channel_id=100,
            response=SimpleNamespace(is_done=lambda: False),
        )

    # Основной организатор — доступ есть
    assert await av._can_view(_ix(_member(10)), str(t.id))
    # Обычный участник — нет
    assert not await av._can_view(_ix(_member(999)), str(t.id))
    # Админ — доступ
    assert await av._can_view(_ix(_member(5, admin=True)), str(t.id))


@pytest.mark.asyncio
async def test_audit_view_other_tournament_denied(monkeypatch):
    """Орг одного турнира не видит журнал другого (другой tid)."""
    from views import tournament_audit_view as av

    t = Tournament(guild_id=1, channel_id=100, size=TournamentSize.EIGHT)
    t.organizer_id = 10

    import storage.json_store as js
    fake = SimpleNamespace(get=lambda g, c: t)
    monkeypatch.setattr(av, "store", fake)

    guild = _guild()
    ix = SimpleNamespace(
        user=_member(10), guild=guild, guild_id=1, channel_id=100,
        response=SimpleNamespace(is_done=lambda: False),
    )
    # tournament_id чужого (старого) турнира → can_manage не сработает
    # по tid-ветке; fallback — только админы/org → False для org-юзера
    # без глобальных прав.
    assert not await av._can_view(ix, "other-tid")


# ---------------------------------------------------------------------------
# Sanitizer / формат карточки
# ---------------------------------------------------------------------------


def test_build_event_card_no_password():
    ev = {
        "action": audit.A.ROOM_UPDATE,
        "source": "room",
        "tournament_id": "t1",
        "match_id": "t1:qualifier:0",
        "actor_user_id": 42,
        "target_user_id": None,
        "before_data": {"room_id": "111"},
        "after_data": {"room_id": "222", "password": "zzz"},
        "created_at": None,
    }
    view = audit.build_event_card(ev)
    text = "\n".join(
        getattr(c, "content", "")
        for cont in view.children
        for c in getattr(cont, "children", [])
        if hasattr(c, "content")
    )
    assert "zzz" not in text
    assert "ДАННЫЕ КОМНАТЫ" in text
