"""Regression-тесты hotfix «Access Rotation Channel Fix».

Покрытие TZ §9: закреплённый канал 1573700340417699890,
отсутствие автосоздания каналов, синхронизация access_state,
идемпотентность ensure_panel (asyncio + PG mutex), защита
устаревших кнопок, Organizer Academy в ЛС.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

import cogs.access as access_cog
from services import access_rotation as rot
from storage import access_store
from views import access_panel
from services.access_academy import AccessDmView

PINNED = 1573700340417699890
OLD_DUP = 1573700340417699891
BOT_ID = 42


# ---------------------------------------------------------------------------
# Фейки Discord
# ---------------------------------------------------------------------------


class FakeAttachment:
    def __init__(self, filename):
        self.filename = filename
        self.url = f"https://cdn.example/{filename}"


class FakeComponent:
    def __init__(self, custom_id=None, children=()):
        self.custom_id = custom_id
        self.children = list(children)


class FakeMessage:
    PANEL_COMPONENTS = [FakeComponent("access:join"),
                        FakeComponent("access:leave")]

    def __init__(self, mid, *, panel=False, author_id=BOT_ID):
        self.id = mid
        self.author = NS(id=author_id)
        self.attachments = []
        self.edits = []
        self.deleted = False
        self.components = (
            list(self.PANEL_COMPONENTS) if panel else [])

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        att = kwargs.get("attachments")
        if att is not None:
            self.attachments = [
                FakeAttachment(getattr(f, "filename", "?"))
                for f in att
            ]
        return self


class FakeChannel:
    def __init__(self, cid, name="доступ-ротация",
                 channel_type=discord.ChannelType.text):
        self.id = cid
        self.name = name
        self.type = channel_type
        self.guild = None
        self.sent = []
        self.messages = {}
        self._next = 1
        self.permissions_calls = 0

    def history(self, limit=100, **_kw):
        async def _gen():
            for m in list(self.messages.values())[:limit]:
                if not m.deleted:
                    yield m
        return _gen()

    async def send(self, **kwargs):
        msg = FakeMessage(9000 + self._next, panel=True)
        self._next += 1
        f = kwargs.get("file")
        if f is not None:
            msg.attachments.append(FakeAttachment(f.filename))
        self.sent.append((msg, kwargs))
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        msg = self.messages.get(int(mid))
        if msg is None or msg.deleted:
            raise discord.NotFound(
                NS(status=404, reason="Not Found", message="nf"), "nf")
        return msg

    def permissions_for(self, _member):
        self.permissions_calls += 1
        return NS(view_channel=True, send_messages=True,
                  read_message_history=True, attach_files=True)


class FakeGuild:
    def __init__(self, gid=1, channels=()):
        self.id = gid
        self.text_channels = list(channels)
        self.members = []
        self.name = "test"
        self.me = None
        self.created_channels = []
        for ch in self.text_channels:
            ch.guild = self

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    async def fetch_channel(self, cid):
        ch = self.get_channel(cid)
        if ch is None:
            raise discord.NotFound(
                NS(status=404, reason="Not Found", message="nf"), "nf")
        return ch

    async def create_text_channel(self, *a, **kw):  # pragma: no cover
        self.created_channels.append((a, kw))
        raise AssertionError("create_text_channel must never be called")

    def get_member(self, uid):
        return None


class FakeStateStore:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.fail_get = False
        self.fail_set = False

    async def get_state(self, gid):
        if self.fail_get:
            raise RuntimeError("db down")
        return self.state.get(
            gid, {"channel_id": None, "message_id": None})

    async def set_panel(self, gid, cid, mid):
        if self.fail_set:
            raise RuntimeError("db write failed")
        self.state[gid] = {"channel_id": cid, "message_id": mid}


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.done = False

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.messages.append({"content": content, **kwargs})
        return NS(id=1)

    @property
    def is_done(self):
        return self.done


class FakeInteraction:
    def __init__(self, guild, *, channel_id, message_id, user_id=7):
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.channel_id = channel_id
        self.message = NS(id=message_id)
        self.user = NS(id=user_id, guild_permissions=NS(administrator=False))
        self.response = FakeResponse()
        self.followups = []

    async def followup_send(self, *a, **kw):
        self.followups.append((a, kw))


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(rot, "ACCESS_ROTATION_CHANNEL_ID", PINNED)
    monkeypatch.setattr(
        access_panel, "ACCESS_ROTATION_CHANNEL_ID", PINNED)
    monkeypatch.setattr(
        access_cog, "ACCESS_ROTATION_CHANNEL_ID", PINNED)


@pytest.fixture
def store(monkeypatch):
    fs = FakeStateStore()
    monkeypatch.setattr(access_cog, "access_store", fs)
    monkeypatch.setattr(rot, "access_store", fs)
    monkeypatch.setattr(access_panel, "access_store", fs)
    return fs


@pytest.fixture
def ctx(monkeypatch):
    async def _ctx(guild):
        return {
            "holders": [], "scores": {}, "queue_count": 0,
            "queue": [], "next_rotation": None, "last": None,
        }
    monkeypatch.setattr(rot, "panel_context", _ctx)


def _guild_with_pinned(name="основной-канал"):
    pinned_ch = FakeChannel(PINNED, name=name)
    dup = FakeChannel(OLD_DUP, name="🎟・доступ-ротация")
    return FakeGuild(channels=[pinned_ch, dup]), pinned_ch, dup


# ---------------------------------------------------------------------------
# §9.1–2: канал по закреплённому ID, без создания
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_found_by_pinned_id_not_name(
        pinned, store, ctx):
    guild, pinned_ch, dup = _guild_with_pinned()
    ok = await access_cog.ensure_panel(guild)
    assert ok
    assert len(pinned_ch.sent) == 1
    assert len(dup.sent) == 0
    assert store.state[1] == {
        "channel_id": PINNED,
        "message_id": pinned_ch.sent[0][0].id,
    }
    assert guild.created_channels == []


@pytest.mark.asyncio
async def test_no_channel_creation_when_pinned_missing(
        pinned, store, ctx):
    """Нет канала с pinned ID → False, create_text_channel не
    вызывается даже при наличии дубля по имени."""
    dup = FakeChannel(OLD_DUP, name="доступ-ротация")
    guild = FakeGuild(channels=[dup])
    ok = await access_cog.ensure_panel(guild)
    assert not ok
    assert len(dup.sent) == 0
    assert guild.created_channels == []
    assert store.state.get(1, {}).get("channel_id") is None


@pytest.mark.asyncio
async def test_no_channel_creation_empty_guild(pinned, store, ctx):
    guild = FakeGuild(channels=[])
    assert not await access_cog.ensure_panel(guild)
    assert guild.created_channels == []


@pytest.mark.asyncio
async def test_pinned_channel_wrong_type_rejected(pinned, store, ctx):
    voice = FakeChannel(
        PINNED, name="voice", channel_type=discord.ChannelType.voice)
    guild = FakeGuild(channels=[voice])
    assert not await access_cog.ensure_panel(guild)
    assert len(voice.sent) == 0


@pytest.mark.asyncio
async def test_pinned_channel_forbidden_rejected(pinned, store, ctx):
    ch = FakeChannel(PINNED)

    def _deny(_member):
        return NS(view_channel=False, send_messages=False,
                  read_message_history=False, attach_files=False)
    ch.permissions_for = _deny
    guild = FakeGuild(channels=[ch])
    guild.me = NS(id=999)
    assert not await access_cog.ensure_panel(guild)
    assert len(ch.sent) == 0


# ---------------------------------------------------------------------------
# §9.3–5: синхронизация state, edit vs send
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_channel_id_in_db_syncs_to_pinned(
        pinned, store, ctx):
    """PostgreSQL хранит старый channel_id дубля — панель
    находится внутри pinned-канала, state переписывается."""
    guild, pinned_ch, dup = _guild_with_pinned()
    # Старая панель в дублирующем канале — НЕ используется.
    old_msg = await dup.send(view=None)
    old_msg.components = list(FakeMessage.PANEL_COMPONENTS)
    await store.set_panel(1, OLD_DUP, old_msg.id)
    # Актуальная панель уже есть в pinned-канале.
    real = await pinned_ch.send(view=None)
    real.components = list(FakeMessage.PANEL_COMPONENTS)

    ok = await access_cog.ensure_panel(guild)
    assert ok
    # Найдена и отредактирована существующая, новых отправок нет
    assert len(pinned_ch.sent) == 1   # только ручная отправка выше
    assert len(real.edits) == 1
    assert store.state[1] == {
        "channel_id": PINNED, "message_id": real.id}


@pytest.mark.asyncio
async def test_existing_message_edited_not_resent(
        pinned, store, ctx):
    guild, pinned_ch, _ = _guild_with_pinned()
    msg = await pinned_ch.send(view=None)
    msg.components = list(FakeMessage.PANEL_COMPONENTS)
    await store.set_panel(1, PINNED, msg.id)

    ok = await access_cog.ensure_panel(guild)
    assert ok
    assert len(pinned_ch.sent) == 1   # send был только вручную
    assert len(msg.edits) == 1


@pytest.mark.asyncio
async def test_missing_message_single_recreate(
        pinned, store, ctx):
    """Сообщение удалено → ровно одно новое сообщение панели,
    повторный запуск редактирует его (без массового дубля)."""
    guild, pinned_ch, _ = _guild_with_pinned()
    ghost = FakeMessage(1234, panel=True)
    pinned_ch.messages[1234] = ghost
    ghost.deleted = True
    await store.set_panel(1, PINNED, 1234)

    assert await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 1
    # второй запуск — edit, не send
    assert await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 1


@pytest.mark.asyncio
async def test_send_ok_but_set_panel_failed_no_duplicate(
        pinned, store, ctx):
    """Discord отправил сообщение, но set_panel упал — следующий
    запуск находит его через history и НЕ шлёт второе."""
    guild, pinned_ch, _ = _guild_with_pinned()
    store.fail_set = True
    # send прошёл, set_panel упал → False, но сообщение есть
    assert not await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 1

    store.fail_set = False
    assert await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 1   # дубля нет
    assert store.state[1]["channel_id"] == PINNED


# ---------------------------------------------------------------------------
# §9.6–7: блокировки, on_ready/rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_ensure_panel_no_duplicates(
        pinned, store, ctx):
    guild, pinned_ch, _ = _guild_with_pinned()
    results = await asyncio.gather(
        access_cog.ensure_panel(guild),
        access_cog.ensure_panel(guild),
        access_cog.ensure_panel(guild),
    )
    assert all(results)
    assert len(pinned_ch.sent) == 1


@pytest.mark.asyncio
async def test_panel_mutex_skips_when_held(pinned, store, ctx,
                                           monkeypatch):
    """Межпроцессная блокировка: mutex=False → обновление
    пропускается, Discord не трогается."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _busy(gid):
        yield False

    monkeypatch.setattr(access_cog.access_store, "panel_mutex", _busy, raising=False)
    guild, pinned_ch, _ = _guild_with_pinned()
    assert not await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 0


