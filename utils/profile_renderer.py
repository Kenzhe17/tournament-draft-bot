"""Рендерер графической карточки профиля 1920x1080 (Player Profile).

Использует чистый template PNG как подложку. Плейсхолдеры в template
помечены метками вида [USERNAME], [BIO], [ELO], [REP] и т.п.

Первопричина прошлых артефактов (размытые «вторые версии» текста,
перечёркнутые цифры, остатки глифов): маркеры затирались
ПРЯМОУГОЛЬНЫМ cv2.inpaint — TELEA размазывал весь прямоугольник,
включая градиенты фона и соседние элементы, а неточные рамки
оставляли «хвосты» букв под новым текстом.

Исправление: для каждого маркера строится ТОЧНАЯ маска — только
светлые пиксели букв (фон template имеет lum ~30, текст ~140-245),
маска дилатируется на антиалиасинг и один раз прогоняется через
cv2.inpaint. Фон, линии и декор не затрагиваются. Результат
кэшируется — чистая подложка строится один раз на процесс.
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


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = ("bold" if bold else "reg", size)
    if key not in _font_cache:
        name = "RussoOne-Regular.ttf" if bold else "DejaVuSans.ttf"
        path = FONTS_DIR / name
        if not path.exists():
            path = FONTS_DIR / "DejaVuSans.ttf"
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


# Плотные рамки маркеров [XXX] в template (x1,y1,x2,y2), измерены
# по OCR-боксам template↔example. Внутри рамки маска берёт ТОЛЬКО
# светлые пиксели букв — статичные подписи рядом не страдают.
_MARKER_REGIONS = (
    (1490, 88, 1660, 132),    # TEMPLATE -> статус игрока
    (340, 296, 668, 362),     # [USERNAME]
    (404, 356, 455, 395),     # [BIO] после статичного «SVO: »
    (826, 290, 1014, 370),    # [ELO]
    (844, 380, 952, 420),     # [△ ELO] под ELO
    (1242, 300, 1342, 360),   # [SR]
    (1322, 398, 1418, 438),   # [LEVEL] после статичного «LEVEL »
    (393, 412, 488, 458),     # [RANK]
    (538, 418, 698, 455),     # BRONZE→LEGEND
    (1692, 418, 1776, 460),   # [TIER]
    (1332, 592, 1492, 638),   # [BALANCE]
    (1626, 592, 1738, 638),   # [ITEMS]
    (1264, 764, 1338, 792),   # [REP] (ниже статичной подписи y748-756)
    (1450, 764, 1578, 792),   # [PARTNER]
    (1646, 764, 1778, 792),   # [BIRTHDAY]
    (102, 860, 300, 906),     # «ПОСЛЕДНИЕ N МАТЧЕЙ» заголовок W/L
    (103, 922, 298, 986),     # [W] / [L]
    (1030, 836, 1148, 886),   # [△ ELO] у графика
    (368, 996, 512, 1038),    # [HISTORY_START]
    (1056, 996, 1192, 1038),  # [CURRENT_ELO]
    (1264, 930, 1424, 978),   # [MATCHES]
    (1434, 928, 1542, 978),   # [WINS]
    (1608, 926, 1702, 980),   # [ELO] ult
    (1262, 994, 1464, 1038),  # [ULTIMATUM_STATUS]
)

# Область графика ELO: в template там запечена серая линия-пример
# (lum ~90, низкая насыщенность) — маскируем порогом ниже,
# чтобы не задеть тёмный фон (p99 lum ~39). Ось y~1009 не трогаем.
_LOW_LUM_REGIONS = (
    (375, 918, 1175, 1006),
)
_LOW_LUM_THRESHOLD = 55

# Маркеры — светлый текст (~lum 140-245) на тёмном фоне (~lum 30).
_MARKER_LUM = 100
_MASK_DILATE = 4
_clean_base_cache: Image.Image | None = None


def _build_clean_base() -> Image.Image:
    """Template без маркеров: единая пиксельная маска + один inpaint.

    Для каждой рамки маркера берутся только пиксели с яркостью выше
    _MARKER_LUM (буквы), маска дилатируется на толщину антиалиасинга
    и один раз прогоняется через cv2.inpaint по всей картинке.
    Фон, градиенты и декоративные линии не затрагиваются.
    """
    global _clean_base_cache
    if _clean_base_cache is not None:
        return _clean_base_cache.copy()

    img = Image.open(TEMPLATE_PATH).convert("RGBA")
    arr = np.array(img)
    lum = arr[..., :3].mean(axis=2)
    mask = np.zeros(lum.shape, np.uint8)
    for (x1, y1, x2, y2) in _MARKER_REGIONS:
        region = lum[y1:y2, x1:x2]
        mask[y1:y2, x1:x2][region > _MARKER_LUM] = 255
    for (x1, y1, x2, y2) in _LOW_LUM_REGIONS:
        region = lum[y1:y2, x1:x2]
        mask[y1:y2, x1:x2][region > _LOW_LUM_THRESHOLD] = 255
    if mask.any():
        kernel = np.ones((_MASK_DILATE * 2 + 1,) * 2, np.uint8)
        mask = cv2.dilate(mask, kernel)
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
        fixed = cv2.inpaint(bgr, mask, 3, cv2.INPAINT_TELEA)
        arr = cv2.cvtColor(fixed, cv2.COLOR_BGR2RGBA)
        img = Image.fromarray(arr, "RGBA")
    _clean_base_cache = img
    return img.copy()


def _base() -> Image.Image:
    return _build_clean_base()


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
    """Синхронный рендер. data — словарь со всеми значениями.

    Все координаты/размеры откалиброваны по паре template↔example
    (rapidocr bbox + ink-bbox подгонка шрифта). Якорь 'ls' = (x, baseline).
    """
    from utils.ranks import RANKS

    # Подложка уже очищена от маркеров (_build_clean_base, один раз).
    img = _base()
    draw = ImageDraw.Draw(img)

    # ================= ШАПКА =================
    _text(draw, (1501, 119), "ACTIVE PLAYER", 28, C_SOFT)

    # ================= ВЕРХ: ИГРОК =================
    name = data.get("name") or "Игрок"
    nstr, nsize = _fit_text(draw, name, 460, 58, bold=True, min_size=22)
    _text(draw, (346, 343), nstr, nsize, C_TEXT, bold=True)

    bio = str(data.get("status") or data.get("bio") or "ок")
    bstr, bsize = _fit_text(draw, bio, 390, 14, min_size=11)
    _text(draw, (405, 383), bstr, bsize, C_SOFT)

    elo_str = _fmt(data.get("elo", 1000))
    estr, esize = _fit_text(draw, elo_str, 185, 72, bold=True, min_size=34)
    _text(draw, (835, 357), estr, esize, C_TEXT, bold=True)

    d_str, d_color = _delta_style(int(data.get("last_elo_change") or 0))
    # центрируем под [△ ELO] (~x897), ширина динамическая
    dsize = 25
    while dsize > 14 and draw.textlength(
            d_str, font=_font(dsize)) > 95:
        dsize -= 2
    _text(draw, (897, 409), d_str, dsize, d_color, anchor="ms")

    sr = float(data.get("skill_rating", 0.0) or 0.0)
    sstr, ssize = _fit_text(draw, f"{sr:.1f} / 10.0", 175, 35, min_size=18)
    _text(draw, (1250, 341), sstr, ssize, C_TEXT)

    level = int(data.get("level") or 1)
    lstr, lsize = _fit_text(draw, str(level), 110, 22, min_size=14)
    # x=1328 — сразу после статичного «LEVEL » (закончивается ~x1318);
    # x=1304 наезжал на букву L.
    _text(draw, (1328, 425), lstr, lsize, C_SOFT)

    # ================= РАНГ =================
    rank_name = (data.get("rank_name") or "Bronze").upper()
    cur_rank, next_rank = None, None
    for i, (rname, min_lvl, _) in enumerate(RANKS):
        if level >= min_lvl:
            cur_rank = rname.upper()
            if i + 1 < len(RANKS):
                next_rank = RANKS[i + 1][0].upper()
        else:
            break
    cur_rank = cur_rank or rank_name

    rstr, rsize = _fit_text(draw, rank_name, 110, 19, bold=True, min_size=13)
    _text(draw, (439, 439), rstr, rsize, C_VIOLET, anchor="ms", bold=True)

    tier_str = f"{cur_rank} → {next_rank}" if next_rank else "MAX TIER"
    tstr, tsize = _fit_text(draw, tier_str, 165, 14, min_size=10)
    _text(draw, (548, 441), tstr, tsize, C_MUTED)

    # Верхняя строка карточки бейджа — «ДО {следующий ранг}»
    # (в example: «ДО DIAMOND»), отделено от текущего ранга ниже.
    if next_rank:
        nx_str = f"ДО {next_rank}"
    else:
        nx_str = "MAX TIER"
    nxstr, nxsize = _fit_text(draw, nx_str, 110, 16, min_size=11)
    _text(draw, (1737, 404), nxstr, nxsize, C_MUTED, anchor="ms")

    bstr2, bsize2 = _fit_text(draw, rank_name, 115, 19, bold=True, min_size=13)
    _text(draw, (1732, 443), bstr2, bsize2, C_VIOLET, anchor="ms",
          bold=True)
    if badge_png:
        try:
            badge = Image.open(io.BytesIO(badge_png)).convert("RGBA")
            badge.thumbnail((88, 88), Image.LANCZOS)
            img.paste(badge, (1588 + (88 - badge.width) // 2,
                              390 + (88 - badge.height) // 2), badge)
        except Exception:
            logger.warning("Rank badge paste failed")

    # ================= БИТВА ОТРЯДОВ =================
    _text(draw, (109, 663), _fmt(data.get("matches", 0)), 31, C_TEXT,
          bold=True)
    _text(draw, (390, 663), _fmt(data.get("wins", 0)), 31, C_TEXT,
          bold=True)
    wr_str, wr_size = _fit_text(
        draw, f"{data.get('winrate', 0.0):.1f}%", 105, 31, bold=True,
        min_size=18)
    _text(draw, (671, 662), wr_str, wr_size, C_TEXT, bold=True)
    kda_str, kda_size = _fit_text(
        draw, f"{data.get('kda', 0.0):.2f}", 105, 31, bold=True,
        min_size=18)
    _text(draw, (952, 663), kda_str, kda_size, C_VIOLET, bold=True)

    _text(draw, (1350, 624), _fmt(data.get("balance", 0)), 22, C_TEXT,
          bold=True)
    items_str, isize = _fit_text(
        draw, f"{_fmt(data.get('items', 0))} шт.", 100, 20, min_size=13)
    _text(draw, (1634, 622), items_str, isize, C_TEXT)

    _text(draw, (109, 756), f"{data.get('avg_kills', 0.0):.2f}", 31,
          C_TEXT, bold=True)
    _text(draw, (390, 756), _fmt(data.get("avg_damage", 0)), 32, C_TEXT,
          bold=True)
    _text(draw, (671, 756), _fmt(data.get("max_kills", 0)), 32, C_TEXT,
          bold=True)
    _text(draw, (951, 759), f"{sr:.1f}/10", 37, C_VIOLET, bold=True)

    # ================= О СЕБЕ =================
    _text(draw, (1272, 779), str(data.get("reputation", 0)), 18, C_TEXT)
    pstr, psize = _fit_text(
        draw, str(data.get("partner") or "—"), 180, 19, min_size=12)
    _text(draw, (1460, 776), pstr, psize, C_SOFT)
    bd_str, bd_size = _fit_text(
        draw, str(data.get("birthday") or "—"), 110, 18, min_size=12)
    _text(draw, (1654, 779), bd_str, bd_size, C_TEXT)

    # ================= ДИНАМИКА ELO =================
    _text(draw, (118, 887), "ПОБЕДЫ / ПОРАЖЕНИЯ", 12, C_MUTED)

    hist_w = int(data.get("hist_wins", 0) or 0)
    hist_l = int(data.get("hist_losses", 0) or 0)
    wl_str, wl_size = _fit_text(
        draw, f"{hist_w}/{hist_l}", 140, 41, bold=True, min_size=20)
    # центр под серединой слота (~x172)
    wl_w = draw.textlength(wl_str, font=_font(wl_size, True))
    _text(draw, (172 - wl_w / 2, 949), wl_str, wl_size, C_TEXT,
          bold=True)

    _text(draw, (101, 1006),
          f"ПОСЛЕДНИЕ {data.get('hist_count', 0)} МАТЧЕЙ", 13, C_MUTED)

    total_delta = int(data.get("chart_delta", 0) or 0)
    if total_delta > 0:
        td_str, td_color = f"+{total_delta} ELO", C_GREEN
    elif total_delta < 0:
        td_str, td_color = f"−{abs(total_delta)} ELO", C_DELTA_NEG
    else:
        td_str, td_color = "+0 ELO", C_MUTED
    _text(draw, (1041, 869), td_str, 24, td_color)

    # График: область x 385–1170, низ ~y1005 (ось y≈1009)
    hist = data.get("elo_history") or []
    px0, px1, py0, py1 = 385, 1170, 895, 1003
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

    _text(draw, (379, 1018),
          f"НАЧАЛО:{_fmt(data.get('hist_start_elo', data.get('elo', 1000)))}",
          14, C_MUTED)
    _text(draw, (1175, 1018),
          f"СЕЙЧАС:{_fmt(data.get('elo', 1000))}", 13, C_MUTED,
          anchor="rs")

    # ================= ULTIMATUM =================
    _text(draw, (1273, 962), _fmt(data.get("ult_matches", 0)), 37,
          C_TEXT, bold=True)
    _text(draw, (1444, 962), _fmt(data.get("ult_wins", 0)), 37,
          C_TEXT, bold=True)
    _text(draw, (1622, 962), _fmt(data.get("ult_elo", 1000)), 38,
          C_TEXT, bold=True)

    ust, usize = _fit_text(
        draw, data.get("ult_status") or "Пока нет сыгранных матчей",
        240, 15, min_size=11)
    _text(draw, (1274, 1019), ust, usize, C_MUTED)

    # ================= АВАТАР =================
    # Круг (95,240) 228px — измерено по diff template↔example
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
