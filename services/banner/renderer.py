"""Рендер PNG баннера сервера: шаблон + динамические элементы (Pillow).

Без браузера и без Chromium: берём утверждённый шаблон
assets_ds_server/server_banner/template_clean.png (1672x941, лого,
рамка карточки и лейблы уже запечены) и рисуем поверх динамические
зоны — аватар участника дня, Discord-ник, время голосовой активности,
количество подтверждённых матчей и дату отчёта.

Рендер CPU-bound и идёт в отдельном ThreadPoolExecutor, чтобы не
блокировать event loop. Одновременных рендеров ограничено admission-
счётчиком (PNG_RENDER_MAX_INFLIGHT), пик RSS сэмплируется во время
рендера для perf_metrics.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets_ds_server"
TEMPLATE_PATH = ASSETS_DIR / "server_banner" / "template_clean.png"
FALLBACK_LOGO_PATH = ASSETS_DIR / "server_banner" / "logo.png"
LOGO_PATH = ASSETS_DIR / "logo+banner" / "logo.png"
FONTS_DIR = ASSETS_DIR / "fonts"
INTER_FONT_PATH = FONTS_DIR / "Inter-Variable.ttf"

# viewport / template — оба 1672x941, масштаб 1:1
VW, VH = 1672, 941
TW, TH = 1672, 941
SX = VW / TW
SY = VH / TH

# Динамические зоны в координатах шаблона 1672x941.
AVATAR_CX, AVATAR_CY, AVATAR_R = 908, 536, 45   # внутри неонового кольца r~58
NICK_X, NICK_TOP = 992, 480                      # верх текста ника
NICK_FONT = 44
NICK_MAX_W = 500                                  # до правого края карточки
SUBTITLE_X, SUBTITLE_TOP, SUBTITLE_FONT = 998, 542, 27
VOICE_X, VOICE_TOP, STAT_FONT = 920, 647, 40
MATCHES_X, MATCHES_TOP = 1232, 634

# ---------------------------------------------------------------------------
# Нативный PNG-рендер: общий executor + bounded admission
# ---------------------------------------------------------------------------

PNG_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.environ.get("PNG_RENDER_WORKERS", "2")),
    thread_name_prefix="banner-png")

_PNG_INFLIGHT = 0
_PNG_INFLIGHT_LOCK = threading.Lock()
_PNG_MAX_INFLIGHT = int(os.environ.get("PNG_RENDER_MAX_INFLIGHT", "8"))

_PNG_PEAK_RSS_MB = 0.0
_RSS_SAMPLE_INTERVAL = 0.01


def png_render_inflight() -> int:
    return _PNG_INFLIGHT


def png_render_peak_rss_mb() -> float:
    return _PNG_PEAK_RSS_MB


def _png_try_acquire() -> bool:
    """Атомарный захват слота. Вызывать только из event loop."""
    global _PNG_INFLIGHT
    with _PNG_INFLIGHT_LOCK:
        if _PNG_INFLIGHT >= _PNG_MAX_INFLIGHT:
            return False
        _PNG_INFLIGHT += 1
        return True


def _png_release() -> None:
    global _PNG_INFLIGHT
    with _PNG_INFLIGHT_LOCK:
        _PNG_INFLIGHT = max(0, _PNG_INFLIGHT - 1)


class _PngQueueFull(RuntimeError):
    """PNG render queue is full — load-shedding, не ошибка рендера."""


def _tree_rss_mb() -> float:
    """RSS процесса + всех дочерних (у banner-рендера детей нет,
    но дёшево и единообразно с остальными PNG-рендерами)."""
    try:
        import psutil

        proc = psutil.Process()
        total = proc.memory_info().rss
        for ch in proc.children(recursive=True):
            try:
                total += ch.memory_info().rss
            except Exception:
                pass
        return total / 1048576
    except Exception:
        return 0.0


def _start_rss_monitor():
    """Фоновый сэмплер RSS на время нативного рендера (~10мс).

    Пик фиксируется сэмплами — ловит промежуточные буферы Pillow,
    которые пост-рендерный замер пропускает. Возвращает stop(),
    привязанный к завершению future (не к жизни asyncio-задачи).
    """
    stop = threading.Event()

    def _loop():
        global _PNG_PEAK_RSS_MB
        while not stop.is_set():
            mb = _tree_rss_mb()
            if mb > _PNG_PEAK_RSS_MB:
                _PNG_PEAK_RSS_MB = mb
            stop.wait(_RSS_SAMPLE_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True,
                         name="banner-rss-monitor")
    t.start()

    def _stop() -> None:
        stop.set()
        t.join(timeout=0.5)

    return _stop


async def _png_run_native(fn, /, *args, **kwargs):
    """Admitted нативный PNG-рендер в PNG_EXECUTOR.

    Слот admission и RSS-монитор привязаны к done_callback
    concurrent.futures.Future: отмена asyncio-ожидания не
    освобождает слот, пока поток реально работает.
    """
    if not _png_try_acquire():
        raise _PngQueueFull("PNG render queue is full")
    import functools

    call = functools.partial(fn, *args, **kwargs)
    stop_mon = _start_rss_monitor()
    try:
        cf = PNG_EXECUTOR.submit(call)
    except Exception:
        stop_mon()
        _png_release()
        raise

    def _done(_f):
        stop_mon()
        _png_release()

    cf.add_done_callback(_done)
    return await asyncio.wrap_future(cf)


# ---------------------------------------------------------------------------
# Шрифт Inter Variable (PIL)
# ---------------------------------------------------------------------------

_font_cache: dict[tuple[int, int], object] = {}


def _pil_font(size: int, weight: int = 700):
    key = (size, weight)
    if key not in _font_cache:
        from PIL import ImageFont

        f = ImageFont.truetype(str(INTER_FONT_PATH), size)
        try:
            # Inter-Variable имеет ДВА вариационных шрифта:
            # 'Optical size' (14–32) и 'Weight' (100–900).
            # set_variation_by_axes([weight]) ставил бы только ПЕРВУЮ
            # ось (opsz) и оставлял wght=400 — текст был бы regular.
            coords = []
            for ax in f.get_variation_axes():
                name = ax["name"]
                name = name.decode() if isinstance(name, bytes) else name
                low = name.lower()
                if "weight" in low:
                    coords.append(float(weight))
                elif "optical" in low:
                    # font-optical-sizing: auto ≈ кегль, clamp в ось
                    coords.append(min(32.0, max(14.0, float(size))))
                else:
                    coords.append(float(ax["default"]))
            f.set_variation_by_axes(coords)
        except Exception:
            pass
        _font_cache[key] = f
    return _font_cache[key]


# ---------------------------------------------------------------------------
# Текстовые helpers
# ---------------------------------------------------------------------------

def _fit_text(text: str, font_size: int, max_w: int) -> tuple[str, int]:
    """Ужать текст под max_w (пиксели шаблона) через Inter."""
    def width(s: str, size: int) -> int:
        f = _pil_font(size, 800)
        return int(f.getlength(s))

    size = font_size
    if width(text, size) <= max_w:
        return text, size
    # сначала сокращаем кегль, потом многоточие
    while size > 24 and width(text, size) > max_w:
        size -= 2
    if width(text, size) > max_w:
        ell = "…"
        text = text.rstrip()
        while size > 20 and width(text + ell, size) > max_w:
            if not text:
                break
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


# ---------------------------------------------------------------------------
# Pillow-рендер
# ---------------------------------------------------------------------------

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
    центр в (35%,30%), затухание к 0.72·size) — чистый PIL,
    без numpy. Кэшируется на весь процесс."""
    global _avatar_empty_bg_cache
    if _avatar_empty_bg_cache is not None and \
            _avatar_empty_bg_cache.size[0] == size:
        return _avatar_empty_bg_cache

    from PIL import ImageDraw

    c1 = (59, 33, 96)     # #3B2160
    c2 = (21, 11, 38)     # #150B26
    cx, cy = size * 0.35, size * 0.30
    maxd = size * 0.72
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    steps = max(64, size * 2)
    # от края к центру: меньшие эллипсы перезаписывают центр
    for i in range(steps, -1, -1):
        t = i / steps
        r = t * maxd
        col = tuple(int(c1[k] * (1 - t) + c2[k] * t) for k in range(3))
        d.ellipse((cx - r, cy - r, cx + r, cy + r),
                  fill=col + (255,))
    _avatar_empty_bg_cache = img
    return img


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