@pytest.mark.asyncio
async def test_panel_mutex_error_no_channel(pinned, store, ctx,
                                            monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _boom(gid):
        raise RuntimeError("pg down")
        yield  # pragma: no cover

    monkeypatch.setattr(access_cog.access_store, "panel_mutex", _boom, raising=False)
    guild, pinned_ch, _ = _guild_with_pinned()
    assert not await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 0
    assert guild.created_channels == []


@pytest.mark.asyncio
async def test_repeated_on_ready_style_calls_idempotent(
        pinned, store, ctx):
    """Многократные ensure_panel (как при повторном on_ready /
    rotation_loop) не создают ни каналов, ни сообщений."""
    guild, pinned_ch, _ = _guild_with_pinned()
    for _ in range(3):
        assert await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 1
    assert guild.created_channels == []


@pytest.mark.asyncio
async def test_rotation_after_panel_update_no_channel(
        pinned, store, ctx):
    """maybe_rotate → _after_rotation → ensure_panel: ротация не
    создаёт канал (run_rotation замокан — здесь только панель)."""
    guild, pinned_ch, _ = _guild_with_pinned()
    await access_cog.ensure_panel(guild)
    cog = access_cog.AccessCog.__new__(access_cog.AccessCog)
    await cog._after_rotation(guild, {"swapped": None,
                                      "filled": []})
    assert len(pinned_ch.sent) == 1
    assert guild.created_channels == []


# ---------------------------------------------------------------------------
# §9.8–10: ошибки Discord API и PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discord_send_error_no_channel(pinned, store, ctx,
                                             monkeypatch):
    guild, pinned_ch, _ = _guild_with_pinned()

    async def _fail(**kwargs):
        raise discord.HTTPException(
            NS(status=500, reason="ISE", message="err"), "err")
    pinned_ch.send = _fail
    assert not await access_cog.ensure_panel(guild)
    assert guild.created_channels == []


