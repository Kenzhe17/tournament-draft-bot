"""Слоты - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class SlotsGame:
    """Логика игры Слоты."""

    def __init__(self):
        self.symbols = ["🍒", "🍋", "🍊", "🍇", "🍉", "⭐", "💎", "7️⃣"]
        self.multipliers = {
            "🍒": 2,
            "🍋": 2,
            "🍊": 3,
            "🍇": 3,
            "🍉": 4,
            "⭐": 5,
            "💎": 7,
            "7️⃣": 10
        }

    def spin(self) -> list[str]:
        """Крутить слоты."""
        return [random.choice(self.symbols) for _ in range(3)]

    def check_win(self, result: list[str]) -> tuple[bool, float]:
        """Проверить выигрыш."""
        if result[0] == result[1] == result[2]:
            # 3 одинаковых
            multiplier = self.multipliers[result[0]]
            return True, multiplier
        elif result[0] == result[1] or result[1] == result[2] or result[0] == result[2]:
            # 2 одинаковых
            return True, 3.0
        else:
            return False, 0.0


class SlotsModal(discord.ui.Modal, title="Слоты"):
    """Модал для ставки в Слоты."""

    def __init__(self, guild_id: int, user_id: int):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

        self.bet = discord.ui.TextInput(
            label="Ставка (🪙)",
            placeholder="Введите сумму ставки",
            min_length=1,
            max_length=10,
            required=True
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Начать игру с указанной ставкой."""
        try:
            bet = int(self.bet.value)
        except ValueError:
            await interaction.response.send_message(
                "❌ Ставка должна быть числом!",
                ephemeral=True
            )
            return

        # Проверить баланс
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance} 🪙",
                ephemeral=True
            )
            return

        # Проверить лимиты ставок
        if bet < MIN_BET or bet > MAX_BET:
            await interaction.response.send_message(
                f"❌ Ставка должна быть между {MIN_BET} и {MAX_BET} 🪙",
                ephemeral=True
            )
            return

        # Списать ставку
        await user_balance_store.subtract_balance(self.guild_id, self.user_id, bet)

        # Создать сессию игры
        game = SlotsGame()
        result = game.spin()
        won, multiplier = game.check_win(result)

        if won:
            winnings = int(bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🎰 Слоты",
                description=f"**Ставка:** {bet} 🪙\n**Результат:** {' '.join(result)}",
                color=discord.Color.gold()
            )
            embed.add_field(
                name="✅ Выигрыш",
                value=f"{winnings} 🪙 ({multiplier}x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🎰 Слоты",
                description=f"**Ставка:** {bet} 🪙\n**Результат:** {' '.join(result)}",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "slots"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        if existing_stats:
            stats = existing_stats[0]
            stats["games_played"] = stats.get("games_played", 0) + 1
            if won:
                stats["games_won"] = stats.get("games_won", 0) + 1
            stats["total_bet"] = stats.get("total_bet", 0) + bet
            stats["total_won"] = stats.get("total_won", 0) + winnings if won else 0
            stats["net_profit"] = stats.get("net_profit", 0) + (winnings - bet) if won else -bet
        else:
            stats = {
                "game_id": game_id,
                "games_played": 1,
                "games_won": 1 if won else 0,
                "total_bet": bet,
                "total_won": winnings if won else 0,
                "net_profit": winnings - bet if won else -bet
            }
            minigame_stats.append(stats)

        await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

        await interaction.response.send_message(embed=embed, ephemeral=True)
