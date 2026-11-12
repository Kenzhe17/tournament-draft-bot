"""Тесты V2-сообщений социальных функций."""

import types
from datetime import UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from services.social.birthday_congrats import build_congrats_view
from services.social.birthdays import (
    _birthday_label,
    build_birthdays_view,
)
from services.social.marriages import build_marriages_view
from services.social.proposal import build_proposal_view


def _member(uid: int, name: str):
    return types.SimpleNamespace(
        id=uid, mention=f"<@{uid}>", display_name=name,
        guild=types.SimpleNamespace(id=1),
    )


def _components(view) -> list:
    return view.to_components()[0]["components"]


def test_birthdays_empty():
    view = build_birthdays_view([])
    texts = [c.get("content", "") for c in _components(view) if c["type"] == 10]
    assert any("нет сохранённых дней рождения" in t for t in texts)


def test_birthdays_today_and_upcoming():
    entries = [
        {"user_id": 1, "name": "A", "birthday": "09.11.2001",
         "days": 0, "mention": "<@1>"},
        {"user_id": 2, "name": "B", "birthday": "12.11.2000",
         "days": 3, "mention": "<@2>"},
        {"user_id": 3, "name": "LeftUser", "birthday": "24.12.1999",
         "days": 45, "mention": None},
    ]
    view = build_birthdays_view(entries)
    texts = [c.get("content", "") for c in _components(view) if c["type"] == 10]
    joined = "\n".join(texts)
    assert "**СЕГОДНЯ**" in joined
    assert "<@1> — 9 ноября · сегодня" in joined
    assert "<@2> — 12 ноября · 3 дн." in joined
    assert "LeftUser — 24 декабря · 45 дн." in joined  # без mention для вышедшего
    assert "**БЛИЖАЙШИЕ**" in joined


def test_birthdays_label_no_year():
    assert _birthday_label("09.11.2001", 5) == "9 ноября · 5 дн."
    assert _birthday_label("29.02.2000", 0) == "29 февраля · сегодня"
    assert _birthday_label("29.02.2000", 1) == "29 февраля · завтра"


def test_proposal_pending_has_buttons():
    view = build_proposal_view(_member(1, "A"), _member(2, "B"), guild_id=1)
    comps = _components(view)
    rows = [c for c in comps if c["type"] == 1]
    assert len(rows) == 1
    labels = [b["label"] for b in rows[0]["components"]]
    assert labels == ["Принять", "Отклонить"]
    assert all(not b["disabled"] for b in rows[0]["components"])
    assert any("attachment://proposal.png" in str(c) for c in comps)


def test_proposal_final_state_disables_buttons():
    view = build_proposal_view(
        _member(1, "A"), _member(2, "B"), state="accepted", guild_id=1)
    comps = _components(view)
    rows = [c for c in comps if c["type"] == 1]
    assert all(b["disabled"] for b in rows[0]["components"])
    texts = "\n".join(c.get("content", "") for c in comps if c["type"] == 10)
    assert "Предложение принято" in texts


def test_congrats_view():
    view = build_congrats_view(_member(7, "Именинник"))
    comps = _components(view)
    texts = "\n".join(c.get("content", "") for c in comps if c["type"] == 10)
    assert "С днём рождения, <@7>!" in texts
    assert any("attachment://birthday_congrats.png" in str(c) for c in comps)


def test_marriages_empty():
    view = build_marriages_view([], invoker_id=1, guild_id=1)
    texts = "\n".join(c.get("content", "") for c in _components(view) if c["type"] == 10)
    assert "нет действующих союзов" in texts
    assert any("attachment://marriages.png" in str(c) for c in _components(view))


def test_marriages_list_and_pagination():
    from datetime import datetime
    entries = [
        {"user_a": i, "user_b": i + 100,
         "married_at": datetime(2026, 11, 9, tzinfo=UTC)}
        for i in range(10)
    ]
    view = build_marriages_view(entries, invoker_id=1, guild_id=1)
    comps = _components(view)
    texts = "\n".join(c.get("content", "") for c in comps if c["type"] == 10)
    assert "Всего действующих браков: 10" in texts
    assert "стр. 1/2" in texts
    assert "01 · <@0> & <@100>" in texts
    # 5 на страницу — 6-й и далее на второй
    assert "<@5>" not in texts
    rows = [c for c in comps if c["type"] == 1]
    assert len(rows) == 1
    labels = [b["label"] for b in rows[0]["components"]]
    assert labels == ["Назад", "Далее"]
    disabled = [b["disabled"] for b in rows[0]["components"]]
    assert disabled == [True, False]


