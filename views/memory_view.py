"""View для Память."""

import discord
import asyncio
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class MemoryView(discord.ui.View):
    """View для Память."""

    def __init__(self, guild_id: int, user_id: int, bet: int, sequence: list[str]):
        super().__init__(timeout=10)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.sequence = sequence
        self.started = False

    @discord.ui.button(label="Готов", style=discord.ButtonStyle.primary, custom_id="memory_ready")
    async def ready_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Начать тест после запоминания."""
        if self.started:
            return

        self.started = True

        # Скрыть последовательность
        embed = discord.Embed(
            title="🧠 Память",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 2x\n\n**Повторите последовательность в правильном порядке:**",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="📝 Инструкция",
            value="Введите эмодзи через пробел в правильном порядке\nПример: 🍎 🍊 🍋",
            inline=False
        )

        # Показать модал для ввода
        await interaction.response.send_modal(MemoryAnswerModal(self.guild_id, self.user_id, self.bet, self.sequence))


class MemoryAnswerModal(discord.ui.Modal, title="Ваша последовательность"):
    """Модал для ввода последовательности."""

    def __init__(self, guild_id: int, user_id: int, bet: int, sequence: list[str]):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.sequence = sequence

        self.sequence_input = discord.ui.TextInput(
            label="Последовательность",
            placeholder="Введите эмодзи через пробел",
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Проверить последовательность."""
        user_sequence = self.sequence_input.value.split()
        correct_sequence = self.sequence

        # Проверить последовательность
        is_correct = user_sequence == correct_sequence

        # Рассчитать множитель
        multiplier = 2.0

        if is_correct:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Правильно!",
                description=f"**Последовательность:** {' '.join(correct_sequence)}\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Неправильно!",
                description=f"**Ваша последовательность:** {' '.join(user_sequence)}\n**Правильная последовательность:** {' '.join(correct_sequence)}\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "memory"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        if existing_stats:
            stats = existing_stats[0]
            stats["games_played"] = stats.get("games_played", 0) + 1
            if is_correct:
                stats["games_won"] = stats.get("games_won", 0) + 1
            stats["total_bet"] = stats.get("total_bet", 0) + self.bet
            stats["total_won"] = stats.get("total_won", 0) + winnings if is_correct else 0
            stats["net_profit"] = stats.get("net_profit", 0) + (winnings - self.bet) if is_correct else -self.bet
        else:
            # Создать новую статистику
            stats = {
                "game_id": game_id,
                "games_played": 1,
                "games_won": 1 if is_correct else 0,
                "total_bet": self.bet,
                "total_won": winnings if is_correct else 0,
                "net_profit": winnings - self.bet if is_correct else -self.bet
            }
            minigame_stats.append(stats)

        # Сохранить статистику
        await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

        await interaction.response.edit_message(embed=embed, view=None)
