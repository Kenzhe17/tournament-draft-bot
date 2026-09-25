"""Баккара - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class BaccaratGame:
    """Логика игры Баккара."""

    def __init__(self):
        self.suits = ["♠️", "♥️", "♦️", "♣️"]
        self.values = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]

    def create_deck(self) -> list[str]:
        """Создать колоду."""
        deck = []
        for suit in self.suits:
            for value in self.values:
                deck.append(f"{value}{suit}")
        random.shuffle(deck)
        return deck

    def calculate_hand_value(self, hand: list[str]) -> int:
        """Рассчитать значение руки."""
        value = 0
        for card in hand:
            card_value = card[:-1]
            if card_value in ["J", "Q", "K"]:
                value += 0
            elif card_value == "A":
                value += 1
            else:
                value += int(card_value)
        return value % 10


class BaccaratModal(discord.ui.Modal, title="Баккара"):
    """Модал для ставки в Баккара."""

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
            placeholder="player/banker",
            default="player",
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

        choice = self.choice.value or "player"

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
        game = BaccaratGame()
        deck = game.create_deck()

        # Раздать карты
        player_hand = [deck.pop(), deck.pop()]
        banker_hand = [deck.pop(), deck.pop()]

        player_value = game.calculate_hand_value(player_hand)
        banker_value = game.calculate_hand_value(banker_hand)

        # Определить победителя
        if player_value > banker_value:
            winner = "player"
        elif banker_value > player_value:
            winner = "banker"
        else:
            winner = "tie"

        won = (choice == winner)

        if won:
            winnings = int(bet * 2.0)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🃏 Баккара",
                description=f"**Ставка:** {bet} 🪙\n**Ваш выбор:** {choice}\n\n**Player:** {' '.join(player_hand)} = {player_value}\n**Banker:** {' '.join(banker_hand)} = {banker_value}",
                color=discord.Color.green()
            )
            embed.add_field(
                name="✅ Выигрыш",
                value=f"{winnings} 🪙 (2x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🃏 Баккара",
                description=f"**Ставка:** {bet} 🪙\n**Ваш выбор:** {choice}\n\n**Player:** {' '.join(player_hand)} = {player_value}\n**Banker:** {' '.join(banker_hand)} = {banker_value}",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "baccarat"
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
