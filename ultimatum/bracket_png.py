"""Рендер турнирной сетки «Ультиматума» в PNG (Pillow).

Динамическая отрисовка из состояния ult2_matches/ult2_entrants:
верхняя сетка — верхняя полоса (слева направо),
нижняя — нижняя полоса, гранд-финал — отдельная колонка справа.
"""

from __future__ import annotations

import io
import re

from PIL import Image, ImageDraw, ImageFont

# Палитра r!z3
BG = (13, 11, 24)
PANEL = (24, 20, 46)
BORDER = (168, 63, 255)
TEXT = (235, 230, 245)
MUTED = (140, 132, 165)
WIN = (88, 214, 141)
LOSE = (235, 87, 87)
PENDING = (240, 190, 80)

BOX_W = 210
BOX_H = 46
COL_GAP = 56
ROW_GAP = 12
PAD = 24
BAND_GAP = 34


def _font(size: int):
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


_FONT = _font(15)
_FONT_B = _font(16)
_FONT_S = _font(12)

_STATUS_COLOR = {
    "COMPLETED": WIN,
    "READY": PENDING,
    "IN_PROGRESS": (90, 140, 255),
    "AWAITING_CONFIRMATION": PENDING,
    "DISPUTED": LOSE,
    "WAITING": MUTED,
    "CANCELLED": MUTED,
}


def _short(label: str, limit: int = 26) -> str:
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _round_of(key: str) -> tuple[str, int]:
    """'L10-2' -> ('L',10); 'GF2' -> ('GF',2); 'U1-3' -> ('U',1)."""
    m = re.match(r"([A-Z]+)(\d*)", key)
    if not m:
        return ("?", 0)
    return m.group(1), int(m.group(2) or 0)


def _band_of(m) -> str:
    pre, _ = _round_of(m.key)
    return {"U": "W", "L": "L", "GF": "GF"}.get(pre, m.bracket)


def _side_label(e) -> str:
    if e is None:
        return "—"
    return f"{_short(e.label(), 24)}  {e.wins}:{e.losses}"


