"""Профиль игрока — Components V2 без PNG/Chromium (r!z3 CLASSIC).

Собирает данные через services.profile_card.collect_profile_data
(тот же источник, что у старой PNG-карточки) и рисует нативный
CV2-профиль: ПРОФИЛЬ / ПРОГРЕСС / СТАТИСТИКА / ЭКОНОМИКА.
Аватар — discord.ui.Thumbnail в Section, упоминаний нет.
"""

import logging

import discord
from discord.utils import escape_markdown

from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)


def _default_stats(guild_id: int, user_id: int, display_name: str):
    """Пустая запись статистики для новых пользователей профиля."""
    from models.player_stats import PlayerStats

    return PlayerStats(
        guild_id=guild_id,
        user_id=user_id,
        name=display_name,
        elo=1000,
        wins=0,
        finals=0,
        games=0,
        current_streak=0,
        best_win_streak=0,
        best_loss_streak=0,
        total_kills=0,
        total_deaths=0,
        best_match_kills=0,
        total_elo_change=0,
        last_elo_change=0,
        xp=0,
        level=1,
        xp_to_next_level=250,
        total_earnings=0,
        tournament_participations=0,
        description="",
    )


def _esc(text) -> str:
    """Экранирование Discord Markdown для пользовательских строк.

    escape_markdown не трогает ``<``/``>`` — экранируем их сами,
    чтобы ``<@id>``/``<#id>`` в нике не превратились в mention.
    """
    return (
        escape_markdown(str(text))
        .replace("<", "\\<")
        .replace(">", "\\>")
    )


def _fmt_delta(delta: int) -> str:
    """+30 / −15 — знак изменения ELO."""
    if delta > 0:
        return f"+{delta}"
    if delta < 0:
        return f"−{abs(delta)}"
    return "0"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def _streak_text(streak: int) -> str:
    if streak > 0:
        return f"{streak} {_plural(streak, 'победа', 'победы', 'побед')}"
    if streak < 0:
        n = abs(streak)
        return f"{n} {_plural(n, 'поражение', 'поражения', 'поражений')}"
    return "—"


def _avatar_url(user) -> str | None:
    """URL аватара с fallback на дефолтный (display_avatar никогда
    не бывает None — для пользователей без аватара отдаёт default)."""
    try:
        avatar = user.display_avatar
        return avatar.url if avatar else None
    except Exception:
        logger.debug("Avatar url unavailable", exc_info=True)
        return None


async def build_profile_layout(
    guild: discord.Guild, user: discord.User
) -> DesignView:
    """CV2-профиль игрока: текстовые блоки + настоящий аватар."""
    from services.profile_card import collect_profile_data

    data = await collect_profile_data(guild, user)
    name = _esc(data["name"])

    description = (data.get("description") or "").strip()
    birthday = (data.get("birthday") or "").strip()
    partner = (data.get("partner") or "").strip()

    view = DesignView(title=f"ПРОФИЛЬ — {name}", timeout=180)

    # ПРОФИЛЬ — имя, описание, репутация, birthday, супруг(а) + аватар
    profile_lines = [
        f"**{name}**",
        f"**Описание:** {_esc(description) if description else 'Не указано'}",
        f"**Репутация:** {data.get('reputation', 0)}",
    ]
    if birthday:
        profile_lines.append(f"**День рождения:** {_esc(birthday)}")
    if partner:
        profile_lines.append(f"**Супруг(а):** {_esc(partner)}")

    avatar_url = _avatar_url(user)
    profile_text = "\n".join(profile_lines)
    if avatar_url:
        view.component(
            discord.ui.Section(
                discord.ui.TextDisplay(content=profile_text),
                accessory=discord.ui.Thumbnail(media=avatar_url),
            )
        )
    else:
        view.text(profile_text)
    view.sep()

    # ПРОГРЕСС — ранг, уровень, ELO, XP
    elo_delta = data.get("last_elo_change") or 0
    xp = data.get("xp", 0)
    xp_next = data.get("xp_to_next_level", 0)
    view.text(
        "**ПРОГРЕСС**\n"
        f"**Ранг:** {_esc(data.get('rank_name', '—'))} · "
        f"**Уровень:** {data.get('level', 1)}\n"
        f"**ELO:** {data.get('elo', 0)} "
        f"`{_fmt_delta(elo_delta)}`\n"
        f"**XP:** {xp}/{xp_next} до следующего уровня"
    )
    view.sep()

    # СТАТИСТИКА — всегда, даже при 0 матчей
    view.text(
        "**СТАТИСТИКА**\n"
        f"**Матчи:** {data.get('matches', 0)} · "
        f"**Победы:** {data.get('wins', 0)} · "
        f"**Поражения:** {data.get('losses', 0)} · "
        f"**Win Rate:** {data.get('winrate', 0)}%\n"
        f"**Убийства:** {data.get('kills', 0)} · "
        f"**Смерти:** {data.get('deaths', 0)} · "
        f"**K/D Ratio:** {data.get('kd', 0)}\n"
        f"**AVG Kills:** {data.get('avg_kills', 0)} · "
        f"**Max Kills:** {data.get('max_kills', 0)} · "
        f"**Skill Rating:** {data.get('skill_rating', 0)}/10\n"
        f"**Финалы:** {data.get('finals', 0)} · "
        f"**Текущая серия:** {_streak_text(data.get('current_streak', 0))}\n"
        f"**Лучшая серия побед:** {data.get('best_win_streak', 0)} · "
        f"**Лучшая серия поражений:** {data.get('best_loss_streak', 0)}"
    )
    view.sep()

    # ЭКОНОМИКА
    view.text(
        "**ЭКОНОМИКА**\n"
        f"**Баланс:** {data.get('balance', 0)} · "
        f"**Предметов:** {data.get('items', 0)}"
    )
    return view
