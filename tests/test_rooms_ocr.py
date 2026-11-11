"""Тесты OCR V2: мультикомнатные сессии, агрегация, парсер, счёт."""

import pytest


def _box(text, x0, y0, x1, y1):
    return {
        "text": text,
        "x0": float(x0), "y0": float(y0),
        "x1": float(x1), "y1": float(y1),
        "cx": (x0 + x1) / 2.0,
        "cy": (y0 + y1) / 2.0,
    }


def _panel_boxes(prefix, cx_kda, cx_name, cx_dmg, cys, skip_kda_at=None):
    """Ряды одной панели: имя | K/D/A | DMG."""
    out = []
    for i, cy in enumerate(cys):
        if skip_kda_at != i:
            out.append(_box(f"{prefix}{i}", cx_name - 180, cy - 12, cx_name, cy + 12))
            out.append(_box("5 / 2 / 1", cx_kda - 50, cy - 12, cx_kda + 50, cy + 12))
            out.append(_box("1234", cx_dmg, cy - 12, cx_dmg + 60, cy + 12))
        else:
            # Строка есть (ник), но K/D/A не распознан
            out.append(_box(f"{prefix}{i}", cx_name - 180, cy - 12, cx_name, cy + 12))
    return out


class TestCsParserRows:
    def test_missing_kda_row_is_padded(self):
        from services.cs_parser import parse_cs_results

        cys = [400, 500, 600, 700]
        boxes = (
            _panel_boxes("L", 400, 300, 480, cys, skip_kda_at=2)
            + _panel_boxes("R", 1700, 1550, 1800, cys)
        )
        parsed = parse_cs_results(boxes)
        assert parsed is not None
        team_a = parsed["team_a"]
        team_b = parsed["team_b"]
        assert len(team_a) == 4
        assert len(team_b) == 4
        missing = [p for p in team_a if p["kills"] is None]
        assert len(missing) == 1
        # Распознанные строки не пострадали
        assert sum(1 for p in team_a if p["kills"] == 5) == 3

    def test_parse_row_fields(self):
        from services.cs_parser import parse_row_fields

        boxes = [
            _box("FrozenX", 100, 0, 200, 20),
            _box("7 / 3 / 1", 300, 0, 380, 20),
            _box("3246", 450, 0, 500, 20),
        ]
        f = parse_row_fields(boxes)
        assert f["name"] == "FrozenX"
        assert f["kills"] == 7 and f["deaths"] == 3 and f["assists"] == 1
        assert f["dmg"] == 3246

    def test_parse_row_fields_partial(self):
        from services.cs_parser import parse_row_fields

        # Только K/D/A — имя и DMG не выдумываются
        boxes = [_box("2 / 0 / 4", 100, 0, 200, 20)]
        f = parse_row_fields(boxes)
        assert f["kills"] == 2
        assert "name" not in f
        assert "dmg" not in f


def _bound_row(name, ocr, k, d, a, dmg, uid):
    return {
        "ocr_name": ocr, "kills": k, "deaths": d, "assists": a,
        "dmg": dmg, "discord_name": name, "discord_id": uid,
        "discord_display": name, "source": "self",
    }


def _room(i, panel_a="left", panel_b="right", rows_a=None, rows_b=None):
    return {
        "room_index": i,
        "sha": f"sha{i}",
        "status": "awaiting_mapping",
        "panels": {"left": {"name": "A", "rows": []},
                   "right": {"name": "B", "rows": []}},
        "maps": {
            "0": {"panel": panel_a, "rows": rows_a or [], "confirmed": True},
            "1": {"panel": panel_b, "rows": rows_b or [], "confirmed": True},
        },
        "panel_picks": {},
        "message_id": None,
    }


def _session(rooms):
    return {
        "mode": "rooms",
        "match_type": "qualifier",
        "match_index": 0,
        "status": "awaiting_mapping",
        "rooms_expected": len(rooms),
        "rooms_received": len(rooms),
        "rooms": rooms,
        "uploaded_by": 999,
        "maps": {},
        "panel_picks": {},
    }


def _tournament():
    """Минимальный tournament для агрегации: 2 команды по 4 игрока.

    team0: P1..P4 (uids 1..4), team1: P5..P8 (uids 5..8).
    """
    from types import SimpleNamespace

    names = [f"P{i}" for i in range(1, 9)]
    return SimpleNamespace(
        teams=[
            {"players": names[0:4]},
            {"players": names[4:8]},
        ],
        player_user_ids={n: i + 1 for i, n in enumerate(names)},
        team_names={},
    )


