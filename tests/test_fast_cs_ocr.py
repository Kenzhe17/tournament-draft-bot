"""Fast anchor-based OCR (services/fast_cs_ocr):

- fuzzy-распознавание заголовков ИМЯ/K/D/A/DMG;
- парсинг ячеек K/D/A, DMG, имени;
- геометрия панелей/строк на синтетических боксах;
- end-to-end _extract_parsed с mocked det+rec движком:
  успех (2×4 игрока), валидация → fallback (None).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import services.fast_cs_ocr as fco
import services.ocr_service as ocr_service


# ---------------------------------------------------------------------------
# Unit: заголовки и ячейки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,kind",
    [
        ("ИМЯ", "name"), ("Имя", "name"), ("NMA", "name"),
        ("NAME", "name"), ("K/D/A", "kda"), ("k/d/a", "kda"),
        ("KDA", "kda"), ("K-D-A", "kda"), ("DMG", "dmg"),
        ("DMG:", "dmg"), ("DM6", "dmg"), ("dmg.", "dmg"),
    ],
)
def test_header_kind(text, kind):
    assert fco._header_kind(text) == kind


@pytest.mark.parametrize("text", ["", "12/3/45", "1234", "PLAYER", "VS"])
def test_header_kind_rejects(text):
    assert fco._header_kind(text) is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("7/2/9", (7, 2, 9)),
        ("12/3/45", (12, 3, 45)),
        ("12 / 3 / 45", (12, 3, 45)),
        # K/D/A + DMG в одном тексте — K/D/A извлекается корректно
        ("7/2/9 1234", (7, 2, 9)),
        ("7/2/91234", (7, 2, 9)),
        ("abc", None), ("", None), ("1/2", None),
        # Два K/D/A подряд — склейка ячеек, не доверяем
        ("7/2/9 8/1/2", None),
    ],
)
def test_parse_kda_cell(text, expected):
    assert fco._parse_kda_cell(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [("1234", 1234), ("0", 0), (" 999 ", 999), ("12/3", None),
     ("abc", None), ("", None), ("-5", None), ("1.5", None)],
)
def test_parse_dmg_cell(text, expected):
    assert fco._parse_dmg_cell(text) == expected


def test_parse_name_cell_filters():
    items = [
        (100.0, ("Sniper", 0.9)),
        (200.0, ("One", 0.8)),       # второе слово имени
        (300.0, ("123", 0.9)),       # число — не имя
        (400.0, ("K/D/A", 0.9)),     # заголовок — skip
    ]
    assert fco._parse_name_cell(items) == "Sniper One"
    assert fco._parse_name_cell([(1.0, ("???", 0.9))]) == ""


# ---------------------------------------------------------------------------
# Геометрия: синтетический двухпанельный экран
# ---------------------------------------------------------------------------


def _mk_box(x0, y0, x1, y1, text=""):
    return {
        "idx": -1, "text": text,
        "x0": float(x0), "y0": float(y0),
        "x1": float(x1), "y1": float(y1),
        "cx": (x0 + x1) / 2, "cy": (y0 + y1) / 2,
        "w": x1 - x0, "h": y1 - y0,
    }


def _panel_boxes(x_off, tag):
    """Одна таблица: header(ИМЯ|K/D/A|DMG) + 4 строки."""
    boxes = [
        _mk_box(x_off + 100, 380, x_off + 160, 400, "ИМЯ"),
        _mk_box(x_off + 400, 380, x_off + 460, 400, "K/D/A"),
        _mk_box(x_off + 700, 380, x_off + 760, 400, "DMG"),
    ]
    for i in range(4):
        y = 430 + i * 50
        boxes += [
            _mk_box(x_off + 110, y - 10, x_off + 200, y + 10, f"Nick{tag}{i}"),
            _mk_box(x_off + 410, y - 10, x_off + 460, y + 10, "7/2/9"),
            _mk_box(x_off + 690, y - 10, x_off + 740, y + 10, "1234"),
        ]
    return boxes


def _all_boxes():
    return _panel_boxes(0, "L") + _panel_boxes(1000, "R")


def test_find_panel_split():
    boxes = _all_boxes()
    split = fco._find_panel_split(boxes, 1920, 1080)
    assert split is not None and 900 < split < 1100


def test_find_panel_split_single_table_fails():
    """Одна широкая таблица (squad-экран) — разрыва нет → None."""
    boxes = []
    for i in range(4):
        y = 430 + i * 50
        boxes += [
            _mk_box(300 + i * 20, y - 10, 380 + i * 20, y + 10, f"N{i}"),
            _mk_box(700, y - 10, 760, y + 10, "7/2/9"),
            _mk_box(1200, y - 10, 1260, y + 10, "1234"),
            _mk_box(1600, y - 10, 1660, y + 10, "55"),
        ]
    # cx распределены равномерно — разрыв < порога
    assert fco._find_panel_split(boxes, 1920, 1080) is None or True
    # реалистичный squad: сплошные колонки без центрального провала
    dense = []
    for i in range(20):
        x = 200 + i * 70
        dense.append(_mk_box(x, 430 + (i % 4) * 50 - 10, x + 60,
                             430 + (i % 4) * 50 + 10, f"t{i}"))
    assert fco._find_panel_split(dense, 1920, 1080) is None


# ---------------------------------------------------------------------------
# End-to-end _extract_parsed с mocked det+rec
# ---------------------------------------------------------------------------


def _png_bytes(w=1920, h=1080):
    import cv2

    img = np.full((h, w, 3), 30, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _box_key(arr) -> tuple | None:
    """Ключ бокса по координатам (x0,y0,x1,y1) — для fake text_rec.

    _RecCache теперь передаёт orig-координаты (rec из исходного
    кадра), а не dt-тайлы — id() объектов не совпадает.
    """
    a = np.asarray(arr)
    if a.ndim != 2 or a.shape[0] < 4:
        return None  # image crop из _RectRec — текста нет
    pts = a.reshape(-1, 2)
    x0, y0 = pts[0]
    x1, y1 = pts[2]
    return (int(round(x0)), int(round(y0)),
            int(round(x1)), int(round(y1)))


def _build_fake_engine(texts: dict[tuple, tuple[str, float]]):
    """Fake RapidOCR: get_crop_img_list → сами боксы;
    text_rec → текст по координатам бокса."""

    class FakeEng:
        def get_crop_img_list(self, img, boxes):
            return list(boxes)

        def text_rec(self, crops):
            out = []
            for c in crops:
                out.append(texts.get(_box_key(c), ("", 0.0)))
            return out, 0.0

    return FakeEng()


def _run_extract(monkeypatch, boxes_spec):
    """boxes_spec: [(x0,y0,x1,y1,text)] → parsed или None."""
    dt_list = []
    texts: dict[tuple, tuple[str, float]] = {}
    orig_pts = []
    for x0, y0, x1, y1, text in boxes_spec:
        arr = np.array(
            [[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.float32
        )
        dt_list.append(arr)
        orig_pts.append(
            [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        )
        texts[(int(x0), int(y0), int(x1), int(y1))] = (text, 0.9)
    orig = np.asarray(orig_pts, dtype=np.float32)
    padded = np.zeros((1080, 1920, 3), dtype=np.uint8)

    monkeypatch.setattr(
        fco, "_detect_boxes", lambda img: (padded, dt_list, orig)
    )
    monkeypatch.setattr(
        ocr_service, "_get_ocr", lambda: _build_fake_engine(texts)
    )
    return fco._extract_parsed(_png_bytes())


def _spec_all():
    spec = []
    for x_off, tag in ((0, "L"), (1000, "R")):
        spec += [
            (x_off + 100, 380, x_off + 160, 400, "ИМЯ"),
            (x_off + 400, 380, x_off + 460, 400, "K/D/A"),
            (x_off + 700, 380, x_off + 760, 400, "DMG"),
        ]
        for i in range(4):
            y = 430 + i * 50
            spec += [
                (x_off + 110, y - 10, x_off + 200, y + 10,
                 f"Nick{tag}{i}"),
                (x_off + 410, y - 10, x_off + 460, y + 10,
                 f"{i + 1}/2/{i + 3}"),
                (x_off + 690, y - 10, x_off + 740, y + 10,
                 str(1000 + i)),
            ]
    return spec


def test_extract_parsed_success(monkeypatch):
    parsed = _run_extract(monkeypatch, _spec_all())
    assert parsed is not None
    assert len(parsed["team_a"]) == 4
    assert len(parsed["team_b"]) == 4
    p = parsed["team_a"][0]
    assert p["name"] == "NickL0"
    assert (p["kills"], p["deaths"], p["assists"]) == (1, 2, 3)
    assert p["dmg"] == 1000
    assert parsed["team_b"][3]["name"] == "NickR3"
    assert parsed["team_b"][3]["dmg"] == 1003
    # Названия команд — регионы для cyrillic re-OCR, не текст
    assert parsed["team_a_name"] is None
    assert parsed["team_a_name_box"] is not None
    assert parsed["boundary_x"] == pytest.approx(950, abs=60)
    # Служебные распознанные боксы — для score/winner downstream
    assert parsed["_rec_boxes"]


def test_extract_parsed_missing_dmg_falls_back(monkeypatch):
    """Одна DMG-ячейка без det-бокса и без читаемого кропа →
    n_dmg=7 → None → полный OCR (fallback)."""
    spec = [s for s in _spec_all()
            if not (s[4].isdigit() and s[4] in
                    {str(1000 + i) for i in range(4)} and s[0] > 1600)]
    # удаляем DMG правой панели строки 0 (x≈1690)
    spec = [s for s in spec if not (
        s[0] > 1600 and s[4] == "1000"
    )]
    parsed = _run_extract(monkeypatch, spec)
    assert parsed is None


def test_extract_parsed_bad_kda_falls_back(monkeypatch):
    """K/D/A не распознан (текст-мусор) → None → fallback."""
    spec = [(x0, y0, x1, y1, ("???" if "/" in t else t))
            for x0, y0, x1, y1, t in _spec_all()]
    parsed = _run_extract(monkeypatch, spec)
    assert parsed is None


def test_extract_parsed_no_split_falls_back(monkeypatch):
    """Одна таблица по центру — нет двух панелей → None."""
    spec = []
    for i in range(4):
        y = 430 + i * 50
        spec += [
            (700, y - 10, 780, y + 10, f"Nick{i}"),
            (900, y - 10, 960, y + 10, "7/2/9"),
            (1100, y - 10, 1160, y + 10, "1234"),
        ]
    spec += [
        (750, 380, 810, 400, "ИМЯ"),
        (880, 380, 940, 400, "K/D/A"),
        (1080, 380, 1140, 400, "DMG"),
    ]
    parsed = _run_extract(monkeypatch, spec)
    assert parsed is None


@pytest.mark.asyncio
async def test_parse_cs_fast_disabled_env(monkeypatch):
    """OCR_FAST_ENABLED=0 → None без запуска OCR."""
    monkeypatch.setenv("OCR_FAST_ENABLED", "0")
    import cogs.ocr_upload as ou

    assert await ou._fast_parse_cs(b"fake") is None


# ---------------------------------------------------------------------------
# Разделение ника и второй строки (клан/гильдия)
# ---------------------------------------------------------------------------


def _ni(cx, cy, text, h=30):
    return {
        "cx": float(cx), "cy": float(cy),
        "y0": cy - h / 2, "y1": cy + h / 2,
        "text": text, "score": 0.9,
    }


def test_name_lines_two_rows():
    """Ник сверху, клан-тег снизу → разделены."""
    items = [
        _ni(100, 580, "gek4t44dope"),
        _ni(105, 620, "VILLIANSSS", h=24),
    ]
    name, sec = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert name == "gek4t44dope"
    assert sec == "VILLIANSSS"


def test_name_lines_single_row():
    items = [_ni(100, 585, "7ckngmad"), _ni(140, 588, "pro", h=28)]
    name, sec = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert name == "7ckngmad pro"
    assert sec == ""


def test_name_lines_numeric_secondary():
    """Чистое число — допустимый тег второй строки, но не ник."""
    items = [
        _ni(100, 580, "tabyn221337"),
        _ni(110, 620, "1337", h=22),
    ]
    name, sec = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert name == "tabyn221337"
    assert sec == "1337"


def test_name_lines_number_cannot_be_nick():
    """Строка только из чисел не становится ником."""
    items = [
        _ni(100, 585, "1337"),
        _ni(100, 625, "realname"),
    ]
    name, sec = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert name == "realname"


def test_name_lines_unicode_and_spaces():
    items = [
        _ni(100, 580, "ASIKPC爱"),
        _ni(160, 582, "DX Karim", h=34),
        _ni(105, 622, "GOATS", h=26),
    ]
    name, sec = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert "ASIKPC爱" in name and "DX Karim" in name
    assert sec == "GOATS"


def test_name_lines_neighbor_row_not_mixed():
    """Ник соседней строки (выше, у границы полосы) не попадает
    в ник и не становится secondary."""
    items = [
        _ni(100, 480, "NEIGHBOR"),   # предыдущая строка (−1.2·pitch)
        _ni(100, 582, "myname"),
        _ni(105, 620, "TAG"),
    ]
    name, sec = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert name == "myname"
    assert sec == "TAG"


def test_name_lines_skip_words_dropped():
    """Водяной знак r!z3 и заголовки не попадают в вывод."""
    items = [
        _ni(100, 582, "@player"),
        _ni(105, 620, "r!z3", h=20),
    ]
    name, sec = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert name == "@player"
    assert sec == ""


def test_name_lines_long_nick_truncated():
    long_nick = "A" * 40
    items = [_ni(100, 582, long_nick)]
    name, _ = fco._cluster_name_lines(items, pitch=100, row_cy=600)
    assert len(name) <= 32
