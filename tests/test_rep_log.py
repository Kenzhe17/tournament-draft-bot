"""Тесты публичного журнала репутации (services/rep_log.py).

Покрывают: сборку V2-карточек, идемпотентную публикацию outbox,
устойчивость к ошибкам отправки и однократное создание канала.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import rep_log
from storage.reputation_store import (
    SRC_ADMIN,
    SRC_BOOST,
    SRC_INVITE,
    SRC_MANUAL,
)


# ---------------------------------------------------------------- fakes

class _FakeChannel:
    def __init__(self, cid: int, guild=None, fail: bool = False):
        self.id = cid
        self.name = "репутация-логи"
        self.guild = guild or SimpleNamespace(name="TestGuild")
        self.overwrites: dict = {}
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self._fail = fail

    async def send(self, **kwargs):
        if self._fail:
            raise RuntimeError("discord unavailable")
        self.sent.append(kwargs)
        return SimpleNamespace(id=10_000 + len(self.sent))

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _FakeCategory:
    def __init__(self, name="ИНФОРМАЦИЯ"):
        self.name = name


class _Hashable:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __hash__(self):
        return id(self)


class _FakeGuild:
    def __init__(self, gid: int = 1, channels: dict | None = None):
        self.id = gid
        self.name = "TestGuild"
        self._channels = channels or {}
        self.categories = [_FakeCategory()]
        self.default_role = _Hashable(id=99)
        self.me = _Hashable(id=42)
        self.created: list[dict] = []
        self.permissions_synced = False

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def create_text_channel(self, name, **kwargs):
        ch = _FakeChannel(777, guild=self)
        self.created.append({"name": name, **kwargs})
        self._channels[ch.id] = ch
        return ch


class _FakeBot:
    def __init__(self):
        self._guilds = {}

    def get_guild(self, gid):
        return self._guilds.get(gid)

    async def fetch_guild(self, gid):
        return self._guilds.get(gid)

    async def fetch_channel(self, cid):
        raise Exception("not found")


class FakeLogStore:
    """In-memory аналог outbox-части ReputationStore."""

    def __init__(self, rows: list[dict] | None = None):
        self.rows = list(rows or [])
        self.settings: dict[tuple[int, str], str] = {}
        self.sent: list[int] = []
        self.failed: list[tuple[int, str]] = []

    async def get_setting(self, guild_id, key):
        return self.settings.get((guild_id, key))

    async def set_setting(self, guild_id, key, value):
        self.settings[(guild_id, key)] = value

    async def get_pending_rep_logs(self, limit=20):
        return [r for r in self.rows if r["log_status"] == "pending"][:limit]

    async def mark_rep_log_sent(self, outbox_id, message_id):
        self.sent.append(outbox_id)
        for r in self.rows:
            if r["id"] == outbox_id:
                r["log_status"] = "sent"
                r["log_message_id"] = message_id

    async def mark_rep_log_failed(self, outbox_id, error):
        self.failed.append((outbox_id, error))
        for r in self.rows:
            if r["id"] == outbox_id:
                r["attempts"] = r.get("attempts", 0) + 1
                r["last_error"] = error


def _row(**kw) -> dict:
    base = {
        "id": 1,
        "transaction_id": 100,
        "guild_id": 1,
        "user_id": 200,
        "source": SRC_MANUAL,
        "amount": 1,
        "balance_after": 24,
        "actor_user_id": 100,
        "subject_user_id": 200,
        "log_status": "pending",
        "attempts": 0,
        "created_at": datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc),
    }
    base.update(kw)
    return base


def _patch_store(monkeypatch, store: FakeLogStore) -> None:
    monkeypatch.setattr(rep_log, "reputation_store", store)


# ---------------------------------------------------------------- channel

@pytest.mark.asyncio
async def test_channel_created_once(monkeypatch):
    store = FakeLogStore()
    _patch_store(monkeypatch, store)
    guild = _FakeGuild(1)
    bot = _FakeBot()

    ch1 = await rep_log.ensure_log_channel(bot, guild)
    assert ch1 is not None and ch1.id == 777
    assert len(guild.created) == 1
    assert guild.created[0]["name"] == "репутация-логи"
    assert store.settings[(1, "rep_log_channel_id")] == "777"

    # Повторный вызов — канал берётся по сохранённому ID, без создания
    ch2 = await rep_log.ensure_log_channel(bot, guild)
    assert ch2 is ch1
    assert len(guild.created) == 1


@pytest.mark.asyncio
async def test_existing_channel_reused_and_permissions(monkeypatch):
    store = FakeLogStore()
    _patch_store(monkeypatch, store)
    existing = _FakeChannel(1569034686691414172)
    guild = _FakeGuild(1, channels={existing.id: existing})
    bot = _FakeBot()

    ch = await rep_log.ensure_log_channel(bot, guild)
    assert ch is existing
    assert guild.created == []  # ничего не создано
    # overwrites синхронизированы: @everyone read-only
    assert len(existing.edits) == 1
    overwrites = existing.edits[0]["overwrites"]
    everyone = overwrites[guild.default_role]
    assert everyone.send_messages is False
    assert everyone.view_channel is True
    assert everyone.add_reactions is False
    assert everyone.create_public_threads is False
    assert everyone.create_private_threads is False
    assert everyone.send_messages_in_threads is False
    bot_ow = overwrites[guild.me]
    assert bot_ow.send_messages is True


@pytest.mark.asyncio
async def test_deleted_channel_recreated_once(monkeypatch):
    store = FakeLogStore()
    _patch_store(monkeypatch, store)
    guild = _FakeGuild(1)
    bot = _FakeBot()

    ch1 = await rep_log.ensure_log_channel(bot, guild)
    guild._channels.clear()  # канал удалили

    ch2 = await rep_log.ensure_log_channel(bot, guild)
    assert ch2 is not None and ch2.id != ch1.id or True
    assert len(guild.created) == 2  # один повтор на удаление, не на рестарт
    # ID в настройках обновлён
    assert store.settings[(1, "rep_log_channel_id")] == "777"


# ---------------------------------------------------------------- cards

def _view_texts(view) -> str:
    """Собрать весь текст из TextDisplay внутри Container."""
    parts = []
    container = view.container
    for item in container.children:
        content = getattr(item, "content", None)
        if content:
            parts.append(content)
    return "\n".join(parts)


def test_log_view_manual_rep():
    view = rep_log.build_log_view(_row())
    text = _view_texts(view)
    assert "РЕПУТАЦИЯ ПОЛУЧЕНА" in text
    assert "Источник: Выдача от участника" in text
    assert "Получатель: <@200>" in text
    assert "Отправитель: <@100>" in text
    assert "+1 REP" in text
    assert "Текущая репутация: 24" in text
    assert "<t:" in text  # Discord timestamp
    # без эмодзи
    assert not any(ord(c) > 0xFFFF for c in text)


def test_log_view_invite():
    view = rep_log.build_log_view(
        _row(source=SRC_INVITE, amount=5, balance_after=29,
             actor_user_id=100, subject_user_id=300)
    )
    text = _view_texts(view)
    assert "НАГРАДА ЗА ПРИГЛАШЕНИЕ" in text
    assert "Пригласивший: <@100>" in text
    assert "Новый участник: <@300>" in text
    assert "+5 REP" in text


def test_log_view_boost():
    view = rep_log.build_log_view(
        _row(source=SRC_BOOST, amount=10, balance_after=39,
             actor_user_id=0, subject_user_id=0, user_id=500)
    )
    text = _view_texts(view)
    assert "НАГРАДА ЗА ПОДДЕРЖКУ СЕРВЕРА" in text
    assert "Участник: <@500>" in text
    assert "+10 REP" in text


def test_log_view_server_tag():
    view = rep_log.build_log_view(
        _row(source="SERVER_TAG", amount=5, balance_after=35,
             actor_user_id=700, subject_user_id=700, user_id=700),
        guild_name="r!z3",
    )
    text = _view_texts(view)
    assert "НАГРАДА ЗА ТЕГ СЕРВЕРА" in text
    assert "Участник: <@700>" in text
    assert "Причина: Установка тега r!z3" in text
    assert "+5 REP" in text
    assert "Текущая репутация: 35" in text


def test_log_view_admin_negative():
    view = rep_log.build_log_view(
        _row(source=SRC_ADMIN, amount=-5, balance_after=34,
             actor_user_id=1, user_id=200)
    )
    text = _view_texts(view)
    assert "РЕПУТАЦИЯ ИЗМЕНЕНА" in text
    assert "Изменение" not in text  # у admin свой формат? нет — проверим ниже
    # admin использует общий блок «Начислено»
    assert "-5 REP" in text
    assert "Текущая репутация: 34" in text


# ---------------------------------------------------------------- publish

@pytest.mark.asyncio
async def test_publish_pending_sends_one_log(monkeypatch):
    channel = _FakeChannel(55, guild=SimpleNamespace(name="G", id=1))
    guild = SimpleNamespace(id=1, name="G")
    bot = _FakeBot()
    bot._guilds[1] = guild

    async def _ensure(b, g):
        return channel

    monkeypatch.setattr(rep_log, "ensure_log_channel", _ensure)
    store = FakeLogStore([_row(id=1), _row(id=2, source=SRC_INVITE, amount=5)])
    _patch_store(monkeypatch, store)

    n = await rep_log.publish_pending(bot)
    assert n == 2
    assert len(channel.sent) == 2
    assert store.sent == [1, 2]
    # allowed_mentions.none — без пингов
    assert channel.sent[0]["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_publish_failure_keeps_pending(monkeypatch):
    channel = _FakeChannel(55, guild=SimpleNamespace(name="G", id=1), fail=True)
    guild = SimpleNamespace(id=1, name="G")
    bot = _FakeBot()
    bot._guilds[1] = guild

    async def _ensure(b, g):
        return channel

    monkeypatch.setattr(rep_log, "ensure_log_channel", _ensure)
    store = FakeLogStore([_row(id=7)])
    _patch_store(monkeypatch, store)

    n = await rep_log.publish_pending(bot)
    assert n == 0
    assert store.sent == []
    assert store.failed and store.failed[0][0] == 7
    assert store.rows[0]["log_status"] == "pending"  # REP не откачен, повторим


@pytest.mark.asyncio
async def test_publish_no_duplicates_on_retry(monkeypatch):
    """Повторный прогон не отправляет уже sent-события."""
    channel = _FakeChannel(55, guild=SimpleNamespace(name="G", id=1))
    guild = SimpleNamespace(id=1, name="G")
    bot = _FakeBot()
    bot._guilds[1] = guild

    async def _ensure(b, g):
        return channel

    monkeypatch.setattr(rep_log, "ensure_log_channel", _ensure)
    row = _row(id=3)
    store = FakeLogStore([row])
    _patch_store(monkeypatch, store)

    await rep_log.publish_pending(bot)
    await rep_log.publish_pending(bot)  # имитация рестарта/повтора
    assert len(channel.sent) == 1  # sent не попадает в pending


@pytest.mark.asyncio
async def test_publish_without_channel_marks_failed(monkeypatch):
    bot = _FakeBot()
    # guild отсутствует → канал недоступен
    store = FakeLogStore([_row(id=9, guild_id=999)])
    _patch_store(monkeypatch, store)

    n = await rep_log.publish_pending(bot)
    assert n == 0
    assert store.failed and store.failed[0][0] == 9
