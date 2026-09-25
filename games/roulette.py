"""Рулетка - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class RouletteGame:
    """Логика игры Рулетка."""

    def __init__(self):
        self.numbers = list(range(37))  # 0-36
        self.red_numbers = [1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]
        self.black_numbers = [2, 4, 6, 8, 10, 11, 13, 15, 17, 20, 22, 24, 26, 28, 29, 31, 33, 35]

    def spin(self) -> int:
        """Крутить рулетку и вернуть число."""
        return random.choice(self.numbers)

    def get_color(self, number: int) -> str:
        """Получить цвет числа."""
        if number == 0:
            return "green"
        elif number in self.red_numbers:
            return "red"
        else:
            return "black"

    def get_multiplier(self, bet_type: str, bet_value: str | None = None) -> float:
        """Получить множитель для ставки."""
        if bet_type == "color":
            return 2.0
        elif bet_type == "parity":
            return 2.0
        elif bet_type == "number" and bet_value:
            return 35.0
        return 0.0


class RouletteModal(discord.ui.Modal, title="Рулетка"):
    """Модал для ставки в Рулетку."""

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

        self.bet_type = discord.ui.TextInput(
            label="Тип ставки",
            placeholder="color/parity/number",
            default="color",
            max_length=10,
            required=False
        )

        self.bet_value = discord.ui.TextInput(
            label="Значение ставки",
            placeholder="Для color: red/black, parity: even/odd, number: 0-36",
            default="red",
            max_length=10,
            required=False
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

        bet_type = self.bet_type.value or "color"
        bet_value = self.bet_value.value or "red"

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
        game = RouletteGame()
        result = game.spin()
        color = game.get_color(result)
        multiplier = game.get_multiplier(bet_type, bet_value)

        # Проверить выигрыш
        won = False
        if bet_type == "color":
            won = (bet_value == color and color != "green")
        elif bet_type == "parity":
            if bet_value == "even":
                won = (result != 0 and result % 2 == 0)
            elif bet_value == "odd":
                won = (result % 2 == 1)
        elif bet_type == "number":
            won = (int(bet_value) == result)

        if won:
            winnings = int(bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🎰 Рулетка",
                description=f"**Ставка:** {bet} 🪙\n**Тип ставки:** {bet_type}\n**Значение:** {bet_value}\n\n**Выпало:** {result} ({color})",
                color=discord.Color.gold()
            )
            embed.add_field(
                name="✅ Выигрыш",
                value=f"{winnings} 🪙 ({multiplier}x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🎰 Рулетка",
                description=f"**Ставка:** {bet} 🪙\n**Тип ставки:** {bet_type}\n**Значение:** {bet_value}\n\n**Выпало:** {result} ({color})",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "roulette"
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
