"""Рендерер графической карточки профиля 1920x1080 (Player Profile).

Использует чистый template PNG как подложку. Новый template не содержит
плейсхолдеров значений — текст рисуется напрямую; зоны с оставшимися
плейсхолдерами ([STATUS], [LEVEL], «?», [HISTORY_START] и т.п.)
восстанавливаются через cv2.inpaint.
Координаты измерены по riz3_player_profile_template 1.png (OCR + анализ).
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


def render_profile_png(data: dict, avatar_png: bytes | None = None,
                       badge_png: bytes | None = None) -> bytes:
    """Синхронный рендер. data — словарь со всеми значениями."""
    img = _base()
    draw = ImageDraw.Draw(img)

    # Шапка справа («PROFILE STATUS» / «ACTIVE PLAYER») — статичный текст
    # нового template, не трогаем

    # ============ ВЕРХНЯЯ ПАНЕЛЬ ============
    # Никнейм (пустой слот — рисуем напрямую)
    name = data.get("name") or "Игрок"
    nstr, nsize = _fit_text(draw, name, 720, 60, min_size=26)
    _text(draw, (346, 348), nstr, nsize, C_TEXT)

    # Статус — в шаблоне «СТАТУС: » + пустой слот
    _inpaint(img, (335, 358, 820, 396), radius=4)
    status = f"СТАТУС: {data.get('status') or 'ок'}"
    sstr, ssize = _fit_text(draw, status, 460, 20, min_size=12)
    _text(draw, (350, 384), sstr, ssize, C_SOFT)

    # Ранг — пустой слот
    _text(draw, (439, 442), (data.get("rank_name") or "BRONZE").upper(),
          20, C_VIOLET, anchor="ms")

    # RANKED ELO — крупное число; под ним в шаблоне статичная подпись
    # «ПОСЛЕДНИХ МАТЧЕЙ», в example она затёрта — удаляем зону и рисуем ELO
    _inpaint(img, (960, 385, 1140, 428), radius=6)
    elo_str = _fmt(data.get("elo", 1000))
    _text(draw, (993, 406), elo_str, 100, C_TEXT, anchor="ms")

    # Последнее изменение ELO сразу справа от текущего значения:
    # плюс — зелёный, минус — красный, ноль — приглушённый
    last_d = int(data.get("last_elo_change") or 0)
    if last_d > 0:
        d_str, d_color = f"+{last_d}", C_GREEN
    elif last_d < 0:
        d_str, d_color = f"−{abs(last_d)}", C_DELTA_NEG
    else:
        d_str, d_color = "+0", C_MUTED
    elo_w = draw.textlength(elo_str, font=_font(100))
    delta_x = min(993 + elo_w / 2 + 14, 1240)
    _text(draw, (delta_x, 410), d_str, 34, d_color, anchor="lm")

    # Skill Rating — пустой слот
    sr = data.get("skill_rating", 0.0)
    _text(draw, (1250, 344), f"{sr:.1f}/ 10.0", 37, C_TEXT)

    # LEVEL — «LEVEL [LEVEL]»
    _inpaint(img, (1235, 400, 1480, 438), radius=4)
    _text(draw, (1252, 425), f"LEVEL {data.get('level', 1)}", 22, C_SOFT)

    # Бейдж ранга — плейсхолдер «?»
    _inpaint(img, (1690, 295, 1790, 350), radius=6)
    if badge_png:
        try:
            badge = Image.open(io.BytesIO(badge_png)).convert("RGBA")
            badge.thumbnail((190, 190), Image.LANCZOS)
            img.paste(badge, (1734 - badge.width // 2,
                              325 - badge.height // 2), badge)
        except Exception:
            logger.warning("Rank badge paste failed")
    # «RANK BADGE» — статичный лейбл шаблона, оставляем
    _text(draw, (1732, 447), (data.get("rank_name") or "").upper(),
          20, C_VIOLET, anchor="ms")

    # ============ БИТВА ОТРЯДОВ ============
    # Колонка «ПОБЕД» убрана: W/L выводится единым блоком внизу карточки
    _inpaint(img, (368, 600, 580, 635), radius=4)
    # Значения — left-aligned по x лейблов, пустые слоты
    squad_cols = [
        (109, _fmt(data.get("matches", 0))),
        (670, f"{data.get('winrate', 0.0):.1f}%"),
        (950.5, f"{data.get('kda', 0.0):.2f}"),
    ]
    squad_cols2 = [
        (109, f"{data.get('avg_kills', 0.0):.2f}"),
        (389.5, _fmt(data.get("avg_damage", 0))),
        (670, _fmt(data.get("max_kills", 0))),
        (950.5, f"{sr:.1f}/10"),
    ]
    for cx, val in squad_cols:
        color = C_VIOLET if cx == 950.5 else C_TEXT
        _text(draw, (cx, 663), val, 30, color)
    for cx, val in squad_cols2:
        color = C_VIOLET if cx == 950.5 else C_TEXT
        _text(draw, (cx, 756), val, 30, color)

    # ============ ЭКОНОМИКА ============
    _text(draw, (1377, 624), _fmt(data.get("balance", 0)), 31, C_TEXT)
    _text(draw, (1666, 633), f"{_fmt(data.get('items', 0))} шт.", 22, C_TEXT)

    # ============ О СЕБЕ ============
    about = [
        (1273, str(data.get("reputation", 0)), C_TEXT, 1445),
        (1460, str(data.get("partner") or "—"), C_SOFT, 1620),
        (1655, str(data.get("birthday") or "—"), C_TEXT, 1880),
    ]
    for x, val, color, x2 in about:
        vstr, vsize = _fit_text(draw, val, x2 - x - 10, 19, min_size=12)
        _text(draw, (x, 779), vstr, vsize, color)

    # ============ ДИНАМИКА ELO ============
    # «ПОСЛЕДНИЕ N МАТЧЕЙ» — в шаблоне с литеральной N, перерисовываем
    _inpaint(img, (105, 875, 300, 900), radius=4)
    _text(draw, (114, 890),
          f"ПОСЛЕДНИЕ {data.get('hist_count', 0)} МАТЧЕЙ", 13, C_MUTED)

    # Область графика: убираем декоративную диагональ и возможные
    # плейсхолдеры, затем рисуем свой график
    _inpaint(img, (350, 905, 1200, 1002), radius=8)
    draw = ImageDraw.Draw(img)

    total_delta = data.get("chart_delta", 0)
    td_color = C_GREEN if total_delta >= 0 else C_DELTA_NEG
    tdsign = "+" if total_delta >= 0 else "−"
    _text(draw, (1135, 868), f"{tdsign}{abs(total_delta)} ELO", 20,
          td_color, anchor="rs")

    hist = data.get("elo_history") or []  # хронологический список дельт
    px0, px1, py0, py1 = 400, 1140, 935, 985
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

    # W/L блок — единственное отображение побед/поражений, поднято выше;
    # шаблонный текст «ПОБЕДЫ ПОРАЖЕНИЯ» и [HISTORY_START] затираем inpaint
    _inpaint(img, (105, 988, 620, 1032), radius=4)
    draw = ImageDraw.Draw(img)
    _text(draw, (114, 916), "ПОБЕДЫ / ПОРАЖЕНИЯ", 12, C_MUTED)
    _text(draw, (114, 943),
          f"{data.get('hist_wins', 0)} / {data.get('hist_losses', 0)}",
          33, C_TEXT)
    _text(draw, (114, 982),
          f"{data.get('hist_count', 0)} записей в истории", 13, C_MUTED)

    # Подписи оси — плейсхолдеры [HISTORY_START] / [CURRENT_ELO]
    _inpaint(img, (370, 1000, 620, 1030), radius=4)
    _inpaint(img, (1050, 1000, 1200, 1030), radius=4)
    draw = ImageDraw.Draw(img)
    _text(draw, (378, 1018),
          f"НАЧАЛО: {_fmt(data.get('hist_start_elo', data.get('elo', 1000)))}",
          13, C_MUTED)
    _text(draw, (1171, 1018), f"СЕЙЧАС: {_fmt(data.get('elo', 1000))}",
          13, C_MUTED, anchor="rs")

    # ============ УЛЬТИМАТУМ ============
    # Значения — пустые слоты
    ult = [
        (1273, _fmt(data.get("ult_matches", 0))),
        (1444, _fmt(data.get("ult_wins", 0))),
        (1621, _fmt(data.get("ult_elo", 1000))),
    ]
    for x, val in ult:
        _text(draw, (x, 961), val, 33, C_TEXT)
    # [ULTIMATUM_STATUS]
    _inpaint(img, (1260, 998, 1470, 1030), radius=4)
    draw = ImageDraw.Draw(img)
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