@pytest.mark.asyncio
async def test_discord_forbidden_no_channel(pinned, store, ctx,
                                            monkeypatch):
    guild, pinned_ch, _ = _guild_with_pinned()

    async def _fail(**kwargs):
        raise discord.Forbidden(
            NS(status=403, reason="Forbidden", message="no"), "no")
    pinned_ch.send = _fail
    assert not await access_cog.ensure_panel(guild)
    assert guild.created_channels == []


@pytest.mark.asyncio
async def test_pg_get_state_error_no_channel(pinned, store, ctx):
    guild, pinned_ch, _ = _guild_with_pinned()
    store.fail_get = True
    assert not await access_cog.ensure_panel(guild)
    assert len(pinned_ch.sent) == 0
    assert guild.created_channels == []


@pytest.mark.asyncio
async def test_refresh_panel_uses_pinned_and_resyncs(
        pinned, store, ctx):
    """refresh_panel не редактирует сообщение в дублирующем
    канале: state с чужим channel_id → поиск в pinned."""
    guild, pinned_ch, dup = _guild_with_pinned()
    old_msg = await dup.send(view=None)
    old_msg.components = list(FakeMessage.PANEL_COMPONENTS)
    await store.set_panel(1, OLD_DUP, old_msg.id)
    real = await pinned_ch.send(view=None)
    real.components = list(FakeMessage.PANEL_COMPONENTS)

    ok = await rot.refresh_panel(guild)
    assert ok
    assert len(real.edits) == 1
    assert len(old_msg.edits) == 0
    assert store.state[1]["channel_id"] == PINNED


