"""View для Время реакции."""

import discord
import random
import time
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class ReactionTimeView(discord.ui.View):
    """View для Время реакции."""

    def __init__(self, guild_id: int, user_id: int, bet: int, emoji: str, game):
        super().__init__(timeout=10)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.emoji = emoji
        self.game = game
        self.start_time = None
        self.can_click = False

    async def start_game(self, interaction: discord.Interaction) -> None:
        """Начать игру."""
        embed = discord.Embed(
            title="⚡ Время реакции",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 2x\n\nПодготовьтесь! Эмодзи появится через 2-5 секунд...",
            color=discord.Color.orange()
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

        # Случайная задержка 2-5 секунд
        delay = random.uniform(2, 5)
        import asyncio
        await asyncio.sleep(delay)

        # Показать эмодзи
        self.start_time = time.time()
        self.can_click = True

        embed = discord.Embed(
            title="⚡ НАЖМИ!",
            description=f"{self.emoji}",
            color=discord.Color.red()
        )

        view = ReactionButtonView(self.guild_id, self.user_id, self.bet, self)

        await interaction.edit_original_response(embed=embed, view=view)


class ReactionButtonView(discord.ui.View):
    """View для кнопки реакции."""

    def __init__(self, guild_id: int, user_id: int, bet: int, game_view: ReactionTimeView):
        super().__init__(timeout=2)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game_view = game_view

    @discord.ui.button(label="🎯 НАЖМИ!", style=discord.ButtonStyle.danger, custom_id="reaction_click")
    async def click_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Обработать нажатие."""
        if not self.game_view.can_click:
            return

        reaction_time = time.time() - self.game_view.start_time
        won = reaction_time < 1.0  # Меньше 1 секунды = победа
        multiplier = 2.0

        if won:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Победа!",
                description=f"**Время реакции:** {reaction_time:.3f} секунд\n**Ваше время:** {reaction_time:.3f}с < 1.0с\n\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Проигрыш",
                description=f"**Время реакции:** {reaction_time:.3f} секунд\n**Ваше время:** {reaction_time:.3f}с > 1.0с\n\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        # Обновить статистику
        game_id = "reaction_time"
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
