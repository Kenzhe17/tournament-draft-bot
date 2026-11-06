"""Команды Free Fire API (siambhau FreeFireApi): /ff info|stats|ban|banner|wishlist|craftland."""

from __future__ import annotations

import io
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
from utils.design.cv2 import design_card

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


def _check_uid(uid: str) -> bool:
    return bool(_UID_PATTERN.match(uid.strip()))


def _prettify_key(key: str) -> str:
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", key)
    spaced = spaced.replace("_", " ")
    return spaced.strip().title()


def _scalar_items(data: dict[str, Any], limit: int = 20) -> list[tuple[str, Any]]:
    items = []
    for key, value in data.items():
        if isinstance(value, (dict, list)):
            continue
        items.append((_prettify_key(key), value))
        if len(items) >= limit:
            break
    return items


def _fmt_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


async def _safe_call(coro):
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
            "Установите переменную `FREEFIRE_API_KEY`."
        ),
        category="warning",
        footer="DISCORD SERVER r1z3",
    )


def _uid_guard(uid: str):
    return _check_uid(uid)


async def _check_and_key(interaction: discord.Interaction, uid: str) -> bool:
    """Валидация UID + API ключ. True — можно продолжать."""
    if not _uid_guard(uid):
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
    region: app_commands.Choice[str],
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
    guild = result.get("guildBasicInfo") or result.get("guildInfo") or {}
    social = result.get("socialInfo") or {}

    nickname = basic.get("nickname", "Unknown")
    signature = social.get("signature") or ""

    def _num(key: str):
        v = basic.get(key)
        return f"{v:,}" if isinstance(v, int) else (v if v is not None else "—")

    avatar = (basic.get("avatar") or basic.get("headpic")
              or basic.get("profilePic") or basic.get("profile_pic"))

    team_lines = []
    clan_name = clan.get("clanName") or guild.get("name") or guild.get("guildName")
    if clan_name:
        team_lines.append(f"• {clan.get('clanName') and 'Клан' or 'Гильдия'}: **{clan_name}**")
    if clan.get("memberNum") is not None:
        team_lines.append(f"• Участники: {clan.get('memberNum', '—')}/{clan.get('capacity', '—')}")

    view = design_card(
        category="game",
        title="FREE FIRE | Профиль",
        description=(
            f"**{nickname}** — ID `{uid}`" + (f"\n{signature}" if signature else "")
        ),
        thumbnail_url=avatar if isinstance(avatar, str) and avatar.startswith("http") else None,
        kv=[
            ("Уровень", str(basic.get("level", "—"))),
            ("Ранг", f"{basic.get('rank', '—')} (макс: {basic.get('maxRank', '—')})"),
            ("Очки рейтинга", str(_num("rankingPoints"))),
            ("Лайки", str(_num("liked"))),
            ("Регион", str(basic.get("region", region.value)).upper()),
        ],
        sections=[("Команда", "\n".join(team_lines))] if team_lines else None,
        footer="DISCORD SERVER r1z3",
    )
    await interaction.followup.send(view=view)


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
    region: app_commands.Choice[str],
    mode: app_commands.Choice[str],
) -> None:
    uid = uid.strip()
    if not await _check_and_key(interaction, uid):
        return

    gamemode, matchmode = mode.value.split("_", 1)
    await interaction.response.defer()
    result = await _safe_call(
        freefire_service.get_stats(region.value, uid,
                                    gamemode=gamemode, matchmode=matchmode.upper())
    )
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    stats = result.get("stats") or result
    lines = [f"**{k}** — {_fmt_value(v)}"
             for k, v in _scalar_items(stats, limit=15)]

    view = design_card(
        category="game",
        title="FREE FIRE | Статистика",
        description=f"ID `{uid}` • режим: **{mode.name}**",
        sections=[("Показатели", "\n".join(lines) if lines else "Данные отсутствуют.")],
        footer="DISCORD SERVER r1z3",
    )
    await interaction.followup.send(view=view)