class TestRoomsHelpers:
    def test_room_session_isolation(self):
        from views.ocr_match_view import _room_session

        rooms = [
            _room(1, rows_a=[_bound_row("P1", "Nick1", 5, 2, 1, 100, 1)] * 4,
                  rows_b=[_bound_row("P5", "Nick5", 1, 4, 0, 50, 5)] * 4),
            _room(2, rows_a=[_bound_row("P1", "OtherNick", 3, 1, 2, 200, 1)] * 4,
                  rows_b=[_bound_row("P6", "Nick6", 2, 2, 2, 60, 6)] * 4),
        ]
        s = _session(rooms)
        rs1 = _room_session(s, 1)
        rs2 = _room_session(s, 2)
        # Разные комнаты — независимые maps
        assert rs1["maps"] is rooms[0]["maps"]
        assert rs2["maps"] is rooms[1]["maps"]
        # Один Discord ID в разных комнатах с разными никами — ок
        assert rs1["maps"]["0"]["rows"][0]["ocr_name"] == "Nick1"
        assert rs2["maps"]["0"]["rows"][0]["ocr_name"] == "OtherNick"

    def test_aggregate_stats_sums_across_rooms(self):
        from views.ocr_match_view import _rooms_aggregate_stats

        rooms = [
            _room(1,
                  rows_a=[
                      _bound_row("P1", "NickA", 5, 2, 1, 100, 1),
                      _bound_row("P2", "NickB", 3, 4, 0, 80, 2),
                  ] + [_bound_row("P3", "N3", 0, 0, 0, 0, 3),
                       _bound_row("P4", "N4", 0, 0, 0, 0, 4)],
                  rows_b=[_bound_row(f"P{i}", "NickE", 4, 3, 2, 90, i)
                          for i in (5, 6, 7, 8)]),
            _room(2,
                  rows_a=[
                      _bound_row("P1", "NewNick", 7, 1, 3, 300, 1),
                      _bound_row("P2", "NickB", 1, 5, 1, 40, 2),
                  ] + [_bound_row("P3", "N3", 0, 0, 0, 0, 3),
                       _bound_row("P4", "N4", 0, 0, 0, 0, 4)],
                  rows_b=[_bound_row(f"P{i}", "NickE", 2, 4, 0, 70, i)
                          for i in (5, 6, 7, 8)]),
        ]
        t = _tournament()
        stats = _rooms_aggregate_stats(_session(rooms), t, 0, 1)
        # P1 (uid 1): разные игровые ники в разных комнатах → сумма
        # по discord_id, ключ результата — имя ростера.
        assert stats["P1"]["kills"] == 12
        assert stats["P1"]["deaths"] == 3
        assert stats["P1"]["assists"] == 4
        assert stats["P1"]["damage"] == 400
        assert "NickA" in stats["P1"]["ocr_name"]
        assert "NewNick" in stats["P1"]["ocr_name"]
        assert stats["P2"]["kills"] == 4
        assert stats["P5"]["kills"] == 6  # 4 + 2 по двум комнатам

    def test_aggregate_rejects_duplicate_discord_id_in_room(self):
        from views.ocr_match_view import _rooms_aggregate_stats

        # Один discord_id на двух строках одной комнаты → ошибка
        dup = _bound_row("P1", "NickX", 5, 1, 1, 100, 1)
        rooms = [
            _room(1,
                  rows_a=[dup, _bound_row("P2", "N2", 1, 1, 1, 10, 2),
                          _bound_row("P3", "N3", 1, 1, 1, 10, 3),
                          dict(dup)],
                  rows_b=[_bound_row(f"P{i}", "NB", 1, 1, 1, 10, i)
                          for i in (5, 6, 7, 8)]),
        ]
        with pytest.raises(ValueError, match="двум строкам"):
            _rooms_aggregate_stats(_session(rooms), _tournament(), 0, 1)

    def test_aggregate_rejects_foreign_discord_id(self):
        from views.ocr_match_view import _rooms_aggregate_stats

        rooms = [
            _room(1,
                  rows_a=[_bound_row("P1", "N", 1, 1, 1, 10, 999)] * 4,
                  rows_b=[_bound_row(f"P{i}", "NB", 1, 1, 1, 10, i)
                          for i in (5, 6, 7, 8)]),
        ]
        with pytest.raises(ValueError, match="не входит в состав"):
            _rooms_aggregate_stats(_session(rooms), _tournament(), 0, 1)

    def test_stats_complete_none_vs_zero(self):
        from views.ocr_match_view import _rooms_stats_complete

        def rows(k=1):
            return [
                {"ocr_name": f"N{i}", "kills": k, "deaths": k,
                 "assists": k, "dmg": k, "discord_id": i,
                 "discord_name": f"P{i}"}
                for i in range(1, 5)
            ]

        rooms = [_room(1, rows_a=rows(), rows_b=rows(2))]
        # uid→roster проверка не нужна: _rooms_stats_complete смотрит
        # только привязку и None
        assert _rooms_stats_complete(_session(rooms))

        # None («не определено») блокирует, явный 0 — нет
        rooms[0]["maps"]["0"]["rows"][0]["kills"] = None
        assert not _rooms_stats_complete(_session(rooms))
        rooms[0]["maps"]["0"]["rows"][0]["kills"] = 0
        assert _rooms_stats_complete(_session(rooms))

    def test_rooms_all_mapped(self):
        from views.ocr_match_view import (
            _rooms_all_mapped, _rooms_unmapped_count,
        )

        def team_rows(base_uid, ocr_prefix):
            return [
                _bound_row(f"P{base_uid + i}", f"{ocr_prefix}{i}",
                           1, 1, 1, 10, base_uid + i)
                for i in range(4)
            ]

        rooms = [
            _room(i, rows_a=team_rows(1, "NA"), rows_b=team_rows(5, "NB"))
            for i in (1, 2)
        ]
        s = _session(rooms)
        assert _rooms_all_mapped(s, 0, 1)
        assert _rooms_unmapped_count(s) == 0

        # Неполная комната → не всё сопоставлено
        rooms[1]["maps"]["1"]["rows"] = [
            dict(r, discord_id=None, discord_name=None)
            for r in rooms[1]["maps"]["1"]["rows"]
        ]
        s2 = _session(rooms)
        assert not _rooms_all_mapped(s2, 0, 1)
        assert _rooms_unmapped_count(s2) >= 1


