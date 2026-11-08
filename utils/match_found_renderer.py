"""Рендер карточки «НАЙДЕН МАТЧ» (1920x1080) по шаблону Figma.

Берёт чистый TEMPLATE из assets_ds_server/match_found, вырезает
плейсхолдеры (ИГРОК 0X, MATCH #-, значения статистики) через inpaint
и подставляет реальные данные: аватарки, ники, ELO, SR, K/D, Winrate,
средние по командам и вероятность победы.

Координаты получены анализом пар TEMPLATE/EXAMPLE (одинаковая геометрия).
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets_ds_server" / "match_found"
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets_ds_server" / "fonts"
TEMPLATE_PATH = ASSETS_DIR / "MATCH FOUND TEMPLATE.png"

TEXT_COLOR = (243, 243, 248)       # имена, MATCH #, средние
VALUE_COLOR = (173, 180, 203)      # значения статистики игроков
FALLBACK_AVATAR_BG = (38, 42, 58)  # тёмный круг при отсутствии аватарки
AVATAR_TIMEOUT = 8.0

# --- Геометрия (1920x1080) ---
# Строки игроков: верх текста ника (y) для строк 1-4, шаг ~125-127
NAME_TOPS = [402, 528, 652, 779]
NAME_X = {"A": 168, "B": 1118}
# Колонки значений (левый край, как у подписей ELO/SR/K/D/WINRATE)
VAL_X = {
    "A": {"elo": 442, "sr": 558, "kd": 648, "wr": 762},
    "B": {"elo": 1392, "sr": 1508, "kd": 1598, "wr": 1713},
}
VAL_Y_OFFSET = 21          # верх значения относительно верха ника
VAL_W = 100                # ширина зоны значения для erase

# Средние по командам (нижняя строка SUMMARY)
AVG_VAL_Y = 982
AVG_X = {
    "A": {"elo": 79, "sr": 280, "kd": 479, "wc": 681},
    "B": {"elo": 1030, "sr": 1228, "kd": 1430, "wc": 1632},
}
AVG_W = 120

# MATCH #NNNNN (справа вверху)
MATCHNUM_BOX = (1495, 96, 1715, 134)
MATCHNUM_XY = (1500, 101)

# Зоны аватаров для поиска фиолетового плейсхолдера
AVATAR_ZONES = {
    "A": [(100, y - 8, 165, y + 60) for y in NAME_TOPS],
    "B": [(1048, y - 8, 1113, y + 60) for y in NAME_TOPS],
}

# Шрифт
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(name: str, size: int) -> ImageFont.FreeTypeFont:
    key = (name, size)
    if key not in _FONT_CACHE:
        path = FONTS_DIR / name
        try:
            _FONT_CACHE[key] = ImageFont.truetype(str(path), size)
        except Exception:
            _FONT_CACHE[key] = ImageFont.load_default(size)
    return _FONT_CACHE[key]


def _display_font(size: int) -> ImageFont.FreeTypeFont:
    return _font("RussoOne-Regular.ttf", size)


def _body_font(size: int) -> ImageFont.FreeTypeFont:
    return _font("DejaVuSans.ttf", size)


def _sanitize(text: str, limit: int = 24) -> str:
    """Убрать emoji/неподдерживаемые глифы и обрезать под лимит."""
    out = []
    for ch in text:
        o = ord(ch)
        if o > 0xFFFF or 0x1F000 <= o <= 0x1FAFF or o in (0xFE0F, 0x200D):
            continue
        out.append(ch)
    s = "".join(out).strip() or "?"
    return s[:limit]


def _fit_text(draw: ImageDraw.ImageDraw, text: str, base_size: int, max_w: int) -> tuple[str, ImageFont.FreeTypeFont]:
    """Подобрать кегль/обрезать текст, чтобы влез в max_w пикселей."""
    for size in (base_size, base_size - 2, base_size - 4, base_size - 6, base_size - 8):
        if size < 16:
            break
        f = _display_font(size)
        if draw.textlength(text, font=f) <= max_w:
            return text, f
    f = _display_font(16)
    s = text
    while s and draw.textlength(s + "…", font=f) > max_w:
        s = s[:-1]
    return s + "…", f


@dataclass
class PlayerCard:
    """Данные одного игрока для карточки."""

    user_id: int
    name: str
    team: str            # "A" | "B"
    elo: int = 0
    sr: float = 0.0
    kd: float = 0.0
    winrate: float = 0.0  # 0..100
    avatar_url: str = ""


@dataclass
class TeamAverages:
    elo: float = 0.0
    sr: float = 0.0
    kd: float = 0.0
    win_chance: float = 0.0  # 0..100


@dataclass
class MatchFoundData:
    match_num: int
    team_a: list[PlayerCard] = field(default_factory=list)
    team_b: list[PlayerCard] = field(default_factory=list)
    avg_a: TeamAverages = field(default_factory=TeamAverages)
    avg_b: TeamAverages = field(default_factory=TeamAverages)


# ----------------------------------------------------------------------
# Загрузка шаблона и маски аватаров (одноразово, кэш)
# ----------------------------------------------------------------------

_TEMPLATE_IMG: Image.Image | None = None
_AVATAR_MASKS: dict[tuple[str, int], Image.Image] = {}


def _load_template() -> Image.Image:
    global _TEMPLATE_IMG
    if _TEMPLATE_IMG is None:
        _TEMPLATE_IMG = Image.open(TEMPLATE_PATH).convert("RGBA")
    return _TEMPLATE_IMG.copy()


def _is_purple_placeholder(rgb) -> bool:
    """Фиолетовый плейсхолдер аватара в TEMPLATE."""
    r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
    return b > r + 25 and b > 90 and b > g + 30 and r < 170


def _avatar_mask(team: str, row: int) -> Image.Image | None:
    """Alpha-маска слота аватара из фиолетового плейсхолдера шаблона."""
    key = (team, row)
    if key in _AVATAR_MASKS:
        return _AVATAR_MASKS[key]

    tpl = Image.open(TEMPLATE_PATH).convert("RGB")
    px = tpl.load()
    x0, y0, x1, y1 = AVATAR_ZONES[team][row]
    mask = Image.new("L", tpl.size, 0)
    mpx = mask.load()
    found = False
    for y in range(y0, y1):
        for x in range(x0, x1):
            if _is_purple_placeholder(px[x, y]):
                mpx[x, y] = 255
                found = True
    if not found:
        _AVATAR_MASKS[key] = None  # type: ignore[assignment]
        return None
    # лёгкое расширение маски на 1px для антиалиасинга
    mask = mask.filter(ImageFilter.MaxFilter(3))
    _AVATAR_MASKS[key] = mask
    return mask


# ----------------------------------------------------------------------
# Скачивание аватарок
# ----------------------------------------------------------------------

async def _fetch_avatar(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=AVATAR_TIMEOUT)) as resp:
            if resp.status == 200:
                return await resp.read()
    except Exception as exc:
        logger.debug("Avatar fetch failed %s: %s", url, exc)
    return None


async def fetch_avatars(urls: list[str]) -> dict[str, bytes]:
    """Скачать аватарки параллельно. Возвращает {url: bytes}."""
    urls = [u for u in dict.fromkeys(urls) if u]
    if not urls:
        return {}
    out: dict[str, bytes] = {}
    connector = aiohttp.TCPConnector(limit=16)
    async with aiohttp.ClientSession(connector=connector) as session:
        results = await asyncio.gather(*(_fetch_avatar(session, u) for u in urls))
    for url, data in zip(urls, results):
        if data:
            out[url] = data
    return out


# ----------------------------------------------------------------------
# Синхронный рендер (вызывать через asyncio.to_thread)
# ----------------------------------------------------------------------

def _erase_regions(img: Image.Image, regions: list[tuple[int, int, int, int]]) -> None:
    """Убрать плейсхолдеры-текст в указанных зонах через inpaint/заливку."""
    try:
        import cv2
        import numpy as np
    except Exception:
        cv2 = None

    arr = np.asarray(img.convert("RGB")) if cv2 is not None else None
    for (x0, y0, x1, y1) in regions:
        if cv2 is not None and arr is not None:
            zone = arr[y0:y1, x0:x1]
            # маска = светлые пиксели (текст плейсхолдера белый)
            bright = (zone.sum(axis=2) > 330).astype("uint8") * 255
            if bright.max() == 0:
                continue
            # дилатация на 1-2px чтобы захватить антиалиасинг
            kernel = np.ones((3, 3), np.uint8)
            bright = cv2.dilate(bright, kernel, iterations=1)
            mask_zone = np.zeros((y1 - y0, x1 - x0), np.uint8)
            mask_zone[:] = bright
            # inpaint в BGR
            bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
            bgr[y0:y1, x0:x1] = cv2.inpaint(bgr[y0:y1, x0:x1], mask_zone, 3, cv2.INPAINT_TELEA)
            arr = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        else:
            # fallback: залить медианным цветом зоны
            zone = img.crop((x0, y0, x1, y1)).convert("RGB")
            px = list(zone.getdata())[::7]
            r = sorted(p[0] for p in px)[len(px) // 2]
            g = sorted(p[1] for p in px)[len(px) // 2]
            b = sorted(p[2] for p in px)[len(px) // 2]
            ImageDraw.Draw(img).rectangle((x0, y0, x1, y1), fill=(r, g, b))
    if cv2 is not None and arr is not None:
        img.paste(Image.fromarray(arr), (0, 0))


def _cover_resize(img: Image.Image, box: tuple[int, int, int, int]) -> Image.Image:
    """Вписать img в box с сохранением пропорций (cover-crop)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    scale = max(w / img.width, h / img.height)
    nw, nh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _draw_avatar(
    canvas: Image.Image,
    mask: Image.Image,
    avatar_bytes: bytes | None,
    name: str,
) -> None:
    """Наложить аватарку (или fallback-инициал) через маску слота."""
    x0, y0, x1, y1 = mask.getbbox()
    if x0 is None:
        return
    box = (x0, y0, x1 + 1, y1 + 1)

    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            av = _cover_resize(av, box)
        except Exception:
            av = None
    else:
        av = None

    if av is None:
        # fallback: тёмный круг с первой буквой
        av = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (*FALLBACK_AVATAR_BG, 255))
        d = ImageDraw.Draw(av)
        initial = _sanitize(name, 1).upper() or "?"
        f = _display_font(max(14, (box[3] - box[1]) // 2))
        tb = d.textbbox((0, 0), initial, font=f)
        d.text(
            ((av.width - (tb[2] - tb[0])) // 2 - tb[0], (av.height - (tb[3] - tb[1])) // 2 - tb[1]),
            initial, font=f, fill=TEXT_COLOR,
        )

    canvas.paste(av, box[:2], mask.crop(box))


def render_match_found_sync(
    data: MatchFoundData,
    avatars: dict[int, bytes],
) -> bytes:
    """Синхронный рендер PNG. avatars: {user_id: bytes}."""
    img = _load_template().convert("RGBA")
    draw = ImageDraw.Draw(img)

    # ---------- 1. Стирание плейсхолдеров ----------
    erase_regions: list[tuple[int, int, int, int]] = []

    # MATCH #NNNNN
    erase_regions.append(MATCHNUM_BOX)

    slots: list[tuple[PlayerCard, int, str]] = (
        [(p, i, "A") for i, p in enumerate(data.team_a)]
        + [(p, i, "B") for i, p in enumerate(data.team_b)]
    )

    for p, row, team in slots:
        nx = NAME_X[team]
        n_top = NAME_TOPS[row]
        # зона ника: от начала текста до начала колонки ELO
        elo_x = VAL_X[team]["elo"]
        erase_regions.append((nx - 8, n_top - 7, elo_x - 10, n_top + 32))
        # зоны значений
        vy = n_top + VAL_Y_OFFSET
        for key in ("elo", "sr", "kd", "wr"):
            vx = VAL_X[team][key]
            erase_regions.append((vx - 4, vy - 5, vx + VAL_W, vy + 32))

    # зоны средних значений
    for team, avgs in (("A", data.avg_a), ("B", data.avg_b)):
        for key, x in AVG_X[team].items():
            erase_regions.append((x - 4, AVG_VAL_Y - 4, x + AVG_W, AVG_VAL_Y + 38))

    _erase_regions(img, erase_regions)
    draw = ImageDraw.Draw(img)

    # ---------- 2. MATCH # ----------
    f_match = _display_font(30)
    match_text = f"MATCH #{data.match_num % 100000}"
    draw.text(MATCHNUM_XY, match_text, font=f_match, fill=TEXT_COLOR)

    # ---------- 3. Игроки ----------
    f_val = _body_font(30)

    for p, row, team in slots:
        nx = NAME_X[team]
        n_top = NAME_TOPS[row]
        vy = n_top + VAL_Y_OFFSET

        # аватар
        mask = _avatar_mask(team, row)
        if mask is not None:
            _draw_avatar(img, mask, avatars.get(p.user_id), p.name)
            draw = ImageDraw.Draw(img)

        # ник (fit в ~250px)
        name = _sanitize(p.name, 26)
        name, f_use = _fit_text(draw, name, 30, 250)
        draw.text((nx, n_top), name, font=f_use, fill=TEXT_COLOR)

        # значения: ELO, SR, K/D, WINRATE
        vals = {
            "elo": str(int(p.elo)),
            "sr": f"{p.sr:.1f}",
            "kd": f"{p.kd:.2f}",
            "wr": f"{p.winrate:.0f}%",
        }
        for key, txt in vals.items():
            draw.text((VAL_X[team][key], vy), txt, font=f_val, fill=VALUE_COLOR)

    # ---------- 4. Средние по командам ----------
    f_avg = _display_font(32)
    for team, avgs in (("A", data.avg_a), ("B", data.avg_b)):
        vals = {
            "elo": str(int(round(avgs.elo))),
            "sr": f"{avgs.sr:.2f}",
            "kd": f"{avgs.kd:.2f}",
            "wc": f"{avgs.win_chance:.0f}%",
        }
        for key, txt in vals.items():
            draw.text((AVG_X[team][key], AVG_VAL_Y), txt, font=f_avg, fill=TEXT_COLOR)

    # ---------- 5. Упаковка ----------
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


async def render_match_found(data: MatchFoundData) -> bytes:
    """Полный асинхронный рендер: аватарки + Pillow в thread."""
    urls = [p.avatar_url for p in data.team_a + data.team_b if p.avatar_url]
    avatar_bytes = await fetch_avatars(urls)
    by_url = {p.avatar_url: avatar_bytes.get(p.avatar_url) for p in data.team_a + data.team_b}
    avatars = {
        p.user_id: by_url[p.avatar_url]
        for p in data.team_a + data.team_b
        if p.avatar_url and by_url.get(p.avatar_url)
    }
    return await asyncio.to_thread(render_match_found_sync, data, avatars)
