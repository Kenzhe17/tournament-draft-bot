"""Тесты Components V2 профиля Free Fire (/ff info).

Покрывают: 4 вкладки, 5 кнопок (включая «✕» — удаление сообщения),
отсутствие повторных API-запросов при переключении, права доступа,
отсутствующие поля, неизвестные ранги/предметы, экранирование
markdown/mentions, timeout View и лимиты Components V2.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

from utils import ff_profile_formatter as fmt
from views.freefire_profile_view import FreeFireProfileView

# ----------------------------------------------------------------- fixtures

SAMPLE = {
    "basicInfo": {
        "accountId": "535606728",
        "nickname": "uNøtaR!z3",
        "level": 71,
        "exp": 3321207,
        "liked": 25441,
        "badgeCnt": 3,
        "region": "RU",
        "seasonId": 51,
        "releaseVersion": "OB53",
        "createAt": "1606659627",
        "lastLoginAt": "1777636197",
        "rank": 313,
        "maxRank": 321,
        "rankingPoints": 1530,
        "csRank": 306,
        "csMaxRank": 324,
        "csRankingPoints": 48,
        "primeInfo": {"primeLevel": 2},
    },
    "socialInfo": {
        "signature": "pro player @everyone <@123> **not bold**",
        "language": "Language_ru",
        "modePrefer": "ModePrefer_BR",
    },
    "profileInfo": {
        "avatarId": 102000022,
        "clothes": [
            "205000051",   # Shoe
            "211000579",   # Mask
            "214000022",   # Facepaint
            "211001035",   # Head
            "203001159",   # Top
            "204000581",   # Bottom
            "999999999",   # неизвестный предмет
        ],
    },
    "petInfo": {"id": "1300000071", "level": 7, "exp": 6000},
    "clanBasicInfo": {
        "clanName": "TestClan",
        "memberNum": 32,
        "capacity": 45,
        "clanLevel": 3,
    },
    "creditScoreInfo": {"creditScore": 100},
    "diamondCostRes": {"diamondCost": 390},
}

OWNER_ID = 42


def _make_view(data=None, **kwargs):
    return FreeFireProfileView(
        uid="535606728",
        region="ru",
        owner_id=OWNER_ID,
        data=SAMPLE if data is None else data,
        timeout=None,
        **kwargs,
    )


def _interaction(user_id=OWNER_ID, custom_id="ffp:overview"):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        data={"custom_id": custom_id},
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
        followup=SimpleNamespace(
            send=AsyncMock(), edit_message=AsyncMock()
        ),
    )


def _buttons(view):
    return [
        c
        for c in view.walk_children()
        if isinstance(c, discord.ui.Button)
    ]


def _press(view, custom_id, user_id=OWNER_ID):
    for btn in _buttons(view):
        if btn.custom_id == custom_id:
            interaction = _interaction(user_id, custom_id)
            return btn, interaction, btn.callback(interaction)
    raise AssertionError(f"button {custom_id} not found")


def _container(view):
    for child in view.children:
        if isinstance(child, discord.ui.Container):
            return child
    raise AssertionError("no container")


def _all_text(view):
    texts = []
    for item in _container(view).walk_children():
        if isinstance(item, discord.ui.TextDisplay):
            texts.append(item.content)
    return "\n".join(texts)


def _component_count(view):
    """Полный счёт компонентов (лимит Discord — 40)."""
    total = 0

    def walk(item):
        nonlocal total
        total += 1
        for ch in getattr(item, "children", []) or []:
            walk(ch)

    for ch in view.children:
        walk(ch)
    return total


# ------------------------------------------------------------ formatter


def test_safe_text_escapes_markdown_and_mentions():
    out = fmt.safe_text("@everyone <@123> **bold** `code`", limit=None)
    assert "@everyone" not in out
    assert "<@123>" not in out
    assert "\\*\\*bold\\*\\*" in out


def test_safe_text_truncates():
    out = fmt.safe_text("x" * 200, limit=50)
    assert len(out) <= 51
    assert out.endswith("…")


def test_fmt_num():
    assert fmt.fmt_num(3321207) == "3 321 207"
    assert fmt.fmt_num(None) == "—"
    assert fmt.fmt_num("abc") == "abc"


def test_fmt_ts():
    assert fmt.fmt_ts("1606659627") == "<t:1606659627:F>"
    assert fmt.fmt_ts(None) == "—"
    assert fmt.fmt_ts("bad") == "—"


def test_br_rank_boundaries():
    assert fmt.br_rank(0)["name"] == "Бронза I–III"
    assert fmt.br_rank(1530)["name"] == "Серебро III"
    assert fmt.br_rank(3990)["name"] in ("Героик", "Элитный Героик")
    assert fmt.br_rank(10268)["name"] == "Грандмастер"
    assert fmt.br_rank(None)["name"] is None
    badge = fmt.br_rank(1530)["badge"]
    assert badge and badge.endswith(".png")


def test_cs_rank_boundaries():
    assert fmt.cs_rank(0)["name"] == "Бронза I"
    assert fmt.cs_rank(48)["name"] == "Платина III"
    assert fmt.cs_rank(900)["name"] == "Грандмастер VI"
    assert fmt.cs_rank(None)["name"] is None


def test_rank_label_fallback_id():
    assert "ID 313" in fmt.rank_label(313, fmt.br_rank(1530))
    assert fmt.rank_label(None, None) == "—"
    assert fmt.rank_label("abc", {"name": "X"}) == "X"


def test_group_clothes_categories_and_unknown():
    groups = fmt.group_clothes(SAMPLE["profileInfo"])
    labels = [label for label, _ in groups]
    assert "Головной убор" in labels
    assert "Маска" in labels
    assert "Верхняя одежда" in labels
    flat = [e for _, entries in groups for e in entries]
    unknown = [e for e in flat if e["id"] == "999999999"]
    assert unknown and unknown[0]["known"] is False
    assert unknown[0]["icon"] is None


def test_pet_entry():
    pet = fmt.pet_entry(SAMPLE["petInfo"])
    assert pet["name"] == "Poring"
    assert pet["icon"]
    assert fmt.pet_entry({}) is None


# ------------------------------------------------------------ view: вкладки


@pytest.mark.asyncio
async def test_overview_renders_key_data_and_buttons():
    view = _make_view()
    text = _all_text(view)
    assert "uNøtaR!z3" in text
    assert "535606728" in text
    assert "Ур. 71" in text
    assert "CLASH SQUAD" in text
    assert "BATTLE ROYALE" in text
    assert "3 321 207" in text
    assert "25 441" in text
    buttons = _buttons(view)
    assert len(buttons) == 5
    labels = {b.label for b in buttons}
    assert labels == {"Обзор", "Ранги", "Экипировка", "Аккаунт", "✕"}
    # активная вкладка выделена
    active = [b for b in buttons if b.style == discord.ButtonStyle.primary]
    assert len(active) == 1 and active[0].label == "Обзор"


@pytest.mark.asyncio
async def test_tab_switch_edits_same_view():
    view = _make_view()
    for tab in ("ranks", "gear", "account", "overview"):
        _, interaction, coro = _press(view, f"ffp:{tab}")
        await coro
        interaction.response.edit_message.assert_awaited_once()
        sent = interaction.response.edit_message.await_args.kwargs["view"]
        assert sent is view
        assert view.tab == tab
        interaction.response.edit_message.reset_mock()


@pytest.mark.asyncio
async def test_all_tabs_within_component_limits():
    view = _make_view()
    for tab, _ in [("overview", 0), ("ranks", 0), ("gear", 0), ("account", 0)]:
        view.tab = tab
        view._render()
        container = _container(view)
        assert len(container.children) <= 10, f"{tab}: {len(container.children)}"
        assert _component_count(view) <= 40, f"{tab}: {_component_count(view)}"
        for item in container.walk_children():
            if isinstance(item, discord.ui.TextDisplay):
                assert len(item.content) <= 2000


@pytest.mark.asyncio
async def test_gear_many_items_stays_within_limits():
    data = {
        "basicInfo": {"nickname": "n"},
        "profileInfo": {"clothes": [str(300000000 + i) for i in range(30)]},
        "petInfo": {"id": "1300000071", "level": 1, "exp": 10},
    }
    view = _make_view(data=data)
    view.tab = "gear"
    view._render()
    container = _container(view)
    assert len(container.children) <= 10
    assert _component_count(view) <= 40


@pytest.mark.asyncio
async def test_missing_fields_do_not_crash():
    view = _make_view(data={"basicInfo": {"nickname": "x"}})
    for tab, _ in [("overview", 0), ("ranks", 0), ("gear", 0), ("account", 0)]:
        view.tab = tab
        view._render()
        assert _container(view).children


@pytest.mark.asyncio
async def test_unknown_rank_and_items_fallback():
    data = {
        "basicInfo": {"nickname": "n", "rank": 999999, "csRank": None},
        "profileInfo": {"clothes": ["111", "222"]},
    }
    view = _make_view(data=data)
    view.tab = "ranks"
    view._render()
    text = _all_text(view)
    assert "ID 999999" in text or "999999" in text
    view.tab = "gear"
    view._render()
    text = _all_text(view)
    assert "ID 111" in text and "ID 222" in text


@pytest.mark.asyncio
async def test_long_nickname_and_signature_safety():
    data = {
        "basicInfo": {"nickname": "A" * 300, "level": 1},
        "socialInfo": {"signature": "@everyone <#123> " + "s" * 300},
    }
    view = _make_view(data=data)
    text = _all_text(view)
    assert "@everyone" not in text
    assert "<#123>" not in text
    for item in _container(view).walk_children():
        if isinstance(item, discord.ui.TextDisplay):
            assert len(item.content) <= 2000


# ------------------------------------------------------------ view: close


@pytest.mark.asyncio
async def test_close_deletes_message():
    view = _make_view()
    message = SimpleNamespace(delete=AsyncMock())
    view.bound_message = message
    _, _, coro = _press(view, "ffp:close")
    await coro
    message.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_without_bound_message_uses_interaction():
    view = _make_view()
    _, interaction, coro = _press(view, "ffp:close")
    await coro
    interaction.delete_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_failure_falls_back_to_ephemeral():
    view = _make_view()
    message = SimpleNamespace(delete=AsyncMock(side_effect=RuntimeError("x")))
    view.bound_message = message
    _, interaction, coro = _press(view, "ffp:close")
    await coro
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs.get(
        "ephemeral"
    )


# ------------------------------------------------------------ view: доступ


@pytest.mark.asyncio
async def test_non_owner_denied_everywhere():
    view = _make_view()
    for cid in ("ffp:overview", "ffp:ranks", "ffp:gear", "ffp:account",
                "ffp:close"):
        _, interaction, coro = _press(view, cid, user_id=999)
        await coro
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs.get(
            "ephemeral"
        )
        interaction.response.send_message.reset_mock()
        interaction.response.edit_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_view_disables_buttons():
    view = _make_view(expired=True)
    for btn in _buttons(view):
        assert btn.disabled is True
    _, interaction, coro = _press(view, "ffp:overview")
    await coro
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_on_timeout_edits_message_with_expired_view():
    view = _make_view()
    message = SimpleNamespace(edit=AsyncMock())
    view.bound_message = message
    await view.on_timeout()
    message.edit.assert_awaited_once()
    sent = message.edit.await_args.kwargs["view"]
    assert isinstance(sent, FreeFireProfileView)
    assert sent._expired is True
    for btn in _buttons(sent):
        assert btn.disabled is True