class TestOrderedRoundScoreRooms:
    def test_manual_match_score(self):
        from services.ocr_service import ordered_round_score

        s = {"mode": "rooms", "match_score": [5, 2]}
        assert ordered_round_score(s, 0, 1) == (5, 2)

    def test_sum_room_scores(self):
        from services.ocr_service import ordered_round_score

        s = {
            "mode": "rooms",
            "rooms": [
                {"score": [5, 2], "maps": {
                    "0": {"panel": "left"}, "1": {"panel": "right"}}},
                {"score": [4, 3], "maps": {
                    "0": {"panel": "right"}, "1": {"panel": "left"}}},
            ],
        }
        # Комната 2: панели поменялись — счёт инвертируется (3,4)
        assert ordered_round_score(s, 0, 1) == (8, 6)

    def test_missing_room_score_returns_none(self):
        from services.ocr_service import ordered_round_score

        s = {
            "mode": "rooms",
            "rooms": [
                {"score": [5, 2], "maps": {
                    "0": {"panel": "left"}, "1": {"panel": "right"}}},
                {"score": None, "maps": {
                    "0": {"panel": "left"}, "1": {"panel": "right"}}},
            ],
        }
        assert ordered_round_score(s, 0, 1) is None


class TestSquadRooms:
    def _squad_room(self, panels=("squad",)):
        return {
            "room_index": 1, "kind": "squad",
            "panels": {k: {"name": "Отряд", "rows": []} for k in panels},
            "maps": {},
        }

    def test_squad_panel_keys(self):
        from views.ocr_match_view import _room_squad_panel_keys

        room = self._squad_room(["squad", "squad2", "left"])
        assert _room_squad_panel_keys(room) == ["squad", "squad2"]

    def test_squad_panel_for_team_first_free(self):
        from views.ocr_match_view import _squad_panel_for_team

        room = self._squad_room(["squad", "squad2"])
        assert _squad_panel_for_team(room, 0) == "squad"
        # После закрепления за командой 0 — следующая свободная
        room["maps"]["0"] = {"panel": "squad", "rows": []}
        assert _squad_panel_for_team(room, 1) == "squad2"
        room["maps"]["1"] = {"panel": "squad2", "rows": []}
        # Свободных нет → None (ручной ввод)
        assert _squad_panel_for_team(room, 0) == "squad"
        # Обе заняты — для новой команды None
        assert _squad_panel_for_team(room, 2) is None

    def test_ensure_manual_map_preserves_none(self):
        from views.ocr_match_view import _ensure_manual_map

        rs = {"maps": {}, "panels": {}}
        tm = _ensure_manual_map(rs, 1)
        assert tm["panel"] == "manual"
        assert len(tm["rows"]) == 4
        # None сохраняется — не подменяется нулём
        assert tm["rows"][0]["kills"] is None
        assert tm["rows"][0]["dmg"] is None


