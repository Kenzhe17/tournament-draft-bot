"""Покер - упрощённая PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class PokerGame:
    """Логика игры Покер."""

    def __init__(self):
        self.suits = ["♠️", "♥️", "♦️", "♣️"]
        self.values = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
        self.value_map = {v: i + 2 for i, v in enumerate(self.values)}

    def create_deck(self) -> list[str]:
        """Создать колоду."""
        deck = []
        for suit in self.suits:
            for value in self.values:
                deck.append(f"{value}{suit}")
        random.shuffle(deck)
        return deck

    def get_card_value(self, card: str) -> int:
        """Получить значение карты."""
        card_value = card[:-1]
        return self.value_map.get(card_value, 0)

    def evaluate_hand(self, hand: list[str]) -> tuple[str, int]:
        """Оценить руку и вернуть (название комбо, значение)."""
        values = sorted([self.get_card_value(card) for card in hand], reverse=True)
        suits = [card[-1] for card in hand]

        # Проверить флеш (все одной масти)
        is_flush = len(set(suits)) == 1

        # Проверить стрит (последовательные значения)
        is_straight = all(values[i] - values[i + 1] == 1 for i in range(len(values) - 1))

        # Подсчёт повторяющихся значений
        value_counts = {}
        for v in values:
            value_counts[v] = value_counts.get(v, 0) + 1

        counts = sorted(value_counts.values(), reverse=True)

        # Определить комбо
        if is_straight and is_flush:
            return "Стрит-флеш", 800
        elif counts == [4, 1]:
            return "Каре", 700
        elif counts == [3, 2]:
            return "Фулл-хаус", 600
        elif is_flush:
            return "Флеш", 500
        elif is_straight:
            return "Стрит", 400
        elif counts == [3, 1, 1]:
            return "Сет", 300
        elif counts == [2, 2, 1]:
            return "Две пары", 200
        elif counts == [2, 1, 1, 1]:
            return "Пара", 100
        else:
            return "Старшая карта", max(values)


class PokerModal(discord.ui.Modal, title="Покер"):
    """Модал для ставки в Покер."""

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
        game = PokerGame()
        deck = game.create_deck()

        # Раздать карты (по 5 карт каждому)
        player_hand = [deck.pop() for _ in range(5)]
        dealer_hand = [deck.pop() for _ in range(5)]

        player_combo, player_value = game.evaluate_hand(player_hand)
        dealer_combo, dealer_value = game.evaluate_hand(dealer_hand)

        # Определить победителя
        if player_value > dealer_value:
            won = True
            multiplier = 2.0
        else:
            won = False
            multiplier = 0.0

        if won:
            winnings = int(bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🃏 Покер",
                description=f"**Ставка:** {bet} 🪙\n\n**Ваша рука:** {' '.join(player_hand)}\n**Комбо:** {player_combo}\n\n**Рука дилера:** {' '.join(dealer_hand)}\n**Комбо:** {dealer_combo}",
                color=discord.Color.green()
            )
            embed.add_field(
                name="✅ Выигрыш",
                value=f"{winnings} 🪙 ({multiplier}x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🃏 Покер",
                description=f"**Ставка:** {bet} 🪙\n\n**Ваша рука:** {' '.join(player_hand)}\n**Комбо:** {player_combo}\n\n**Рука дилера:** {' '.join(dealer_hand)}\n**Комбо:** {dealer_combo}",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "poker"
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
