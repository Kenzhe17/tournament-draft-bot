"""View для Угадай флаг."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class FlagQuizView(discord.ui.View):
    """View для Угадай флаг."""

    def __init__(self, guild_id: int, user_id: int, bet: int, flag_data: dict, game):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.flag_data = flag_data
        self.game = game

    async def show_flag(self, interaction: discord.Interaction) -> None:
        """Показать флаг."""
        flag_emoji = self.flag_data["emoji"]
        continent = self.flag_data["continent"]

        # Получить 4 варианта ответа
        options = [self.flag_data["country"]]
        other_flags = [f for f in self.game.flags if f["country"] != self.flag_data["country"]]
        random.shuffle(other_flags)
        options.extend([f["country"] for f in other_flags[:3]])
        random.shuffle(options)

        embed = discord.Embed(
            title="🏳 Угадай флаг",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 3x\n\n**Флаг:** {flag_emoji}\n**Континент:** {continent}",
            color=discord.Color.blue()
        )

        view = FlagChoiceView(self.guild_id, self.user_id, self.bet, self.flag_data, options)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class FlagChoiceView(discord.ui.View):
    """View для выбора страны."""

    def __init__(self, guild_id: int, user_id: int, bet: int, flag_data: dict, options: list[str]):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.flag_data = flag_data
        self.options = options

        # Добавить кнопки для каждой страны
        for i, option in enumerate(options):
            self.add_item(CountryButton(option, i, self))

    async def handle_selection(self, interaction: discord.Interaction, selected_country: str) -> None:
        """Обработать выбор страны."""
        correct_country = self.flag_data["country"]

        won = (selected_country == correct_country)
        multiplier = 3.0

        if won:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Флаг:** {self.flag_data['emoji']}\n**Правильный ответ:** {correct_country}\n**Ваш ответ:** {selected_country}\n\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Неправильно!",
                description=f"**Флаг:** {self.flag_data['emoji']}\n**Правильный ответ:** {correct_country}\n**Ваш ответ:** {selected_country}\n\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "flag_quiz"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        if existing_stats:
            stats = existing_stats[0]
            stats["games_played"] = stats.get("games_played", 0) + 1
            if won:
                stats["games_won"] = stats.get("games_won", 0) + 1
            stats["total_bet"] = stats.get("total_bet", 0) + self.bet
            stats["total_won"] = stats.get("total_won", 0) + winnings if won else 0
            stats["net_profit"] = stats.get("net_profit", 0) + (winnings - self.bet) if won else -self.bet
        else:
            stats = {
                "game_id": game_id,
                "games_played": 1,
                "games_won": 1 if won else 0,
                "total_bet": self.bet,
                "total_won": winnings if won else 0,
                "net_profit": winnings - self.bet if won else -self.bet
            }
            minigame_stats.append(stats)

        await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

        await interaction.response.edit_message(embed=embed, view=None)


class CountryButton(discord.ui.Button):
    """Кнопка для страны."""

    def __init__(self, country: str, index: int, view: FlagChoiceView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=country,
            custom_id=f"country_{index}"
        )
        self.country = country
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие на страну."""
        await self.view.handle_selection(interaction, self.country)
