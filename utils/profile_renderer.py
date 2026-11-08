"""Рендерер графической карточки профиля 1920x1080 (Player Profile).

Использует чистый template PNG как подложку: динамические области
затираются цветом панели, поверх рисуются актуальные данные.
Все координаты/якоря/кегли соответствуют riz3_player_profile_example.svg.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets_ds_server"
TEMPLATE_PATH = ASSETS_DIR / "player_profile" / "riz3_player_profile_template.png"
FONTS_DIR = ASSETS_DIR / "fonts"

CARD_RENDER_SEM = asyncio.Semaphore(2)

# Цвета из example.svg
C_TEXT = (242, 244, 251)     # #F2F4FB
C_MUTED = (126, 136, 157)   # #7E889D
C_SOFT = (184, 193, 211)    # #B8C1D3
C_VIOLET = (170, 102, 255)  # #AA66FF
C_DELTA_NEG = (250, 136, 153)  # #FA8899
C_GREEN = (74, 222, 128)

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_base_cache: Image.Image | None = None


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = ("bold" if bold else "reg", size)
    if key not in _font_cache:
        name = "RussoOne-Regular.ttf" if bold else "DejaVuSans.ttf"
        path = FONTS_DIR / name
        if not path.exists():
            path = FONTS_DIR / "DejaVuSans.ttf"
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


def _base() -> Image.Image:
    global _base_cache
    if _base_cache is None:
        _base_cache = Image.open(TEMPLATE_PATH).convert("RGBA")
    return _base_cache.copy()


def _erase(img: Image.Image, color, x1, y1, x2, y2) -> None:
    ImageDraw.Draw(img).rectangle([x1, y1, x2, y2], fill=color)


def _text(draw: ImageDraw.ImageDraw, xy, s: str, size: int,
          color=C_TEXT, anchor="ls", bold=False) -> None:
    """anchor — PIL-якорь; 's' = baseline (как y в SVG)."""
    font = _font(size, bold)
    draw.text(xy, s, font=font, fill=color, anchor=anchor)


def _fmt(n) -> str:
    """Целое число с разделителем тысяч пробелом."""
    try:
        return f"{int(round(float(n))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _fit_text(draw, s: str, max_w: int, size: int, bold=False,
              min_size: int = 14) -> tuple[str, int]:
    font = _font(size, bold)
    while size > min_size and draw.textlength(s, font=font) > max_w:
        size -= 2
        font = _font(size, bold)
    if draw.textlength(s, font=font) > max_w:
        while s and draw.textlength(s + "…", font=font) > max_w:
            s = s[:-1]
        s += "…"
    return s, size


def render_profile_png(data: dict, avatar_png: bytes | None = None,
                       badge_png: bytes | None = None) -> bytes:
    """Синхронный рендер. data — словарь со всеми значениями."""
    img = _base()
    draw = ImageDraw.Draw(img)

    # --- Цвета панелей (сэмпл из template) ---
    c_top = img.getpixel((700, 300))
    c_head = img.getpixel((1600, 100))
    c_stats = img.getpixel((600, 620))
    c_econ = img.getpixel((1400, 600))
    c_about = img.getpixel((1400, 720))
    c_chart = img.getpixel((600, 950))
    c_ult = img.getpixel((1400, 950))

    # ============ ШАПКА (справа) ============
    _erase(img, c_head, 1485, 72, 1900, 140)
    _text(draw, (1500, 85), "PROFILE STATUS", 13, C_MUTED)
    _text(draw, (1500, 120), "ACTIVE PLAYER", 27, C_TEXT, bold=True)
    _text(draw, (1771, 85), "PROFILE", 13, C_MUTED)
    _text(draw, (1771, 120), "r!z3", 27, C_VIOLET, bold=True)

    # ============ ВЕРХНЯЯ ПАНЕЛЬ ============
    # Никнейм (fs60 в example)
    _erase(img, c_top, 330, 295, 1100, 360)
    name = data.get("name") or "Игрок"
    nstr, nsize = _fit_text(draw, name, 720, 60, min_size=26)
    _text(draw, (346, 348), nstr, nsize, C_TEXT)

    # Статус
    _erase(img, c_top, 335, 366, 800, 398)
    _text(draw, (350, 384), f"СТАТУС: {data.get('status') or 'ок'}", 20, C_SOFT)

    # Ранг — violet, по центру x=439 (baseline)
    _erase(img, c_top, 385, 424, 530, 458)
    _text(draw, (439, 442), (data.get("rank_name") or "BRONZE").upper(),
          20, C_VIOLET, anchor="ms")

    # RANKED ELO
    _erase(img, c_top, 800, 290, 1130, 372)
    _text(draw, (832, 354), _fmt(data.get("elo", 1000)), 62, C_TEXT)
    # Δ ELO последнего матча
    _erase(img, c_top, 855, 392, 1150, 435)
    delta = data.get("last_elo_change", 0)
    dcolor = C_GREEN if delta >= 0 else C_DELTA_NEG
    dsign = "+" if delta >= 0 else "−"
    _text(draw, (897, 410), f"{dsign}{abs(delta)}", 24, dcolor, anchor="ms")
    _text(draw, (979, 408), "последний матч", 16, C_MUTED)

    # Skill Rating + Level
    _erase(img, c_top, 1235, 305, 1520, 360)
    sr = data.get("skill_rating", 0.0)
    _text(draw, (1250, 344), f"{sr:.1f} / 10.0", 37, C_TEXT)
    _erase(img, c_top, 1235, 408, 1520, 445)
    _text(draw, (1252, 425), f"LEVEL {data.get('level', 1)}", 22, C_SOFT)

    # Бейдж ранга + тир
    _erase(img, c_top, 1560, 240, 1900, 480)
    if badge_png:
        try:
            badge = Image.open(io.BytesIO(badge_png)).convert("RGBA")
            badge.thumbnail((210, 210), Image.LANCZOS)
            img.paste(badge, (1734 - badge.width // 2,
                              350 - badge.height // 2), badge)
        except Exception:
            logger.warning("Rank badge paste failed")
    _text(draw, (1732, 446), (data.get("rank_name") or "").upper(),
          20, C_VIOLET, anchor="ms")

    # ============ БИТВА ОТРЯДОВ ============
    # Значения — LEFT-aligned по x лейбла (как в example), fs30
    squad_cols = [
        (109, "МАТЧЕЙ", _fmt(data.get("matches", 0))),
        (389.5, "ПОБЕД", _fmt(data.get("wins", 0))),
        (670, "WINRATE", f"{data.get('winrate', 0.0):.1f}%"),
        (950.5, "KDA", f"{data.get('kda', 0.0):.2f}"),
    ]
    squad_cols2 = [
        (109, "AVG KILLS", f"{data.get('avg_kills', 0.0):.2f}"),
        (389.5, "AVG DAMAGE", _fmt(data.get("avg_damage", 0))),
        (670, "MAX KILLS", _fmt(data.get("max_kills", 0))),
        (950.5, "SKILL RATING", f"{sr:.1f} / 10"),
    ]
    bounds = [(100, 640, 380, 700), (380, 640, 660, 700),
              (655, 640, 940, 700), (940, 640, 1225, 700),
              (100, 712, 380, 782), (380, 712, 660, 782),
              (655, 712, 940, 782), (940, 712, 1225, 782)]
    for (x1, y1, x2, y2), (cx, lab, val) in zip(bounds[:4], squad_cols):
        _erase(img, c_stats, x1, y1, x2, y2)
        _text(draw, (cx, 624), lab, 14, C_MUTED)
        color = C_VIOLET if lab == "SKILL RATING" else C_TEXT
        _text(draw, (cx, 664), val, 30, color)
    for (x1, y1, x2, y2), (cx, lab, val) in zip(bounds[4:], squad_cols2):
        _erase(img, c_stats, x1, y1, x2, y2)
        _text(draw, (cx, 717), lab, 14, C_MUTED)
        color = C_VIOLET if lab == "SKILL RATING" else C_TEXT
        _text(draw, (cx, 757), val, 30, color)

    # ============ ЭКОНОМИКА ============
    _erase(img, c_econ, 1350, 600, 1600, 660)
    _text(draw, (1377, 625), _fmt(data.get("balance", 0)), 31, C_TEXT)
    _erase(img, c_econ, 1630, 595, 1900, 665)
    _text(draw, (1666, 634), f"{_fmt(data.get('items', 0))} шт.", 22, C_TEXT)

    # ============ О СЕБЕ ============
    about = [
        (1273, data.get("reputation", 0), C_TEXT),
        (1460, data.get("partner") or "—", C_SOFT),
        (1655, data.get("birthday") or "—", C_TEXT),
    ]
    for i, (x, val, color) in enumerate(about):
        x2 = [1445, 1620, 1880][i]
        _erase(img, c_about, x - 10, 762, x2, 800)
        vstr, vsize = _fit_text(draw, str(val), x2 - x - 10, 19,
                                min_size=12)
        _text(draw, (x, 779), vstr, vsize, color)

    # ============ ДИНАМИКА ELO ============
    _erase(img, c_chart, 95, 858, 1205, 1040)
    _text(draw, (114, 889),
          f"ПОСЛЕДНИЕ {data.get('hist_count', 0)} МАТЧЕЙ", 13, C_MUTED)
    total_delta = data.get("chart_delta", 0)
    td_color = C_GREEN if total_delta >= 0 else C_DELTA_NEG
    tdsign = "+" if total_delta >= 0 else "−"
    _text(draw, (1088, 870), f"{tdsign}{abs(total_delta)} ELO", 24,
          td_color, anchor="ms")

    # График: область ~x140-1140, y915-990
    hist = data.get("elo_history") or []  # хронологический список дельт
    px0, px1, py0, py1 = 150, 1130, 920, 985
    if hist:
        cum = []
        s = 0
        for d in hist:
            s += d
            cum.append(s)
        if len(cum) == 1:
            cum.append(cum[0])
        lo, hi = min(cum), max(cum)
        span = (hi - lo) or 1
        n = len(cum)
        pts = [
            (px0 + (px1 - px0) * i / (n - 1),
             py1 - (py1 - py0) * (v - lo) / span)
            for i, v in enumerate(cum)
        ]
        if len(pts) >= 2:
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            area = pts + [(pts[-1][0], py1), (pts[0][0], py1)]
            od.polygon(area, fill=(*C_VIOLET, 45))
            od.line(pts, fill=(*C_VIOLET, 255), width=4, joint="curve")
            for x, y in pts:
                od.ellipse([x - 4, y - 4, x + 4, y + 4],
                           fill=(*C_VIOLET, 255))
            img.alpha_composite(overlay)
            draw = ImageDraw.Draw(img)
        else:
            draw.line(pts * 2, fill=C_VIOLET, width=4)
    else:
        _text(draw, ((px0 + px1) / 2, (py0 + py1) / 2), "НЕТ ДАННЫХ",
              20, C_MUTED, anchor="ms")

    # W/L блок (как в example: лейбл / значение / подпись)
    _text(draw, (114, 942), "ПОБЕДЫ / ПОРАЖЕНИЯ", 12, C_MUTED)
    _text(draw, (114, 980),
          f"{data.get('hist_wins', 0)} / {data.get('hist_losses', 0)}",
          33, C_TEXT)
    _text(draw, (114, 1009),
          f"{data.get('hist_count', 0)} записей в истории", 13, C_MUTED)

    _text(draw, (379, 1018),
          f"НАЧАЛО: {_fmt(data.get('hist_start_elo', data.get('elo', 1000)))}",
          13, C_MUTED)
    _text(draw, (1171, 1018), f"СЕЙЧАС: {_fmt(data.get('elo', 1000))}",
          13, C_MUTED, anchor="rs")

    # ============ УЛЬТИМАТУМ ============
    ult = [
        (1273, _fmt(data.get("ult_matches", 0))),
        (1444, _fmt(data.get("ult_wins", 0))),
        (1621, _fmt(data.get("ult_elo", 1000))),
    ]
    for x, val in ult:
        _erase(img, c_ult, x - 10, 940, x + 175, 985)
        _text(draw, (x, 962), val, 33, C_TEXT)
    _erase(img, c_ult, 1250, 1005, 1900, 1038)
    _text(draw, (1274, 1020),
          data.get("ult_status") or "Пока нет сыгранных матчей", 16, C_MUTED)

    # ============ АВАТАР ============
    if avatar_png:
        try:
            av = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
            w, h = av.size
            side = min(w, h)
            av = av.crop(((w - side) // 2, (h - side) // 2,
                          (w + side) // 2, (h + side) // 2))
            av = av.resize((214, 214), Image.LANCZOS)
            mask = Image.new("L", (214, 214), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, 213, 213], fill=255)
            img.paste(av, (102, 247), mask)
        except Exception:
            logger.warning("Avatar paste failed")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


async def render_profile_png_async(data: dict,
                                   avatar_png: bytes | None = None,
                                   badge_png: bytes | None = None) -> bytes:
    """Асинхронная обёртка: рендер в executor с ограничением Semaphore."""
    async with CARD_RENDER_SEM:
        loop = asyncio.get_event_loop()
        t0 = loop.time()
        try:
            result = await loop.run_in_executor(
                None, render_profile_png, data, avatar_png, badge_png
            )
            logger.info("Profile PNG rendered in %.0fms",
                        (loop.time() - t0) * 1000)
            return result
        except Exception:
            logger.exception("Profile PNG render failed")
            raise
