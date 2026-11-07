"""Команды Free Fire API (siambhau FreeFireApi): /ff info|stats|ban|banner|wishlist."""

from __future__ import annotations

import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import replace_emojis, get_emoji
from services.freefire_service import FreeFireError, freefire_service

logger = logging.getLogger(__name__)

ff_group = app_commands.Group(
    name="ff", description="Free Fire — статистика игроков (API)"
)

REGION_CHOICES = [
    app_commands.Choice(name="SG (Singapur/Азия)", value="sg"),
    app_commands.Choice(name="IND (Индия)", value="ind"),
    app_commands.Choice(name="BR (Бразилия)", value="br"),
    app_commands.Choice(name="BD (Бангладеш)", value="bd"),
    app_commands.Choice(name="RU / CIS", value="ru"),
    app_commands.Choice(name="ID (Индонезия)", value="id"),
    app_commands.Choice(name="TH (Таиланд)", value="th"),
    app_commands.Choice(name="TW (Тайвань)", value="tw"),
    app_commands.Choice(name="US", value="us"),
]

_UID_PATTERN = re.compile(r"^\d{5,15}$")

COLOR_MAIN = discord.Color.from_rgb(69, 69, 69)
FOOTER = "DISCORD SERVER r1z3"


def _check_uid(uid: str) -> bool:
    return bool(_UID_PATTERN.match(uid.strip()))


def _prettify_key(key: str) -> str:
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", key)
    return spaced.replace("_", " ").strip().title()


def _fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _scalar_items(data: dict[str, Any], limit: int = 15) -> list[tuple[str, Any]]:
    items = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue
        items.append((_prettify_key(key), value))
        if len(items) >= limit:
            break
    return items


def _error_embed(exc: FreeFireError) -> discord.Embed:
    return discord.Embed(
        title=replace_emojis(f"{get_emoji('error')} FREE FIRE | Ошибка"),
        description=str(exc),
        color=discord.Color.red(),
    )


def _no_key_embed() -> discord.Embed:
    return discord.Embed(
        title=replace_emojis(f"{get_emoji('warning')} FREE FIRE | API не настроен"),
        description=(
            "Ключ Free Fire API не задан.\n"
            "Установите переменную `FREEFIRE_API_KEY`."
        ),
        color=discord.Color.orange(),
    )


async def _safe_call(coro):
    try:
        return await coro
    except FreeFireError as e:
        return e
    except Exception:
        logger.exception("Free Fire API unexpected error")
        return FreeFireError("Внутренняя ошибка при запросе к API.", "INTERNAL")


async def _check_and_key(interaction: discord.Interaction, uid: str) -> bool:
    """Валидация UID + API ключ. True — можно продолжать."""
    if not _check_uid(uid):
        await interaction.response.send_message(
            embed=_error_embed(
                FreeFireError("ID игрока должен содержать 5-15 цифр.", "BAD_UID")
            ),
            ephemeral=True,
        )
        return False
    if not freefire_service.configured:
        await interaction.response.send_message(embed=_no_key_embed(), ephemeral=True)
        return False
    return True


# ------------------------------------------------------------------ /ff info
@ff_group.command(name="info", description="Профиль игрока Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)", region="Регион аккаунта")
@app_commands.choices(region=REGION_CHOICES)
async def ff_info(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str] = app_commands.Choice(
        name="RU / CIS", value="ru"),
) -> None:
    uid = uid.strip()
    if not await _check_and_key(interaction, uid):
        return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_info(region.value, uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    basic = result.get("basicInfo") or {}
    clan = result.get("clanBasicInfo") or result.get("clanInfo") or {}
    guild_info = result.get("guildBasicInfo") or result.get("guildInfo") or {}
    social = result.get("socialInfo") or {}

    nickname = basic.get("nickname", "Unknown")
    signature = social.get("signature") or ""

    embed = discord.Embed(
        title=replace_emojis(f"{get_emoji('game')} FREE FIRE | Профиль"),
        description=(
            f"**{nickname}** — ID `{uid}`"
            + (f"\n{signature}" if signature else "")
        ),
        color=COLOR_MAIN,
    )
    embed.set_footer(text=FOOTER)

    def _num(key: str):
        v = basic.get(key)
        return f"{v:,}" if isinstance(v, int) else (v if v is not None else "—")

    embed.add_field(
        name=replace_emojis(f"{get_emoji('stats')} Основное"),
        value="\n".join(
            [
                f"• Уровень: **{basic.get('level', '—')}**",
                f"• Ранг: **{basic.get('rank', '—')}** (макс: {basic.get('maxRank', '—')})",
                f"• Очки рейтинга: **{_num('rankingPoints')}**",
                f"• Лайки: **{_num('liked')}**",
                f"• Регион: {str(basic.get('region', region.value)).upper()}",
            ]
        ),
        inline=False,
    )

    team_lines = []
    clan_name = clan.get("clanName") or guild_info.get("name") or guild_info.get("guildName")
    if clan_name:
        team_lines.append(f"• {'Клан' if clan.get('clanName') else 'Гильдия'}: **{clan_name}**")
    if clan.get("memberNum") is not None:
        team_lines.append(f"• Участники: {clan.get('memberNum', '—')}/{clan.get('capacity', '—')}")
    if team_lines:
        embed.add_field(
            name=replace_emojis(f"{get_emoji('team')} Команда"),
            value="\n".join(team_lines),
            inline=False,
        )

    await interaction.followup.send(embed=embed)


# ----------------------------------------------------------------- /ff stats
@ff_group.command(name="stats", description="Статистика игрока Free Fire")
@app_commands.describe(
    uid="ID игрока (5-15 цифр)", region="Регион аккаунта", mode="Режим/матчмод"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="BR — Карьера", value="br_career"),
        app_commands.Choice(name="BR — Обычные", value="br_normal"),
        app_commands.Choice(name="BR — Рейтинг", value="br_ranked"),
        app_commands.Choice(name="CS — Карьера", value="cs_career"),
        app_commands.Choice(name="CS — Рейтинг", value="cs_ranked"),
    ],
    region=REGION_CHOICES,
)
async def ff_stats(
    interaction: discord.Interaction,
    uid: str,
    mode: app_commands.Choice[str],
    region: app_commands.Choice[str] = app_commands.Choice(
        name="RU / CIS", value="ru"),
) -> None:
    uid = uid.strip()
    if not await _check_and_key(interaction, uid):
        return

    gamemode, matchmode = mode.value.split("_", 1)
    await interaction.response.defer()
    result = await _safe_call(
        freefire_service.get_stats(
            region.value, uid, gamemode=gamemode, matchmode=matchmode.upper()
        )
    )
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    stats = result.get("stats") or result
    lines = [f"• **{k}** — {_fmt_value(v)}" for k, v in _scalar_items(stats)]

    embed = discord.Embed(
        title=replace_emojis(f"{get_emoji('stats')} FREE FIRE | Статистика"),
        description=(
            f"ID `{uid}` • режим: **{mode.name}**\n\n"
            + ("\n".join(lines) if lines else "Данные отсутствуют.")
        ),
        color=COLOR_MAIN,
    )
    embed.set_footer(text=FOOTER)
    await interaction.followup.send(embed=embed)


class FreeFireCog(commands.Cog):
    """Cog-носитель группы /ff (команды — app_commands.Group)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot) -> None:
    await bot.add_cog(FreeFireCog(bot))
    bot.tree.add_command(ff_group)
