"""Рендер карточки «РЕЗУЛЬТАТ МАТЧА» (1920x1080) по шаблону Figma.

Берёт чистый TEMPLATE из assets_ds_server/match_results, вырезает
плейсхолдеры («—», «УЧАСТНИК 0X», счёт, MVP, статистика команд)
через inpaint и подставляет реальные данные завершённого матча:
аватарки, ники, K/D/A, урон, изменение ELO, MVP, итоги команд.

Координаты и привязки текста получены из SVG-исходников
TEMPLATE/EXAMPLE (идентитичная геометрия).

Генератор не считает статистику, победителя и ELO — всё приходит
в MatchResultData готовым.
"""

from __future__ import annotations

import asyncio
import io
import logging
import time
import tracemalloc
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

from utils.match_found_renderer import (
    CARD_RENDER_SEM,
    _PROFILE_MEM,
    _body_font,
    _cover_resize,
    _display_font,
    _erase_regions,
    _fit_text,
    _sanitize,
    fetch_avatars,
)

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets_ds_server" / "match_results"
TEMPLATE_PATH = ASSETS_DIR / "riz3_match_results_template.png"

# --- Цвета (brand_colors из spec.json) ---
TEXT = (243, 245, 250)
SUB = (178, 190, 208)
MUTED = (126, 136, 157)
GREEN = (124, 242, 194)
RED = (255, 137, 153)
GOLD = (255, 214, 156)
VIOLET = (156, 88, 255)
BLUE = (75, 168, 255)
FALLBACK_AVATAR_BG = (32, 35, 57)

# --- Геометрия (baseline-y из SVG, anchor "s" = baseline) ---
# Строки игроков: name/sub/stat baseline-y для строк 0-3 (шаг 86)
ROW_NAME_Y = [586, 672, 758, 844]
ROW_SUB_Y = [608, 694, 780, 866]
ROW_STAT_Y = [600, 686, 772, 858]

TEAM_X = {
    "A": {
        "name": 179,          # start
        "sub": 179,           # start
        "avatar": (106, 564), # левый верх слота 55x55, шаг y +86
        "cols": {"k": 460, "d": 531, "a": 599, "dmg": 674, "elo": 849.5},
    },
    "B": {
        "name": 1087,
        "sub": 1087,
        "avatar": (1014, 564),
        "cols": {"k": 1368, "d": 1439, "a": 1507, "dmg": 1582, "elo": 1757.5},
    },
}
COL_HALF_W = {"k": 45, "d": 45, "a": 45, "dmg": 60, "elo": 48}

# Шапка
MATCH_ID_BOX = (1485, 90, 1690, 136)
DURATION_BOX = (1690, 90, 1908, 136)
SCORE_BOXES = [(845, 270, 920, 380), (935, 295, 985, 375), (1005, 270, 1085, 380)]
SCORE_LABEL_BOX = (810, 368, 1110, 398)
TEAM_RESULT_BOX = {"A": (100, 276, 660, 400), "B": (1500, 276, 1850, 400)}
STATUS_LABEL_BOX = {"A": (825, 470, 925, 508), "B": (1735, 470, 1835, 508)}

# MVP-карточка
MVP_NAME_BOX = (160, 960, 700, 1038)
MVP_KDA_BOX = (450, 962, 665, 1035)
MVP_DMG_BOX = (670, 962, 850, 1035)

# Статистика команд (правая карточка)
TEAMSTAT_BOXES = [(995, 986, 1270, 1042), (1280, 986, 1555, 1042), (1575, 986, 1850, 1042)]

_TEMPLATE_IMG: Image.Image | None = None


def _load_template() -> Image.Image:
    global _TEMPLATE_IMG
    if _TEMPLATE_IMG is None:
        _TEMPLATE_IMG = Image.open(TEMPLATE_PATH).convert("RGBA")
    return _TEMPLATE_IMG.copy()


