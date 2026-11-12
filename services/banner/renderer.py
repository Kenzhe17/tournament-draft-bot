"""Рендер PNG баннера сервера: шаблон + Playwright/Chromium.

База — assets_ds_server/server_banner/template_clean.png (утверждённый
дизайн с вычищенными динамическими зонами). Поверх — аватар в кольце
и текст участника дня. Шрифт Inter Variable (base64 @font-face),
как в профиле.

Дизайн — новый шаблон 1672x941 («Neon Purple Temple Participant
Banner»): лого, рамка карточки и лейблы запечены в template_clean.png,
динамические зоны (аватар, ник, подзаголовок, стата) отрисовываются
поверх. Шрифт Inter Variable (base64 @font-face), как в профиле.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets_ds_server" / "server_banner"
TEMPLATE_PATH = ASSETS_DIR / "template_clean.png"
FALLBACK_LOGO_PATH = ASSETS_DIR / "logo.png"
LOGO_PATH = ASSETS_DIR.parent / "logo+banner" / "logo.png"

# viewport / template — оба 1672x941 (новый дизайн «Neon Purple
# Temple Participant Banner»), масштаб 1:1
VW, VH = 1672, 941
TW, TH = 1672, 941
SX = VW / TW
SY = VH / TH

# Динамические зоны в координатах шаблона 1672x941 (OCR-разметка
# примера + HoughCircles для аватара). Статичный дизайн (лого, рамка
# карточки, лейблы) запечён в template_clean.png.
AVATAR_CX, AVATAR_CY, AVATAR_R = 908, 536, 45   # внутри неонового кольца r~58
NICK_X, NICK_TOP = 992, 480                      # верх текста ника
NICK_FONT = 44
NICK_MAX_W = 500                                  # до правого края карточки
SUBTITLE_X, SUBTITLE_TOP, SUBTITLE_FONT = 998, 542, 27
VOICE_X, VOICE_TOP, STAT_FONT = 920, 652, 42
MATCHES_X, MATCHES_TOP = 1232, 634

RENDER_TIMEOUT = 20
RENDER_SEM = asyncio.Semaphore(2)

_template_b64_cache: str | None = None
_logo_b64_cache: str | None = None
_inter_css_cache: str | None = None


def _b64_file(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def _template_b64() -> str:
    global _template_b64_cache
    if _template_b64_cache is None:
        _template_b64_cache = _b64_file(TEMPLATE_PATH)
    return _template_b64_cache


def _logo_b64() -> str:
    """Логотип для пустого состояния; None если файла нет."""
    global _logo_b64_cache
    if _logo_b64_cache is None:
        for p in (FALLBACK_LOGO_PATH, LOGO_PATH):  # локальный logo.png в приоритете
            if p.exists():
                try:
                    _logo_b64_cache = _b64_file(p)
                    return _logo_b64_cache
                except Exception:
                    continue
        _logo_b64_cache = ""
    return _logo_b64_cache or None


def _inter_css() -> str:
    """Переиспользуем @font-face Inter из profile_renderer (кэш общий)."""
    global _inter_css_cache
    if _inter_css_cache is None:
        from utils.profile_renderer import _inter_css as _ic
        _inter_css_cache = _ic()
    return _inter_css_cache


def _fit_text(text: str, font_size: int, max_w: int) -> tuple[str, int]:
    """Ужать текст под max_w (в координатах шаблона) через Inter."""
    from PIL import ImageFont
    from utils.profile_renderer import INTER_FONT_PATH

    def width(s: str, size: int) -> int:
        f = ImageFont.truetype(str(INTER_FONT_PATH), size)
        return int(f.getlength(s))

    size = font_size
    if width(text, size) <= max_w:
        return text, size
    # сначала сокращаем кегль, потом многоточие
    while size > 24 and width(text, size) > max_w:
        size -= 2
    if width(text, size) > max_w:
        ell = "…"
        while size > 20 and width(text + ell, size) > max_w:
            text = text[:-1]
            size -= 0
        while size > 20 and width(text + ell, size) > max_w:
            text = text[:-1]
        text = text + ell
        while size > 20 and width(text, size) > max_w:
            size -= 2
    return text, size


def _plural_matches(n: int) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 14:
        return "матчей"
    n %= 10
    if n == 1:
        return "матч"
    if 2 <= n <= 4:
        return "матча"
    return "матчей"


def _format_voice(seconds: int) -> str:
    """'4 ч 27 мин' / '35 мин' / '6 ч'."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h and m:
        return f"{h} ч {m} мин"
    if h:
        return f"{h} ч"
    if m:
        return f"{m} мин"
    return "0 мин"


