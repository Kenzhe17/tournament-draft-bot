"""Регрессии CV2-профиля игрока (/player profile).

- Нативный LayoutView без PNG/Embed.
- Заголовок «ПРОФИЛЬ — имя» без декоративных эмодзи.
- Настоящий аватар через Section + Thumbnail (display_avatar.url).
- Полная статистика всегда, включая 0 матчей (без заглушки).
- Никаких ping-упоминаний: текст экранируется, имя без <@id>.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.player_stats import PlayerStats
from services import profile_card
from storage.match_history_store import match_history_store
from storage.player_stats_store import player_stats_store
from storage.shop_store import inventory_store
from storage.user_balance_store import user_balance_store
from views.profile_view import build_profile_layout


# ---------------------------------------------------------------- helpers


def _guild(spouse_member=None):
    members = {}
    if spouse_member is not None:
        members[spouse_member.id] = spouse_member
    return types.SimpleNamespace(
        id=1,
        get_member=lambda uid: members.get(uid),
        fetch_member=AsyncMock(return_value=None),
    )


def _user(user_id=42, name="Player", avatar_url="https://cdn.discordapp.com/avatars/42/a.png"):
    avatar = types.SimpleNamespace(url=avatar_url) if avatar_url else None
    return types.SimpleNamespace(
        id=user_id,
        display_name=name,
        display_avatar=avatar,
        mention=f"<@{user_id}>",
    )


def _stub_stores(monkeypatch, stats, *, balance=0, items=()):
    monkeypatch.setattr(
        player_stats_store, "get", AsyncMock(return_value=stats)
    )
    monkeypatch.setattr(
        player_stats_store, "update", AsyncMock()
    )
    monkeypatch.setattr(
        match_history_store,
        "get_user_history",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        user_balance_store, "get_balance", AsyncMock(return_value=balance)
    )
    monkeypatch.setattr(
        inventory_store,
        "get_player_inventory",
        lambda guild_id, user_id: list(items),
    )
    import ultimatum.elo as ue

    monkeypatch.setattr(
        ue.ult_elo_store, "get_rating", AsyncMock(return_value=None)
    )


def _all_texts(components) -> list[str]:
    """Все TextDisplay.content из to_components() рекурсивно."""
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == 10 and "content" in node:
                out.append(node["content"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(components)
    return out


def _find_section(components):
    """Section (type=9) с Thumbnail-аксессуаром, если есть."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == 9:
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(components)
    return found


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_zero_match_profile_shows_zeros_and_no_placeholder(monkeypatch):
    stats = PlayerStats(guild_id=1, user_id=42, name="Player", elo=1000)
    _stub_stores(monkeypatch, stats)

    view = await build_profile_layout(_guild(), _user())
    texts = _all_texts(view.to_components())
    joined = "\n".join(texts)

    assert "ПРОФИЛЬ — Player" in texts[0]
    assert "Не указано" in joined          # пустое описание
    assert "Ещё не играл" not in joined    # заглушка удалена
    assert "**Матчи:** 0" in joined
    assert "**Поражения:** 0" in joined
    assert "**Смерти:** 0" in joined
    assert "**Финалы:** 0" in joined
    for section in ("ПРОГРЕСС", "СТАТИСТИКА", "ЭКОНОМИКА"):
        assert section in joined


@pytest.mark.asyncio
async def test_full_stats_50_matches(monkeypatch):
    stats = PlayerStats(
        guild_id=1, user_id=42, name="Player", elo=1234,
        wins=30, finals=5, games=50,
        current_streak=3, best_win_streak=7, best_loss_streak=4,
        total_kills=300, total_deaths=200, best_match_kills=15,
        last_elo_change=-15, xp=120, level=4,
        reputation=5, birthday="01.01.1990", description="Люблю турики",
    )
    _stub_stores(monkeypatch, stats, balance=777, items=["sword"])

    view = await build_profile_layout(_guild(), _user())
    joined = "\n".join(_all_texts(view.to_components()))

    assert "**Поражения:** 20" in joined       # losses = games - wins
    assert "**Win Rate:** 60.0%" in joined
    assert "**Убийства:** 300" in joined
    assert "**Смерти:** 200" in joined
    assert "**K/D Ratio:** 1.5" in joined
    assert "**AVG Kills:** 6.0" in joined
    assert "**Max Kills:** 15" in joined
    assert "**Skill Rating:**" in joined
    assert "**Финалы:** 5" in joined
    assert "3 победы" in joined                # текущая серия
    assert "**Баланс:** 777" in joined
    assert "**Предметов:** 1" in joined
    assert "Люблю турики" in joined
    assert "01.01.1990" in joined
    assert "`−15`" in joined                   # отрицательная дельта ELO


