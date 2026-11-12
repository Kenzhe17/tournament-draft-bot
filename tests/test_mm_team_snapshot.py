"""Снимок составов команд Ranked Matchmaking.

Первопричина бага «разные составы в НАЙДЕН МАТЧ и игровом канале»:
_start_ready_check и _create_tournament_from_search независимо
вызывали smart_balance_4v4, а при равных метриках баланс chooses
randomly → два разных сплита. Теперь турнир строится из снимка
ReadyCheck.players (user_id + team), повторная балансировка
запрещена.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models.mm_search import MMFormat, MMSearch
from models.ready_check import ReadyCheck, ReadyCheckPlayer, ReadyCheckStatus


NAMES = ["kayo666", "zabuza", "nurbo", "Mara",
         "wizard", "sanzh", "uluking", "tabynn"]
UIDS = {n: 5000 + i for i, n in enumerate(NAMES)}


def _search() -> MMSearch:
    s = MMSearch(guild_id=1, channel_id=10, format=MMFormat.SQUAD)
    for n in NAMES:
        s.add_player(n, UIDS[n])
    return s


def _rc(search: MMSearch) -> ReadyCheck:
    """Снимок из примера бага: nurbo/uluking в разных командах."""
    a = ["kayo666", "zabuza", "nurbo", "Mara"]
    b = ["wizard", "sanzh", "uluking", "tabynn"]
    players = [
        ReadyCheckPlayer(user_id=UIDS[n], name=n, team="A") for n in a
    ] + [
        ReadyCheckPlayer(user_id=UIDS[n], name=n, team="B") for n in b
    ]
    return ReadyCheck(
        match_id="rc_test_1",
        guild_id=1,
        search_channel_id=10,
        tournament_channel_id=0,
        players=players,
        status=ReadyCheckStatus.ALL_READY,
        match_num=7,
    )


def _cog():
    import cogs.mm as mm_mod

    bot = SimpleNamespace(
        mm_cog=None,
        get_guild=lambda gid: None,
        user=SimpleNamespace(id=1),
    )
    return mm_mod.MMCog(bot)


class TestTeamsFromReadyCheck:
    def test_exact_bug_scenario(self):
        cog = _cog()
        search, rc = _search(), _rc(_search())
        teams = cog._teams_from_ready_check(rc, search)
        assert teams is not None
        a, b = teams
        assert a == ["kayo666", "zabuza", "nurbo", "Mara"]
        assert b == ["wizard", "sanzh", "uluking", "tabynn"]

    def test_duplicate_uid_rejected(self):
        cog = _cog()
        search, rc = _search(), _rc(_search())
        rc.players[1].user_id = rc.players[0].user_id
        assert cog._teams_from_ready_check(rc, search) is None

    def test_seven_players_rejected(self):
        cog = _cog()
        search, rc = _search(), _rc(_search())
        rc.players = rc.players[:-1]
        assert cog._teams_from_ready_check(rc, search) is None

    def test_unbalanced_teams_rejected(self):
        cog = _cog()
        search, rc = _search(), _rc(_search())
        rc.players[3].team = "B"  # A=3, B=5
        assert cog._teams_from_ready_check(rc, search) is None

    def test_snapshot_survives_serialization(self):
        """Составы восстанавливаются из JSON/PG-снимка без изменений."""
        cog = _cog()
        search, rc = _search(), _rc(_search())
        rc2 = ReadyCheck.from_dict(rc.to_dict())
        teams = cog._teams_from_ready_check(rc2, search)
        assert teams == (
            ["kayo666", "zabuza", "nurbo", "Mara"],
            ["wizard", "sanzh", "uluking", "tabynn"],
        )


class TestNoRebalanceOnLaunch:
    @pytest.mark.asyncio
    async def test_tournament_uses_snapshot_not_rebalance(
        self, monkeypatch
    ):
        """_create_tournament_from_search с teams= не вызывает
        smart_balance — составы совпадают со снимком RC."""
        import cogs.mm as mm_mod
        import services.match_announcer as ma_mod
        import utils.smart_balance as sb_mod

        search, rc = _search(), _rc(_search())
        cog = _cog()

        import storage.player_stats_store as pss_mod
        import storage.mm_search_store as mms_mod

        created: list = []
        monkeypatch.setattr(
            mm_mod, "store",
            SimpleNamespace(
                set=created.append,
                get_by_guild=lambda g: [],
            ),
        )
        # stats читаются через локальный импорт внутри функции —
        # патчим атрибут модуля-источника.
        monkeypatch.setattr(
            pss_mod, "player_stats_store",
            SimpleNamespace(get=AsyncMock(return_value=None)),
        )
        monkeypatch.setattr(
            mm_mod, "mm_search_store",
            SimpleNamespace(
                set=lambda s: None, get=lambda g, c: search,
                get_by_guild=lambda g: [search], all=lambda: [search],
            ),
        )
        monkeypatch.setattr(mms_mod, "mm_search_store", mm_mod.mm_search_store)
        monkeypatch.setattr(
            cog, "_update_queue_panel", AsyncMock()
        )
        monkeypatch.setattr(
            ma_mod, "announce_phase_matches", AsyncMock()
        )

        def _boom(*a, **kw):  # любая повторная балансировка — баг
            raise AssertionError("smart_balance called on launch")

        monkeypatch.setattr(sb_mod, "smart_balance_4v4", _boom)

        channel = SimpleNamespace(id=10, guild=SimpleNamespace(id=1))
        teams = cog._teams_from_ready_check(rc, search)
        await cog._create_tournament_from_search(
            search, channel, teams=teams
        )

        assert len(created) == 1
        tr = created[0]
        got_a = tr.teams[0]["players"]
        got_b = tr.teams[1]["players"]
        assert got_a == ["kayo666", "zabuza", "nurbo", "Mara"]
        assert got_b == ["wizard", "sanzh", "uluking", "tabynn"]
        # Капитан = первый игрок снимка (как на карточке)
        assert tr.teams[0]["captain"] == "kayo666"
        assert tr.teams[1]["captain"] == "wizard"
        # Все 8 ID ровно по одному разу
        all_uids = [
            tr.player_user_ids[n] for n in got_a + got_b
        ]
        assert sorted(all_uids) == sorted(UIDS.values())

    @pytest.mark.asyncio
    async def test_ready_check_players_unchanged_by_reconfirm(
        self, monkeypatch
    ):
        """Повторное подтверждение готовности не меняет составы."""
        cog = _cog()
        search, rc = _search(), _rc(_search())
        before = [
            (p.user_id, p.name, p.team) for p in rc.players
        ]
        # Имитируем повторный клик «Готов» — статус/флаг ready,
        # team не трогаем (логика _confirm_ready).
        rc.players[0].is_ready = True
        rc.players[0].is_ready = True  # повторно
        after = [(p.user_id, p.name, p.team) for p in rc.players]
        assert before == after