class TestManualEditValidation:
    def test_num_empty_is_none_zero_ok(self):
        from views.ocr_match_view import RoomRowEditModal

        assert RoomRowEditModal._num("") is None
        assert RoomRowEditModal._num("   ") is None
        assert RoomRowEditModal._num("0") == 0
        assert RoomRowEditModal._num("42") == 42

    def test_num_rejects_invalid(self):
        from views.ocr_match_view import RoomRowEditModal

        for bad in ("-1", "1.5", "abc", "12x", "1,5"):
            with pytest.raises(ValueError):
                RoomRowEditModal._num(bad)


class TestStrictAdminRollback:
    def test_strict_admin_permissions(self):
        from cogs.ocr_upload import _is_strict_admin
        from types import SimpleNamespace

        admin = SimpleNamespace(
            id=111, guild_permissions=SimpleNamespace(administrator=True),
        )
        member = SimpleNamespace(
            id=222, guild_permissions=SimpleNamespace(administrator=False),
        )
        no_perms = SimpleNamespace(id=333)  # без guild_permissions
        assert _is_strict_admin(admin)
        assert not _is_strict_admin(member)
        assert not _is_strict_admin(no_perms)

    def test_owner_without_admin_rights_denied(self, monkeypatch):
        import config
        from cogs.ocr_upload import _is_strict_admin
        from types import SimpleNamespace

        monkeypatch.setattr(config, "BOT_OWNER_ID", 777)
        owner_no_perms = SimpleNamespace(id=777)  # нет administrator
        owner_member = SimpleNamespace(
            id=777,
            guild_permissions=SimpleNamespace(administrator=False),
        )
        # Владелец бота БЕЗ administrator на сервере — откат запрещён
        assert not _is_strict_admin(owner_no_perms)
        assert not _is_strict_admin(owner_member)
        # Владелец + administrator — разрешено
        owner_admin = SimpleNamespace(
            id=777,
            guild_permissions=SimpleNamespace(administrator=True),
        )
        assert _is_strict_admin(owner_admin)


class TestFinalizeGate:
    """Идемпотентность финализации при сбоях (stats_done/done/JSON)."""

    def test_fresh_match_applies_stats(self):
        from views.ocr_match_view import _finalize_stats_gate

        assert _finalize_stats_gate(None, False) == (False, False)
        assert _finalize_stats_gate("processing", False) == (False, False)

    def test_stats_done_skips_elo_but_finishes(self):
        from views.ocr_match_view import _finalize_stats_gate

        # PG stats закоммичены, JSON-флаг потерян при падении:
        # статистику НЕ трогаем, историю/подтверждение добиваем.
        assert _finalize_stats_gate("stats_done", False) == (False, True)
        assert _finalize_stats_gate("stats_done", True) == (False, True)

    def test_done_is_fully_processed(self):
        from views.ocr_match_view import _finalize_stats_gate

        assert _finalize_stats_gate("done", False) == (True, True)
        assert _finalize_stats_gate("done", True) == (True, True)

    def test_no_db_falls_back_to_json_flag(self):
        from views.ocr_match_view import _finalize_stats_gate

        assert _finalize_stats_gate(None, True) == (True, True)
        assert _finalize_stats_gate("processing", True) == (False, False)