@pytest.mark.asyncio
async def test_avatar_thumbnail_and_special_chars(monkeypatch):
    stats = PlayerStats(guild_id=1, user_id=42, name="x", elo=1000)
    _stub_stores(monkeypatch, stats)

    user = _user(name="Kenzhe*_Test<@42>", avatar_url="https://cdn/x.png")
    view = await build_profile_layout(_guild(), user)
    comps = view.to_components()

    sections = _find_section(comps)
    assert len(sections) == 1
    accessory = sections[0]["accessory"]
    assert accessory["type"] == 11             # Thumbnail
    assert accessory["media"]["url"] == "https://cdn/x.png"

    joined = "\n".join(_all_texts(comps))
    # имя экранировано в заголовке и теле, mention разметка погашена
    assert "Kenzhe\\*\\_Test" in joined
    assert "\\<@42\\>" in joined
    assert not re.search(r"(?<!\\)<@", joined)


@pytest.mark.asyncio
async def test_avatar_fallback_without_custom_avatar(monkeypatch):
    stats = PlayerStats(guild_id=1, user_id=42, name="Player", elo=1000)
    _stub_stores(monkeypatch, stats)

    # display_avatar=None → текст без Thumbnail, профиль не падает
    view = await build_profile_layout(_guild(), _user(avatar_url=None))
    comps = view.to_components()
    assert _find_section(comps) == []
    assert "**Player**" in "\n".join(_all_texts(comps))

    # display_avatar с default-url (как у discord.py) → Thumbnail есть
    default_url = "https://cdn.discordapp.com/embed/avatars/0.png"
    view2 = await build_profile_layout(
        _guild(), _user(avatar_url=default_url)
    )
    sections = _find_section(view2.to_components())
    assert sections[0]["accessory"]["media"]["url"] == default_url


@pytest.mark.asyncio
async def test_no_pings_anywhere(monkeypatch):
    stats = PlayerStats(
        guild_id=1, user_id=42, name="P", elo=1000, spouse_id=7,
        description="текст с <@1> и <#2>",
    )
    spouse = types.SimpleNamespace(id=7, display_name="Spouse<@9>")
    _stub_stores(monkeypatch, stats)

    guild = _guild(spouse_member=spouse)
    view = await build_profile_layout(guild, _user(name="P<@42>"))
    joined = "\n".join(_all_texts(view.to_components()))

    # неэкранированных mention-паттернов быть не должно
    assert not re.search(r"(?<!\\)<@", joined)
    assert not re.search(r"(?<!\\)<#", joined)
    # супруг показан именем, без mention-разметки
    assert "Spouse\\<@9\\>" in joined


@pytest.mark.asyncio
async def test_other_user_profile(monkeypatch):
    """Профиль другого участника использует его данные и ник."""
    stats = PlayerStats(
        guild_id=1, user_id=99, name="Other", elo=1500,
        wins=10, games=10, total_kills=50, total_deaths=40,
    )
    _stub_stores(monkeypatch, stats)

    other = _user(user_id=99, name="OtherPlayer")
    view = await build_profile_layout(_guild(), other)
    joined = "\n".join(_all_texts(view.to_components()))

    assert "ПРОФИЛЬ — OtherPlayer" in joined
    assert "**ELO:** 1500" in joined


@pytest.mark.asyncio
async def test_stats_survive_restart_roundtrip():
    """Данные профиля сериализуются/десериализуются без потерь."""
    stats = PlayerStats(
        guild_id=1, user_id=42, name="P", elo=1300, wins=3, games=5,
        current_streak=-2, best_win_streak=4, best_loss_streak=2,
        total_kills=40, total_deaths=30, best_match_kills=12,
        last_elo_change=25, xp=90, level=3, description="bio",
        reputation=2, birthday="02.02.1990", spouse_id=7,
    )
    restored = PlayerStats.from_dict(stats.to_dict())
    assert restored.wins == 3 and restored.games == 5
    assert restored.current_streak == -2
    assert restored.best_loss_streak == 2
    assert restored.description == "bio"
    assert restored.spouse_id == 7
    assert restored.xp == 90 and restored.level == 3


def test_collect_profile_data_keys(monkeypatch):
    """collect_profile_data отдаёт все поля, нужные виду."""
    import asyncio

    stats = PlayerStats(guild_id=1, user_id=42, name="P", elo=1000)
    _stub_stores(monkeypatch, stats)

    data = asyncio.run(
        profile_card.collect_profile_data(_guild(), _user())
    )
    for key in (
        "name", "description", "reputation", "partner", "birthday",
        "level", "rank_name", "xp", "xp_to_next_level", "elo",
        "last_elo_change", "skill_rating", "matches", "wins",
        "losses", "winrate", "kills", "deaths", "kd", "avg_kills",
        "max_kills", "finals", "current_streak", "best_win_streak",
        "best_loss_streak", "balance", "items",
    ):
        assert key in data, key
