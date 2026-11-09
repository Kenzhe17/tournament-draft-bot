"""View для навигации по истории матчей."""
from config import replace_emojis, get_emoji
import discord
from utils.design import compose
from typing import List, Optional
from datetime import datetime

from storage.match_history_store import match_history_store
from models.match_history import MatchHistory


class HistoryView(discord.ui.View):
    """View для навигации по истории матчей."""

    def __init__(self, guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, user_name: str = "", limit: int = 10):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.matches = matches
        self.stats = stats
        self.user_name = user_name
        self.limit = limit
        self.current_page = 0
        self.total_pages = len(matches) + 1  # +1 for stats page

        self.refresh_buttons()

    def refresh_buttons(self):
        """Обновляет состояние кнопок навигации."""
        # Remove existing navigation buttons
        for item in self.children[:]:
            if isinstance(item, discord.ui.Button):
                self.remove_item(item)

        # Previous button
        if self.current_page > 0:
            prev_button = PrevButton()
            self.add_item(prev_button)
        else:
            disabled_button = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)

        # Next button
        if self.current_page < self.total_pages - 1:
            next_button = NextButton()
            self.add_item(next_button)
        else:
            disabled_button = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.primary,
                disabled=True
            )
            self.add_item(disabled_button)


class PrevButton(discord.ui.Button):
    """Кнопка 'Назад'."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="◀"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти назад."""
        # Get the parent view
        parent_view = self.view
        if parent_view and hasattr(parent_view, 'current_page'):
            if parent_view.current_page > 0:
                parent_view.current_page -= 1
                parent_view.refresh_buttons()
                embed = create_history_embed(parent_view.guild_id, parent_view.user_id, parent_view.matches, parent_view.stats, parent_view.current_page, parent_view.user_name, parent_view.limit)
                await interaction.response.edit_message(
                    view=compose(embed, parent_view),
                    allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.defer()


class NextButton(discord.ui.Button):
    """Кнопка 'Вперед'."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label="▶"
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти вперед."""
        # Get the parent view
        parent_view = self.view
        if parent_view and hasattr(parent_view, 'current_page'):
            if parent_view.current_page < parent_view.total_pages - 1:
                parent_view.current_page += 1
                parent_view.refresh_buttons()
                embed = create_history_embed(parent_view.guild_id, parent_view.user_id, parent_view.matches, parent_view.stats, parent_view.current_page, parent_view.user_name, parent_view.limit)
                await interaction.response.edit_message(
                    view=compose(embed, parent_view),
                    allowed_mentions=discord.AllowedMentions.none())
            else:
                await interaction.response.defer()


_MATCH_TYPE_MAP = {
    "qualifier": "Отборочный",
    "semifinal": "Полуфинал",
    "final": "Финал"
}


def _format_date(date) -> str:
    """Discord-таймстамп — локализованная дата."""
    if isinstance(date, str):
        try:
            date = datetime.fromisoformat(date)
        except ValueError:
            return date
    if isinstance(date, datetime):
        return f"<t:{int(date.timestamp())}:f>"
    return str(date)


def _match_type_text(match) -> str:
    return _MATCH_TYPE_MAP.get(match.match_type, match.match_type)


def _format_players(players: List[dict], user_id: int = None, user_name: str = None) -> str:
    """Список игроков вида name `K/D`, свой игрок выделен жирным."""
    if not players:
        return "Нет данных"
    formatted = []
    for p in players:
        name = p.get('name', 'Unknown')
        kills = p.get('kills', 0)
        deaths = p.get('deaths', 0)
        is_viewer = False
        if user_id and 'user_id' in p and p.get('user_id') == user_id:
            is_viewer = True
        elif user_name and name.lower() == user_name.lower():
            is_viewer = True

        if is_viewer:
            formatted.append(f"**{name}** `({kills}/{deaths})`")
        else:
            formatted.append(f"{name} `({kills}/{deaths})`")
    return ", ".join(formatted)


def create_history_embed(guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, page: int = 0, user_name: str = "", limit: int = 10) -> discord.Embed:
    """Создать embed для истории матчей."""

    def _result_line(match: MatchHistory) -> str:
        icon = "✅" if match.result == "win" else "❌"
        return f"{icon} {_match_type_text(match)} — {match.team_name} vs {match.opponent_team_name} • {match.kills}/{match.deaths} K/D • ELO {match.elo_change:+d} • {_format_date(match.date)}"

    # Первая страница — сводка + последние результаты
    if page == 0:
        total = stats.get('total_matches', 0)
        wins = stats.get('wins', 0)
        losses = stats.get('losses', 0)
        win_rate = stats.get('win_rate', 0.0)

        parts = ["**Сводка**"]
        parts.append(f" Матчей: {total} • Побед: {wins} `({win_rate:.1f}%)` • Поражений: {losses}")
        parts.append(f" K/D/A: {stats.get('total_kills', 0)}/{stats.get('total_deaths', 0)}/{stats.get('total_assists', 0)}")
        parts.append(f" K+A/D: {stats.get('kda_ratio', stats.get('kd_ratio', 0)):.2f} • Средний урон: {stats.get('avg_damage', 0):,.0f}")
        parts.append(f" Всего урона: {stats.get('total_damage', 0):,.0f}")

        if matches:
            parts.append("")
            parts.append("**Последние результаты**")
            for m in matches[:5]:
                parts.append(f" {_result_line(m)}")
        else:
            parts.append("")
            parts.append(" Ещё не играл в турниры")

        title = f" ИСТОРИЯ МАТЧЕЙ — {user_name}" if user_name else f" ИСТОРИЯ МАТЧЕЙ"
        embed = discord.Embed(
            title=title,
            description="\n".join(parts),
            color=discord.Color.from_rgb(69, 69, 69)
        )
    else:
        # Конкретный матч (page - 1 = индекс, page 0 — статистика)
        match_index = page - 1
        if match_index < len(matches):
            match = matches[match_index]
            won = match.result == "win"
            result_text = "Победа" if won else "Поражение"

            parts = [
                f"**{_match_type_text(match)}**",
                f" {_format_date(match.date)}",
                "",
                f"**{match.team_name} vs {match.opponent_team_name}**",
                f" {('✅' if won else '❌')} {result_text} • ELO {match.elo_change:+d}",
                "",
                "**Ваша статистика**",
                f" K/D/A: {match.kills}/{match.deaths}/{match.assists}",
                f" Урон: {match.damage:,.0f}",
                "",
                f"**Союзники ({match.team_name})**",
                f" {_format_players(match.teammates, user_id, user_name)}",
                "",
                f"**Противники ({match.opponent_team_name})**",
                f" {_format_players(match.opponents, user_id, user_name)}",
            ]

            if match.bet_amount > 0:
                parts.append("")
                parts.append(f" **Ставка:** {match.bet_amount} {get_emoji('money')} → {match.bet_win} {get_emoji('money')}")

            embed = discord.Embed(
                title=f" ИСТОРИЯ МАТЧЕЙ — Матч #{match_index + 1}",
                description="\n".join(parts),
                color=discord.Color.from_rgb(69, 69, 69)
            )
        else:
            embed = discord.Embed(
                title=f" ИСТОРИЯ МАТЧЕЙ",
                description="Матч не найден",
                color=discord.Color.from_rgb(69, 69, 69)
            )

    return embed