@pytest.mark.asyncio
async def test_refresh_panel_no_state_no_create(pinned, store, ctx):
    guild, pinned_ch, _ = _guild_with_pinned()
    assert not await rot.refresh_panel(guild)
    assert len(pinned_ch.sent) == 0
    assert guild.created_channels == []


# ---------------------------------------------------------------------------
# §9.11–13: кнопки — устаревшие отклоняются, актуальные работают,
# Academy в ЛС не затронута
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_panel_button_rejected(pinned, store):
    guild, _, _ = _guild_with_pinned()
    await store.set_panel(1, PINNED, 5555)
    joined = []

    async def _qj(gid, uid):
        joined.append(uid)
        return True
    store.queue_join = _qj  # type: ignore[attr-defined]

    btn = access_panel._JoinButton()
    # Кнопка из ДРУГОГО канала (дубликат)
    ix = FakeInteraction(guild, channel_id=OLD_DUP, message_id=5555)
    await btn.callback(ix)
    assert joined == []
    assert len(ix.response.messages) == 1
    assert "устарела" in ix.response.messages[0]["content"]

    # Кнопка другого сообщения в ТОМ ЖЕ канале
    ix2 = FakeInteraction(guild, channel_id=PINNED, message_id=9999)
    await btn.callback(ix2)
    assert joined == []


@pytest.mark.asyncio
async def test_current_panel_button_works(pinned, store):
    guild, _, _ = _guild_with_pinned()
    await store.set_panel(1, PINNED, 5555)
    joined = []

    async def _qj(gid, uid):
        joined.append(uid)
        return True
    store.queue_join = _qj  # type: ignore[attr-defined]
    store.queue_leave = lambda *a: None  # type: ignore[attr-defined]

    btn = access_panel._JoinButton()
    ix = FakeInteraction(guild, channel_id=PINNED, message_id=5555)
    await btn.callback(ix)
    assert joined == [7]
    assert "Заявка на доступ подана" in \
        ix.response.messages[-1]["content"]


@pytest.mark.asyncio
async def test_leave_button_stale_rejected(pinned, store):
    guild, _, _ = _guild_with_pinned()
    await store.set_panel(1, PINNED, 5555)
    left = []

    async def _ql(gid, uid):
        left.append(uid)
        return True
    store.queue_leave = _ql  # type: ignore[attr-defined]

    btn = access_panel._LeaveButton()
    ix = FakeInteraction(guild, channel_id=OLD_DUP, message_id=5555)
    await btn.callback(ix)
    assert left == []
    assert "устарела" in ix.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_guide_and_orgpanel_stale_rejected(pinned, store):
    guild, _, _ = _guild_with_pinned()
    await store.set_panel(1, PINNED, 5555)
    for cls in (access_panel._GuideButton,
                access_panel._OrgPanelButton):
        btn = cls()
        ix = FakeInteraction(guild, channel_id=OLD_DUP,
                             message_id=5555)
        await btn.callback(ix)
        assert "устарела" in ix.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_dm_academy_buttons_unaffected(pinned, store):
    """Кнопки Organizer Academy в ЛС (access_dm:*) не проверяют
    привязку панели — guild=None, ephemeral-ответ с гайдом."""
    view = AccessDmView()
    guide_btn = next(
        c for c in view.children
        if getattr(c, "custom_id", "") == "access_dm:guide")
    ix = FakeInteraction(None, channel_id=0, message_id=0)
    await guide_btn.callback(ix)
    assert ix.response.messages
    assert ix.response.messages[0].get("view") is not None