def render_bracket_png(tournament, matches: list, ent_map: dict) -> bytes:
    """PNG сетки. matches — UltMatch, ent_map — id -> UltEntrant."""
    # Колонки внутри каждой полосы: band -> [round,...]
    bands: dict[str, dict[int, list]] = {"W": {}, "L": {}, "GF": {0: []}}
    for m in matches:
        band = _band_of(m)
        _, rnd = _round_of(m.key)
        if band == "GF":
            bands["GF"][0].append(m)   # GF1/GF2 — одна колонка
        else:
            bands[band].setdefault(rnd, []).append(m)
    for band in bands.values():
        for ms in band.values():
            ms.sort(key=lambda m: m.key)

    w_rounds = sorted(bands["W"])
    l_rounds = sorted(bands["L"])
    gf_matches = bands["GF"][0]
    n_w, n_l = len(w_rounds), len(l_rounds)
    n_cols = max(n_w, n_l) + (1 if gf_matches else 0)

    row_h = BOX_H + ROW_GAP
    upper_rows = max((len(bands["W"][r]) for r in w_rounds), default=1)
    lower_rows = max((len(bands["L"][r]) for r in l_rounds), default=1)

    top = PAD + 22
    mid = top + upper_rows * row_h + BAND_GAP
    bottom = mid + lower_rows * row_h + PAD
    total_h = bottom + 8
    total_w = PAD * 2 + n_cols * (BOX_W + COL_GAP) - COL_GAP + 16

    img = Image.new("RGB", (total_w, total_h), BG)
    draw = ImageDraw.Draw(img)

    fmt_name = (
        "СОЛО"
        if getattr(tournament.format, "value", tournament.format) == "solo"
        else "ДУО"
    )
    draw.text((PAD, 8), f"Ультиматум — {tournament.name} ({fmt_name})",
              font=_FONT_B, fill=TEXT)
    draw.text((PAD, top - 18), "Верхняя сетка", font=_FONT_S, fill=MUTED)
    draw.text((PAD, mid - 18), "Нижняя сетка", font=_FONT_S, fill=MUTED)
    if gf_matches:
        draw.text(
            (PAD + n_cols * (BOX_W + COL_GAP) - BOX_W - COL_GAP + 8, top - 18),
            "Гранд-финал", font=_FONT_S, fill=PENDING,
        )

    pos: dict[int, tuple[int, int]] = {}

    def place_band(band: str, rounds: list[int], y0: int) -> None:
        for ci, rnd in enumerate(rounds):
            ms = bands[band].get(rnd, [])
            x = PAD + ci * (BOX_W + COL_GAP)
            count = len(ms)
            band_h = (
                upper_rows if band == "W"
                else lower_rows if band == "L"
                else max(upper_rows, lower_rows)
            ) * row_h
            start_y = y0 + max(0, (band_h - count * row_h) // 2)
            for i, m in enumerate(ms):
                y = start_y + i * row_h
                pos[m.id] = (x, y)
                _draw_match(draw, m, ent_map, x, y)

    place_band("W", w_rounds, top)
    place_band("L", l_rounds, mid)
    if gf_matches:
        # GF — колонка справа, по центру всей высоты
        gf_x = PAD + (n_cols - 1) * (BOX_W + COL_GAP)
        gf_band_h = (upper_rows + lower_rows) * row_h + BAND_GAP
        gf_y0 = top + max(
            0, (gf_band_h - len(gf_matches) * row_h) // 2
        )
        for i, m in enumerate(gf_matches):
            y = gf_y0 + i * row_h
            pos[m.id] = (gf_x, y)
            _draw_match(draw, m, ent_map, gf_x, y)

    # Линии связей
    by_key = {m.key: m for m in matches}
    for m in matches:
        if m.id not in pos:
            continue
        x, y = pos[m.id]
        for side_idx, src in enumerate((m.side_a_src, m.side_b_src)):
            kind, _, ref = src.partition(":")
            if kind not in ("winner", "loser"):
                continue
            src_m = by_key.get(ref)
            if not src_m or src_m.id not in pos:
                continue
            sx, sy = pos[src_m.id]
            x1, y1 = sx + BOX_W, sy + BOX_H // 2
            x2, y2 = x, y + BOX_H // 2 + (side_idx * BOX_H // 4 - BOX_H // 8)
            _elbow(draw, x1, y1, x2, y2,
                   BORDER if kind == "winner" else (90, 80, 120))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _draw_match(draw, m, ent_map, x: int, y: int) -> None:
    ea = ent_map.get(m.entrant_a or -1)
    eb = ent_map.get(m.entrant_b or -1)
    status_col = _STATUS_COLOR.get(m.status, MUTED)

    draw.rounded_rectangle(
        [x, y, x + BOX_W, y + BOX_H],
        radius=6, fill=PANEL, outline=status_col, width=1,
    )
    draw.text((x + 6, y + 3), m.key, font=_FONT_S, fill=MUTED)

    if m.status == "COMPLETED":
        a_fill = WIN if m.entrant_a == m.winner_entrant else LOSE
        b_fill = WIN if m.entrant_b == m.winner_entrant else LOSE
    else:
        a_fill = b_fill = TEXT

    draw.text((x + 6, y + 17), "A " + _side_label(ea), font=_FONT, fill=a_fill)
    draw.text((x + 6, y + 33), "B " + _side_label(eb), font=_FONT, fill=b_fill)


def _elbow(draw, x1, y1, x2, y2, color) -> None:
    mx = (x1 + x2) // 2
    draw.line([(x1, y1), (mx, y1)], fill=color, width=1)
    draw.line([(mx, y1), (mx, y2)], fill=color, width=1)
    draw.line([(mx, y2), (x2, y2)], fill=color, width=1)
