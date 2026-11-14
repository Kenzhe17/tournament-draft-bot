"""P0: отображение ников с экипированной косметикой + UserSelect fix.

- _player_display: имя (не <@uid>) + [TAG] + значок из магазина.
- Неэкипированная косметика не отображается.
- Одинаковые display_name у разных user_id не конфликтуют.
- AdminAddUserView._on_pick принимает User/Member-объекты
  из discord.ui.UserSelect.values.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: F401

import utils.cosmetics as cosmetics
import utils.embeds as embeds
import views.setup_view as sv
from models.shop_item import (
    CosmeticRarity,
    CosmeticType,
    PlayerCosmetic,
    ShopItem,
)
from models.tournament import (
    FormationMode,
    RegistrationState,
    Tournament,
    TournamentPhase,
)

GID = 111
CID = 222


# ------------------------------------------------------------------ mocks


def _tag_item(item_id="tag_vip", value="[VIP]"):
    return ShopItem(
        id=item_id, name="VIP Tag", description="",
        price=100, cosmetic_type=CosmeticType.TAG,
        rarity=CosmeticRarity.PREMIUM, value=value,
        category="tags",
    )


def _icon_item(item_id="icon_crown", value="crown"):
    return ShopItem(
        id=item_id, name="Crown", description="",
        price=100, cosmetic_type=CosmeticType.ICON,
        rarity=CosmeticRarity.ELITE, value=value,
        category="icons",
    )


class _FakeShop:
    def __init__(self, items):
        self._items = items  # item_id -> ShopItem

    def get_item(self, item_id):
        return self._items.get(item_id)


class _FakeInventory:
    def __init__(self, equipped):
        # equipped: {(gid, uid): [PlayerCosmetic]}
        self._equipped = equipped

    def get_equipped_cosmetics(self, guild_id, user_id):
        return [
            c for c in self._equipped.get((guild_id, user_id), [])
            if c.equipped
        ]


@pytest.fixture
def shop_env(monkeypatch):
    """Подменить shop_store/inventory_store в utils.cosmetics.

    Тесты пишут в state["items"]/["equipped"] — фейки читают
    те же контейнеры.
    """
    state = {"items": {}, "equipped": {}}
    shop = _FakeShop(state["items"])
    inv = _FakeInventory(state["equipped"])
    monkeypatch.setattr(cosmetics, "shop_store", shop)
    monkeypatch.setattr(cosmetics, "inventory_store", inv)
    # get_emoji импортирован в cosmetics из config — патчим
    # обе привязки.
    import config
    monkeypatch.setattr(config, "get_emoji", lambda key: f"<:{key}:999>")
    monkeypatch.setattr(cosmetics, "get_emoji", lambda key: f"<:{key}:999>")
    return state


def _tournament():
    return Tournament(
        guild_id=GID, channel_id=CID,
        size=__import__("models.tournament", fromlist=["TournamentSize"]).TournamentSize.EIGHT,
        registration=RegistrationState.OPEN,
        phase=TournamentPhase.SETUP,
        formation_mode=FormationMode.RANDOM,
    )


# ------------------------------------------------------- format_player_name


def test_format_tag_and_icon(shop_env):
    shop_env["items"]["tag_vip"] = _tag_item()
    shop_env["items"]["icon_crown"] = _icon_item()
    shop_env["equipped"][(GID, 555)] = [
        PlayerCosmetic(GID, 555, "tag_vip", equipped=True),
        PlayerCosmetic(GID, 555, "icon_crown", equipped=True),
    ]
    out = cosmetics.format_player_name(GID, 555, "Nota")
    assert "[VIP]" in out
    assert "Nota" in out
    assert "<:crown:999>" in out


def test_format_tag_only(shop_env):
    shop_env["items"]["tag_vip"] = _tag_item()
    shop_env["equipped"][(GID, 555)] = [
        PlayerCosmetic(GID, 555, "tag_vip", equipped=True),
    ]
    out = cosmetics.format_player_name(GID, 555, "Nota")
    assert "[VIP]" in out and "Nota" in out
    assert "<:" not in out


def test_format_icon_only(shop_env):
    shop_env["items"]["icon_crown"] = _icon_item()
    shop_env["equipped"][(GID, 555)] = [
        PlayerCosmetic(GID, 555, "icon_crown", equipped=True),
    ]
    out = cosmetics.format_player_name(GID, 555, "Nota")
    assert "Nota" in out and "<:crown:999>" in out
    assert "[VIP]" not in out


def test_format_unequipped_not_shown(shop_env):
    shop_env["items"]["tag_vip"] = _tag_item()
    # Куплен, но НЕ экипирован
    shop_env["equipped"][(GID, 555)] = [
        PlayerCosmetic(GID, 555, "tag_vip", equipped=False),
    ]
    out = cosmetics.format_player_name(GID, 555, "Nota")
    assert out == "Nota"


def test_format_no_cosmetics(shop_env):
    assert cosmetics.format_player_name(GID, 555, "Nota") == "Nota"


# ------------------------------------------------------------- _player_display


def test_player_display_cosmetic_name(shop_env):
    shop_env["items"]["tag_vip"] = _tag_item()
    shop_env["equipped"][(GID, 777)] = [
        PlayerCosmetic(GID, 777, "tag_vip", equipped=True),
    ]
    t = _tournament()
    t.circle1 = ["Nota"]
    t.player_user_ids = {"Nota": 777}
    out = embeds._player_display(t, "Nota", GID)
    assert "[VIP]" in out and "Nota" in out
    assert "<@" not in out


def test_player_display_duplicate_names_no_conflict(shop_env):
    """Одинаковые display_name у разных user_id — разные ключи,
    косметика применяется по user_id, а не по имени."""
    shop_env["items"]["tag_vip"] = _tag_item()
    shop_env["equipped"][(GID, 777)] = [
        PlayerCosmetic(GID, 777, "tag_vip", equipped=True),
    ]
    # Второй игрок с тем же именем — другой uid, без косметики.
    t = _tournament()
    t.circle1 = ["Nota", "Nota "]  # trailing space — иное имя
    t.player_user_ids = {"Nota": 777, "Nota ": 778}
    out1 = embeds._player_display(t, "Nota", GID)
    out2 = embeds._player_display(t, "Nota ", GID)
    assert "[VIP]" in out1
    assert "[VIP]" not in out2


def test_display_name_for_member_priority(monkeypatch):
    """display_name_for: member.display_name > сохранённое имя > id."""
    member = SimpleNamespace(display_name="Fresh Nick")
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 42 else None)
    bot = SimpleNamespace(get_guild=lambda gid: guild)
    import models.tournament as mt
    monkeypatch.setattr(mt, "_bot_instance", bot)

    out = cosmetics.display_name_for(GID, 42, "Old Nick")
    assert out == "Fresh Nick"
    # Нет member → fallback
    assert cosmetics.display_name_for(GID, 43, "Old Nick") == "Old Nick"
    # Нет fallback → id
    assert cosmetics.display_name_for(GID, 44, "") == "44"


# ------------------------------------------------------------- UserSelect fix


def _interaction(guild=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=1, display_name="Org"),
        guild=guild or SimpleNamespace(id=GID, get_member=lambda u: None),
        guild_id=GID,
        channel_id=CID,
        client=SimpleNamespace(update_tournament_message=AsyncMock()),
        data={"resolved": {"users": {}}},
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            send_modal=AsyncMock(),
            defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _find(view, cls):
    for item in view.children:
        if isinstance(item, cls):
            return item
    raise AssertionError(f"{cls} not found")


@pytest.mark.asyncio
async def test_on_pick_accepts_user_object(monkeypatch):
    """UserSelect.values[0] — User/Member-объект с .id."""
    t = _tournament()

    import storage.json_store as js
    monkeypatch.setattr(js, "store", SimpleNamespace(get=lambda g, c: t))
    monkeypatch.setattr(sv, "store", js.store)

    # Права организатора
    import utils.permissions as perm
    monkeypatch.setattr(
        perm, "can_manage_channel_tournament", lambda *a, **k: True)
    # _on_pick импортирует can_manage_channel_tournament локально —
    # патчим в модуле permissions (from-import внутри функции).

    fake_user = SimpleNamespace(id=555, display_name="ObjUser")
    guild = SimpleNamespace(
        id=GID,
        get_member=lambda uid: SimpleNamespace(
            id=uid, display_name="ObjUser",
            display_avatar=SimpleNamespace(url="http://x"),
        ) if uid == 555 else None,
        fetch_member=AsyncMock(side_effect=Exception("no member")),
    )
    interaction = _interaction(guild=guild)

    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = [fake_user]  # объект, не строка
    await select.callback(interaction)

    # Подтверждение показано, имя из member
    edit = interaction.response.edit_message
    assert edit.await_count >= 1
    last_view = edit.call_args.kwargs.get("view")
    texts = []
    for item in getattr(last_view, "children", []):
        for c in getattr(item, "children", []):
            texts.append(getattr(c, "content", ""))
    blob = "\n".join(texts)
    assert "ObjUser" in blob


@pytest.mark.asyncio
async def test_on_pick_empty_values(monkeypatch):
    t = _tournament()
    import storage.json_store as js
    monkeypatch.setattr(js, "store", SimpleNamespace(get=lambda g, c: t))
    monkeypatch.setattr(sv, "store", js.store)

    interaction = _interaction()
    view = sv.AdminAddUserView(GID, CID, 4)
    select = _find(view, discord.ui.UserSelect)
    select._values = []
    await select.callback(interaction)
    interaction.response.send_message.assert_awaited()
