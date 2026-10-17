"""View для навигации по истории матчей."""
from config import replace_emojis, get_emoji
import discord
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

        # Page indicator
        indicator = discord.ui.Button(
            label=f"{self.current_page + 1}/{self.total_pages}",
            style=discord.ButtonStyle.secondary,
            disabled=True
        )
        self.add_item(indicator)

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
                await interaction.response.edit_message(embed=embed, view=parent_view)
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
                await interaction.response.edit_message(embed=embed, view=parent_view)
            else:
                await interaction.response.defer()


def create_history_embed(guild_id: int, user_id: int, matches: List[MatchHistory], stats: dict, page: int = 0, user_name: str = "", limit: int = 10) -> discord.Embed:
    """Создать embed для истории матчей."""

    # Форматирование даты
    def format_date(date: datetime) -> str:
        if isinstance(date, str):
            date = datetime.fromisoformat(date)
        return date.strftime("%d.%m.%Y %H:%M")

    # Форматирование списка игроков с Kill/Death
    def format_players(players: List[dict], user_id: int = None, user_name: str = None) -> str:
        if not players:
            return "Нет данных"
        formatted = []
        for p in players:
            name = p.get('name', 'Unknown')
            kills = p.get('kills', 0)
            deaths = p.get('deaths', 0)
            # Если это просматриваемый игрок, выделяем его стату
            # Сначала проверяем по user_id, если есть и совпадает
            # Затем проверяем по имени (для старых записей)
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

    # Если это первая страница (index 0), показываем статистику
    if page == 0:
        description_parts = [f"Статистика за последние {limit} матчей:\n"]

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
            description_parts.append(f"{replace_emojis('a_dot_smaller')} {format_players(match.teammates, user_id, user_name)}")
            description_parts.append("")

            # Противники
            description_parts.append(f"{replace_emojis('⚪')} **Противники ({match.opponent_team_name}):**")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} {format_players(match.opponents, user_id, user_name)}")
            description_parts.append("")

            # Статистика игрока
            result_text = "Победа" if match.result == "win" else "Поражение"

            description_parts.append(f"{replace_emojis('⚪')} **Ваша статистика:**")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Kill/Death: {match.kills}/{match.deaths}")
            description_parts.append(f"{replace_emojis('a_dot_smaller')} Результат: {result_text}")
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

    return embed
