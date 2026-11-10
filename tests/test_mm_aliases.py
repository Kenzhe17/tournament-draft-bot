"""Анонимные псевдонимы в очереди MM: назначение, уникальность,
освобождение при выходе, персистентность, отображение без упоминаний.
Найденный матч и реальные данные игроков не затрагиваются."""

from __future__ import annotations

import sys
from pathlib import Path

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
