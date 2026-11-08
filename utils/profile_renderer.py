"""Рендерер графической карточки профиля 1920x1080 (Player Profile).

Использует чистый template PNG как подложку. Плейсхолдеры в template
помечены метками вида [USERNAME], [BIO], [ELO], [REP] и т.п. —
каждая метка затирается через cv2.inpaint, значение рисуется по
координатам, измеренным по riz3_player_profile_template 1.png
(OCR + pixel-diff против example).
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets_ds_server"
TEMPLATE_PATH = ASSETS_DIR / "player_profile" / "riz3_player_profile_template.png"
FONTS_DIR = ASSETS_DIR / "fonts"

CARD_RENDER_SEM = asyncio.Semaphore(2)

# Цвета из example.svg
C_TEXT = (242, 244, 251)        # #F2F4FB
C_MUTED = (126, 136, 157)      # #7E889D
C_SOFT = (184, 193, 211)       # #B8C1D3
C_VIOLET = (170, 102, 255)     # #AA66FF
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


def _inpaint(img: Image.Image, box, radius: int = 5) -> None:
    """Восстановить область с плейсхолдером (градиенты сохраняются)."""
    x1, y1, x2, y2 = [int(v) for v in box]
    arr = np.array(img)
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    mask = np.zeros(bgr.shape[:2], np.uint8)
    mask[y1:y2, x1:x2] = 255
    # feather mask, чтобы не было резких краёв
    mask = cv2.GaussianBlur(mask, (0, 0), 2)
    _, m = cv2.threshold(mask, 40, 255, cv2.THRESH_BINARY)
    fixed = cv2.inpaint(bgr, m, radius, cv2.INPAINT_TELEA)
    img.paste(Image.fromarray(cv2.cvtColor(fixed, cv2.COLOR_BGR2RGBA)),
              (0, 0))


def _text(draw: ImageDraw.ImageDraw, xy, s: str, size: int,
          color=C_TEXT, anchor="ls", bold=False) -> None:
    """anchor — PIL-якорь; 's' = baseline (как y в SVG)."""
    font = _font(size, bold)
    draw.text(xy, s, font=font, fill=color, anchor=anchor)


def _fmt(n) -> str:
    """Целое число без разделителей (как в example: 53746, 1641)."""
    try:
        return str(int(round(float(n))))
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


def _delta_style(delta: int) -> tuple[str, tuple]:
    """(строка, цвет) для изменения ELO: + зелёный, − красный, 0 — muted."""
    if delta > 0:
        return f"+{delta}", C_GREEN
    if delta < 0:
        return f"−{abs(delta)}", C_DELTA_NEG
    return "+0", C_MUTED


def render_profile_png(data: dict, avatar_png: bytes | None = None,
                       badge_png: bytes | None = None) -> bytes:
    """Синхронный рендер. data — словарь со всеми значениями."""
    from utils.ranks import RANKS

    img = _base()
    draw = ImageDraw.Draw(img)

    # ============ ШАПКА ============
    # «PROFILE STATUS» — слот статуса справа вверху (в template «TEMPLATE»)
    _inpaint(img, (1490, 90, 1730, 128), radius=4)
    _text(draw, (1500, 121), "ACTIVE PLAYER", 24, C_SOFT)

    # ============ ВЕРХНЯЯ ПАНЕЛЬ ============
    # [USERNAME] — крупный ник
    _inpaint(img, (338, 293, 710, 362), radius=6)
    name = data.get("name") or "Игрок"
    nstr, nsize = _fit_text(draw, name, 480, 58, min_size=24)
    _text(draw, (346, 352), nstr, nsize, C_TEXT)

    # «SVO: [BIO]» — статичный префикс в template, слот — только значение
    _inpaint(img, (396, 355, 900, 398), radius=4)
    bio = str(data.get("status") or data.get("bio") or "ок")
    bstr, bsize = _fit_text(draw, bio, 430, 22, min_size=13)
    _text(draw, (410, 386), bstr, bsize, C_SOFT)

    # [ELO] — крупное число по центру ~(916, 358)
    _inpaint(img, (825, 288, 1010, 372), radius=6)
    elo_str = _fmt(data.get("elo", 1000))
    elo_size = 84
    while (elo_size > 40
           and draw.textlength(elo_str, font=_font(elo_size)) > 178):
        elo_size -= 4
    _text(draw, (916, 360), elo_str, elo_size, C_TEXT, anchor="ms")

    # [Δ ELO] — изменение под основным ELO (зелёный/красный)
    _inpaint(img, (845, 380, 955, 420), radius=4)
    d_str, d_color = _delta_style(int(data.get("last_elo_change") or 0))
    _text(draw, (868, 413), d_str, 26, d_color)

    # [SR] — Skill Rating
    _inpaint(img, (1240, 300, 1420, 356), radius=4)
    sr = float(data.get("skill_rating", 0.0) or 0.0)
    _text(draw, (1250, 348), f"{sr:.1f} / 10.0", 36, C_TEXT)

    # LEVEL [LEVEL]
    _inpaint(img, (1240, 398, 1420, 438), radius=4)
    level = int(data.get("level") or 1)
    _text(draw, (1250, 430), f"LEVEL {level}", 22, C_SOFT)

    # [RANK] — название ранга
    _inpaint(img, (378, 412, 510, 458), radius=4)
    rank_name = (data.get("rank_name") or "Bronze").upper()
    rstr, rsize = _fit_text(draw, rank_name, 115, 22, min_size=14)
    _text(draw, (390, 447), rstr, rsize, C_VIOLET)

    # «BRONZE→LEGEND» — слот прогрессии: текущий → следующий ранг
    cur_rank = None
    next_rank = None
    for i, (rname, min_lvl, _) in enumerate(RANKS):
        if level >= min_lvl:
            cur_rank = rname.upper()
            if i + 1 < len(RANKS):
                next_rank = RANKS[i + 1][0].upper()
        else:
            break
    cur_rank = cur_rank or rank_name
    tier_str = f"{cur_rank} → {next_rank}" if next_rank else "MAX TIER"
    _inpaint(img, (535, 418, 720, 458), radius=4)
    tstr, tsize = _fit_text(draw, tier_str, 175, 16, min_size=11)
    _text(draw, (547, 443), tstr, tsize, C_MUTED)

    # Бейдж ранга: «?» + следующий ранг над лейблом + [TIER] под ним
    _inpaint(img, (1700, 288, 1760, 330), radius=6)
    _inpaint(img, (1660, 388, 1800, 414), radius=4)
    _text(draw, (1732, 406), next_rank or "MAX", 13, C_MUTED, anchor="ms")
    _inpaint(img, (1670, 420, 1800, 460), radius=4)
    _text(draw, (1732, 448), rank_name, 19, C_VIOLET, anchor="ms")
    if badge_png:
        try:
            badge = Image.open(io.BytesIO(badge_png)).convert("RGBA")
            badge.thumbnail((88, 124), Image.LANCZOS)
            img.paste(badge, (1734 - badge.width // 2,
                              332 - badge.height // 2), badge)
        except Exception:
            logger.warning("Rank badge paste failed")

    # ============ БИТВА ОТРЯДОВ ============
    # Ряд 1: МАТЧИ / ПОБЕДЫ / WINRATE / KDA — пустые слоты
    _text(draw, (105, 668), _fmt(data.get("matches", 0)), 32, C_TEXT)
    _text(draw, (386, 668), _fmt(data.get("wins", 0)), 32, C_TEXT)
    _text(draw, (668, 668), f"{data.get('winrate', 0.0):.1f}%",
          32, C_TEXT)
    _text(draw, (947, 668), f"{data.get('kda', 0.0):.2f}", 32, C_VIOLET)

    # БАЛАНС / ПРЕДМЕТОВ — значения в одной строке с лейблами
    _inpaint(img, (1330, 596, 1490, 634), radius=4)
    _text(draw, (1345, 627), _fmt(data.get("balance", 0)), 26, C_TEXT)
    _inpaint(img, (1625, 596, 1740, 634), radius=4)
    items_str, isize = _fit_text(
        draw, f"{_fmt(data.get('items', 0))} шт.", 100, 22, min_size=14)
    _text(draw, (1638, 627), items_str, isize, C_TEXT)

    # Ряд 2: AVG KILLS / AVG DAMAGE / MAX KILLS / SKILL RATING
    _text(draw, (104, 762), f"{data.get('avg_kills', 0.0):.2f}",
          30, C_TEXT)
    _text(draw, (387, 762), _fmt(data.get("avg_damage", 0)), 30, C_TEXT)
    _text(draw, (667, 762), _fmt(data.get("max_kills", 0)), 30, C_TEXT)
    _text(draw, (949, 762), f"{sr:.1f}/10", 30, C_VIOLET)

    # ============ О СЕБЕ ============
    _inpaint(img, (1265, 755, 1340, 792), radius=4)
    _text(draw, (1274, 783), str(data.get("reputation", 0)), 20, C_TEXT)
    _inpaint(img, (1450, 755, 1580, 792), radius=4)
    pstr, psize = _fit_text(
        draw, str(data.get("partner") or "—"), 280, 20, min_size=12)
    _text(draw, (1461, 783), pstr, psize, C_SOFT)
    _inpaint(img, (1645, 755, 1785, 792), radius=4)
    bstr2, bsize2 = _fit_text(
        draw, str(data.get("birthday") or "—"), 290, 20, min_size=12)
    _text(draw, (1655, 783), bstr2, bsize2, C_TEXT)

    # ============ ДИНАМИКА ELO ============
    # Левая колонка: W/L блок («ПОСЛЕДНИЕ N МАТЧЕЙ» в template — слот)
    _inpaint(img, (105, 866, 560, 902), radius=4)
    _text(draw, (112, 893), "ПОБЕДЫ / ПОРАЖЕНИЯ", 15, C_MUTED)

    _inpaint(img, (105, 905, 270, 985), radius=4)
    hist_w = int(data.get("hist_wins", 0) or 0)
    hist_l = int(data.get("hist_losses", 0) or 0)
    _text(draw, (120, 952), f"{hist_w} / {hist_l}", 40, C_TEXT)

    _inpaint(img, (95, 985, 280, 1022), radius=4)
    _text(draw, (105, 1011),
          f"ПОСЛЕДНИЕ {data.get('hist_count', 0)} МАТЧЕЙ", 13, C_MUTED)

    # [Δ ELO] — суммарная дельта графика (справа от графика)
    total_delta = int(data.get("chart_delta", 0) or 0)
    if total_delta > 0:
        td_str, td_color = f"+{total_delta} ELO", C_GREEN
    elif total_delta < 0:
        td_str, td_color = f"−{abs(total_delta)} ELO", C_DELTA_NEG
    else:
        td_str, td_color = "+0 ELO", C_MUTED
    _inpaint(img, (1030, 838, 1150, 884), radius=4)
    _text(draw, (1040, 876), td_str, 22, td_color)

    # Область графика
    _inpaint(img, (355, 895, 1225, 1000), radius=8)
    draw = ImageDraw.Draw(img)

    hist = data.get("elo_history") or []  # хронологический список дельт
    px0, px1, py0, py1 = 385, 1170, 918, 984
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
            # Прямая полилиния — joint="curve" давал петли («фигуру»)
            od.line(pts, fill=(*C_VIOLET, 255), width=4)
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

    # Подписи оси — [HISTORY_START] / [CURRENT_ELO]
    _inpaint(img, (370, 998, 620, 1032), radius=4)
    _inpaint(img, (1050, 998, 1200, 1032), radius=4)
    draw = ImageDraw.Draw(img)
    _text(draw, (380, 1022),
          f"НАЧАЛО: {_fmt(data.get('hist_start_elo', data.get('elo', 1000)))}",
          14, C_MUTED)
    _text(draw, (1175, 1022), f"СЕЙЧАС: {_fmt(data.get('elo', 1000))}",
          14, C_MUTED, anchor="rs")

    # ============ ULTIMATUM ============
    _inpaint(img, (1268, 930, 1440, 972), radius=4)
    _text(draw, (1278, 966), _fmt(data.get("ult_matches", 0)), 30, C_TEXT)
    _inpaint(img, (1438, 930, 1545, 972), radius=4)
    _text(draw, (1448, 966), _fmt(data.get("ult_wins", 0)), 30, C_TEXT)
    _inpaint(img, (1610, 930, 1730, 972), radius=4)
    _text(draw, (1626, 966), _fmt(data.get("ult_elo", 1000)), 30, C_TEXT)

    # [ULTIMATUM_STATUS]
    _inpaint(img, (1265, 996, 1520, 1032), radius=4)
    draw = ImageDraw.Draw(img)
    ust, usize = _fit_text(
        draw, data.get("ult_status") or "Пока нет сыгранных матчей",
        500, 17, min_size=12)
    _text(draw, (1276, 1024), ust, usize, C_MUTED)

    # ============ АВАТАР ============
    # Слот — круг ~(209, 354) диаметром ~228 (в example помечен AVATAR SLOT)
    if avatar_png:
        try:
            av = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
            w, h = av.size
            side = min(w, h)
            av = av.crop(((w - side) // 2, (h - side) // 2,
                          (w + side) // 2, (h + side) // 2))
            av = av.resize((228, 228), Image.LANCZOS)
            mask = Image.new("L", (228, 228), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, 227, 227], fill=255)
            img.paste(av, (95, 240), mask)
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
