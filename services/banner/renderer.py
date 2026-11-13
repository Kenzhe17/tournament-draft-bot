"""Рендер PNG баннера сервера: шаблон + динамические элементы.

Два backend'а (PNG_RENDER_BACKEND):

- ``native``/``auto`` — Pillow поверх template_clean.png: без
  браузера, ~10–30 МБ RSS, миллисекунды;
- ``chromium`` — Playwright/Chromium + HTML/CSS (legacy).

База — assets_ds_server/server_banner/template_clean.png (утверждённый
дизайн с вычищенными динамическими зонами). Поверх — аватар в кольце
и текст участника дня. Шрифт Inter Variable.

Дизайн — новый шаблон 1672x941 («Neon Purple Temple Participant
Banner»): лого, рамка карточки и лейблы запечены в template_clean.png,
динамические зоны (аватар, ник, подзаголовок, стата) отрисовываются
поверх.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import io
import logging
import math
import os
from pathlib import Path

from PIL import Image

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
VOICE_X, VOICE_TOP, STAT_FONT = 920, 647, 40
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


# ---------------------------------------------------------------------------
# Native backend: Pillow поверх template_clean.png
# ---------------------------------------------------------------------------

_NATIVE_SEM = asyncio.Semaphore(2)
_template_img_cache = None
_avatar_empty_bg_cache = None
_logo_img_cache = None


def _load_template():
    global _template_img_cache
    if _template_img_cache is None:
        _template_img_cache = Image.open(TEMPLATE_PATH).convert("RGBA")
    return _template_img_cache.copy()


def _avatar_empty_bg(size: int):
    """Радиальный градиент пустого аватара (#3B2160 → #150B26,
    центр смещён в 35%/30% — как radial-gradient(circle at 35% 30%))."""
    global _avatar_empty_bg_cache
    if _avatar_empty_bg_cache is None or _avatar_empty_bg_cache.size[0] != size:
        import numpy as np

        c = np.array([59, 33, 96], dtype=np.float32)    # #3B2160
        e = np.array([21, 11, 38], dtype=np.float32)    # #150B26
        yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
        cx, cy = size * 0.35, size * 0.30
        d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
        t = np.clip(d / (size * 0.72), 0, 1)[:, :, None]
        rgb = c[None, None, :] * (1 - t) + e[None, None, :] * t
        _avatar_empty_bg_cache = Image.fromarray(
            np.dstack([rgb, np.full((size, size, 1), 255, np.float32)])
            .astype(np.uint8), "RGBA")
    return _avatar_empty_bg_cache


def _load_logo():
    global _logo_img_cache
    if _logo_img_cache is None:
        for p in (FALLBACK_LOGO_PATH, LOGO_PATH):
            if p.exists():
                try:
                    _logo_img_cache = Image.open(p).convert("RGBA")
                    break
                except Exception:
                    continue
        if _logo_img_cache is None:
            _logo_img_cache = False
    return _logo_img_cache or None


def _pil_font(size: int, weight: int):
    from utils.profile_renderer import _pil_font as _pf

    return _pf(size, weight)


def _tracked_width(draw, text: str, font, ls: float) -> float:
    if not text:
        return 0.0
    w = sum(font.getlength(ch) for ch in text)
    return w + ls * max(0, len(text) - 1)


def _draw_tracked(draw, x: float, baseline_y: float, text: str,
                  font, fill, ls: float = 0.0) -> None:
    """Текст с letter-spacing (PIL не умеет ls нативно)."""
    cx = x
    for ch in text:
        draw.text((cx, baseline_y), ch, font=font, fill=fill,
                  anchor="ls")
        cx += font.getlength(ch) + ls


def _baseline_y(top: float, font, line_height_factor: float | None) -> float:
    """CSS top → baseline. line-height: factor*size (None = normal≈em)."""
    size = font.size
    asc, desc = font.getmetrics()
    em = asc + desc
    if line_height_factor is None:
        lh = em
    else:
        lh = line_height_factor * size
    half = (lh - em) / 2.0
    return top + half + asc


def _draw_banner_text(img, *, x: float, top: float, text: str,
                      size: int, weight: int, fill,
                      ls: float = 0.0, line_height: float | None = None,
                      shadow=(0, 2, 10, 0.65)) -> None:
    """Ник/подпись/стата с text-shadow как в CSS.

    shadow=(dx, dy, blur_px, alpha). blur — PIL GaussianBlur radius
    (CSS blur≈2*radius — задаём radius=blur/2).
    """
    from PIL import ImageDraw, ImageFilter

    font = _pil_font(size, weight)
    asc, desc = font.getmetrics()
    em = asc + desc
    lh = (line_height * size) if line_height else em
    baseline = top + (lh - em) / 2.0 + asc

    if shadow:
        dx, dy, blur, alpha = shadow
        # blur-слой только под текст (не весь кадр) — в разы дешевле.
        # Слой покрывает bbox текста + pad; композитим со сдвигом.
        tw = _tracked_width(ImageDraw.Draw(img), text, font, ls)
        pad = int(blur + max(abs(dx), abs(dy)) + 6)
        asc, desc = font.getmetrics()
        em = asc + desc
        lw = int(tw + pad * 2 + 8)
        lh = int(em + pad * 2 + 8)
        layer = Image.new("RGBA", (max(1, lw), max(1, lh)), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        # baseline слоя: asc + dy + pad (x_local = dx + pad)
        _draw_tracked(ld, dx + pad, asc + dy + pad, text, font,
                      (0, 0, 0, int(255 * alpha)), ls)
        if blur:
            layer = layer.filter(ImageFilter.GaussianBlur(blur / 2.0))
        # origin слоя на канве: (x - pad, baseline - asc - pad)
        img.alpha_composite(
            layer, (int(round(x)) - pad, int(round(baseline)) - asc - pad))

    d = ImageDraw.Draw(img)
    _draw_tracked(d, x, baseline, text, font, fill, ls)


def _draw_avatar(img, avatar_png: bytes | None, has_activity: bool) -> None:
    """Круглый аватар r=45 в (908,536) + неоновое кольцо и тень."""
    from PIL import ImageDraw, ImageFilter

    cx, cy, r = AVATAR_CX, AVATAR_CY, AVATAR_R
    d = r * 2
    x0, y0 = cx - r, cy - r

    # drop-shadow 0 6px 24px rgba(0,0,0,.5) — blur-слой только под аватаром
    pad = 40
    lx0, ly0 = int(x0) - pad, int(y0) - pad + 6
    lw = d + pad * 2
    lh = d + pad * 2
    shadow = Image.new("RGBA", (lw, lh), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.ellipse((pad, pad, pad + d, pad + d), fill=(0, 0, 0, 128))
    shadow = shadow.filter(ImageFilter.GaussianBlur(12))
    img.alpha_composite(shadow, (lx0, ly0))

    # кольцо 0 0 0 3px rgba(168,85,247,.55)
    ring = Image.new("RGBA", img.size, (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((x0 - 1, y0 - 1, x0 + d + 1, y0 + d + 1),
               outline=(168, 85, 247, 140), width=4)
    img.alpha_composite(ring)

    if has_activity and avatar_png:
        try:
            av = Image.open(io.BytesIO(avatar_png)).convert("RGBA")
        except Exception:
            av = None
        if av is not None:
            # object-fit: cover → заполнить квадрат, центр-кроп
            scale = max(d / av.width, d / av.height)
            nw, nh = int(av.width * scale + 0.5), int(av.height * scale + 0.5)
            av = av.resize((nw, nh), Image.LANCZOS)
            ox = (nw - d) // 2
            oy = (nh - d) // 2
            av = av.crop((ox, oy, ox + d, oy + d))
            mask = Image.new("L", (d, d), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse((0, 0, d - 1, d - 1), fill=255)
            img.paste(av, (int(x0), int(y0)), mask)
            return

    # пустое состояние: градиент + логотип 58%
    bg = _avatar_empty_bg(d + 4).resize((d, d), Image.LANCZOS)
    img.paste(bg, (int(x0), int(y0)))
    logo = _load_logo()
    if logo is not None:
        lw = int(d * 0.58)
        lh = int(logo.height * lw / logo.width)
        lg = logo.resize((lw, lh), Image.LANCZOS)
        img.alpha_composite(
            lg, (int(cx - lw / 2), int(cy - lh / 2)))


def _render_banner_native(
    *,
    nickname: str | None,
    avatar_png: bytes | None,
    voice_seconds: int = 0,
    matches: int = 0,
    has_activity: bool = True,
    report_day_label: str | None = None,
) -> bytes:
    """Pillow: template + аватар + текст → PNG 1672x941."""
    from PIL import ImageDraw

    img = _load_template()
    _draw_avatar(img, avatar_png, has_activity)

    nick_x, nick_top = _px(NICK_X), _py(NICK_TOP)
    sub_x, sub_top = _px(SUBTITLE_X), _py(SUBTITLE_TOP)

    if has_activity and nickname:
        nick, size = _fit_text(nickname, NICK_FONT, NICK_MAX_W)
        _draw_banner_text(
            img, x=nick_x, top=nick_top, text=nick, size=size,
            weight=800, fill=(255, 255, 255), ls=0.5,
            line_height=1.05, shadow=(0, 2, 10, 0.65))
        _draw_banner_text(
            img, x=sub_x, top=sub_top,
            text="Самый активный за прошедший день",
            size=SUBTITLE_FONT, weight=600, fill=(233, 221, 247),
            ls=0.3, shadow=(0, 1, 6, 0.6))
    else:
        _draw_banner_text(
            img, x=nick_x, top=nick_top, text="Участников не было",
            size=40, weight=800, fill=(201, 184, 232), ls=0.5,
            line_height=1.05, shadow=(0, 2, 10, 0.65))
        _draw_banner_text(
            img, x=sub_x, top=sub_top,
            text="Вчера активных участников не было",
            size=SUBTITLE_FONT, weight=600, fill=(233, 221, 247),
            ls=0.3, shadow=(0, 1, 6, 0.6))

    if has_activity:
        voice_txt = _format_voice(voice_seconds)
        matches_txt = f"{matches} {_plural_matches(matches)}"
        stat_fill = (255, 255, 255)
    else:
        voice_txt = matches_txt = "—"
        stat_fill = (143, 127, 176)
    _draw_banner_text(
        img, x=_px(VOICE_X), top=_py(VOICE_TOP), text=voice_txt,
        size=STAT_FONT, weight=800, fill=stat_fill, ls=0.5,
        shadow=(0, 2, 8, 0.6))
    _draw_banner_text(
        img, x=_px(MATCHES_X), top=_py(MATCHES_TOP), text=matches_txt,
        size=STAT_FONT, weight=800, fill=stat_fill, ls=0.5,
        shadow=(0, 2, 8, 0.6))

    if report_day_label:
        font = _pil_font(18, 600)
        from PIL import Image, ImageFilter
        _d = ImageDraw.Draw(img)
        w = _tracked_width(_d, report_day_label, font, 1.0)
        asc, desc = font.getmetrics()
        baseline = VH - 36 - desc
        x = VW - 50 - w
        # text-shadow subtitle 0 1px 6px — blur-слой только под текст
        pad = 12
        lw = int(w + pad * 2 + 8)
        lh = int(asc + desc + pad * 2 + 8)
        layer = Image.new("RGBA", (max(1, lw), max(1, lh)), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        _draw_tracked(ld, pad, asc + 1 + pad, report_day_label, font,
                      (0, 0, 0, 153), 1.0)
        layer = layer.filter(ImageFilter.GaussianBlur(3))
        img.alpha_composite(layer, (int(x) - pad, baseline - asc - pad))
        _draw_tracked(_d, x, baseline, report_day_label,
                      font, (126, 108, 156), 1.0)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG")
    return out.getvalue()


def _banner_backend() -> str:
    return os.environ.get("PNG_RENDER_BACKEND", "auto").strip().lower()


async def render_banner_png(
    *,
    nickname: str | None,
    avatar_png: bytes | None,
    voice_seconds: int = 0,
    matches: int = 0,
    has_activity: bool = True,
    report_day_label: str | None = None,
) -> bytes:
    """PNG баннера 1672x941. native → Pillow; chromium → Playwright."""
    backend = _banner_backend()
    if backend != "chromium":
        async with _NATIVE_SEM:
            try:
                import time as _time
                _t0 = _time.monotonic()
                loop = asyncio.get_running_loop()
                png = await loop.run_in_executor(
                    None, functools.partial(
                        _render_banner_native,
                        nickname=nickname, avatar_png=avatar_png,
                        voice_seconds=voice_seconds, matches=matches,
                        has_activity=has_activity,
                        report_day_label=report_day_label))
                try:
                    from utils import perf_metrics
                    perf_metrics.record(
                        "banner_render_native_ms",
                        (_time.monotonic() - _t0) * 1000)
                    perf_metrics.incr("banner_renders_native")
                except Exception:
                    pass
                return png
            except Exception:
                if backend == "native":
                    raise
                logger.warning(
                    "Native banner render failed — falling back to "
                    "chromium", exc_info=True)
                try:
                    from utils import perf_metrics
                    perf_metrics.incr("png_render_fallbacks")
                except Exception:
                    pass
    return await _render_banner_png_chromium(
        nickname=nickname, avatar_png=avatar_png,
        voice_seconds=voice_seconds, matches=matches,
        has_activity=has_activity, report_day_label=report_day_label)


async def _render_banner_png_chromium(
    *,
    nickname: str | None,
    avatar_png: bytes | None,
    voice_seconds: int = 0,
    matches: int = 0,
    has_activity: bool = True,
    report_day_label: str | None = None,
) -> bytes:
    """HTML → Chromium screenshot 1672x941 → PNG bytes (legacy)."""
    async with RENDER_SEM:
        html_doc = build_banner_html(
            nickname=nickname,
            avatar_png=avatar_png,
            voice_seconds=voice_seconds,
            matches=matches,
            has_activity=has_activity,
            report_day_label=report_day_label,
        )
        from utils import profile_renderer as _pr

        _pr._render_started()
        try:
            browser = await _pr._get_browser()
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
        finally:
            _pr._render_finished()
