"""Команды Free Fire Community API: /ff info|stats|ban|wishlist|craftland."""

from __future__ import annotations

import logging
import re
from typing import Any

import discord
from discord import app_commands

from services.freefire_service import (
    FreeFireError,
    freefire_service,
)
from utils.design import DesignEmbed, emoji

logger = logging.getLogger(__name__)

ff_group = app_commands.Group(
    name="ff", description="Free Fire — статистика игроков (API)"
)

REGION_CHOICES = [
    app_commands.Choice(name="SG (Singapur/Азия)", value="sg"),
    app_commands.Choice(name="IND (Индия)", value="ind"),
    app_commands.Choice(name="BR (Бразилия/Америка)", value="br"),
]

_UID_PATTERN = re.compile(r"^\d{5,15}$")


def _check_uid(uid: str) -> bool:
    return bool(_UID_PATTERN.match(uid.strip()))


def _prettify_key(key: str) -> str:
    """camelCase / snake_case -> Title Case."""
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", key)
    spaced = spaced.replace("_", " ")
    return spaced.strip().title()


def _scalar_items(data: dict[str, Any], limit: int = 20) -> list[tuple[str, Any]]:
    """Скалярные поля словаря для embed (пропуская вложенные структуры)."""
    items = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue
        items.append((_prettify_key(key), value))
        if len(items) >= limit:
            break
    return items


def _career_fields(career: dict[str, Any], limit: int = 15) -> list[str]:
    """Строки статистики одного режима (solo/duo/squad)."""
    if not isinstance(career, dict):
        return []
    lines = []
    for name, value in _scalar_items(career, limit=limit):
        if isinstance(value, float):
            value = f"{value:,.1f}"
        elif isinstance(value, int):
            value = f"{value:,}"
        lines.append(f"**{name}** — {value}")
    return lines


async def _safe_call(coro):
    """Вызов API с единообразной обработкой ошибок."""
    try:
        return await coro
    except FreeFireError as e:
        return e
    except Exception:
        logger.exception("Free Fire API unexpected error")
        return FreeFireError("Внутренняя ошибка при запросе к API.", "INTERNAL")


def _error_embed(exc: FreeFireError) -> DesignEmbed:
    return DesignEmbed(
        title=f"{emoji('error')} FREE FIRE | Ошибка",
        description=str(exc),
        category="error",
        footer="DISCORD SERVER r1z3",
    )


def _no_key_embed() -> DesignEmbed:
    return DesignEmbed(
        title=f"{emoji('warning')} FREE FIRE | API не настроен",
        description=(
            "Ключ Free Fire API не задан.\n"
            "Установите переменную `FREEFIRE_API_KEY` "
            "(ключ на developers.freefirecommunity.com)."
        ),
        category="warning",
        footer="DISCORD SERVER r1z3",
    )


