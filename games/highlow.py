"""High-Low - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class HighLowGame:
    """Логика игры High-Low."""

    def __init__(self):
        self.suits = ["♠️", "♥️", "♦️", "♣️"]
        self.values = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        self.value_map = {v: i + 2 for i, v in enumerate(self.values)}

    def get_card_value(self, card: str) -> int:
        """Получить значение карты."""
        card_value = card[:-1]
        return self.value_map.get(card_value, 0)

    def draw_card(self) -> str:
        """Нарисовать карту."""
        suit = random.choice(self.suits)
        value = random.choice(self.values)
        return f"{value}{suit}"


class HighLowModal(discord.ui.Modal, title="High-Low"):
    """Модал для ставки в High-Low."""

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

        self.guess = discord.ui.TextInput(
            label="Ваш выбор",
            placeholder="high/low",
            default="high",
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

        guess = self.guess.value or "high"

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
        game = HighLowGame()
        first_card = game.draw_card()
        second_card = game.draw_card()

        first_value = game.get_card_value(first_card)
        second_value = game.get_card_value(second_card)

        # Определить результат
        if second_value > first_value:
            result = "high"
        elif second_value < first_value:
            result = "low"
        else:
            result = "tie"

        won = (guess == result)

        if won:
            winnings = int(bet * 2.0)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🃏 High-Low",
                description=f"**Ставка:** {bet} 🪙\n**Ваш выбор:** {guess}\n\n**Первая карта:** {first_card}\n**Вторая карта:** {second_card}",
                color=discord.Color.green()
            )
            embed.add_field(
                name="✅ Выигрыш",
                value=f"{winnings} 🪙 (2x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🃏 High-Low",
                description=f"**Ставка:** {bet} 🪙\n**Ваш выбор:** {guess}\n\n**Первая карта:** {first_card}\n**Вторая карта:** {second_card}",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "highlow"
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
