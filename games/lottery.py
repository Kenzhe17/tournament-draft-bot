"""Лотерея - PvE игра."""

import random
import discord
from storage.user_balance_store import user_balance_store
from config import MIN_BET, MAX_BET


class LotteryGame:
    """Логика игры Лотерея."""

    def __init__(self):
        self.max_number = 50
        self.pick_count = 5

    def draw_numbers(self) -> list[int]:
        """Выбрать случайные числа."""
        return random.sample(range(1, self.max_number + 1), self.pick_count)

    def check_win(self, player_numbers: list[int], drawn_numbers: list[int]) -> tuple[int, float]:
        """Проверить выигрыш."""
        matches = len(set(player_numbers) & set(drawn_numbers))

        if matches == 5:
            return matches, 100.0
        elif matches == 4:
            return matches, 50.0
        elif matches == 3:
            return matches, 10.0
        elif matches == 2:
            return matches, 2.0
        else:
            return matches, 0.0


class LotteryModal(discord.ui.Modal, title="Лотерея"):
    """Модал для ставки в Лотерею."""

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

        self.numbers = discord.ui.TextInput(
            label="Ваши числа (через запятую)",
            placeholder="5 чисел от 1 до 50, например: 1,2,3,4,5",
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
            player_numbers = [int(n.strip()) for n in self.numbers.value.split(",")]
            if len(player_numbers) != 5 or any(n < 1 or n > 50 for n in player_numbers):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Введите 5 чисел от 1 до 50 через запятую!",
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
        game = LotteryGame()
        drawn_numbers = game.draw_numbers()
        matches, multiplier = game.check_win(player_numbers, drawn_numbers)

        if matches > 0:
            winnings = int(bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="🎟️ Лотерея",
                description=f"**Ставка:** {bet} 🪙\n**Ваши числа:** {', '.join(map(str, player_numbers))}\n**Выпавшие числа:** {', '.join(map(str, drawn_numbers))}",
                color=discord.Color.gold()
            )
            embed.add_field(
                name="✅ Совпадения",
                value=f"{matches} из 5",
                inline=False
            )
            embed.add_field(
                name="💰 Выигрыш",
                value=f"{winnings} 🪙 ({multiplier}x)",
                inline=False
            )
        else:
            embed = discord.Embed(
                title="🎟️ Лотерея",
                description=f"**Ставка:** {bet} 🪙\n**Ваши числа:** {', '.join(map(str, player_numbers))}\n**Выпавшие числа:** {', '.join(map(str, drawn_numbers))}",
                color=discord.Color.red()
            )
            embed.add_field(
                name="❌ Совпадения",
                value=f"{matches} из 5",
                inline=False
            )
            embed.add_field(
                name="💸 Проигрыш",
                value=f"{bet} 🪙",
                inline=False
            )

        # Обновить статистику
        from storage.minigame_store import minigame_store
        game_id = "lottery"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        if existing_stats:
            stats = existing_stats[0]
            stats["games_played"] = stats.get("games_played", 0) + 1
            if matches > 0:
                stats["games_won"] = stats.get("games_won", 0) + 1
            stats["total_bet"] = stats.get("total_bet", 0) + bet
            stats["total_won"] = stats.get("total_won", 0) + winnings if matches > 0 else 0
            stats["net_profit"] = stats.get("net_profit", 0) + (winnings - bet) if matches > 0 else -bet
        else:
            stats = {
                "game_id": game_id,
                "games_played": 1,
                "games_won": 1 if matches > 0 else 0,
                "total_bet": bet,
                "total_won": winnings if matches > 0 else 0,
                "net_profit": winnings - bet if matches > 0 else -bet
            }
            minigame_stats.append(stats)

        await minigame_store.update_player_stats(self.guild_id, self.user_id, game_id, stats)

        await interaction.response.send_message(embed=embed, ephemeral=True)
