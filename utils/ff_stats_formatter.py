"""Форматирование статистики Free Fire (/ff stats) для Components V2.

Перенесено из cogs/freefire.py: блоки solostats/duostats/quadstats
(BR) и csstats (CS) -> markdown-текст для TextDisplay.
"""

from __future__ import annotations

from typing import Any

from utils import ff_profile_formatter as fmt

# key -> (gamemode, matchmode, подпись)
STATS_MODES: tuple[tuple[str, str, str, str], ...] = (
    ("br_career", "br", "CAREER", "BR · Карьера"),
    ("br_normal", "br", "NORMAL", "BR · Обычные"),
    ("br_ranked", "br", "RANKED", "BR · Рейтинг"),
    ("cs_career", "cs", "CAREER", "CS · Карьера"),
    ("cs_ranked", "cs", "RANKED", "CS · Рейтинг"),
)

MODE_BY_KEY = {key: (gm, mm, label) for key, gm, mm, label in STATS_MODES}

_BR_MODES = (("solostats", "Соло"), ("duostats", "Дуо"), ("quadstats", "Сквад"))


def _get(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _kd(kills: Any, deaths: Any) -> str:
    try:
        k, d = float(kills or 0), float(deaths or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{k / d:,.2f}".replace(",", " ") if d else "—"


def _fmt_hours(seconds: Any) -> str:
    try:
        return f"{int(seconds) / 3600:,.1f} ч".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _fmt_km(meters: Any) -> str:
    try:
        return f"{int(meters) / 1000:,.0f} км".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _stat_lines(block: dict[str, Any], br: bool) -> list[str]:
    """Строки статистики для одного блока (solo/duo/quad/cs)."""
    det = block.get("detailedstats") or {}
    kills = _get(block, "kills") or 0
    deaths = _get(det, "deaths") or _get(det, "knockdowns")
    games = _get(block, "gamesplayed")
    wins = _get(block, "wins")

    lines = [
        f"• Игр: **{fmt.fmt_num(games)}** • Побед: **{fmt.fmt_num(wins)}**",
        (f"• Убийств: **{fmt.fmt_num(kills)}** • Смертей: "
         f"**{fmt.fmt_num(deaths)}** • K/D: **{_kd(kills, deaths)}**"),
    ]
    if br:
        lines += [
            (f"• Топ: **{fmt.fmt_num(_get(det, 'topntimes'))}** • "
             f"Выживание: **{_fmt_hours(_get(det, 'survivaltime'))}**"),
            (f"• Урон: **{fmt.fmt_num(_get(det, 'damage'))}** • "
             f"Хедшоты: **{fmt.fmt_num(_get(det, 'headshotkills'))}**"),
            (f"• Макс. киллов за матч: "
             f"**{fmt.fmt_num(_get(det, 'highestkills'))}** • "
             f"Дистанция: **{_fmt_km(_get(det, 'distancetravelled'))}**"),
        ]
        extra = []
        if _get(det, "revives"):
            extra.append(f"Ревайвы: {fmt.fmt_num(_get(det, 'revives'))}")
        if _get(det, "knockdown"):
            extra.append(f"Нокдауны: {fmt.fmt_num(_get(det, 'knockdown'))}")
        if _get(det, "roadkills"):
            extra.append(
                f"Убийства с транспорта: {fmt.fmt_num(_get(det, 'roadkills'))}"
            )
        if extra:
            lines.append("• " + " • ".join(extra))
    else:
        lines += [
            (f"• Урон: **{fmt.fmt_num(_get(det, 'damage'))}** • "
             f"Хедшоты: **{fmt.fmt_num(_get(det, 'headshotkills'))}**"),
            (f"• Ассисты: **{fmt.fmt_num(_get(det, 'assists'))}** • "
             f"MVP: **{fmt.fmt_num(_get(det, 'mvpcount'))}**"),
        ]
        multi = []
        for key, label in (
            ("doublekills", "дабл"),
            ("triplekills", "трипл"),
            ("fourkills", "квадра"),
        ):
            val = _get(det, key)
            if val:
                multi.append(f"{label}: {fmt.fmt_num(val)}")
        if multi:
            lines.append("• " + " • ".join(multi))
    return lines


def stats_markdown(stats: dict[str, Any]) -> str:
    """stats из /freefireinfo/stats -> markdown для TextDisplay."""
    sections: list[str] = []
    is_cs = "csstats" in stats

    if is_cs:
        cs = stats.get("csstats") or {}
        sections.append(
            "**Clash Squad**\n" + "\n".join(_stat_lines(cs, br=False))
        )
    else:
        for key, label in _BR_MODES:
            block = stats.get(key)
            if isinstance(block, dict) and block:
                sections.append(
                    f"**{label}**\n" + "\n".join(_stat_lines(block, br=True))
                )

    if not sections:
        # Fallback: скалярные поля верхнего уровня
        lines = []
        for key, value in stats.items():
            if isinstance(value, (dict, list)):
                continue
            lines.append(f"• **{fmt.safe_text(key)}** — {fmt.fmt_num(value)}")
            if len(lines) >= 20:
                break
        return "\n".join(lines) if lines else "Данные отсутствуют."

    return "\n\n".join(sections)
