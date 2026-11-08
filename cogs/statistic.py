"""Команда /statistic — OCR скриншота результатов CS 4x4 (Битва отрядов)."""

from __future__ import annotations

import asyncio
import logging

import discord
from utils.design import compose
from discord import app_commands
from discord.ext import commands

from services import cs_parser
from services.ocr_service import (
    OCR_EXECUTOR,
    detect_winner_side,
    extract_text_boxes,
)

logger = logging.getLogger(__name__)

COLOR_MAIN = discord.Color.from_rgb(69, 69, 69)
COLOR_TOP5 = discord.Color.gold()
FOOTER = "DISCORD SERVER r1z3"

MAX_ATTACH = 10 * 1024 * 1024  # 10 MB


def _error_embed(text: str) -> discord.Embed:
    return discord.Embed(
        title="❌ STATISTIC | Ошибка",
        description=text,
        color=discord.Color.red(),
    )


def _team_field(title: str, players: list[dict]) -> str:
    if not players:
        return "*Не распознано*"
    lines = []
    for p in players:
        dmg = f" • DMG {p['dmg']:,}".replace(",", " ") if p.get("dmg") is not None else ""
        lines.append(
            f"**{p['name']}** — {p['kills']}/{p['deaths']}/{p['assists']}{dmg}"
        )
    return "\n".join(lines)


def _team_title(parsed: dict, side: str, is_winner: bool) -> str:
    """'🏆 Команда <название>' / '🔵 Команда <название>' — имя = шапка панели."""
    icon = "🏆" if is_winner else ("🔵" if side == "a" else "🔴")
    name = parsed.get(f"team_{side}_name") or side.upper()
    return f"{icon} Команда {name}"


def _result_line(parsed: dict) -> str | None:
    """Строка результата матча: победитель + баннер победы/поражения."""
    parts = []
    winner = parsed.get("winner")
    if winner in ("a", "b"):
        wname = parsed.get(f"team_{winner}_name") or winner.upper()
        parts.append(f"🏆 Победитель: **Команда {wname}**")
    banner = parsed.get("banner")
    if banner == "win":
        parts.append("✅ Ваш результат: **ПОБЕДА**")
    elif banner == "lose":
        parts.append("❌ Ваш результат: **ПОРАЖЕНИЕ**")
    return "\n".join(parts) if parts else None


def _build_embed(parsed: dict) -> discord.Embed:
    team_a, team_b = parsed["team_a"], parsed["team_b"]

    # MVP — максимальный урон среди всех
    all_players = [(p, "A") for p in team_a] + [(p, "B") for p in team_b]
    with_dmg = [x for x in all_players if x[0].get("dmg") is not None]
    mvp_name = None
    if with_dmg:
        mvp_name = max(with_dmg, key=lambda x: x[0]["dmg"])[0]["name"]

    embed = discord.Embed(
        title="⚔️ FREE FIRE | CS 4x4 — Результаты",
        color=COLOR_TOP5 if parsed.get("top5") else COLOR_MAIN,
    )
    desc_parts = []
    if parsed.get("top5"):
        desc_parts.append("🏆 **Вы попали в пятёрку лучших!**")
    result_line = _result_line(parsed)
    if result_line:
        desc_parts.append(result_line)
    if desc_parts:
        embed.description = "\n".join(desc_parts)
    # Подсветка победившей команды в заголовке поля
    winner = parsed.get("winner")
    embed.add_field(
        name=_team_title(parsed, "a", winner == "a"),
        value=_team_field("A", team_a),
        inline=True,
    )
    embed.add_field(
        name=_team_title(parsed, "b", winner == "b"),
        value=_team_field("B", team_b),
        inline=False,
    )
    if mvp_name:
        embed.set_footer(text=f"MVP по урону: {mvp_name} • {FOOTER}")
    else:
        embed.set_footer(text=FOOTER)
    return embed


async def run_statistic(
    interaction: discord.Interaction, screenshot: discord.Attachment
) -> None:
    """Общая логика OCR-статистики (используется /statistic и /ff ocr)."""
    ctype = (screenshot.content_type or "").lower()
    if not ctype.startswith("image/"):
        await interaction.response.send_message( view=compose(_error_embed("Файл не является изображением.")), ephemeral=True
        )
        return
    if screenshot.size and screenshot.size > MAX_ATTACH:
        await interaction.response.send_message( view=compose(_error_embed("Файл слишком большой (макс. 10 МБ).")),
            ephemeral=True,
        )
        return

    await interaction.response.defer()

    try:
        data = await screenshot.read()
        boxes = await extract_text_boxes(data)
    except Exception:
        logger.exception("OCR failed for /statistic")
        await interaction.followup.send( view=compose(_error_embed("Ошибка OCR — не удалось обработать изображение.")),
            ephemeral=True,
        )
        return

    if not boxes:
        await interaction.followup.send( view=compose(_error_embed(
                "На скриншоте не найдено текста. "
                "Нужен экран результатов «Битва отрядов»."
            )),
            ephemeral=True,
        )
        return

    parsed = cs_parser.parse_cs_results(boxes)
    if parsed is None or (not parsed["team_a"] and not parsed["team_b"]):
        logger.warning(
            "/statistic parse failed: %d boxes | %s",
            len(boxes),
            " | ".join(b["text"] for b in boxes[:60]),
        )
        await interaction.followup.send( view=compose(_error_embed(
                "Не удалось распознать таблицу игроков. "
                "Убедитесь, что это экран результатов CS 4x4."
            )),
            ephemeral=True,
        )
        return

    # Определяем победителя по цветовой подсветке панели
    try:
        side = await asyncio.get_running_loop().run_in_executor(
            OCR_EXECUTOR, detect_winner_side, data, boxes
        )
    except Exception:
        logger.exception("winner detection failed")
        side = None
    parsed["winner"] = {"left": "a", "right": "b"}.get(side)

    await interaction.followup.send( view=compose(_build_embed(parsed)))


class StatisticCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # Слэш-команда скрыта: декораторы закомментированы, код сохранён.
    # @app_commands.command(
    #     name="statistic", description="Распознать статистику CS 4x4 по скриншоту"
    # )
    # @app_commands.describe(screenshot="Скриншот экрана результатов матча")
    async def statistic(
        self, interaction: discord.Interaction, screenshot: discord.Attachment
    ) -> None:
        await run_statistic(interaction, screenshot)


async def setup(bot) -> None:
    await bot.add_cog(StatisticCog(bot))
