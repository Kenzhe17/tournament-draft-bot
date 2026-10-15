"""View для навигации по истории матчей."""
from config import replace_emojis, get_emoji
import discord
from typing import List, Optional
from datetime import datetime

from storage.match_history_store import match_history_store
from models.match_history import MatchHistory


class HistoryView(discord.ui.View):
    """View для навигации по истории матчей."""

    def __init__(self, guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.matches = matches
        self.stats = stats
        self.current_page = 0
        self.total_pages = len(matches)

        # Only show navigation buttons if there are matches
        if matches:
            self.add_item(PrevButton(guild_id, user_id, matches, stats))
            self.add_item(NextButton(guild_id, user_id, matches, stats))
        else:
            self.add_item(NoMatchesButton())


class PrevButton(discord.ui.Button):
    """Кнопка 'Предыдущая'."""

    def __init__(self, guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="◀️ Предыдущая",
            custom_id=f"history_prev:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id
        self.matches = matches
        self.stats = stats

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти к предыдущему матчу."""
        view = interaction.message.view
        if view and hasattr(view, 'current_page'):
            if view.current_page > 0:
                view.current_page -= 1
                embed = create_history_embed(self.guild_id, self.user_id, self.matches, self.stats, view.current_page)
                await interaction.response.edit_message(embed=embed, view=view)
            else:
                await interaction.response.defer()


class NextButton(discord.ui.Button):
    """Кнопка 'Следующая'."""

    def __init__(self, guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Следующая ▶️",
            custom_id=f"history_next:{guild_id}:{user_id}"
        )
        self.guild_id = guild_id
        self.user_id = user_id
        self.matches = matches
        self.stats = stats

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перейти к следующему матчу."""
        view = interaction.message.view
        if view and hasattr(view, 'current_page'):
            if view.current_page < len(self.matches) - 1:
                view.current_page += 1
                embed = create_history_embed(self.guild_id, self.user_id, self.matches, self.stats, view.current_page)
                await interaction.response.edit_message(embed=embed, view=view)
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


def create_history_embed(guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, page: int = 0) -> discord.Embed:
    """Создать embed для истории матчей."""

    # Форматирование даты
    def format_date(date: datetime) -> str:
        if isinstance(date, str):
            date = datetime.fromisoformat(date)
        return date.strftime("%d.%m.%Y %H:%M")

    # Форматирование списка игроков с K/D
    def format_players(players: List[dict]) -> str:
        if not players:
            return "Нет данных"
        formatted = []
        for p in players:
            kd = p.get("kd", 0)
            formatted.append(f"{p.get('name', 'Unknown')} `({kd:.1f})`")
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

        if matches:
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Нажмите 'Следующая ▶️' для просмотра последнего матча")
        else:
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Ещё не игррал в турниры")

        embed = discord.Embed(
            title=f"{replace_emojis('a_star')} ИСТОРИЯ МАТЧЕЙ",
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
            kd = match.kills / match.deaths if match.deaths > 0 else 0
            result_emoji = "🏆" if match.result == "win" else "💔"
            result_text = "Победа" if match.result == "win" else "Поражение"

            description_parts.append(f"{replace_emojis('⚪')} **Ваша статистика:**")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} K/D: {match.kills}/{match.deaths} `({kd:.2f})`")
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
