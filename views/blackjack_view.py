"""View для Блэкджек."""

import discord
from storage.user_balance_store import user_balance_store
from storage.minigame_store import minigame_store


class BlackjackView(discord.ui.View):
    """View для Блэкджек."""

    def __init__(self, guild_id: int, user_id: int, bet: int, deck: list[str], player_hand: list[str], dealer_hand: list[str], game):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.game = game
        self.game_over = False

    async def show_hand(self, interaction: discord.Interaction, player_hand: list[str], dealer_hand: list[str], show_dealer: bool = False) -> None:
        """Показать руки."""
        player_value = self.game.calculate_hand_value(player_hand)
        dealer_value = self.game.calculate_hand_value(dealer_hand)

        embed = discord.Embed(
            title="🃏 Блэкджек",
            description=f"**Ставка:** {self.bet} 🪙\n**Множитель:** 2x (победа), 3x (blackjack)",
            color=discord.Color.dark_green()
        )

        embed.add_field(
            name="👤 Ваша рука",
            value=f"{' '.join(player_hand)} = {player_value}",
            inline=False
        )

        if show_dealer:
            embed.add_field(
                name="🎰 Рука дилера",
                value=f"{' '.join(dealer_hand)} = {dealer_value}",
                inline=False
            )
        else:
            embed.add_field(
                name="🎰 Рука дилера",
                value=f"{dealer_hand[0]} ?",
                inline=False
            )

        if not self.game_over:
            view = BlackjackActionView(self.guild_id, self.user_id, self.bet, self.deck, player_hand, dealer_hand, self.game)
        else:
            view = None

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def hit(self, interaction: discord.Interaction) -> None:
        """Взять карту."""
        self.player_hand.append(self.deck.pop())
        player_value = self.game.calculate_hand_value(self.player_hand)

        if player_value > 21:
            # Перебор
            self.game_over = True
            dealer_value = self.game.calculate_hand_value(self.dealer_hand)

            if dealer_value > 21:
                # Дилер тоже перебрал - ничья
                await user_balance_store.add_balance(self.guild_id, self.user_id, self.bet)
                embed = discord.Embed(
                    title="🃏 Блэкджек",
                    description=f"**Перебор!**\nВаша рука: {' '.join(self.player_hand)} = {player_value}\nРука дилера: {' '.join(self.dealer_hand)} = {dealer_value}\n\nСтавка возвращена.",
                    color=discord.Color.orange()
                )
            else:
                embed = discord.Embed(
                    title="🃏 Блэкджек",
                    description=f"**Перебор!**\nВаша рука: {' '.join(self.player_hand)} = {player_value}\nРука дилера: {' '.join(self.dealer_hand)} = {dealer_value}\n\nПотеря: {self.bet} 🪙",
                    color=discord.Color.red()
                )

            await self.update_stats(interaction, False)
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await self.show_hand(interaction, self.player_hand, self.dealer_hand)

    async def stand(self, interaction: discord.Interaction) -> None:
        """Остановиться."""
        # Дилер добирает карты до 17+
        while self.game.calculate_hand_value(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())

        player_value = self.game.calculate_hand_value(self.player_hand)
        dealer_value = self.game.calculate_hand_value(self.dealer_hand)
        self.game_over = True

        # Определить победителя
        if player_value > 21:
            won = False
        elif dealer_value > 21:
            won = True
        elif player_value > dealer_value:
            won = True
        elif dealer_value > player_value:
            won = False
        else:
            won = True  # Ничья в пользу игрока

        # Проверить blackjack
        player_blackjack = self.game.is_blackjack(self.player_hand)
        multiplier = 3.0 if player_blackjack else 2.0

        if won:
            winnings = int(self.bet * multiplier)
            await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)

            embed = discord.Embed(
                title="✅ Победа!",
                description=f"**Ваша рука:** {' '.join(self.player_hand)} = {player_value}\n**Рука дилера:** {' '.join(self.dealer_hand)} = {dealer_value}\n\n**Выигрыш:** {winnings} 🪙 ({multiplier}x)",
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Проигрыш",
                description=f"**Ваша рука:** {' '.join(self.player_hand)} = {player_value}\n**Рука дилера:** {' '.join(self.dealer_hand)} = {dealer_value}\n\n**Потеря:** {self.bet} 🪙",
                color=discord.Color.red()
            )

        await self.update_stats(interaction, won)
        await interaction.response.edit_message(embed=embed, view=None)

    async def update_stats(self, interaction: discord.Interaction, won: bool) -> None:
        """Обновить статистику."""
        game_id = "blackjack"
        minigame_stats = await minigame_store.get_player_stats(self.guild_id, self.user_id)
        existing_stats = [s for s in minigame_stats if s.get("game_id") == game_id]

        winnings = int(self.bet * 3.0) if won and self.game.is_blackjack(self.player_hand) else int(self.bet * 2.0) if won else 0

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


class BlackjackActionView(discord.ui.View):
    """View для действий в Блэкджек."""

    def __init__(self, guild_id: int, user_id: int, bet: int, deck: list[str], player_hand: list[str], dealer_hand: list[str], game):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.deck = deck
        self.player_hand = player_hand
        self.dealer_hand = dealer_hand
        self.game = game

    @discord.ui.button(label="📥 Взять карту", style=discord.ButtonStyle.primary, custom_id="blackjack_hit")
    async def hit_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Взять карту."""
        view = BlackjackView(self.guild_id, self.user_id, self.bet, self.deck, self.player_hand, self.dealer_hand, self.game)
        await view.hit(interaction)

    @discord.ui.button(label="✋ Остановиться", style=discord.ButtonStyle.secondary, custom_id="blackjack_stand")
    async def stand_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        """Остановиться."""
        view = BlackjackView(self.guild_id, self.user_id, self.bet, self.deck, self.player_hand, self.dealer_hand, self.game)
        await view.stand(interaction)
