"""View для навигации по истории матчей."""
from config import replace_emojis, get_emoji
import discord
from typing import List, Optional
from datetime import datetime

from storage.match_history_store import match_history_store
from models.match_history import MatchHistory


class HistoryView(discord.ui.View):
    """View для навигации по истории матчей."""

    def __init__(self, guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, user_name: str = ""):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.matches = matches
        self.stats = stats
        self.user_name = user_name
        self.current_page = 0
        self.total_pages = len(matches)

        # Only show navigation buttons if there are matches
        if matches:
            self.add_item(PrevButton(guild_id, user_id, matches, stats, user_name))
            self.add_item(NextButton(guild_id, user_id, matches, stats, user_name))
        else:
            self.add_item(NoMatchesButton())


class PrevButton(discord.ui.Button):
    """Кнопка 'Назад'."""

    def __init__(self, guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, user_name: str = ""):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀️ Назад",
            custom_id=f"history_prev:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id
        self.matches = matches
        self.stats = stats
        self.user_name = user_name

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти назад."""
        # Get the parent view
        parent_view = self.view
        if parent_view and hasattr(parent_view, 'current_page'):
            if parent_view.current_page > 0:
                parent_view.current_page -= 1
                embed = create_history_embed(self.guild_id, self.user_id, self.matches, self.stats, parent_view.current_page, self.user_name)
                await interaction.response.edit_message(embed=embed, view=parent_view)
            else:
                await interaction.response.defer()


class NextButton(discord.ui.Button):
    """Кнопка 'Вперед'."""

    def __init__(self, guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, user_name: str = ""):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Вперед ▶️",
            custom_id=f"history_next:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id
        self.matches = matches
        self.stats = stats
        self.user_name = user_name

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти вперед."""
        # Get the parent view
        parent_view = self.view
        if parent_view and hasattr(parent_view, 'current_page'):
            if parent_view.current_page < len(self.matches) - 1:
                parent_view.current_page += 1
                embed = create_history_embed(self.guild_id, self.user_id, self.matches, self.stats, parent_view.current_page, self.user_name)
                await interaction.response.edit_message(embed=embed, view=parent_view)
            else:
                await interaction.response.defer()


class NoMatchesButton(discord.ui.Button):
    """Кнопка для случая когда нет матчей."""

    def __init__(self):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Нет матчей",
            disabled=True
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        """Ничего не делать."""
        pass


def create_history_embed(guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, page: int = 0, user_name: str = "") -> discord.Embed:
    """Создать embed для истории матчей."""

    # Форматирование даты
    def format_date(date: datetime) -> str:
        if isinstance(date, str):
            date = datetime.fromisoformat(date)
        return date.strftime("%d.%m.%Y %H:%M")

    # Форматирование списка игроков с Kill/Death
    def format_players(players: List[dict]) -> str:
        if not players:
            return "Нет данных"
        formatted = []
        for p in players:
            formatted.append(f"{p.get('name', 'Unknown')} `({p.get('kills', 0)}/{p.get('deaths', 0)})`")
        return ", ".join(formatted)

    # Если это первая страница (index 0), показываем статистику
    if page == 0:
        description_parts = ["Статистика за последние 10 матчей:\n"]

        # Общая статистика
        description_parts.append(f"{replace_emojis('⚪')} **Общая статистика:**")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Матчей: {stats['total_matches']}")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Побед: {stats['wins']} | Поражений: {stats['losses']}")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} Винрейт: {stats['win_rate']:.1f}%")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} K/D Ratio: {stats['kd_ratio']:.2f}")
        description_parts.append(f"{replace_emojis('a_dot_smaller')} AVG Kills: {stats['avg_kills']:.2f}")
        description_parts.append("")

        if not matches:
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Ещё не играл в турниры")

        title = f"{replace_emojis('a_star')} ИСТОРИЯ МАТЧЕЙ — {user_name}" if user_name else f"{replace_emojis('a_star')} ИСТОРИЯ МАТЧЕЙ"
        embed = discord.Embed(
            title=title,
            description="\n".join(description_parts),
            color=discord.Color.from_rgb(69, 69, 69)
        )
    else:
        # Показываем конкретный матч (page - 1 = индекс в списке, так как page 0 это статистика)
        match_index = page - 1
        if match_index < len(matches):
            match = matches[match_index]

            description_parts = []

            # Тип матча
            match_type_map = {
                "qualifier": "Отбор",
                "semifinal": "Полуфинал",
                "final": "Финал"
            }
            match_type_text = match_type_map.get(match.match_type, match.match_type)

            description_parts.append(f"{replace_emojis('⚪')} **Тип матча:** {match_type_text}")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Время: {format_date(match.date)}")
            description_parts.append("")

            # Команда игрока
            description_parts.append(f"{replace_emojis('⚪')} **Ваша команда ({match.team_name}):**")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} {format_players(match.teammates)}")
            description_parts.append("")

            # Противники
            description_parts.append(f"{replace_emojis('⚪')} **Противники ({match.opponent_team_name}):**")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} {format_players(match.opponents)}")
            description_parts.append("")

            # Статистика игрока
            result_emoji = "🏆" if match.result == "win" else "💔"
            result_text = "Победа" if match.result == "win" else "Поражение"

            description_parts.append(f"{replace_emojis('⚪')} **Ваша статистика:**")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Kill/Death: {match.kills}/{match.deaths}")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Результат: {result_emoji} {result_text}")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} ELO: {match.elo_change:+d}")

            # Ставки если есть
            if match.bet_amount > 0:
                description_parts.append(f"{replace_emojis('a_dot_smaller')} Ставка: {match.bet_amount} → {match.bet_win} {get_emoji('money')}")

            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} ИСТОРИЯ МАТЧЕЙ — Матч #{match_index + 1}",
                description="\n".join(description_parts),
                color=discord.Color.from_rgb(69, 69, 69)
            )
        else:
            embed = discord.Embed(
                title=f"{replace_emojis('a_star')} ИСТОРИЯ МАТЧЕЙ",
                description="Матч не найден",
                color=discord.Color.from_rgb(69, 69, 69)
            )

    # Page indicator
    footer_text = f"Страница {page + 1}/{len(matches) + 1 if matches else 1}"
    embed.set_footer(text=footer_text)

    return embed
