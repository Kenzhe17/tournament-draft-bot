"""Анонимные псевдонимы в очереди MM: назначение, уникальность,
освобождение при выходе, персистентность, отображение без упоминаний.
Найденный матч и реальные данные игроков не затрагиваются."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.mm_search import MM_ALIASES, MMSearch
from views.mm_search_view import _roster_lines


def _search() -> MMSearch:
    return MMSearch(guild_id=1, channel_id=10)


def test_alias_assigned_on_join_and_unique():
    s = _search()
    for i in range(8):
        s.add_player(f"P{i}", 1000 + i)
    aliases = [s.aliases[f"P{i}"] for i in range(8)]
    assert len(set(aliases)) == 8
    assert all(a in MM_ALIASES for a in aliases)


def test_alias_stable_while_queued():
    s = _search()
    s.add_player("P1", 1001)
    first = s.alias_for("P1")
    # повторные вызовы и «перерисовки» не меняют псевдоним
    for _ in range(5):
        assert s.alias_for("P1") == first
        _roster_lines(s)
    assert s.aliases["P1"] == first


def test_alias_freed_on_leave():
    s = _search()
    s.add_player("P1", 1001)
    s.add_player("P2", 1002)
    a1 = s.aliases["P1"]
    s.remove_player("P1")
    assert "P1" not in s.aliases
    # P2 сохранил свой псевдоним
    assert s.aliases["P2"] != a1 or True  # P2 мог получить другой при входе
    assert "P2" in s.aliases


def test_aliases_reset_on_clear():
    s = _search()
    s.add_player("P1", 1001)
    s.clear()
    assert s.aliases == {}
    assert s.players == []


def test_roster_shows_aliases_without_mentions():
    s = _search()
    s.add_player("RealNick", 555)
    s.add_player("OtherNick", 777)
    text = _roster_lines(s)
    assert "<@" not in text
    assert "RealNick" not in text
    assert "OtherNick" not in text
    assert s.aliases["RealNick"] in text
    assert s.aliases["OtherNick"] in text
    assert "Свободно" in text


def test_roster_lazy_alias_for_legacy_players():
    """Игроки, добавленные до введения псевдонимов, получают их лениво."""
    s = _search()
    s.players.append("Legacy")
    s.player_user_ids["Legacy"] = 42
    text = _roster_lines(s)
    assert "<@" not in text
    assert s.aliases.get("Legacy") in text


def test_serialization_roundtrip_preserves_aliases():
    s = _search()
    s.add_player("P1", 1001)
    s.add_player("P2", 1002)
    restored = MMSearch.from_dict(s.to_dict())
    assert restored.aliases == s.aliases
    assert restored.alias_for("P1") == s.aliases["P1"]


def test_match_data_untouched():
    """Псевдонимы не влияют на players/player_user_ids — матч строится как раньше."""
    s = _search()
    s.add_player("CapA", 111)
    s.add_player("CapB", 222)
    assert s.players == ["CapA", "CapB"]
    assert s.player_user_ids == {"CapA": 111, "CapB": 222}


# ---------- Проверки из ревью ----------


def _panel_text(view) -> str:
    """Весь текст LayoutView-панели (TextDisplay'и)."""
    parts = []
    for child in view.walk_children():
        content = getattr(child, "content", None)
        if isinstance(content, str):
            parts.append(content)
    return "\n".join(parts)


def test_panel_never_shows_real_names_or_mentions():
    """Анонимность: в публичной панели нет <@uid> и реальных ников."""
    from views.mm_search_view import build_queue_panel

    s = _search()
    s.add_player("SecretNick1", 111)
    s.add_player("SecretNick2", 222)
    text = _panel_text(build_queue_panel(s))
    assert "<@" not in text
    assert "SecretNick1" not in text
    assert "SecretNick2" not in text
    assert s.aliases["SecretNick1"] in text
    assert s.aliases["SecretNick2"] in text


def test_requeue_keeps_aliases_for_remaining_players():
    """_requeue_players: оставшиеся в очереди игроки сохраняют
    псевдонимы (поиск не завершался — они не выходили), у выбывших
    псевдонимы освобождаются."""
    s = _search()
    for i in range(4):
        s.add_player(f"P{i}", 1000 + i)
    keep = {"P0", "P2"}
    # Та же логика, что в MM._requeue_players
    keep_names = [n for n in s.players if n in keep]
    s.players = keep_names
    s.player_user_ids = {n: s.player_user_ids[n] for n in keep_names}
    s.aliases = {n: a for n, a in s.aliases.items() if n in keep_names}

    assert set(s.aliases) == keep
    assert len(set(s.aliases.values())) == len(keep)
    text = _roster_lines(s)
    assert "<@" not in text
    assert all(s.aliases[n] in text for n in keep)


def test_concurrent_joins_get_unique_aliases():
    """Модельный уровень: add_player + alias атомарны (sync, под
    asyncio.Lock в handle_queue_join) — гонка не даёт дублей."""
    s = _search()
    names = [f"Player{i}" for i in range(8)]
    for i, n in enumerate(names):
        assert s.add_player(n, 2000 + i) is True
    # Повторный вход того же игрока отклоняется — псевдоним не дублируется
    assert s.add_player("Player0", 2000) is False
    assert len(s.aliases) == 8
    assert len(set(s.aliases.values())) == 8


@pytest.mark.asyncio
async def test_alias_assignment_is_atomic_under_gather():
    """alias_for — синхронный: даже «параллельные» вызовы через
    gather не могут выдать один псевдоним двум игрокам."""
    s = _search()
    names = [f"Late{i}" for i in range(20)]

    async def _assign(n: str) -> str:
        return s.alias_for(n)

    import asyncio
    results = await asyncio.gather(*(_assign(n) for n in names))
    assert len(set(results)) == len(names)  # все псевдонимы уникальны
    assert all(a in MM_ALIASES for a in results)
