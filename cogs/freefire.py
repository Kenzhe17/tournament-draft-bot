"""Команды Free Fire API (siambhau FreeFireApi): /ff info|stats|ban|banner|wishlist."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, GUILD_ID, get_emoji, replace_emojis
from services import ff_catalog
from services.freefire_service import FreeFireError, freefire_service
from storage.ff_cooldowns import ff_info_cooldowns, ff_stats_cooldowns
from utils.design import compose
from utils.ff_stats_formatter import MODE_BY_KEY
from views.freefire_profile_view import FreeFireProfileView
from views.freefire_stats_view import FreeFireStatsView

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

FOOTER = "DISCORD SERVER r1z3"


def _check_uid(uid: str) -> bool:
    return bool(_UID_PATTERN.match(uid.strip()))


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


def _has_server_tag(user) -> bool:
    """Активный тег сервера r!z3 в профиле Discord (primary_guild)."""
    pg = getattr(user, "primary_guild", None)
    if pg is None or getattr(pg, "id", None) != GUILD_ID:
        return False
    return getattr(pg, "identity_enabled", None) is True


async def _check_and_key(interaction: discord.Interaction, uid: str) -> bool:
    """Тег r!z3 + валидация UID + API ключ. True — можно продолжать."""
    if not _has_server_tag(interaction.user):
        await interaction.response.send_message(
            "🚫 Команды `/ff` доступны только участникам "
            "с тегом **r!z3** в профиле Discord.",
            ephemeral=True,
        )
        return False
    if not _check_uid(uid):
        await interaction.response.send_message( view=compose(_error_embed(
                FreeFireError("ID игрока должен содержать 5-15 цифр.", "BAD_UID")
            )),
            ephemeral=True,
        )
        return False
    if not freefire_service.configured:
        await interaction.response.send_message( view=compose(_no_key_embed()), ephemeral=True)
        return False
    return True


# ------------------------------------------------------------------ /ff info
@ff_group.command(name="info", description="Профиль игрока Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)", region="Регион аккаунта")
@app_commands.choices(region=REGION_CHOICES)
async def ff_info(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str] = None,
) -> None:
    uid = uid.strip()
    region = region or app_commands.Choice(name="RU / CIS", value="ru")
    if not await _check_and_key(interaction, uid):
        return

    if interaction.user.id != BOT_OWNER_ID:
        remaining = ff_info_cooldowns.seconds_left(interaction.user.id)
        if remaining > 0:
            ready_at = int(time.time()) + remaining
            await interaction.response.send_message(
                f"⏳ `/ff info` доступна через <t:{ready_at}:R>.",
                ephemeral=True,
            )
            return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_info(region.value, uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send( view=compose(_error_embed(result)))
        return
    ff_info_cooldowns.mark_used(interaction.user.id)

    view = FreeFireProfileView(
        uid=uid,
        region=region.value,
        owner_id=interaction.user.id,
        data=result,
    )
    view.bound_message = await interaction.followup.send(view=view)


# ----------------------------------------------------------------- /ff stats
@ff_group.command(name="stats", description="Статистика игрока Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)", region="Регион аккаунта")
@app_commands.choices(region=REGION_CHOICES)
async def ff_stats(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str] = None,
) -> None:
    uid = uid.strip()
    region = region or app_commands.Choice(name="RU / CIS", value="ru")
    if not await _check_and_key(interaction, uid):
        return

    if interaction.user.id != BOT_OWNER_ID:
        remaining = ff_stats_cooldowns.seconds_left(interaction.user.id)
        if remaining > 0:
            ready_at = int(time.time()) + remaining
            await interaction.response.send_message(
                f"⏳ `/ff stats` доступна через <t:{ready_at}:R>.",
                ephemeral=True,
            )
            return

    async def _fetch(mode: str) -> dict[str, Any]:
        gamemode, matchmode, _ = MODE_BY_KEY[mode]
        res = await _safe_call(
            freefire_service.get_stats(
                region.value, uid, gamemode=gamemode, matchmode=matchmode
            )
        )
        if isinstance(res, FreeFireError):
            raise res
        return res

    await interaction.response.defer()
    result = await _safe_call(
        freefire_service.get_stats(
            region.value, uid, gamemode="br", matchmode="CAREER"
        )
    )
    if isinstance(result, FreeFireError):
        await interaction.followup.send( view=compose(_error_embed(result)))
        return
    ff_stats_cooldowns.mark_used(interaction.user.id)

    view = FreeFireStatsView(
        uid=uid,
        region=region.value,
        owner_id=interaction.user.id,
        data=result,
        fetch=_fetch,
    )
    view.bound_message = await interaction.followup.send(view=view)


# ----------------------------------------------------------------- /ff item
async def _item_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    items = ff_catalog.search_items(current, limit=25)
    return [
        app_commands.Choice(
            name=(f"{it.get('name', '?')} ({it.get('type', '?')})")[:100],
            value=str(it.get("itemID")),
        )
        for it in items
    ]


# Слэш-команда скрыта: декораторы закомментированы, код сохранён.
# @ff_group.command(name="item", description="Поиск предмета Free Fire в каталоге")
# @app_commands.describe(query="Название предмета или ID")
# @app_commands.autocomplete(query=_item_autocomplete)
async def ff_item(
    interaction: discord.Interaction, query: str
) -> None:
    query = query.strip()
    item = None
    if query.isdigit():
        item = ff_catalog.get_item(query)
    if item is None:
        results = ff_catalog.search_items(query, limit=1)
        item = results[0] if results else None
    if item is None:
        await interaction.response.send_message( view=compose(_error_embed(
                FreeFireError(f"Предмет «{query}» не найден в каталоге.", "NOT_FOUND")
            )),
            ephemeral=True,
        )
        return

    name = item.get("name") or "Unknown"
    desc = (item.get("description") or "").strip()
    embed = discord.Embed(
        title=name[:256],
        description=desc[:400] if desc else None,
        color=ff_catalog.rarity_color(item.get("rarity")),
    )
    embed.add_field(name="Тип", value=str(item.get("type") or "—"), inline=True)
    embed.add_field(
        name="Редкость", value=str(item.get("rarity") or "—"), inline=True
    )
    embed.add_field(name="ID", value=f"`{item.get('itemID')}`", inline=True)
    embed.set_thumbnail(url=ff_catalog.icon_url(item.get("itemID")))
    embed.set_footer(text=FOOTER)
    await interaction.response.send_message( view=compose(embed))


# ------------------------------------------------------------------ /ff ocr
# Слэш-команда скрыта: декораторы закомментированы, код сохранён.
# @ff_group.command(
#     name="ocr", description="Распознать статистику CS 4x4 по скриншоту"
# )
# @app_commands.describe(screenshot="Скриншот экрана результатов матча")
async def ff_ocr(
    interaction: discord.Interaction, screenshot: discord.Attachment
) -> None:
    from cogs.statistic import run_statistic

    await run_statistic(interaction, screenshot)


class FreeFireCog(commands.Cog):
    """Cog-носитель группы /ff (команды — app_commands.Group)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot


async def setup(bot) -> None:
    await bot.add_cog(FreeFireCog(bot))
    bot.tree.add_command(ff_group)