@dataclass
class ResultPlayer:
    """Строка игрока в карточке результата."""

    user_id: int
    name: str
    team: str  # "A" | "B"
    avatar_url: str = ""
    sr: float = 0.0
    elo: int = 0
    kills: int = 0
    deaths: int = 0
    assists: int = 0
    damage: int = 0
    elo_change: int = 0
    is_mvp: bool = False


@dataclass
class TeamTotals:
    """Агрегаты команды для нижней карточки (считает вызывающий код)."""

    kills: int = 0
    damage: int = 0
    elo_change: int = 0  # сумма дельт (знаковая)


@dataclass
class MatchResultData:
    """Все данные карточки результата — готовые, без пересчёта."""

    match_num: str = ""        # "#RZ-1042"
    duration: str = "—"          # "10 мин. 25 сек." / "1 ч. 12 мин."
    score_a: int = 0
    score_b: int = 0
    score_label: str = "СЧЁТ МАТЧА"  # "СЧЁТ МАТЧА" | "УБИЙСТВА КОМАНД"
    winner: str = "A"          # "A" | "B"
    team_a: list[ResultPlayer] = field(default_factory=list)
    team_b: list[ResultPlayer] = field(default_factory=list)
    totals_a: TeamTotals = field(default_factory=TeamTotals)
    totals_b: TeamTotals = field(default_factory=TeamTotals)
    mvp_name: str = ""
    mvp_kda: str = ""          # "10 / 6 / 4"
    mvp_damage: str = ""


def _fmt_num(v: int | float) -> str:
    """Число с разделителем тысяч пробелом: 3510 -> '3 510'."""
    return f"{int(v):,}".replace(",", " ")


def _elo_delta_str(v: int) -> str:
    return f"{v:+d}" if v else "0"


def _elo_delta_color(v: int) -> tuple[int, int, int]:
    if v > 0:
        return GREEN
    if v < 0:
        return RED
    return MUTED


def _anchor(h: str, v: str = "s") -> str:
    return h + v


def _draw_baseline(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, anchor_h: str = "l") -> None:
    draw.text(xy, text, font=font, fill=fill, anchor=_anchor(anchor_h))