# ---------------------------------------------------------------------------
# Final hardening: fail-closed binding, find_panel_message, retry_429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_binding_fail_closed_on_pg_error(pinned, store):
    """PG недоступен → get_state падает → кнопка отклонена,
    очередь/роли не трогаются (fail-closed)."""
    guild, _, _ = _guild_with_pinned()
    store.fail_get = True
    await store.set_panel(1, PINNED, 5555)
    btn = access_panel._JoinButton()
    ix = FakeInteraction(guild, channel_id=PINNED, message_id=5555)
    await btn.callback(ix)
    assert "проверить" in ix.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_binding_rejected_without_message_id(pinned, store):
    """state есть, но message_id отсутствует → действие запрещено."""
    guild, _, _ = _guild_with_pinned()
    await store.set_panel(1, PINNED, None)
    btn = access_panel._JoinButton()
    ix = FakeInteraction(guild, channel_id=PINNED, message_id=5555)
    await btn.callback(ix)
    assert "устарела" in ix.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_binding_rejected_on_message_mismatch(pinned, store):
    """message_id не совпадает → кнопка отклонена."""
    guild, _, _ = _guild_with_pinned()
    await store.set_panel(1, PINNED, 5555)
    btn = access_panel._JoinButton()
    ix = FakeInteraction(guild, channel_id=PINNED, message_id=9999)
    await btn.callback(ix)
    assert "устарела" in ix.response.messages[0]["content"]


@pytest.mark.asyncio
async def test_find_panel_message_incomplete_blocks_send(
        pinned, store, monkeypatch):
    """Неполный поиск панели (история недоступна) → ensure_panel
    НЕ отправляет новую панель (защита от дубля)."""
    import cogs.access as access_cog
    guild, pinned_ch, _ = _guild_with_pinned()

    async def _no_history(*a, **kw):
        return None, False
    monkeypatch.setattr(rot, "find_panel_message", _no_history)
    # Все fetch/edit/send должны отсутствовать: канал без
    # сохранённого mid → find → incomplete → return False.
    sent = []

    async def _send(*a, **kw):
        sent.append(1)
        return NS(id=1)
    pinned_ch.send = _send
    ok = await access_cog.ensure_panel(guild)
    assert ok is False
    assert sent == []


@pytest.mark.asyncio
async def test_find_panel_message_complete_empty_sends(
        pinned, store, monkeypatch):
    """Полный поиск без находки → панель отправляется один раз."""
    import cogs.access as access_cog
    guild, pinned_ch, _ = _guild_with_pinned()

    async def _empty(*a, **kw):
        return None, True
    monkeypatch.setattr(rot, "find_panel_message", _empty)
    sent = []

    async def _send(*a, **kw):
        sent.append(1)
        return NS(id=777)
    pinned_ch.send = _send
    monkeypatch.setattr(access_cog, "banner_file",
                        lambda: None, raising=False)
    ok = await access_cog.ensure_panel(guild)
    assert ok is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_retry_429_honors_full_retry_after(monkeypatch):
    """_retry_429 ждёт полный retry_after, а не обрезает до 5с."""
    import cogs.access as access_cog
    import discord as _d
    calls = []
    slept = []

    class _429(_d.HTTPException):
        def __init__(self):
            resp = NS(status=429, reason="rate limited")
            super().__init__(resp, {"code": 0, "message": "slow"})
            self.retry_after = 42.0

    async def _factory():
        calls.append(1)
        if len(calls) == 1:
            raise _429()
        return "ok"

    async def _sleep(sec):
        slept.append(sec)
    monkeypatch.setattr(access_cog.asyncio, "sleep", _sleep)
    result = await access_cog._retry_429(_factory)
    assert result == "ok"
    assert slept and slept[0] >= 42.0   # НЕ min(...,5)


@pytest.mark.asyncio
async def test_binding_rejected_when_state_channel_differs_from_pinned(
        pinned, store):
    """ACCESS_ROTATION_CHANNEL_ID задан, но сохранённый
    access_state.channel_id указывает на другой канал —
    привязка ещё не синхронизирована → кнопки отклоняются,
    даже если message_id и канал нажатия «совпадают»."""
    guild, _, _ = _guild_with_pinned()
    # state.channel_id = другой канал, message_id = 5555.
    await store.set_panel(1, OLD_DUP, 5555)
    joined = []

    async def _qj(gid, uid):
        joined.append(uid)
        return True
    store.queue_join = _qj  # type: ignore[attr-defined]

    btn = access_panel._JoinButton()
    # Нажатие в pinned-канале на «правильное» message_id —
    # всё равно отклоняется: state.channel_id != pinned.
    ix = FakeInteraction(guild, channel_id=PINNED, message_id=5555)
    await btn.callback(ix)
    assert joined == []
    assert "устарела" in ix.response.messages[0]["content"]