class TestRoomBundleHelpers:
    def test_room_has_panels_and_next_index(self):
        from cogs.ocr_upload import (
            _room_has_panels, _rooms_next_index, _rooms_completed_count,
        )

        session = {
            "mode": "rooms", "rooms_expected": 3,
            "rooms": [
                {"room_index": 1, "panels": {"left": {}, "right": {}}},
                {"room_index": 2, "panels": {}},
            ],
        }
        assert _rooms_completed_count(session) == 1
        assert _rooms_next_index(session) == 2
        session["rooms"][1]["panels"] = {"squad": {}}
        assert _rooms_next_index(session) == 3
        session["rooms"].append({"room_index": 3, "panels": {"left": {}}})
        assert _rooms_next_index(session) is None
        assert _rooms_completed_count(session) == 3

    def test_all_room_shas_dedup(self):
        from cogs.ocr_upload import _all_room_shas

        session = {"rooms": [
            {"sha": "a", "shas": ["b"]},
            {"sha": "c", "shas": []},
        ]}
        assert _all_room_shas(session) == {"a", "b", "c"}


class TestProcessingCardState:
    """Скрытие «Отправить результат» при приёме файлов (не OCR)."""

    def test_progress_text_states(self):
        from cogs.ocr_upload import _rooms_progress_text

        s = {"rooms_expected": 3, "rooms": []}
        assert "Сбор скриншотов" in _rooms_progress_text(s)
        assert "комнату 1" in _rooms_progress_text(s)
        s["rooms"] = [{"room_index": 1, "panels": {"left": {}}}]
        txt = _rooms_progress_text(s)
        assert "Обработка OCR" in txt
        assert "комнату 2" in txt
        s["rooms"] += [
            {"room_index": 2, "panels": {"left": {}}},
            {"room_index": 3, "panels": {"left": {}}},
        ]
        assert "Комплект полный" in _rooms_progress_text(s)

    @pytest.mark.asyncio
    async def test_ensure_processing_card_hides_once(self, monkeypatch):
        import cogs.ocr_upload as up
        from types import SimpleNamespace

        calls = []

        async def fake_update(guild, tournament, mt, mi, state="active", **kw):
            calls.append(state)

        monkeypatch.setattr(
            "services.match_announcer.update_match_card", fake_update
        )
        monkeypatch.setattr(up.store, "set", lambda t: None)

        tournament = SimpleNamespace(
            ocr_sessions={
                "qualifier_0": {
                    "mode": "rooms", "status": "collecting",
                    "card_state": "active",
                }
            },
            channel_id=1,
        )
        guild = SimpleNamespace(id=1)
        await up._ensure_processing_card(guild, tournament, "qualifier", 0)
        await up._ensure_processing_card(guild, tournament, "qualifier", 0)
        assert tournament.ocr_sessions["qualifier_0"]["card_state"] == "processing"
        assert calls == ["processing"]

    @pytest.mark.asyncio
    async def test_ensure_processing_card_no_session(self, monkeypatch):
        import cogs.ocr_upload as up
        from types import SimpleNamespace

        calls = []

        async def fake_update(*a, **kw):
            calls.append(1)

        monkeypatch.setattr(
            "services.match_announcer.update_match_card", fake_update
        )
        tournament = SimpleNamespace(ocr_sessions={}, channel_id=1)
        await up._ensure_processing_card(SimpleNamespace(id=1), tournament,
                                         "qualifier", 0)
        assert calls == []


class TestCreateRoomsSessionRace:
    @pytest.mark.asyncio
    async def test_second_session_rejected(self, monkeypatch):
        """Два одновременных create → только одна сессия."""
        import asyncio
        import cogs.ocr_upload as up
        from types import SimpleNamespace

        sent = []

        class _Msg:
            id = 555

        class _Chan:
            id = 777

            async def send(self, content=None, view=None):
                sent.append(content)
                return _Msg()

        class _T(SingleNamespace if False else object):
            pass

        tournament = SimpleNamespace(
            ocr_sessions={}, channel_id=1, guild_id=1,
            qualifier_matches=[(0, 1)],
        )
        # store.get должен вернуть тот же объект
        monkeypatch.setattr(up.store, "get", lambda g, c: tournament)
        monkeypatch.setattr(up.store, "set", lambda t: None)

        user = SimpleNamespace(id=42, display_name="Cap", name="cap")
        guild = SimpleNamespace(id=1)

        async def go():
            return await up.create_rooms_session(
                guild, _Chan(), user, tournament,
                "qualifier", 0, 3,
            )

        r1, r2 = await asyncio.gather(go(), go())
        results = [r for r in (r1, r2) if r is not None]
        assert len(results) == 1
        assert tournament.ocr_sessions["qualifier_0"]["uploaded_by"] == 42
        assert len(sent) == 1