def render_match_result_sync(data: MatchResultData, avatars: dict[int, bytes]) -> bytes:
    """Синхронный рендер PNG. avatars: {user_id: bytes}."""
    img = _load_template().convert("RGBA")

    # ---------- 1. Стирание плейсхолдеров ----------
    erase: list[tuple[int, int, int, int]] = [
        MATCH_ID_BOX, DURATION_BOX,
        *SCORE_BOXES, SCORE_LABEL_BOX,
        TEAM_RESULT_BOX["A"], TEAM_RESULT_BOX["B"],
        STATUS_LABEL_BOX["A"], STATUS_LABEL_BOX["B"],
        MVP_NAME_BOX, MVP_KDA_BOX, MVP_DMG_BOX,
        *TEAMSTAT_BOXES,
    ]
    for team in ("A", "B"):
        tx = TEAM_X[team]
        for r in range(4):
            ny = ROW_NAME_Y[r]
            x_name = tx["name"] - (0 if team == "A" else 8)
            x_end = 452 if team == "A" else 1360
            erase.append((x_name - 8, ny - 30, x_end, ny + 40))
            sy = ROW_STAT_Y[r]
            for key, cx in tx["cols"].items():
                hw = COL_HALF_W[key]
                erase.append((cx - hw, sy - 28, cx + hw, sy + 10))
    _erase_regions(img, [(int(a), int(b), int(c), int(d)) for a, b, c, d in erase])
    draw = ImageDraw.Draw(img)

    # ---------- 2. Шапка ----------
    draw.text((1498, 119), _sanitize(data.match_num, 16), font=_display_font(28), fill=TEXT, anchor="ls")
    draw.text((1701, 119), _sanitize(data.duration, 12), font=_display_font(26), fill=TEXT, anchor="ls")

    # ---------- 3. Счёт / убийства команд ----------
    draw.text(
        (960, 388), _sanitize(data.score_label, 30),
        font=_body_font(18), fill=SUB, anchor="ms",
    )
    f_score = _display_font(92)
    f_colon = _body_font(68)
    draw.text((882, 364), str(data.score_a), font=f_score, fill=TEXT, anchor="ms")
    draw.text((960, 358), ":", font=f_colon, fill=MUTED, anchor="ms")
    draw.text((1040, 364), str(data.score_b), font=f_score, fill=TEXT, anchor="ms")

    # ---------- 4. Результат команд ----------
    f_team_res = _display_font(48)
    f_team_sub = _body_font(19)
    for team, players, won in (
        ("A", data.team_a, data.winner == "A"),
        ("B", data.team_b, data.winner == "B"),
    ):
        if team == "A":
            res = "ПОБЕДА" if won else "ПОРАЖЕНИЕ"
            draw.text((106, 331), res, font=f_team_res, fill=TEXT, anchor="ls")
            sub = "Команда победила в матче" if won else "Команда проиграла матч"
            draw.text((107, 372), sub, font=f_team_sub, fill=SUB, anchor="ls")
        else:
            res = "ПОБЕДА" if won else "ПОРАЖЕНИЕ"
            draw.text((1815, 331), res, font=f_team_res, fill=TEXT, anchor="rs")
            sub = "Команда победила в матче" if won else "Команда проиграла матч"
            draw.text((1815, 372), sub, font=f_team_sub, fill=SUB, anchor="rs")

    # ---------- 5. Статус колонок (ПОБЕДИТЕЛЬ/СОПЕРНИК) ----------
    f_status = _body_font(14)
    for team, won in (("A", data.winner == "A"), ("B", data.winner == "B")):
        text = "ПОБЕДИТЕЛЬ" if won else "СОПЕРНИК"
        color = VIOLET if team == "A" else BLUE
        if team == "A":
            draw.text((916, 491), text, font=f_status, fill=color, anchor="rs")
        else:
            draw.text((1824, 491), text, font=f_status, fill=color, anchor="rs")

    # ---------- 6. Игроки ----------
    f_name = _display_font(21)
    f_sub = _body_font(13)
    f_stat = _body_font(24)
    f_stat_small = _body_font(22)
    f_mvp_badge = _body_font(12)

    slots = [(p, i, "A") for i, p in enumerate(data.team_a[:4])] + [
        (p, i, "B") for i, p in enumerate(data.team_b[:4])
    ]
    for p, row, team in slots:
        tx = TEAM_X[team]
        ny = ROW_NAME_Y[row]
        sy = ROW_STAT_Y[row]
        avatar_top = tx["avatar"][1] + row * 86

        # аватар: rounded-rect слот 55x55, inset 2px
        _draw_avatar(img, avatars.get(p.user_id), p.name, tx["avatar"][0] + 2, avatar_top + 2)
        draw = ImageDraw.Draw(img)

        # ник
        name = _sanitize(p.name, 24)
        name, f_use = _fit_text(draw, name, 20, 265 if team == "A" else 260)
        draw.text((tx["name"], ny), name, font=f_use, fill=TEXT, anchor="ls")

        # SR / ELO
        sub = f"SR {p.sr:.1f}  •  ELO {int(p.elo)}"
        draw.text((tx["sub"], ROW_SUB_Y[row]), sub, font=f_sub, fill=MUTED, anchor="ls")

        # K / D / A / DAMAGE / ΔELO — middle-anchored по колонкам
        vals = {
            "k": str(p.kills),
            "d": str(p.deaths),
            "a": str(p.assists),
            "dmg": _fmt_num(p.damage),
            "elo": _elo_delta_str(p.elo_change),
        }
        for key, txt in vals.items():
            cx = tx["cols"][key]
            f = f_stat_small if key == "dmg" else f_stat
            color = TEXT if key != "elo" else _elo_delta_color(p.elo_change)
            draw.text((cx, sy), txt, font=f, fill=color, anchor="ms")

        # бейдж MVP на строке
        if p.is_mvp:
            draw.text((400 if team == "A" else 1340, 579 + row * 86), "MVP", font=f_mvp_badge, fill=GOLD, anchor="ms")

    # ---------- 7. MVP карточка ----------
    if data.mvp_name:
        draw.text((173, 1001), _sanitize(data.mvp_name, 24).upper(), font=_display_font(30), fill=TEXT, anchor="ls")
        draw.text((464, 1000), _sanitize(data.mvp_kda, 20), font=_body_font(24), fill=TEXT, anchor="ls")
        draw.text((685, 1000), _sanitize(data.mvp_damage, 16), font=_body_font(26), fill=TEXT, anchor="ls")

    # ---------- 8. Статистика команд ----------
    f_tot = _body_font(24)
    stats_vals = [
        (1007, f"{data.totals_a.kills} : {data.totals_b.kills}"),
        (1294, f"{_fmt_num(data.totals_a.damage)} : {_fmt_num(data.totals_b.damage)}"),
        (1594, f"{_elo_delta_str(data.totals_a.elo_change)}  /  {_elo_delta_str(data.totals_b.elo_change)}"),
    ]
    for x, txt in stats_vals:
        draw.text((x, 1015), txt, font=f_tot, fill=TEXT, anchor="ls")

    # ---------- 9. Упаковка ----------
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _draw_avatar(canvas: Image.Image, avatar_bytes: bytes | None, name: str, x: int, y: int) -> None:
    """Аватар 51x51 со скруглением ~12px или fallback-инициал."""
    size = 51
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    md.rounded_rectangle((0, 0, size - 1, size - 1), radius=12, fill=255)

    av = None
    if avatar_bytes:
        try:
            src = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA")
            av = _cover_resize(src, (0, 0, size, size))
        except Exception:
            av = None
    if av is None:
        av = Image.new("RGBA", (size, size), (*FALLBACK_AVATAR_BG, 255))
        d = ImageDraw.Draw(av)
        initial = _sanitize(name, 1).upper() or "?"
        f = _display_font(24)
        tb = d.textbbox((0, 0), initial, font=f)
        d.text(
            ((size - (tb[2] - tb[0])) // 2 - tb[0], (size - (tb[3] - tb[1])) // 2 - tb[1]),
            initial, font=f, fill=TEXT,
        )
    canvas.paste(av, (x, y), mask)


def match_num_from_id(match_id: str) -> str:
    """Стабильный номер матча для надписи #RZ-XXXXX."""
    return f"#RZ-{zlib.crc32(match_id.encode('utf-8')) % 100000:05d}"


async def render_match_result(data: MatchResultData) -> bytes:
    """Полный асинхронный рендер: аватарки + Pillow в thread + лимит + логирование."""
    import utils.match_found_renderer as _mfr

    urls = [p.avatar_url for p in data.team_a + data.team_b if p.avatar_url]
    avatar_bytes = await fetch_avatars(urls)
    by_url = {p.avatar_url: avatar_bytes.get(p.avatar_url) for p in data.team_a + data.team_b}
    avatars = {
        p.user_id: by_url[p.avatar_url]
        for p in data.team_a + data.team_b
        if p.avatar_url and by_url.get(p.avatar_url)
    }

    async with CARD_RENDER_SEM:
        _mfr._ACTIVE_RENDERS += 1
        t0 = time.perf_counter()
        if _PROFILE_MEM:
            tracemalloc.start()
        try:
            png = await asyncio.to_thread(render_match_result_sync, data, avatars)
        finally:
            if _PROFILE_MEM:
                _current, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
            else:
                peak = 0
            _mfr._ACTIVE_RENDERS -= 1
        elapsed = time.perf_counter() - t0
        try:
            from utils import perf_metrics

            perf_metrics.record("render_match_result_ms", elapsed * 1000.0)
        except Exception:
            pass
        logger.info(
            "Match result rendered: %.2fs, png=%d bytes, peak_rss~%d MB, active=%d",
            elapsed, len(png), peak // (1024 * 1024), _mfr._ACTIVE_RENDERS,
        )
        return png
