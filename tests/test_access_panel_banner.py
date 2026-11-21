"""Тесты CV2-панели ротации доступа с PNG-баннером.

Покрытие: MediaGallery в панели, первое создание с вложением,
обновление без повторной отправки PNG, восстановление
отсутствующего attachment, отсутствие дублей сообщений,
persistent-кнопки после «рестарта».
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

import cogs.access as access_cog
from services import access_rotation as rot
from storage import access_store
from utils import access_banner
from views import access_panel


ACCESS_ROLE = 1566949895078940672


# ---------------------------------------------------------------------------
# Фейки Discord
# ---------------------------------------------------------------------------


class FakeAttachment:
    def __init__(self, filename):
        self.filename = filename
        self.url = f"https://cdn.example/{filename}"


class FakeMessage:
    def __init__(self, mid, attachments=()):
        self.id = mid
        self.attachments = list(attachments)
        self.edits = []          # kwargs каждого edit()
        self.deleted = False

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        # эмулируем Discord: attachments=[File] заменяет список
        att = kwargs.get("attachments")
        if att is not None:
            self.attachments = [
                FakeAttachment(getattr(f, "filename", "?"))
                for f in att
            ]
        return self


class FakeChannel:
    def __init__(self, cid, name="доступ-ротация"):
        self.id = cid
        self.name = name
        self.sent = []
        self.messages = {}
        self._next = 1

    async def send(self, **kwargs):
        msg = FakeMessage(9000 + self._next, ())
        self._next += 1
        # file= → attachment в сообщении
        f = kwargs.get("file")
        if f is not None:
            msg.attachments.append(FakeAttachment(f.filename))
        self.sent.append((msg, kwargs))
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        msg = self.messages.get(int(mid))
        if msg is None:
            raise discord.NotFound(
                NS(status=404, reason="Not Found", message="nf"), "nf")
        return msg


class FakeGuild:
    def __init__(self, gid=1, channels=()):
        self.id = gid
        self.text_channels = list(channels)
        self.members = []
        self.name = "test"

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    def get_member(self, uid):
        return None


class FakeStateStore:
    def __init__(self):
        self.state = {}

    async def get_state(self, gid):
        return self.state.get(gid, {"channel_id": None, "message_id": None})

    async def set_panel(self, gid, cid, mid):
        self.state[gid] = {"channel_id": cid, "message_id": mid}


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------


@pytest.fixture
def store(monkeypatch):
    fs = FakeStateStore()
    monkeypatch.setattr(access_cog, "access_store", fs)
    monkeypatch.setattr(rot, "access_store", fs)
    return fs


@pytest.fixture
def ctx(monkeypatch):
    async def _ctx(guild):
        return {
            "holders": [], "scores": {}, "queue_count": 0,
            "queue": [], "next_rotation": None, "last": None,
        }
    monkeypatch.setattr(rot, "panel_context", _ctx)


# ---------------------------------------------------------------------------
# Тесты
# ---------------------------------------------------------------------------


def test_banner_asset_exists():
    assert access_banner.available()
    assert access_banner._PATH.exists()


def test_render_panel_has_mediagallery_banner():
    view = access_panel.render_panel_view(None, {
        "holders": [], "scores": {}, "queue_count": 0,
        "queue": [], "next_rotation": None, "last": None,
    })
    # LayoutView → Container → [MediaGallery, Separator, TextDisplay, ...]
    container = view.children[0]
    gallery = next(
        c for c in container.children
        if isinstance(c, discord.ui.MediaGallery))
    item = gallery.items[0]
    assert isinstance(item, discord.MediaGalleryItem)
    assert getattr(item.media, "url", item.media) == access_banner.MEDIA
    assert access_banner.MEDIA == "attachment://access_rotation_banner.png"


def test_render_panel_no_banner_when_file_missing(monkeypatch):
    monkeypatch.setattr(access_banner, "_PATH",
                        access_banner._PATH.with_name("missing.png"))
    # banner_available читает _PATH — временно «удаляем» файл
    import utils.access_banner as ab
    orig = ab._PATH
    ab._PATH = orig.parent / "definitely_missing_banner.png"
    try:
        assert not ab.available()
        view = access_panel.render_panel_view(None, {
            "holders": [], "scores": {}, "queue_count": 0,
            "queue": [], "next_rotation": None, "last": None,
        })
        container = view.children[0]
        assert not any(
            isinstance(c, discord.ui.MediaGallery)
            for c in container.children)
    finally:
        ab._PATH = orig


@pytest.mark.asyncio
async def test_ensure_panel_first_create_sends_banner(
        store, ctx, monkeypatch):
    monkeypatch.setattr(access_cog, "ACCESS_ROTATION_CHANNEL_ID", 0)
    ch = FakeChannel(10)
    guild = FakeGuild(channels=[ch])

    ok = await access_cog.ensure_panel(guild)
    assert ok
    assert len(ch.sent) == 1
    msg, kwargs = ch.sent[0]
    # PNG прикреплён при создании
    assert kwargs.get("file") is not None
    assert kwargs["file"].filename == access_banner.FILENAME
    assert msg.attachments[0].filename == access_banner.FILENAME
    # state сохранён
    assert store.state[1] == {"channel_id": 10, "message_id": msg.id}


@pytest.mark.asyncio
async def test_ensure_panel_edit_preserves_banner_no_resend(
        store, ctx, monkeypatch):
    monkeypatch.setattr(access_cog, "ACCESS_ROTATION_CHANNEL_ID", 0)
    ch = FakeChannel(10)
    guild = FakeGuild(channels=[ch])
    await access_cog.ensure_panel(guild)
    mid = store.state[1]["message_id"]

    # повторный ensure → edit существующего, без новой отправки
    ok = await access_cog.ensure_panel(guild)
    assert ok
    assert len(ch.sent) == 1            # дублей нет
    msg = ch.messages[mid]
    assert len(msg.edits) == 1
    assert "attachments" not in msg.edits[0]  # баннер уже есть — не трогаем
    assert len(msg.attachments) == 1    # PNG на месте


@pytest.mark.asyncio
async def test_ensure_panel_restores_missing_banner(
        store, ctx, monkeypatch):
    monkeypatch.setattr(access_cog, "ACCESS_ROTATION_CHANNEL_ID", 0)
    ch = FakeChannel(10)
    guild = FakeGuild(channels=[ch])
    await access_cog.ensure_panel(guild)
    mid = store.state[1]["message_id"]

    # «Потеряли» вложение (например, сообщение пересоздано без файла)
    msg = ch.messages[mid]
    msg.attachments.clear()

    ok = await access_cog.ensure_panel(guild)
    assert ok
    assert len(ch.sent) == 1
    assert "attachments" in msg.edits[-1]
    assert msg.attachments[0].filename == access_banner.FILENAME


@pytest.mark.asyncio
async def test_refresh_panel_edit_with_banner_restore(
        store, ctx, monkeypatch):
    ch = FakeChannel(10)
    guild = FakeGuild(channels=[ch])
    # state уже есть (панель создана до «рестарта»)
    await store.set_panel(1, 10, None)
    # создаём сообщение вручную с баннером, как было до рестарта
    msg = await ch.send(view=None)
    msg.attachments.append(FakeAttachment(access_banner.FILENAME))
    await store.set_panel(1, 10, msg.id)

    ok = await rot.refresh_panel(guild)
    assert ok
    assert "attachments" not in msg.edits[-1]   # баннер сохранился
    assert msg.attachments[0].filename == access_banner.FILENAME

    # убрали вложение → refresh вернёт его
    msg.attachments.clear()
    ok = await rot.refresh_panel(guild)
    assert ok
    assert msg.attachments[0].filename == access_banner.FILENAME


@pytest.mark.asyncio
async def test_ensure_panel_deleted_message_recreates_once(
        store, ctx, monkeypatch):
    monkeypatch.setattr(access_cog, "ACCESS_ROTATION_CHANNEL_ID", 0)
    ch = FakeChannel(10)
    guild = FakeGuild(channels=[ch])
    await access_cog.ensure_panel(guild)
    mid = store.state[1]["message_id"]
    del ch.messages[mid]  # сообщение удалено

    ok = await access_cog.ensure_panel(guild)
    assert ok
    assert len(ch.sent) == 2               # одно новое сообщение
    new_mid = store.state[1]["message_id"]
    assert new_mid != mid


@pytest.mark.asyncio
async def test_persistent_buttons_after_restart():
    """AccessPanelView регистрирует те же custom_id — кнопки
    работают после рестарта, не привязаны к сообщению."""
    view = access_panel.AccessPanelView()
    flat = [getattr(item, "custom_id", None) for item in view.children]
    assert "access:join" in flat
    assert "access:leave" in flat