def _px(x: float) -> float:
    return round(x * SX, 2)


def _py(y: float) -> float:
    return round(y * SY, 2)


def build_banner_html(
    *,
    nickname: str | None,
    avatar_png: bytes | None,
    voice_seconds: int = 0,
    matches: int = 0,
    has_activity: bool = True,
    report_day_label: str | None = None,
) -> str:
    """Собрать HTML страницы баннера 1920x1080."""
    parts: list[str] = []
    a = parts.append

    a("<!doctype html><html lang='ru'><head><meta charset='UTF-8'><style>")
    a(f"html,body{{margin:0;padding:0;width:{VW}px;height:{VH}px;overflow:hidden;"
      "background:#0B0E19;font-family:'Inter',Arial,sans-serif;}")
    a(f".stage{{position:relative;width:{VW}px;height:{VH}px;}}")
    a(f".bg{{position:absolute;inset:0;width:{VW}px;height:{VH}px;"
      "object-fit:fill;display:block;}")
    a(".avatar{position:absolute;border-radius:50%;object-fit:cover;"
      "box-shadow:0 0 0 3px rgba(168,85,247,.55),0 6px 24px rgba(0,0,0,.5);"
      "background:#171226;}")
    a(".avatar-empty{position:absolute;border-radius:50%;overflow:hidden;"
      "background:radial-gradient(circle at 35% 30%,#3B2160,#150B26 70%);"
      "display:flex;align-items:center;justify-content:center;}")
    a(".avatar-empty img{width:58%;height:58%;object-fit:contain;opacity:.9;}")
    a(".nick{position:absolute;color:#fff;font-weight:800;white-space:nowrap;"
      "letter-spacing:.5px;text-shadow:0 2px 10px rgba(0,0,0,.65);"
      "line-height:1.05;}")
    a(".subtitle{position:absolute;color:#E9DDF7;font-weight:600;white-space:nowrap;"
      "letter-spacing:.3px;text-shadow:0 1px 6px rgba(0,0,0,.6);}")
    a(".stat{position:absolute;color:#fff;font-weight:800;white-space:nowrap;"
      "letter-spacing:.5px;text-shadow:0 2px 8px rgba(0,0,0,.6);}")
    a(_inter_css())
    a("</style></head><body><div class='stage'>")

    # фон — чистый шаблон
    a(f"<img class='bg' src='{_template_b64()}' alt=''>")

    # аватар (внутри кольца r=68, кольцо шаблона r~76 остаётся)
    ax, ay = _px(AVATAR_CX), _py(AVATAR_CY)
    d = _px(AVATAR_R * 2)
    if has_activity:
        if avatar_png:
            import base64 as _b64
            a64 = "data:image/png;base64," + _b64.b64encode(avatar_png).decode()
            a(f"<img class='avatar' src='{a64}' "
              f"style='left:{ax - d/2}px;top:{ay - d/2}px;width:{d}px;height:{d}px;'>")
        else:
            # Discord default avatar — серый круг с инициалами нет; нейтральный
            logo = _logo_b64()
            inner = f"<img src='{logo}'>" if logo else ""
            a(f"<div class='avatar-empty' "
              f"style='left:{ax - d/2}px;top:{ay - d/2}px;width:{d}px;height:{d}px;'>"
              f"{inner}</div>")
    else:
        logo = _logo_b64()
        inner = f"<img src='{logo}'>" if logo else ""
        a(f"<div class='avatar-empty' "
          f"style='left:{ax - d/2}px;top:{ay - d/2}px;width:{d}px;height:{d}px;'>"
          f"{inner}</div>")

    # ник / пустое состояние
    if has_activity and nickname:
        nick, size = _fit_text(nickname, NICK_FONT, NICK_MAX_W)
        a(f"<div class='nick' style='left:{_px(NICK_X)}px;top:{_py(NICK_TOP)}px;"
          f"font-size:{round(size*SY)}px;'>{_esc(nick)}</div>")
        a(f"<div class='subtitle' style='left:{_px(SUBTITLE_X)}px;top:{_py(SUBTITLE_TOP)}px;"
          f"font-size:{round(SUBTITLE_FONT*SY)}px;'>Самый активный за прошедший день</div>")
    else:
        a(f"<div class='nick' style='left:{_px(NICK_X)}px;top:{_py(NICK_TOP)}px;"
          f"font-size:{round(40*SY)}px;color:#C9B8E8;'>Участников не было</div>")
        a(f"<div class='subtitle' style='left:{_px(SUBTITLE_X)}px;top:{_py(SUBTITLE_TOP)}px;"
          f"font-size:{round(SUBTITLE_FONT*SY)}px;'>Вчера активных участников не было</div>")

    # статистика
    if has_activity:
        voice_txt = _format_voice(voice_seconds)
        matches_txt = f"{matches} {_plural_matches(matches)}"
        a(f"<div class='stat' style='left:{_px(VOICE_X)}px;top:{_py(VOICE_TOP)}px;"
          f"font-size:{round(STAT_FONT*SY)}px;'>{_esc(voice_txt)}</div>")
        a(f"<div class='stat' style='left:{_px(MATCHES_X)}px;top:{_py(MATCHES_TOP)}px;"
          f"font-size:{round(STAT_FONT*SY)}px;'>{_esc(matches_txt)}</div>")
    else:
        a(f"<div class='stat' style='left:{_px(VOICE_X)}px;top:{_py(VOICE_TOP)}px;"
          f"font-size:{round(STAT_FONT*SY)}px;color:#8F7FB0;'>—</div>")
        a(f"<div class='stat' style='left:{_px(MATCHES_X)}px;top:{_py(MATCHES_TOP)}px;"
          f"font-size:{round(STAT_FONT*SY)}px;color:#8F7FB0;'>—</div>")

    if report_day_label:
        a(f"<div class='subtitle' style='right:50px;bottom:36px;font-size:18px;"
          f"color:#7E6C9C;letter-spacing:1px;'>{_esc(report_day_label)}</div>")

    a("</div></body></html>")
    return "".join(parts)


