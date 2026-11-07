"""Команды Free Fire API (siambhau FreeFireApi): /ff info|stats|ban|banner|wishlist."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import discord
from utils.design import compose
from discord import app_commands
from discord.ext import commands

from config import replace_emojis, get_emoji
from services import ff_catalog
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
    if value is None:
        return "—"
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


_BR_MODES = {"solostats": "Соло", "duostats": "Дуо", "quadstats": "Сквад"}


def _kd(kills: Any, deaths: Any) -> str:
    try:
        k, d = float(kills or 0), float(deaths or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{k / d:,.2f}" if d else "—"


def _fmt_hours(seconds: Any) -> str:
    try:
        return f"{int(seconds) / 3600:,.1f} ч"
    except (TypeError, ValueError):
        return "—"


def _fmt_km(meters: Any) -> str:
    try:
        return f"{int(meters) / 1000:,.0f} км"
    except (TypeError, ValueError):
        return "—"


def _get(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _stat_lines(block: dict[str, Any], br: bool) -> list[str]:
    """Строки статистики для одного блока (solo/duo/quad/cs)."""
    det = block.get("detailedstats") or {}
    kills = _get(block, "kills") or 0
    deaths = _get(det, "deaths") or _get(det, "knockdowns")
    games = _get(block, "gamesplayed")
    wins = _get(block, "wins")

    lines = [
        f"• Игр: **{_fmt_value(games)}** • Побед: **{_fmt_value(wins)}**",
        f"• Убийств: **{_fmt_value(kills)}** • Смертей: **{_fmt_value(deaths)}** • K/D: **{_kd(kills, deaths)}**",
    ]
    if br:
        lines += [
            f"• Топ: **{_fmt_value(_get(det, 'topntimes'))}** • Выживание: **{_fmt_hours(_get(det, 'survivaltime'))}**",
            f"• Урон: **{_fmt_value(_get(det, 'damage'))}** • Хедшоты: **{_fmt_value(_get(det, 'headshotkills'))}**",
            f"• Макс. киллов за матч: **{_fmt_value(_get(det, 'highestkills'))}** • Дистанция: **{_fmt_km(_get(det, 'distancetravelled'))}**",
        ]
        extra = []
        if _get(det, "revives"):
            extra.append(f"Ревайвы: {_fmt_value(_get(det, 'revives'))}")
        if _get(det, "knockdown"):
            extra.append(f"Нокдауны: {_fmt_value(_get(det, 'knockdown'))}")
        if _get(det, "roadkills"):
            extra.append(f"Убийства с транспорта: {_fmt_value(_get(det, 'roadkills'))}")
        if extra:
            lines.append("• " + " • ".join(extra))
    else:
        lines += [
            f"• Урон: **{_fmt_value(_get(det, 'damage'))}** • Хедшоты: **{_fmt_value(_get(det, 'headshotkills'))}**",
            f"• Ассисты: **{_fmt_value(_get(det, 'assists'))}** • MVP: **{_fmt_value(_get(det, 'mvpcount'))}**",
        ]
        multi = []
        for key, label in (("doublekills", "дабл"), ("triplekills", "трипл"), ("fourkills", "квадра")):
            val = _get(det, key)
            if val:
                multi.append(f"{label}: {_fmt_value(val)}")
        if multi:
            lines.append("• " + " • ".join(multi))
    return lines


def _stats_embed_text(stats: dict[str, Any]) -> str:
    """Форматирует stats из /freefireinfo/stats в текст для embed."""
    sections: list[str] = []
    is_cs = "csstats" in stats

    if is_cs:
        cs = stats.get("csstats") or {}
        sections.append("**Clash Squad**\n" + "\n".join(_stat_lines(cs, br=False)))
    else:
        for key, label in _BR_MODES.items():
            block = stats.get(key)
            if isinstance(block, dict) and block:
                sections.append(f"**{label}**\n" + "\n".join(_stat_lines(block, br=True)))

    if not sections:
        # Fallback: скалярные поля верхнего уровня
        lines = [f"• **{k}** — {_fmt_value(v)}" for k, v in _scalar_items(stats, limit=20)]
        return "\n".join(lines) if lines else "Данные отсутствуют."

    return "\n\n".join(sections)[:3900]


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

    await interaction.response.defer()
    result = await _safe_call(freefire_service.get_info(region.value, uid))
    if isinstance(result, FreeFireError):
        await interaction.followup.send( view=compose(_error_embed(result)))
        return

    basic = result.get("basicInfo") or {}
    clan = result.get("clanBasicInfo") or result.get("clanInfo") or {}
    guild_info = result.get("guildBasicInfo") or result.get("guildInfo") or {}
    social = result.get("socialInfo") or {}
    pet = result.get("petInfo") or {}
    profile = result.get("profileInfo") or {}
    credit = result.get("creditScoreInfo") or {}
    diamond = result.get("diamondCostRes") or {}
    prime = basic.get("primeInfo") or {}

    nickname = basic.get("nickname", "Unknown")
    signature = social.get("signature") or ""

    def _date(ts: Any) -> str:
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime(
                "%d.%m.%Y %H:%M UTC")
        except (TypeError, ValueError, OSError):
            return "—"

    def _mode_prefer() -> str:
        mp = social.get("modePrefer") or ""
        return {"ModePrefer_BR": "Battle Royale", "ModePrefer_CS": "Clash Squad"}.get(mp, mp or "—")

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
                f"• Уровень: **{basic.get('level', '—')}** • EXP: **{_num('exp')}**",
                f"• Ранг BR: **{basic.get('rank', '—')}** (макс: {basic.get('maxRank', '—')}) • Очки: **{_num('rankingPoints')}**",
                f"• Ранг CS: **{basic.get('csRank', '—')}** (макс: {basic.get('csMaxRank', '—')}) • Очки: **{_fmt_value(basic.get('csRankingPoints'))}**",
                f"• Лайки: **{_num('liked')}** • Бейджи: **{basic.get('badgeCnt', '—')}**",
                f"• Регион: {str(basic.get('region', region.value)).upper()} • Версия: {basic.get('releaseVersion', '—')} • Сезон: {basic.get('seasonId', '—')}",
            ]
        ),
        inline=False,
    )

    account_lines = [
        f"• Создан: {_date(basic.get('createAt'))}",
        f"• Последний вход: {_date(basic.get('lastLoginAt'))}",
        f"• Язык: {str(social.get('language', '—')).replace('Language_', '')}",
        f"• Любимый режим: {_mode_prefer()}",
        f"• Кредитный счёт: **{_fmt_value(credit.get('creditScore'))}**",
    ]
    if diamond.get("diamondCost") is not None:
        account_lines.append(f"• Diamond cost: **{_fmt_value(diamond.get('diamondCost'))}**")
    if prime.get("primeLevel"):
        account_lines.append(f"• Prime уровень: **{prime.get('primeLevel')}**")
    if pet.get("id"):
        pet_item = ff_catalog.get_item(pet.get("id"))
        pet_name = pet_item.get("name") if pet_item else f"ID `{pet.get('id')}`"
        account_lines.append(
            f"• Питомец: **{pet_name}** ур. **{pet.get('level', '—')}**"
            + (f" • EXP: {_fmt_value(pet.get('exp'))}" if pet.get("exp") else "")
        )
    embed.add_field(
        name=replace_emojis(f"{get_emoji('profile')} Аккаунт"),
        value="\n".join(account_lines),
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

    avatar_id = profile.get("avatarId")
    if avatar_id and ff_catalog.get_item(avatar_id):
        embed.set_thumbnail(url=ff_catalog.icon_url(avatar_id))

    clothes = [c for c in (profile.get("clothes") or []) if c]
    if clothes:
        names = []
        for cid in clothes:
            item = ff_catalog.get_item(cid)
            names.append(item.get("name") if item else f"`{cid}`")
        embed.add_field(
            name=replace_emojis(f"{get_emoji('sword')} Экипировка"),
            value="\n".join(f"• {n}" for n in names[:10]),
            inline=False,
        )

    await interaction.followup.send( view=compose(embed))


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
    region: app_commands.Choice[str] = None,
) -> None:
    uid = uid.strip()
    region = region or app_commands.Choice(name="RU / CIS", value="ru")
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
        await interaction.followup.send( view=compose(_error_embed(result)))
        return

    stats = result.get("stats") or result

    embed = discord.Embed(
        title=replace_emojis(f"{get_emoji('stats')} FREE FIRE | Статистика"),
        description=(
            f"ID `{uid}` • режим: **{mode.name}**\n\n{_stats_embed_text(stats)}"
        ),
        color=COLOR_MAIN,
    )
    embed.set_footer(text=FOOTER)
    await interaction.followup.send( view=compose(embed))


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


@ff_group.command(name="item", description="Поиск предмета Free Fire в каталоге")
@app_commands.describe(query="Название предмета или ID")
@app_commands.autocomplete(query=_item_autocomplete)
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
@ff_group.command(
    name="ocr", description="Распознать статистику CS 4x4 по скриншоту"
)
@app_commands.describe(screenshot="Скриншот экрана результатов матча")
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
