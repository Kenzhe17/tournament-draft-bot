"""Крэпс - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class CrapsGame:
    """Логика игры Крэпс."""

    def __init__(self):
        pass

    def roll_dice(self) -> tuple[int, int]:
        """Бросить кубики."""
        return random.randint(1, 6), random.randint(1, 6)

    def get_sum(self, dice1: int, dice2: int) -> int:
        """Получить сумму."""
        return dice1 + dice2


class CrapsModal(discord.ui.Modal, title="Крэпс"):
    """Модал для ставки в Крэпс."""

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

        self.choice = discord.ui.TextInput(
            label="Выбор",
            placeholder="pass/don't",
            default="pass",
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

        choice = self.choice.value or "pass"

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
        game = CrapsGame()
        dice1, dice2 = game.roll_dice()
        dice_sum = game.get_sum(dice1, dice2)

        # Упрощённые правила крэпса
        if choice == "pass":
            # Pass Line: 7 или 11 = победа, 2, 3, 12 = проигрыш
            if dice_sum in [7, 11]:
                won = True
                multiplier = 2.0
            elif dice_sum in [2, 3, 12]:
                won = False
                multiplier = 0.0
            else:
                # Для простоты - другие числа = проигрыш
                won = False
                multiplier = 0.0
        else:  # don't pass
            # Don't Pass: 2 или 3 = победа, 7 или 11 = проигрыш
            if dice_sum in [2, 3]:
                won = True
                multiplier = 2.0
            elif dice_sum in [7, 11]:
                won = False
                multiplier = 0.0
            else:
                # Для простоты - другие числа = проигрыш
                won = False
                multiplier = 0.0

        if won:
            winnings = int(bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🎲 Крэпс",
                description=f"**Ставка:** {bet} 🪙\n**Ваш выбор:** {choice}\n\n**Выпало:** {dice1} + {dice2} = {dice_sum}",
                color=discord.Color.green()
            )
            embed.add_field(
                name="✅ Выигрыш",
                value=f"{winnings} 🪙 ({multiplier}x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🎲 Крэпс",
                description=f"**Ставка:** {bet} 🪙\n**Ваш выбор:** {choice}\n\n**Выпало:** {dice1} + {dice2} = {dice_sum}",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "craps"
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