def _esc(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
         .replace('"', "&quot;")
    )


async def render_banner_png(
    *,
    nickname: str | None,
    avatar_png: bytes | None,
    voice_seconds: int = 0,
    matches: int = 0,
    has_activity: bool = True,
    report_day_label: str | None = None,
) -> bytes:
    """HTML → Chromium screenshot 1920x1080 → PNG bytes."""
    async with RENDER_SEM:
        html_doc = build_banner_html(
            nickname=nickname,
            avatar_png=avatar_png,
            voice_seconds=voice_seconds,
            matches=matches,
            has_activity=has_activity,
            report_day_label=report_day_label,
        )
        from utils.profile_renderer import _get_browser
        browser = await _get_browser()
        page = await browser.new_page(
            viewport={"width": VW, "height": VH}, device_scale_factor=1
        )
        try:
            await asyncio.wait_for(
                page.set_content(html_doc, wait_until="load"),
                timeout=RENDER_TIMEOUT,
            )
            try:
                await page.evaluate(
                    "() => Promise.all([document.fonts.ready,"
                    "...Array.from(document.images).map(i => i.decode().catch(() => {}))"
                    "]).then(() => true)"
                )
            except Exception:
                await page.wait_for_timeout(150)
            return await asyncio.wait_for(
                page.screenshot(type="png"), timeout=RENDER_TIMEOUT
            )
        finally:
            try:
                await page.close()
            except Exception:
                pass