# ------------------------------------------------------------------ /ff info
@ff_group.command(name="info", description="Профиль игрока Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)", region="Регион аккаунта")
async def ff_info(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str],
) -> None:
    uid = uid.strip()
    if not _check_uid(uid):
        await interaction.response.send_message(
            embed=_error_embed(
                FreeFireError("ID игрока должен содержать 5-15 цифр.", "BAD_UID")
            ),
            ephemeral=True,
        )
        return
    if not freefire_service.configured:
        await interaction.response.send_message(embed=_no_key_embed(), ephemeral=True)
        return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_info(region.value, uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    basic = result.get("basicInfo") or {}
    clan = result.get("clanBasicInfo") or {}
    pet = result.get("petInfo") or {}
    social = result.get("socialInfo") or {}
    credit = result.get("creditScoreInfo") or {}

    nickname = basic.get("nickname", "Unknown")
    signature = social.get("signature") or ""
    embed = DesignEmbed(
        title=f"{emoji('game')} FREE FIRE | Профиль",
        description=(
            f"**{nickname}** — ID `{uid}`" + (f"\n{signature}" if signature else "")
        ),
        category="game",
        footer="DISCORD SERVER r1z3",
    )

    def _num(key: str):
        v = basic.get(key)
        return f"{v:,}" if isinstance(v, int) else (v if v is not None else "—")

    embed.add_section(
        f"{emoji('stats')} Основное",
        "\n".join(
            [
                f"• Уровень: **{basic.get('level', '—')}**",
                f"• Ранг: **{basic.get('rank', '—')}** (макс: {basic.get('maxRank', '—')})",
                f"• Очки рейтинга: **{_num('rankingPoints')}**",
                f"• Лайки: **{_num('liked')}**",
                f"• Титул: {basic.get('title') or '—'}",
                f"• Регион: {str(basic.get('region', region.value)).upper()}",
            ]
        ),
    )

    clan_lines = []
    if clan.get("clanName"):
        clan_lines.append(f"• Клан: **{clan['clanName']}** (ур. {clan.get('clanLevel', '—')})")
        clan_lines.append(f"• Участники: {clan.get('memberNum', '—')}/{clan.get('capacity', '—')}")
    if clan_lines:
        embed.add_section(f"{emoji('team')} Клан", "\n".join(clan_lines))

    extra_lines = []
    if pet.get("id"):
        extra_lines.append(f"• Питомец: ID `{pet['id']}` (ур. {pet.get('level', '—')})")
    if credit.get("creditScore") is not None:
        extra_lines.append(f"• Кредитный рейтинг: **{credit['creditScore']}**")
    if extra_lines:
        embed.add_section(f"{emoji('info')} Дополнительно", "\n".join(extra_lines))

    await interaction.followup.send(embed=embed)


# ----------------------------------------------------------------- /ff stats
@ff_group.command(name="stats", description="Карьерная статистика игрока")
@app_commands.describe(
    uid="ID игрока (5-15 цифр)", region="Регион аккаунта", mode="Режим игры"
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Solo", value="solo"),
        app_commands.Choice(name="Duo", value="duo"),
        app_commands.Choice(name="Squad", value="squad"),
        app_commands.Choice(name="Все режимы", value="all"),
    ]
)
async def ff_stats(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str],
    mode: app_commands.Choice[str],
) -> None:
    uid = uid.strip()
    if not _check_uid(uid):
        await interaction.response.send_message(
            embed=_error_embed(
                FreeFireError("ID игрока должен содержать 5-15 цифр.", "BAD_UID")
            ),
            ephemeral=True,
        )
        return
    if not freefire_service.configured:
        await interaction.response.send_message(embed=_no_key_embed(), ephemeral=True)
        return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_stats(region.value, uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    mode_key = {
        "solo": "brCareer_solo",
        "duo": "brCareer_duo",
        "squad": "brCareer_squad",
    }

    embed = DesignEmbed(
        title=f"{emoji('stats')} FREE FIRE | Статистика",
        description=f"ID `{uid}` • режим: **{mode.name}**",
        category="game",
        footer="DISCORD SERVER r1z3",
    )

    careers: list[tuple[str, dict]] = []
    if mode.value == "all":
        for key, value in result.items():
            if key.startswith("brCareer_") and isinstance(value, dict):
                careers.append((key.replace("brCareer_", "").title(), value))
    else:
        careers.append((mode.name, result.get(mode_key[mode.value]) or {}))

    shown = 0
    for label, career in careers:
        lines = _career_fields(career)
        if not lines:
            continue
        embed.add_section(f"{emoji('sword')} {label}", "\n".join(lines))
        shown += 1

    if shown == 0:
        embed.description += "\n\nСтатистика по этому режиму отсутствует."

    await interaction.followup.send(embed=embed)


# ------------------------------------------------------------------- /ff ban
@ff_group.command(name="ban", description="Проверить бан аккаунта Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)")
async def ff_ban(interaction: discord.Interaction, uid: str) -> None:
    uid = uid.strip()
    if not _check_uid(uid):
        await interaction.response.send_message(
            embed=_error_embed(
                FreeFireError("ID игрока должен содержать 5-15 цифр.", "BAD_UID")
            ),
            ephemeral=True,
        )
        return
    if not freefire_service.configured:
        await interaction.response.send_message(embed=_no_key_embed(), ephemeral=True)
        return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_ban(uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    data = result.get("data") or {}
    is_banned = data.get("is_banned", 0)
    period = data.get("period")

    if is_banned:
        desc = f"Аккаунт `{uid}` **забанен**."
        if period:
            desc += f"\n{emoji('timer')} Период бана: **{period}**"
        embed = DesignEmbed(
            title=f"{emoji('ban')} FREE FIRE | Бан",
            description=desc,
            category="error",
            footer="DISCORD SERVER r1z3",
        )
    else:
        embed = DesignEmbed(
            title=f"{emoji('check')} FREE FIRE | Бан",
            description=f"Аккаунт `{uid}` **не забанен**.",
            category="success",
            footer="DISCORD SERVER r1z3",
        )

    await interaction.followup.send(embed=embed)


# --------------------------------------------------------------- /ff wishlist
@ff_group.command(name="wishlist", description="Вишлист игрока Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)", region="Регион аккаунта")
async def ff_wishlist(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str],
) -> None:
    uid = uid.strip()
    if not _check_uid(uid):
        await interaction.response.send_message(
            embed=_error_embed(
                FreeFireError("ID игрока должен содержать 5-15 цифр.", "BAD_UID")
            ),
            ephemeral=True,
        )
        return
    if not freefire_service.configured:
        await interaction.response.send_message(embed=_no_key_embed(), ephemeral=True)
        return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_wishlist(region.value, uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    items = result.get("wishlist_items") or []
    lines = []
    for item in items[:15]:
        if isinstance(item, dict):
            name = (
                item.get("name")
                or item.get("itemName")
                or item.get("title")
                or str(item.get("itemId", item.get("id", "?")))
            )
        else:
            name = str(item)
        lines.append(f"• {name}")

    embed = DesignEmbed(
        title=f"{emoji('star')} FREE FIRE | Вишлист",
        description=(
            f"ID `{uid}` — предметов: **{len(items)}**\n\n"
            + ("\n".join(lines) if lines else "Вишлист пуст.")
        ),
        category="game",
        footer="DISCORD SERVER r1z3",
    )

    await interaction.followup.send(embed=embed)


# ------------------------------------------------------------- /ff craftland
@ff_group.command(name="craftland", description="Информация о карте Craftland")
@app_commands.describe(code="Код карты", region="Регион")
async def ff_craftland(
    interaction: discord.Interaction,
    code: str,
    region: app_commands.Choice[str],
) -> None:
    if not freefire_service.configured:
        await interaction.response.send_message(embed=_no_key_embed(), ephemeral=True)
        return

    await interaction.response.defer()
    result = await _safe_call(
        freefire_service.get_craftland(region.value, code.strip())
    )
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    map_info = result.get("map_info") or result
    fields = (
        _scalar_items(map_info, limit=15)
        if isinstance(map_info, dict)
        else []
    )

    embed = DesignEmbed(
        title=f"{emoji('game')} FREE FIRE | Craftland",
        description=f"Карта `{code.strip()}`",
        category="game",
        footer="DISCORD SERVER r1z3",
    )
    embed.add_section(
        f"{emoji('info')} Информация",
        "\n".join(f"• **{k}** — {v}" for k, v in fields)
        if fields
        else "Детали карты недоступны.",
    )

    await interaction.followup.send(embed=embed)


async def setup(bot) -> None:
    bot.tree.add_command(ff_group)
