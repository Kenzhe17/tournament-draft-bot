"""Регрессия: очередь «БЕЗ РУИН» полностью удалена.

Проверяем, что в активном коде не осталось queue_type/no_ruin,
панель очереди — единственная (join/leave), а модели не знают
о втором режиме. Хранилище остаётся совместимым со старой
PostgreSQL-схемой (queue_type='normal' пишется константой).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]

# Файлы, где режим no_ruin НЕ должен встречаться в активном коде
NO_RUIN_FREE_FILES = [
    "cogs/mm.py",
    "views/mm_search_view.py",
    "models/mm_search.py",
    "models/ready_check.py",
    "models/tournament.py",
    "models/match_history.py",
    "services/match_announcer.py",
    "views/history_view.py",
    "views/matches_view.py",
    "views/match_stats_view.py",
]


def test_no_ruin_removed_from_active_code():
    for rel in NO_RUIN_FREE_FILES:
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "no_ruin" not in src, f"{rel} still references no_ruin"
        assert "БЕЗ РУИН" not in src, f"{rel} still mentions БЕЗ РУИН"
        assert "queue_type" not in src, f"{rel} still references queue_type"


def test_mm_search_model_has_no_queue_type():
    from models.mm_search import MMSearch

    assert not hasattr(MMSearch(guild_id=1, channel_id=2), "queue_type")


def test_ready_check_model_has_no_queue_type():
    from models.ready_check import ReadyCheck

    rc = ReadyCheck(
        match_id="m1", guild_id=1, search_channel_id=2,
        tournament_channel_id=3, players=[],
    )
    assert not hasattr(rc, "queue_type")


def test_queue_panel_single_mode():
    """Панель очереди: кнопки входа/выхода, без второго режима."""
    import discord

    from models.mm_search import MMFormat, MMSearch
    from views.mm_search_view import build_queue_panel

    search = MMSearch(
        guild_id=1, channel_id=2, message_id=0,
        format=MMFormat.SQUAD, players=["P1", "P2"],
        player_user_ids={"P1": 11, "P2": 22},
    )
    view = build_queue_panel(search)

    labels = []
    rows = []
    for child in view.container.children:
        if isinstance(child, discord.ui.ActionRow):
            rows.append(child)
            labels += [getattr(i, "label", "") for i in child.children]

    assert len(rows) == 1
    assert any("Войти" in l for l in labels)
    assert any("Покинуть" in l for l in labels)
    assert not any("РУИН" in l or "SR" in l for l in labels)

    # Весь текст карточки — без упоминаний второго режима
    texts = []
    for child in view.container.walk_children():
        content = getattr(child, "content", None)
        if content:
            texts.append(str(content))
    blob = "\n".join(texts)
    assert "РУИН" not in blob
    assert "no_ruin" not in blob


def test_store_persists_normal_queue_type():
    """PG-persist пишет queue_type='normal' — совместимость со схемой."""
    import inspect

    from storage import mm_search_store as mss

    src = inspect.getsource(mss.MMSearchStore._persist_pg_one)
    assert "queue_type" in src
    assert "'normal'" in src