def _draw_banner_text(img, *, x: float, top: float, text: str,
                      size: int, weight: int, fill,
                      ls: float = 0.0, line_height: float | None = None,
                      shadow=(0, 2, 10, 0.65)) -> None:
    """Ник/подпись/стата с text-shadow как в CSS.

    shadow=(dx, dy, blur_px, alpha). blur — PIL GaussianBlur radius
    (CSS blur≈2*radius — задаём radius=blur/2). Blur-слой строится
    только под bbox текста, а не на весь кадр.
    """
    from PIL import ImageDraw, ImageFilter

    font = _pil_font(size, weight)
    asc, desc = font.getmetrics()
    em = asc + desc
    lh = (line_height * size) if line_height else em
    baseline = top + (lh - em) / 2.0 + asc

    if shadow:
        dx, dy, blur, alpha = shadow
        tw = _tracked_width(ImageDraw.Draw(img), text, font, ls)
        pad = int(blur + max(abs(dx), abs(dy)) + 6)
        lw = int(tw + pad * 2 + 8)
        lh_i = int(em + pad * 2 + 8)
        layer = Image.new("RGBA", (max(1, lw), max(1, lh_i)), (0, 0, 0, 0))
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
            # Круглая маска + сохранение альфы аватара: прозрачные
            # области PNG-аватара показывают шаблон, а не чёрное.
            mask = Image.new("L", (d, d), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse((0, 0, d - 1, d - 1), fill=255)
            av_layer = Image.composite(
                av, Image.new("RGBA", (d, d), (0, 0, 0, 0)), mask)
            img.alpha_composite(av_layer, (int(x0), int(y0)))
            return

    # пустое состояние: градиент + логотип 58%
    bg = _avatar_empty_bg(d + 4).resize((d, d), Image.LANCZOS)
    img.paste(bg, (int(x0), int(y0)))
    logo = _load_logo()
    if logo is not None:
        # CSS: width:58%;height:58%;object-fit:contain;opacity:.9
        box = d * 0.58
        scale = min(box / logo.width, box / logo.height)
        lw, lh = int(logo.width * scale + 0.5), int(logo.height * scale + 0.5)
        lg = logo.resize((max(1, lw), max(1, lh)), Image.LANCZOS)
        lg.putalpha(lg.getchannel("A").point(lambda v: int(v * 0.9)))
        img.alpha_composite(
            lg, (int(cx - lg.width / 2), int(cy - lg.height / 2)))


def _render_banner_native(
    *,
    nickname: str | None,
    avatar_png: bytes | None,
    voice_seconds: int = 0,
    matches: int = 0,
    has_activity: bool = True,
    report_day_label: str | None = None,
) -> bytes:
    """Pillow: template + аватар + текст → PNG 1672x941. Синхронно."""
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
        _d = ImageDraw.Draw(img)
        w = _tracked_width(_d, report_day_label, font, 1.0)
        asc, desc = font.getmetrics()
        baseline = VH - 36 - desc
        x = VW - 50 - w
        # text-shadow subtitle 0 1px 6px — blur-слой только под текст
        from PIL import ImageFilter

        pad = 12
        lw = int(w + pad * 2 + 8)
        lh_i = int(asc + desc + pad * 2 + 8)
        layer = Image.new("RGBA", (max(1, lw), max(1, lh_i)), (0, 0, 0, 0))
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


async def render_banner_png(
    *,
    nickname: str | None,
    avatar_png: bytes | None,
    voice_seconds: int = 0,
    matches: int = 0,
    has_activity: bool = True,
    report_day_label: str | None = None,
) -> bytes:
    """PNG баннера 1672x941. Только Pillow — без браузера."""
    try:
        import time as _time
        _t0 = _time.monotonic()
        png = await _png_run_native(
            _render_banner_native,
            nickname=nickname, avatar_png=avatar_png,
            voice_seconds=voice_seconds, matches=matches,
            has_activity=has_activity,
            report_day_label=report_day_label)
        try:
            from utils import perf_metrics
            perf_metrics.record(
                "banner_render_native_ms",
                (_time.monotonic() - _t0) * 1000)
            perf_metrics.incr("banner_renders_native")
        except Exception:
            pass
        return png
    except _PngQueueFull:
        raise
    except Exception:
        try:
            from utils import perf_metrics
            perf_metrics.incr("png_render_errors")
        except Exception:
            pass
        raise
