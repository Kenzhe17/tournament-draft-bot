"""Dicebet - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class DicebetGame:
    """Логика игры Dicebet."""

    def __init__(self):
        pass

    def roll_dice(self) -> tuple[int, int]:
        """Бросить кубики."""
        return random.randint(1, 6), random.randint(1, 6)

    def get_sum(self, dice1: int, dice2: int) -> int:
        """Получить сумму."""
        return dice1 + dice2


class DicebetModal(discord.ui.Modal, title="Dicebet"):
    """Модал для ставки в Dicebet."""

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

        self.prediction = discord.ui.TextInput(
            label="Предсказание суммы (2-12)",
            placeholder="Число от 2 до 12",
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

        try:
            prediction = int(self.prediction.value)
            if prediction < 2 or prediction > 12:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Введите число от 2 до 12!",
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
        game = DicebetGame()
        dice1, dice2 = game.roll_dice()
        dice_sum = game.get_sum(dice1, dice2)

        # Определить множитель
        if prediction == dice_sum:
            # Множитель зависит от суммы (более редкие суммы имеют больший множитель)
            if dice_sum == 2 or dice_sum == 12:
                multiplier = 6.0
            elif dice_sum == 3 or dice_sum == 11:
                multiplier = 5.0
            elif dice_sum == 4 or dice_sum == 10:
                multiplier = 4.0
            elif dice_sum == 5 or dice_sum == 9:
                multiplier = 3.0
            else:
                multiplier = 2.0
            won = True
        else:
            multiplier = 0.0
            won = False

        if won:
            winnings = int(bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🎲 Dicebet",
                description=f"**Ставка:** {bet} 🪙\n**Ваше предсказание:** {prediction}\n\n**Выпало:** {dice1} + {dice2} = {dice_sum}",
                color=discord.Color.green()
            )
            embed.add_field(
                name="✅ Выигрыш",
                value=f"{winnings} 🪙 ({multiplier}x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🎲 Dicebet",
                description=f"**Ставка:** {bet} 🪙\n**Ваше предсказание:** {prediction}\n\n**Выпало:** {dice1} + {dice2} = {dice_sum}",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "dicebet"
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
