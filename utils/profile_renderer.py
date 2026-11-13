"""Рендерер графической карточки профиля 1920x1080 — Player Profile V2.

Новая реализация: SVG-сцена собирается в Python из реальных данных,
затем рендерится в PNG через Playwright/Chromium (viewport 1920x1080).

Вся типографика — Inter (variable font, в assets_ds_server/fonts).
Вёрстка повторяет шаблон riz3_profile_v2: CSS/SVG Grid-зоны,
без ручного «попиксельного» позиционирования текста поверх растровой
подложки. График ELO и ранги строятся из данных игрока.
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets_ds_server"
FONTS_DIR = ASSETS_DIR / "fonts"
PROFILE_V2_DIR = ASSETS_DIR / "profile_v2"
INTER_FONT_PATH = FONTS_DIR / "Inter-Variable.ttf"
LOGO_PATH = PROFILE_V2_DIR / "logo.png"

CARD_RENDER_SEM = asyncio.Semaphore(2)
RENDER_TIMEOUT = 20.0

# ---------------------------------------------------------------------------
# SVG helpers (портировано из riz3_profile_v2_pack/create_riz3_v2.py)
# ---------------------------------------------------------------------------

_E: list[str] = []


def _add(s: str) -> None:
    _E.append(s)


def _esc(v) -> str:
    return html.escape(str(v), quote=True)


def _rect(x, y, w, h, fill, rad=0, stroke=None, sw=1, opacity=None, extra=""):
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    op = f' opacity="{opacity}"' if opacity is not None else ""
    _add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rad}" fill="{fill}"{st}{op} {extra}/>')


def _line(x1, y1, x2, y2, color="#293047", width=1, opacity=1, extra=""):
    _add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}" opacity="{opacity}" {extra}/>')


def _txt(x, y, s, size=20, color="#E9ECF6", weight=400, anchor="start",
         spacing=None, extra="", eid=None):
    sp = f' letter-spacing="{spacing}"' if spacing is not None else ""
    name = f' id="{eid}"' if eid else ""
    _add(
        f'<text{name} x="{x}" y="{y}" fill="{color}" font-size="{size}" '
        f'font-weight="{weight}" text-anchor="{anchor}"{sp} {extra}>'
        f"{_esc(s)}</text>"
    )


def _circle(cx, cy, r, fill, stroke=None, sw=1, opacity=None, extra=""):
    st = f' stroke="{stroke}" stroke-width="{sw}"' if stroke else ""
    op = f' opacity="{opacity}"' if opacity is not None else ""
    _add(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}"{st}{op} {extra}/>')


def _img(x, y, w, h, href, extra="", preserve="xMidYMid meet"):
    _add(
        f'<image x="{x}" y="{y}" width="{w}" height="{h}" '
        f'preserveAspectRatio="{preserve}" href="{href}" {extra}/>'
    )


def _panel(x, y, w, h, title=None, sub=None, accent="#A66AFF"):
    _rect(x, y, w, h, "#121725", 18, "#28314A", 1.5)
    _rect(x, y, 5, h, accent, 3)
    if title:
        _txt(x + 27, y + 41, title, 21, "#E9ECF6", 750)
    if sub:
        _txt(x + w - 23, y + 40, sub, 15, "#8590AA", 500, anchor="end")


def _mini(x, y, w, label, value, color="#F5F4FC"):
    _rect(x, y, w, 70, "#171D2D", 12, "#303B55", 1)
    _txt(x + 20, y + 24, label, 13, "#909BB1", 600, spacing=".8")
    _txt(x + 20, y + 57, value, 29, color, 700)


# ---------------------------------------------------------------------------
# Текст: измерение ширины через Inter (PIL, variable font)
# ---------------------------------------------------------------------------

_font_cache: dict[tuple[int, int], object] = {}


def _pil_font(size: int, weight: int = 700):
    key = (size, weight)
    if key not in _font_cache:
        from PIL import ImageFont

        f = ImageFont.truetype(str(INTER_FONT_PATH), size)
        try:
            f.set_variation_by_axes([float(weight)])
        except Exception:
            pass
        _font_cache[key] = f
    return _font_cache[key]


def _fit_text(s: str, max_w: int, size: int, weight: int = 700,
              min_size: int = 12) -> tuple[str, int]:
    """Уменьшить кегль/обрезать строку, чтобы влезть в max_w пикселей."""
    s = str(s or "")
    while size > min_size:
        f = _pil_font(size, weight)
        if f.getlength(s) <= max_w:
            return s, size
        size -= 2
    f = _pil_font(min_size, weight)
    while s and f.getlength(s + "…") > max_w:
        s = s[:-1]
    return (s + "…") if s else "—", min_size


def _fmt(n) -> str:
    try:
        return f"{int(round(float(n))):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _delta_parts(delta: int) -> tuple[str, str]:
    if delta > 0:
        return f"+{delta}", "#4ADE80"
    if delta < 0:
        return f"−{abs(delta)}", "#FA8899"
    return "+0", "#7E889D"


def _data_uri(raw: bytes | None, default_mime: str = "image/png") -> str:
    if not raw:
        return ""
    mime = default_mime
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw))
        mime = Image.MIME.get(im.format or "", default_mime) or default_mime
    except Exception:
        pass
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


# ---------------------------------------------------------------------------
# Ранг: прогресс по порогам уровней из utils.ranks
# ---------------------------------------------------------------------------


def _rank_info(level: int) -> dict:
    from utils.ranks import RANKS

    level = max(1, int(level or 1))
    cur_i = 0
    for i, (_, min_lvl, _) in enumerate(RANKS):
        if level >= min_lvl:
            cur_i = i
        else:
            break
    cur_name, cur_min, _ = RANKS[cur_i]
    nxt = RANKS[cur_i + 1] if cur_i + 1 < len(RANKS) else None
    if nxt:
        nxt_name, nxt_min, _ = nxt
        prog = (level - cur_min) / max(1, (nxt_min - cur_min))
        remaining = max(0, nxt_min - level)
    else:
        nxt_name, nxt_min, prog, remaining = "", 0, 1.0, 0
    return {
        "cur_name": cur_name.upper(),
        "next_name": nxt_name.upper(),
        "progress": max(0.0, min(1.0, prog)),
        "remaining": remaining,
        "max_tier": nxt is None,
    }


# ---------------------------------------------------------------------------
# Построение SVG
# ---------------------------------------------------------------------------


def build_profile_svg(data: dict, avatar_png: bytes | None = None,
                      badge_png: bytes | None = None) -> str:
    """Собрать SVG-сцену 1920x1080 из словаря данных профиля."""
    global _E
    _E = []

    _add(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" '
        'viewBox="0 0 1920 1080"><defs>'
        '<radialGradient id="ambient" cx="0%" cy="0%" r="120%">'
        '<stop offset="0%" stop-color="#23123F"/>'
        '<stop offset="42%" stop-color="#0B0E19"/>'
        '<stop offset="100%" stop-color="#080B14"/></radialGradient>'
        '<linearGradient id="bar" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0%" stop-color="#8D48F9"/>'
        '<stop offset="100%" stop-color="#C68EFF"/></linearGradient>'
        '<linearGradient id="chartfill" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0%" stop-color="#9B5CFF" stop-opacity=".32"/>'
        '<stop offset="100%" stop-color="#9B5CFF" stop-opacity=".01"/>'
        '</linearGradient>'
        '<filter id="blur"><feGaussianBlur stdDeviation="35"/></filter>'
        '<clipPath id="avatarClip"><circle cx="170" cy="304" r="64"/></clipPath>'
        "</defs>"
    )
    _rect(0, 0, 1920, 1080, "url(#ambient)")
    _add('<ellipse cx="225" cy="140" rx="400" ry="260" fill="#5A23A8" opacity=".11" filter="url(#blur)"/>')
    _add('<ellipse cx="1750" cy="1020" rx="420" ry="290" fill="#2C3B9C" opacity=".09" filter="url(#blur)"/>')
    for gx in range(110, 1920, 140):
        _line(gx, 20, gx, 1080, "#313552", 0.8, 0.13)
    for gy in range(140, 1080, 140):
        _line(0, gy, 1920, gy, "#313552", 0.8, 0.10)

    name = str(data.get("name") or "Игрок")
    bio = str(data.get("status") or data.get("bio") or "ок")
    level = int(data.get("level") or 1)
    elo = int(data.get("elo") or 1000)
    delta = int(data.get("last_elo_change") or 0)
    sr = float(data.get("skill_rating") or 0.0)
    rank = _rank_info(level)

    # --- header ---
    _rect(60, 42, 4, 89, "#A66AFF", 2)
    logo_uri = _data_uri(LOGO_PATH.read_bytes() if LOGO_PATH.exists() else None)
    if logo_uri:
        _img(85, 49, 77, 77, logo_uri)
    else:
        _rect(84, 48, 79, 79, "#131020", 16, "#57338B", 1.2)
    _txt(188, 69, "r!z3  /  COMPETITIVE", 16, "#AE7FFE", 700, spacing="1.7")
    _txt(187, 114, "ПРОФИЛЬ ИГРОКА", 39, "#F7F4FF", 800, spacing=".3")
    _rect(1517, 60, 340, 57, "#151A2A", 14, "#303B54", 1)
    _circle(1545, 89, 5, "#A66AFF")
    _txt(1562, 95, "FREE FIRE  ·  CS 4×4", 17, "#C2C7DD", 650)
    _line(60, 148, 1860, 148, "#333951", 2)
    _line(60, 148, 323, 148, "#AB6CFF", 4)

    # --- hero: player ---
    _panel(60, 175, 567, 257)
    _circle(170, 304, 75, "#271B3E", "#8446D2", 2)
    _circle(170, 304, 66, "#35214F", "#745399", 1)
    avatar_uri = _data_uri(avatar_png)
    if avatar_uri:
        _img(
            105, 239, 130, 130, avatar_uri,
            extra='clip-path="url(#avatarClip)" preserveAspectRatio="xMidYMid slice"',
            preserve="xMidYMid slice",
        )
    else:
        initial = (name[:1] or "?").upper()
        _txt(170, 324, initial, 91, "#EAE1F9", 750, anchor="middle")
    _txt(273, 235, "УЧАСТНИК", 14, "#8792A8", 700, spacing="1.4")
    nstr, nsize = _fit_text(name, 340, 58, 800, min_size=22)
    _txt(269, 295, nstr, nsize, "#F5F2FC", 800)
    bstr, bsize = _fit_text(bio, 340, 19, 450, min_size=12)
    _txt(272, 330, bstr, bsize, "#A2A8BD", 450)
    rname = rank["cur_name"]
    chip, chip_size = _fit_text(rname, 124, 19, 700, min_size=9)
    _rect(269, 356, 132, 42, "#2C2148", 10, "#7450AA", 1)
    _txt(335, 384, chip, chip_size, "#B17BFF", 700, anchor="middle")
    _txt(418, 384, f"УРОВЕНЬ {level}", 16, "#97A1B8", 650)

    # --- hero: ELO ---
    _panel(649, 175, 382, 257)
    _txt(676, 217, "RANKED ELO", 16, "#99A3B8", 700, spacing="1.2")
    estr, esize = _fit_text(_fmt(elo), 330, 88, 800, min_size=40)
    _txt(674, 331, estr, esize, "#F7F5FD", 800)
    d_str, d_color = _delta_parts(delta)
    _rect(677, 356, 103, 38, "#3A2133", 10)
    _txt(729, 381, d_str, 21, d_color, 720, anchor="middle")
    _txt(795, 381, "ЗА МАТЧ", 13, "#99A2BA", 700, spacing=".8")

    # --- hero: rank ---
    _panel(1053, 175, 807, 257)
    _txt(1086, 215, "ТЕКУЩИЙ РАНГ", 16, "#99A3B8", 700, spacing="1")
    _circle(1176, 316, 72, "#23173B", "#A768FF", 2)
    _circle(1176, 316, 60, "#2E2049", "#8A5BD0", 1)
    badge_uri = _data_uri(badge_png)
    if badge_uri:
        # Эмблемы FF CS — полноцветные оригинальные PNG, фильтр не нужен.
        _img(
            1116, 256, 120, 120, badge_uri,
            preserve="xMidYMid meet",
        )
    else:
        _txt(1176, 330, rname[:1], 44, "#DDBDFF", 780, anchor="middle")
    # Длинные названия («GRANDMASTER II») не должны вылезти за панель (правый край ~1860)
    rstr, rsize = _fit_text(rank["cur_name"], 545, 35, 780, min_size=20)
    _txt(1281, 279, rstr, rsize, "#DDBDFF", 780)
    _txt(1282, 312, f"Текущий ранг  ·  Ур. {level}", 17, "#AFB9D1", 450)
    _line(1281, 343, 1827, 343, "#303951", 1)
    if rank["max_tier"]:
        _txt(1281, 380, "MAX TIER", 15, "#9DA7BC", 750, spacing="1")
        _rect(1413, 364, 308, 12, "#2D344A", 6)
        _rect(1413, 364, 308, 12, "url(#bar)", 6)
        _txt(1827, 380, "MAX", 16, "#D0D3E2", 600, anchor="end")
    else:
        _txt(1281, 380, f"ДО {rank['next_name']}", 15, "#9DA7BC", 750, spacing="1")
        _rect(1413, 364, 308, 12, "#2D344A", 6)
        fill_w = max(8, int(308 * rank["progress"]))
        _rect(1413, 364, fill_w, 12, "url(#bar)", 6)
        _txt(1827, 380, f"{rank['remaining']} ур.", 16, "#D0D3E2", 600, anchor="end")

    # --- stats grid 4x2 ---
    _panel(60, 456, 1800, 248, "СТАТИСТИКА CLASH SQUAD",
           "ПОСЛЕДНИЕ ПОДТВЕРЖДЁННЫЕ МАТЧИ")
    stats = [
        ("МАТЧЕЙ", _fmt(data.get("matches", 0))),
        ("ПОБЕД", _fmt(data.get("wins", 0))),
        ("WINRATE", f"{float(data.get('winrate') or 0):.1f}%"),
        ("KDA", f"{float(data.get('kda') or 0):.2f}"),
        ("СР. УБИЙСТВ", f"{float(data.get('avg_kills') or 0):.2f}"),
        ("СР. УРОН", _fmt(data.get("avg_damage", 0))),
        ("МАКС. УБИЙСТВ", _fmt(data.get("max_kills", 0))),
        ("SKILL RATING", f"{sr:.1f} / 10"),
    ]
    cw, gap = 422, 14
    for i, (lab, val) in enumerate(stats):
        x = 84 + (i % 4) * (cw + gap)
        y = 513 + (i // 4) * 83
        color = "#BA83FF" if i in (3, 7) else "#F4F4FC"
        _mini(x, y, cw, lab, val, color)

    # --- chart ---
    _panel(60, 725, 1100, 291, "ДИНАМИКА ELO")
    chart_delta = int(data.get("chart_delta") or 0)
    if chart_delta > 0:
        cd_str, cd_color = f"+{chart_delta} ELO", "#4ADE80"
    elif chart_delta < 0:
        cd_str, cd_color = f"−{abs(chart_delta)} ELO", "#FA8899"
    else:
        cd_str, cd_color = "+0 ELO", "#7E889D"
    _rect(897, 745, 234, 34, "#2B203D", 10)
    _txt(1014, 768, cd_str, 20, cd_color, 720, anchor="middle")

    plot_x0, plot_x1, plot_top, plot_bottom = 304, 1115, 801, 953
    for gy in (816, 869, 922):
        _line(plot_x0, gy, plot_x1, gy, "#343A50", 1, 0.75)

    hist = [int(d) for d in (data.get("elo_history") or [])]
    start_elo = int(data.get("hist_start_elo") or elo)
    if hist:
        # Абсолютные значения ELO по матчам: start + кумулятивная сумма дельт
        values = []
        acc = start_elo
        for d in hist:
            acc += d
            values.append(acc)
        if len(values) == 1:
            values = [values[0], values[0]]
        lo, hi = min(values), max(values)
        span = (hi - lo) or 1
        pad = max(15, span * 0.15)
        lo -= pad
        hi += pad
        n = len(values)
        xs = [plot_x0 + (plot_x1 - plot_x0) * i / (n - 1) for i in range(n)]
        ys = [plot_bottom - (plot_bottom - plot_top) * (v - lo) / (hi - lo)
              for v in values]
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
        poly = f"{plot_x0},{plot_bottom} {pts} {plot_x1},{plot_bottom}"
        _add(f'<polygon points="{poly}" fill="url(#chartfill)"/>')
        _add(f'<polyline points="{pts}" fill="none" stroke="#AC6EFF" stroke-width="4" '
             f'stroke-linecap="round" stroke-linejoin="round"/>')
        for x, y in zip(xs, ys):
            _circle(round(x, 1), round(y, 1), 6, "#D1A6FF", "#8746D6", 1)
        _txt(85, 856, _fmt(values[0]), 44, "#F7F6FB", 760)
    else:
        _txt((plot_x0 + plot_x1) / 2, (plot_top + plot_bottom) / 2,
             "Пока нет данных о матчах", 20, "#909BB0", 500, anchor="middle")
    _txt(84, 884, "СТАРТОВОЕ ELO", 13, "#909BB0", 650)
    _txt(304, 986, f"НАЧАЛО: {_fmt(start_elo)}", 14, "#8D98AE", 500)
    _txt(1117, 986, f"СЕЙЧАС: {_fmt(elo)}", 14, "#8D98AE", 500, anchor="end")

    # --- right column ---
    _panel(1184, 725, 676, 88)
    _txt(1208, 754, "ЭКОНОМИКА", 19, "#E8EAF4", 750)
    _txt(1208, 788, "Баланс", 15, "#92A0B8", 450)
    _txt(1316, 788, _fmt(data.get("balance", 0)), 22, "#F8F7FE", 750)
    _line(1482, 764, 1482, 797, "#30384F", 1)
    _txt(1508, 788, "Предметов", 15, "#92A0B8", 450)
    _txt(1659, 788, _fmt(data.get("items", 0)), 22, "#F8F7FE", 750)

    _panel(1184, 825, 676, 88)
    _txt(1208, 854, "СОЦИАЛЬНОЕ", 19, "#E8EAF4", 750)
    _txt(1208, 887, "REP", 15, "#92A0B8", 550)
    _txt(1260, 887, _fmt(data.get("reputation", 0)), 22, "#F8F7FE", 750)
    _line(1391, 862, 1391, 898, "#30384F")
    partner = str(data.get("partner") or "—")
    pstr, _ = _fit_text(partner, 190, 18, 650, min_size=12)
    _txt(1417, 887, "Брак", 15, "#92A0B8")
    _txt(1477, 887, pstr, 18, "#F8F7FE", 650)
    _line(1626, 862, 1626, 898, "#30384F")
    bday = str(data.get("birthday") or "—")
    bstr2, _ = _fit_text(bday, 200, 16, 650, min_size=12)
    _txt(1646, 887, "Дата рождения", 14, "#92A0B8")
    _txt(1775, 887, bstr2, 16, "#F8F7FE", 650)

    _panel(1184, 925, 676, 91)
    _txt(1208, 955, "УЛЬТИМАТУМ", 19, "#E8EAF4", 750)
    _txt(1208, 992, f"Матчей: {_fmt(data.get('ult_matches', 0))}", 17, "#BAC4D9", 550)
    _line(1391, 971, 1391, 1005, "#30384F")
    _txt(1417, 992, f"Побед: {_fmt(data.get('ult_wins', 0))}", 17, "#BAC4D9", 550)
    _line(1585, 971, 1585, 1005, "#30384F")
    _txt(1615, 992, f"ELO: {_fmt(data.get('ult_elo', 1000))}", 19, "#F3F1FC", 750)
    ustr, usize = _fit_text(
        str(data.get("ult_status") or "Пока нет сыгранных матчей"),
        620, 15, 500, min_size=11,
    )
    _txt(1208, 1012, ustr, usize, "#8D98AE", 500)

    # --- footer ---
    _line(60, 1033, 1860, 1033, "#29324B", 1)
    _txt(60, 1057, "r!z3  /  PLAYER PROFILE V2", 14, "#6F7991", 550, spacing=".6")
    _txt(1860, 1057, "1920×1080", 14, "#6F7991", 550, anchor="end")

    _add("</svg>")
    return "\n".join(_E)


# ---------------------------------------------------------------------------
# HTML + Playwright
# ---------------------------------------------------------------------------

_inter_css_cache: str | None = None


def _inter_css() -> str:
    """@font-face для Inter Variable (base64, кэшируется на процесс)."""
    global _inter_css_cache
    if _inter_css_cache is None:
        raw = INTER_FONT_PATH.read_bytes()
        b64 = base64.b64encode(raw).decode("ascii")
        _inter_css_cache = (
            "@font-face{font-family:'Inter';src:url(data:font/ttf;base64,"
            + b64
            + ");font-weight:100 900;font-style:normal;}"
        )
    return _inter_css_cache


def build_profile_html(svg: str) -> str:
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"UTF-8\">"
        "<style>html,body{margin:0;background:#080B14;width:100%;height:100%;}"
        "svg{display:block;width:1920px;height:1080px;}"
        "svg text{font-family:'Inter',Arial,sans-serif;}"
        + _inter_css()
        + "</style></head><body>"
        + svg
        + "</body></html>"
    )


_browser = None
_playwright = None
_browser_lock = asyncio.Lock()

# Chromium держим только пока он нужен: после IDLE_CLOSE_SECONDS
# без рендеров браузер закрывается — фоновый профиль-рендер редок,
# а спящий Chromium держит сотни МБ RSS. Playwright-драйвер
# остаётся — повторный launch ~1с, несравнимо дешевле постоянного
# RSS. Значение переопределяется env PROFILE_BROWSER_IDLE_S.
IDLE_CLOSE_SECONDS = float(
    os.environ.get("PROFILE_BROWSER_IDLE_S", "600")
)
_last_browser_used = 0.0
_idle_reaper_task: "asyncio.Task | None" = None


async def _close_browser() -> None:
    """Закрыть Chromium (Playwright-драйвер оставляем)."""
    global _browser
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        pass
    _browser = None


async def _idle_reaper() -> None:
    """Periodic check: закрыть Chromium после долгого простоя."""
    global _browser
    while True:
        try:
            await asyncio.sleep(60)
            if (
                _browser is not None
                and time.monotonic() - _last_browser_used
                > IDLE_CLOSE_SECONDS
            ):
                logger.info(
                    "Profile renderer: Chromium idle %.0fs — closing",
                    time.monotonic() - _last_browser_used,
                )
                await _close_browser()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


def _ensure_reaper() -> None:
    global _idle_reaper_task
    if _idle_reaper_task is None or _idle_reaper_task.done():
        try:
            _idle_reaper_task = asyncio.get_running_loop().create_task(
                _idle_reaper()
            )
            _idle_reaper_task.set_name("profile_browser_reaper")
        except Exception:
            _idle_reaper_task = None


async def _get_browser():
    """Переиспользуемый Chromium-инстанс на процесс."""
    global _browser, _playwright, _last_browser_used
    _last_browser_used = time.monotonic()
    _ensure_reaper()
    if _browser is not None and _browser.is_connected():
        return _browser
    async with _browser_lock:
        _last_browser_used = time.monotonic()
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "playwright is not installed — pip install playwright"
            ) from exc
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        logger.info("Profile renderer: Chromium launched")
        return _browser


async def shutdown_profile_renderer() -> None:
    """Закрыть браузер и reaper при остановке бота (best-effort)."""
    global _browser, _playwright, _idle_reaper_task
    try:
        if _idle_reaper_task is not None and not _idle_reaper_task.done():
            _idle_reaper_task.cancel()
    except Exception:
        pass
    _idle_reaper_task = None
    await _close_browser()
    try:
        if _playwright is not None:
            await _playwright.stop()
    except Exception:
        pass
    _browser = _playwright = None


async def render_profile_png_async(data: dict,
                                   avatar_png: bytes | None = None,
                                   badge_png: bytes | None = None) -> bytes:
    """Собрать SVG → HTML → скриншот Chromium 1920x1080 → PNG bytes.

    Сигнатура сохранена совместимой с прежним рендерером:
    data — словарь collect_profile_data, avatar/badge — PNG bytes.
    """
    import time

    async with CARD_RENDER_SEM:
        t0 = time.monotonic()
        svg = build_profile_svg(data, avatar_png, badge_png)
        html_doc = build_profile_html(svg)

        browser = await _get_browser()
        page = await browser.new_page(
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
        )
        try:
            await asyncio.wait_for(
                page.set_content(html_doc, wait_until="load"),
                timeout=RENDER_TIMEOUT,
            )
            # Дожидаемся шрифтов и декодирования data-URI изображений
            try:
                await page.evaluate(
                    "() => Promise.all(["
                    "document.fonts.ready,"
                    "...Array.from(document.images).map(i => i.decode().catch(() => {}))"
                    "]).then(() => true)"
                )
            except Exception:
                await page.wait_for_timeout(150)
            png = await asyncio.wait_for(
                page.screenshot(type="png"), timeout=RENDER_TIMEOUT
            )
        finally:
            try:
                await page.close()
            except Exception:
                pass

        render_ms = (time.monotonic() - t0) * 1000
        try:
            from utils import perf_metrics

            perf_metrics.record("profile_render_ms", render_ms)
            perf_metrics.incr("profile_renders")
        except Exception:
            pass
        logger.info(
            "Profile PNG rendered in %.0fms | renderer=%s v%s module=%s",
            render_ms,
            Path(__file__).name, "svg-v2", Path(__file__).resolve(),
        )
        return png


# Синхронная точка входа для executor-вызовов/тестов не нужна:
# весь пайплайн асинхронный (SVG-сборка синхронная и быстрая).
RENDERER_VERSION = "svg-v2"