# ------------------------------------------------------------------- /ff ban
@ff_group.command(name="ban", description="Проверить бан аккаунта Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)")
async def ff_ban(interaction: discord.Interaction, uid: str) -> None:
    uid = uid.strip()
    if not await _check_and_key(interaction, uid):
        return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_ban(uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    ban_status = str(result.get("ban_status", "")).lower()
    period = result.get("ban_period")
    nickname = result.get("nickname", "")

    if "not banned" in ban_status or "no ban" in ban_status:
        view = design_card(
            category="success",
            title="FREE FIRE | Бан",
            description=f"Аккаунт `{uid}` **не забанен**."
                        + (f"\nИгрок: **{nickname}**" if nickname else ""),
            footer="DISCORD SERVER r1z3",
        )
    else:
        desc = f"Аккаунт `{uid}` — статус: **{result.get('ban_status', 'banned')}**"
        if period:
            desc += f"\nПериод: **{period}**"
        view = design_card(
            category="error",
            title="FREE FIRE | Бан",
            description=desc,
            footer="DISCORD SERVER r1z3",
        )
    await interaction.followup.send(view=view)


# ---------------------------------------------------------------- /ff banner
@ff_group.command(name="banner", description="Баннер профиля Free Fire (PNG)")
@app_commands.describe(uid="ID игрока (5-15 цифр)", region="Регион аккаунта")
@app_commands.choices(region=REGION_CHOICES)
async def ff_banner(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str],
) -> None:
    uid = uid.strip()
    if not await _check_and_key(interaction, uid):
        return

    await interaction.response.defer()
    try:
        data, content_type = await freefire_service.get_banner(region.value, uid)
    except FreeFireError as e:
        await interaction.followup.send(embed=_error_embed(e))
        return
    except Exception:
        logger.exception("ff banner failed")
        await interaction.followup.send(embed=_error_embed(
            FreeFireError("Не удалось получить баннер.", "INTERNAL")))
        return

    if not data or len(data) < 100:
        await interaction.followup.send(embed=_error_embed(
            FreeFireError("Баннер не найден или недоступен.", "NOT_FOUND")))
        return

    file = discord.File(io.BytesIO(data), filename=f"ff_banner_{uid}.png")
    view = design_card(
        category="game",
        title="FREE FIRE | Баннер",
        description=f"ID `{uid}`",
        footer="DISCORD SERVER r1z3",
    )
    await interaction.followup.send(view=view, file=file)


# --------------------------------------------------------------- /ff wishlist
@ff_group.command(name="wishlist", description="Вишлист игрока Free Fire")
@app_commands.describe(uid="ID игрока (5-15 цифр)", region="Регион аккаунта")
@app_commands.choices(region=REGION_CHOICES)
async def ff_wishlist(
    interaction: discord.Interaction,
    uid: str,
    region: app_commands.Choice[str],
) -> None:
    uid = uid.strip()
    if not await _check_and_key(interaction, uid):
        return

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_wishlist(region.value, uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    items = result.get("wishlist") or result.get("items") or []
    if isinstance(items, dict):
        items = list(items.values())
    lines = []
    for item in items[:15]:
        if isinstance(item, dict):
            name = (item.get("name") or item.get("itemName")
                    or item.get("title") or item.get("itemId")
                    or str(item.get("id", "?")))
        else:
            name = str(item)
        lines.append(f"• {name}")

    view = design_card(
        category="game",
        title="FREE FIRE | Вишлист",
        description=f"ID `{uid}` — предметов: **{len(items)}**",
        sections=[("Предметы", "\n".join(lines) if lines else "Вишлист пуст.")],
        footer="DISCORD SERVER r1z3",
    )
    await interaction.followup.send(view=view)


# ------------------------------------------------------------- /ff craftland
@ff_group.command(name="craftland", description="Информация о карте Craftland")
@app_commands.describe(code="Код карты", region="Регион")
@app_commands.choices(region=REGION_CHOICES)
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
        # fallback на полные детали
        result = await _safe_call(
            freefire_service.get_craftland_details(region.value, code.strip())
        )
    if isinstance(result, FreeFireError):
        await interaction.followup.send(embed=_error_embed(result))
        return

    map_info = result.get("map_info") or result.get("data") or result
    fields = (
        _scalar_items(map_info, limit=15)
        if isinstance(map_info, dict)
        else []
    )

    view = design_card(
        category="game",
        title="FREE FIRE | Craftland",
        description=f"Карта `{code.strip()}`",
        sections=[("Информация",
                   "\n".join(f"• **{k}** — {_fmt_value(v)}" for k, v in fields)
                   if fields else "Детали карты недоступны.")],
        footer="DISCORD SERVER r1z3",
    )
    await interaction.followup.send(view=view)


async def setup(bot) -> None:
    bot.tree.add_command(ff_group)