def _marriages_entries(n: int = 10):
    from datetime import datetime
    return [
        {"user_a": i, "user_b": i + 100,
         "married_at": datetime(2026, 11, 9, tzinfo=UTC)}
        for i in range(n)
    ]


def _find_nav_button(view, label: str):
    for child in view.walk_children():
        if isinstance(child, discord.ui.Button) and child.label == label:
            return child
    raise AssertionError(f"button {label!r} not found")


def _interaction(user_id: int):
    response = SimpleNamespace(
        edit_message=AsyncMock(),
        send_message=AsyncMock(),
        is_done=lambda: False,
    )
    return SimpleNamespace(user=SimpleNamespace(id=user_id), response=response)


@pytest.mark.asyncio
async def test_marriages_next_page_sends_banner_file():
    """Регрессия: edit страницы обязан пересылать discord.File баннера —
    иначе Discord отвечает 50035 «attachment not found»."""
    view = build_marriages_view(_marriages_entries(), invoker_id=1, guild_id=1)
    next_btn = _find_nav_button(view, "Далее")

    interaction = _interaction(1)
    await next_btn.callback(interaction)

    interaction.response.edit_message.assert_awaited_once()
    kwargs = interaction.response.edit_message.await_args.kwargs
    # Новый LayoutView с кнопками пагинации на месте
    new_view = kwargs["view"]
    assert isinstance(new_view, discord.ui.LayoutView)
    assert _find_nav_button(new_view, "Назад") is not None
    assert _find_nav_button(new_view, "Далее") is not None
    # И баннер переслан файлом, а не ссылкой на старые attachments
    files = kwargs["attachments"]
    assert len(files) == 1
    assert isinstance(files[0], discord.File)
    assert files[0].filename == "marriages.png"


@pytest.mark.asyncio
async def test_marriages_pages_and_bounds():
    """Стр.1 -> Далее -> стр.2 -> Назад -> стр.1; выход за границы
    клампится _build_view (page=max(0, min(page, total-1)))."""
    view = build_marriages_view(_marriages_entries(), invoker_id=1, guild_id=1)

    async def go(page: int):
        interaction = _interaction(1)
        await view._go(interaction, page)
        interaction.response.edit_message.assert_awaited_once()
        return interaction.response.edit_message.await_args.kwargs["view"]

    def page_label(v) -> str:
        texts = [
            c.content for c in v.walk_children()
            if isinstance(c, discord.ui.TextDisplay)
        ]
        return next(t for t in texts if "стр." in t)

    v2 = await go(1)
    assert "стр. 2/2" in page_label(v2)
    # На последней странице «Далее» disabled, «Назад» активна
    assert _find_nav_button(v2, "Далее").disabled is True
    assert _find_nav_button(v2, "Назад").disabled is False

    # Возврат на первую
    interaction = _interaction(1)
    await _find_nav_button(v2, "Назад").callback(interaction)
    v1 = interaction.response.edit_message.await_args.kwargs["view"]
    assert "стр. 1/2" in page_label(v1)
    assert _find_nav_button(v1, "Назад").disabled is True

    # За границами — кламп к валидной странице, без ошибок
    assert "стр. 2/2" in page_label(await go(99))
    assert "стр. 1/2" in page_label(await go(-5))


@pytest.mark.asyncio
async def test_marriages_foreign_user_cannot_page():
    view = build_marriages_view(_marriages_entries(), invoker_id=1, guild_id=1)
    next_btn = _find_nav_button(view, "Далее")

    interaction = _interaction(999)
    await next_btn.callback(interaction)

    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs.get("ephemeral") is True
    interaction.response.edit_message.assert_not_awaited()
