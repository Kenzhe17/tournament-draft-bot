"""View для Вспомни последовательность."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class SequenceMemoryView(discord.ui.View):
    """View для Вспомни последовательность."""

    def __init__(self, guild_id: int, user_id: int, bet: int, sequence: list[str], game):
        super().__init__(timeout=10)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.sequence = sequence
        self.game = game
        self.showing_sequence = True

    async def show_sequence(self, interaction: discord.Interaction) -> None:
        """Показать последовательность."""
        embed = discord.Embed(
            title="🧠 Вспомни последовательность",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 2x\n\n**Запомните последовательность:**\n{' '.join(self.sequence)}",
            color=discord.Color.purple()
        )

        embed.add_field(
            name="⏱️ Время",
            value="5 секунд",
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Подождать 5 секунд и показать варианты
        import asyncio
        await asyncio.sleep(5)

        await self.show_choices(interaction)

    async def show_choices(self, interaction: discord.Interaction) -> None:
        """Показать варианты ответа."""
        # Перемешать последовательность для вариантов
        shuffled = self.sequence.copy()
        random.shuffle(shuffled)

        embed = discord.Embed(
            title="🧠 Вспомни последовательность",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 2x\n\n**Выберите правильный порядок:**",
            color=discord.Color.purple()
        )

        view = SequenceChoiceView(self.guild_id, self.user_id, self.bet, self.sequence, shuffled)

        await interaction.edit_original_response(embed=embed, view=view)


class SequenceChoiceView(discord.ui.View):
    """View для выбора последовательности."""

    def __init__(self, guild_id: int, user_id: int, bet: int, correct_sequence: list[str], shuffled_sequence: list[str]):
        super().__init__(timeout=30)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.correct_sequence = correct_sequence
        self.shuffled_sequence = shuffled_sequence

        # Добавить кнопки для каждого эмодзи
        for i, emoji in enumerate(shuffled_sequence):
            self.add_item(EmojiButton(emoji, i, self))

    async def handle_selection(self, interaction: discord.Interaction, selected_emoji: str) -> None:
        """Обработать выбор эмодзи."""
        user_sequence = [selected_emoji]

        # Проверить результат
        if user_sequence == self.correct_sequence:
            won = True
            multiplier = 2.0
        else:
            won = False
            multiplier = 0.0

        if won:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Правильная последовательность:** {' '.join(self.correct_sequence)}\n**Ваша последовательность:** {' '.join(user_sequence)}\n\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Неправильно!",
                description=f"**Правильная последовательность:** {' '.join(self.correct_sequence)}\n**Ваша последовательность:** {' '.join(user_sequence)}\n\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "sequence_memory"
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


class EmojiButton(discord.ui.Button):
    """Кнопка для эмодзи."""

    def __init__(self, emoji: str, index: int, view: SequenceChoiceView):
        super().__init__(
            style=discord.ButtonStyle.primary,
            label=emoji,
            custom_id=f"emoji_{index}"
        )
        self.emoji = emoji
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        """Обработать нажатие на эмодзи."""
        await self.view.handle_selection(interaction, self.emoji)
