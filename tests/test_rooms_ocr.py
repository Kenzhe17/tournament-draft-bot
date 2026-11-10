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
                  rows_b=[_bound_row("P5", "NickE", 4, 3, 2, 90, 5)] * 4),
            _room(2,
                  rows_a=[
                      _bound_row("P1", "NewNick", 7, 1, 3, 300, 1),
                      _bound_row("P2", "NickB", 1, 5, 1, 40, 2),
                  ] + [_bound_row("P3", "N3", 0, 0, 0, 0, 3),
                       _bound_row("P4", "N4", 0, 0, 0, 0, 4)],
                  rows_b=[_bound_row("P5", "NickE", 2, 4, 0, 70, 5)] * 4),
        ]
        stats = _rooms_aggregate_stats(_session(rooms))
        # P1: разные игровые ники в разных комнатах → сумма по discord_name
        assert stats["P1"]["kills"] == 12
        assert stats["P1"]["deaths"] == 3
        assert stats["P1"]["assists"] == 4
        assert stats["P1"]["damage"] == 400
        assert "NickA" in stats["P1"]["ocr_name"]
        assert "NewNick" in stats["P1"]["ocr_name"]
        assert stats["P2"]["kills"] == 4
        assert stats["P5"]["kills"] == 24  # 4+4 по 4 строки × 2 комнаты

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
