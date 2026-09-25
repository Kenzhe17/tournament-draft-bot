"""Блэкджек - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class BlackjackGame:
    """Логика игры Блэкджек."""

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
        aces = 0

        for card in hand:
            card_value = card[:-1]  # Убираем масть
            if card_value in ["J", "Q", "K"]:
                value += 10
            elif card_value == "A":
                aces += 1
                value += 11
            else:
                value += int(card_value)

        # Обработка тузов
        while value > 21 and aces > 0:
            value -= 10
            aces -= 1

        return value

    def is_blackjack(self, hand: list[str]) -> bool:
        """Проверить blackjack (21 с 2 карт)."""
        return len(hand) == 2 and self.calculate_hand_value(hand) == 21


class BlackjackModal(discord.ui.Modal, title="Блэкджек"):
    """Модал для ставки в Блэкджек."""

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
        game = BlackjackGame()
        deck = game.create_deck()

        # Раздать карты
        player_hand = [deck.pop(), deck.pop()]
        dealer_hand = [deck.pop(), deck.pop()]

        # Создать view для игры
        from views.blackjack_view import BlackjackView
        view = BlackjackView(self.guild_id, self.user_id, bet, deck, player_hand, dealer_hand, game)

        await view.show_hand(interaction, player_hand, dealer_hand)
