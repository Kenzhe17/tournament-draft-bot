"""Рендерер графической карточки профиля 1920x1080 (Player Profile).

Использует чистый template PNG как подложку: динамические области
затираются цветом панели, поверх рисуются актуальные данные.
Все координаты соответствуют riz3_player_profile_template.svg.
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

# Цвета дизайна
C_TEXT = (242, 244, 255)
C_MUTED = (138, 147, 184)
C_VIOLET = (170, 102, 255)
C_GREEN = (74, 222, 128)
C_RED = (248, 113, 113)
C_GOLD = (255, 209, 102)
C_PANEL_TOP = None  # сэмплируется из template

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


def _panel_color(img: Image.Image, x: int, y: int) -> tuple[int, int, int, int]:
    return img.getpixel((x, y))


def _erase(img: Image.Image, color, x1, y1, x2, y2) -> None:
    ImageDraw.Draw(img).rectangle([x1, y1, x2, y2], fill=color)


def _text(draw: ImageDraw.ImageDraw, xy, s: str, size: int,
          color=C_TEXT, anchor="ls", bold=False, stroke: int = 0) -> None:
    font = _font(size, bold)
    draw.text(xy, s, font=font, fill=color, anchor=anchor,
              stroke_width=stroke, stroke_fill=color)


def _fmt(n: float) -> str:
    """Формат числа с разделителем тысяч."""
    if isinstance(n, float):
        s = f"{n:.2f}".rstrip("0").rstrip(".")
        return s
    return f"{int(n):,}".replace(",", " ")


def _fit_text(draw, s: str, max_w: int, size: int, bold=False,
              min_size: int = 14) -> tuple[str, int]:
    """Ужать текст до max_w, уменьшая кегль."""
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
    c_top = _panel_color(img, 700, 300)
    c_stats = _panel_color(img, 600, 620)
    c_econ = _panel_color(img, 1400, 600)
    c_about = _panel_color(img, 1400, 720)
    c_chart = _panel_color(img, 600, 950)
    c_ult = _panel_color(img, 1400, 950)

    # ============ ВЕРХНЯЯ ПАНЕЛЬ ============
    # Никнейм
    _erase(img, c_top, 330, 300, 980, 360)
    name = data.get("name") or "Игрок"
    nstr, nsize = _fit_text(draw, name, 620, 48, bold=True, min_size=24)
    _text(draw, (346, 348), nstr, nsize, C_TEXT, bold=True, stroke=1)

    # Статус
    _erase(img, c_top, 335, 366, 800, 398)
    status = data.get("status") or "ок"
    _text(draw, (350, 384), f"СТАТУС: {status}", 20, C_MUTED)

    # Ранг (имя) — узкая зона, чтобы не затереть подпись "BRONZE → LEGEND"
    _erase(img, c_top, 385, 424, 530, 456)
    rank_name = data.get("rank_name") or "Bronze"
    _text(draw, (439, 442), rank_name.upper(), 20, C_GOLD, anchor="ms",
          bold=True)

    # ELO
    _erase(img, c_top, 800, 295, 1120, 372)
    _text(draw, (832, 354), _fmt(data.get("elo", 1000)), 62, C_TEXT, bold=True)
    # Δ ELO последнего матча
    _erase(img, c_top, 855, 392, 1150, 432)
    delta = data.get("last_elo_change", 0)
    dcolor = C_GREEN if delta >= 0 else C_RED
    dsign = "+" if delta >= 0 else "−"
    _text(draw, (897, 410), f"{dsign}{abs(delta)}", 24, dcolor, anchor="ms",
          bold=True)
    _text(draw, (979, 408), "последний матч", 16, C_MUTED)

    # Skill Rating + Level
    _erase(img, c_top, 1235, 305, 1500, 360)
    sr = data.get("skill_rating", 0.0)
    _text(draw, (1250, 344), f"{sr:.1f} / 10.0", 37, C_VIOLET, bold=True)
    _erase(img, c_top, 1235, 408, 1500, 442)
    _text(draw, (1252, 425), f"LEVEL {data.get('level', 1)}", 22, C_TEXT,
          bold=True)

    # Бейдж ранга + тир
    _erase(img, c_top, 1560, 240, 1900, 480)
    if badge_png:
        try:
            badge = Image.open(io.BytesIO(badge_png)).convert("RGBA")
            badge.thumbnail((210, 210), Image.LANCZOS)
            bx = 1734 - badge.width // 2
            by = 345 - badge.height // 2
            img.paste(badge, (bx, by), badge)
        except Exception:
            logger.warning("Rank badge paste failed")
    _text(draw, (1732, 452), rank_name.upper(), 20, C_GOLD, anchor="ms",
          bold=True)

    # ============ БИТВА ОТРЯДОВ ============
    cols_x = [109, 389.5, 670, 950.5]
    labels1 = ["МАТЧЕЙ", "ПОБЕД", "WINRATE", "KDA"]
    labels2 = ["AVG KILLS", "AVG DAMAGE", "MAX KILLS", "SKILL RATING"]
    vals1 = [
        _fmt(data.get("matches", 0)),
        _fmt(data.get("wins", 0)),
        f"{data.get('winrate', 0.0):.1f}%",
        f"{data.get('kda', 0.0):.2f}",
    ]
    vals2 = [
        f"{data.get('avg_kills', 0.0):.2f}",
        _fmt(data.get("avg_damage", 0)),
        _fmt(data.get("max_kills", 0)),
        f"{sr:.1f} / 10.0",
    ]
    for cx, lab, val, vy in zip(cols_x, labels1, vals1, [664] * 4):
        _erase(img, c_stats, cx - 140, vy - 40, cx + 140, vy + 15)
        _text(draw, (cx, 624), lab, 14, C_MUTED, anchor="ms")
        _text(draw, (cx, vy), val, 30, C_TEXT, anchor="ms", bold=True)
    for cx, lab, val in zip(cols_x, labels2, vals2):
        _erase(img, c_stats, cx - 140, 717, cx + 140, 775)
        _text(draw, (cx, 717), lab, 14, C_MUTED, anchor="ms")
        _text(draw, (cx, 757), val, 30, C_TEXT, anchor="ms", bold=True)

    # ============ ЭКОНОМИКА ============
    _erase(img, c_econ, 1350, 600, 1600, 655)
    _text(draw, (1377, 625), _fmt(data.get("balance", 0)), 25, C_GOLD,
          bold=True)
    _erase(img, c_econ, 1630, 595, 1880, 660)
    _text(draw, (1666, 634), _fmt(data.get("items", 0)), 22, C_TEXT,
          bold=True)

    # ============ О СЕБЕ ============
    about = [
        (1273, "РЕПУТАЦИЯ", _fmt(data.get("reputation", 0))),
        (1460, "БРАК", data.get("partner") or "—"),
        (1655, "ДЕНЬ РОЖДЕНИЯ", data.get("birthday") or "—"),
    ]
    for x, lab, val in about:
        _erase(img, c_about, x - 10, 762, x + 195, 800)
        _text(draw, (x, 779), val, 18, C_TEXT)

    # ============ ДИНАМИКА ELO ============
    _erase(img, c_chart, 95, 858, 1205, 1040)
    _text(draw, (114, 895), f"ПОСЛЕДНИЕ {data.get('hist_count', 0)} МАТЧЕЙ",
          13, C_MUTED)
    total_delta = data.get("chart_delta", 0)
    td_color = C_GREEN if total_delta >= 0 else C_RED
    _text(draw, (1088, 875),
          f"{'+' if total_delta >= 0 else '−'}{abs(total_delta)}",
          24, td_color, anchor="ms", bold=True)

    # График: область ~x130-1150, y915-985
    hist = data.get("elo_history") or []  # хронологический список дельт
    px0, px1, py0, py1 = 140, 1140, 925, 985
    if len(hist) >= 1:
        cum = []
        s = 0
        for d in hist:
            s += d
            cum.append(s)
        if len(cum) == 1:
            cum = [cum[0], cum[0]]
        lo, hi = min(cum), max(cum)
        span = (hi - lo) or 1
        pts = []
        n = len(cum)
        for i, v in enumerate(cum):
            x = px0 + (px1 - px0) * i / (n - 1)
            y = py1 - (py1 - py0) * (v - lo) / span
            pts.append((x, y))
        # область под линией — через overlay для прозрачности
        if len(pts) >= 2:
            overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            area = pts + [(pts[-1][0], py1), (pts[0][0], py1)]
            od.polygon(area, fill=(170, 102, 255, 45))
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

    w = data.get("hist_wins", 0)
    l = data.get("hist_losses", 0)
    _text(draw, (114, 968), f"{w}W / {l}L", 26, C_TEXT, bold=True)
    _text(draw, (379, 1020),
          f"старт: {data.get('hist_start_elo', data.get('elo', 1000))}",
          13, C_MUTED)
    _text(draw, (1171, 1020), f"текущий: {_fmt(data.get('elo', 1000))}",
          13, C_MUTED, anchor="rs")

    # ============ УЛЬТИМАТУМ ============
    ult = [
        (1273, "СЫГРАНО", _fmt(data.get("ult_matches", 0))),
        (1444, "ПОБЕД", _fmt(data.get("ult_wins", 0))),
        (1621, "ELO", _fmt(data.get("ult_elo", 1000))),
    ]
    for x, lab, val in ult:
        _erase(img, c_ult, x - 10, 945, x + 175, 985)
        _text(draw, (x, 962), val, 24, C_TEXT, bold=True)
    _erase(img, c_ult, 1250, 1005, 1880, 1038)
    _text(draw, (1274, 1022), data.get("ult_status") or "—", 16, C_MUTED)

    # ============ АВАТАР ============
    if avatar_png:
        try:
            av = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
            av = ImageOps_fit_square(av)
            av = av.resize((214, 214), Image.LANCZOS)
            mask = _circle_mask(214)
            img.paste(av, (102, 247), mask)
        except Exception:
            logger.warning("Avatar paste failed")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def ImageOps_fit_square(im: Image.Image) -> Image.Image:
    """Центральный кроп до квадрата."""
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return im.crop((left, top, left + side, top + side))


def _circle_mask(size: int):
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse([0, 0, size - 1, size - 1], fill=255)
    return mask


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
