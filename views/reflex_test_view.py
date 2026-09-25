"""View для игры быстрый тест на реакцию."""

import asyncio
import random
import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button, button
from games.reflex_test import ReflexTestGame


class ReflexTestBetModal(Modal, title="⚡ Быстрый тест"):
    """Modal для ввода ставки."""

    bet = TextInput(label="Ставка (монеты)", placeholder="Введите сумму ставки", min_length=1, max_length=10)

    def __init__(self, guild_id: int, user_id: int) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        from storage.user_balance_store import user_balance_store

        try:
            bet = int(self.bet.value)
        except ValueError:
            await interaction.response.send_message("❌ Введите корректную ставку!", ephemeral=True)
            return

        if bet < 10:
            await interaction.response.send_message("❌ Минимальная ставка: 10 🪙", ephemeral=True)
            return

        if bet > 200:
            await interaction.response.send_message("❌ Максимальная ставка: 200 🪙", ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message("❌ Недостаточно монет!", ephemeral=True)
            return

        # Списать ставку
        await user_balance_store.add_balance(self.guild_id, self.user_id, -bet)

        # Создать embed
        embed = discord.Embed(
            title="⚡ Быстрый тест",
            description="🎯 Нажмите кнопку чтобы начать! После задержки появится эмодзи - нажмите быстро!",
            color=discord.Color.blue()
        )
        embed.add_field(name="Ставка", value=f"{bet} 🪙", inline=True)
        embed.add_field(name="Порог", value="1.0 сек", inline=True)

        # Создать view
        view = ReflexTestGameView(self.guild_id, self.user_id, bet)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ReflexTestGameView(View):
    """View для игры быстрый тест на реакцию."""

    def __init__(self, guild_id: int, user_id: int, bet: int) -> None:
        super().__init__(timeout=300)  # 5 минут на игру
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = ReflexTestGame()
        self.started = False

    @button(label="🎯 Начать", style=discord.ButtonStyle.primary)
    async def start_game(self, interaction: discord.Interaction, button: Button) -> None:
        """Начать игру."""
        if self.started:
            await interaction.response.send_message("❌ Игра уже началась!", ephemeral=True)
            return

        self.started = True

        # Изменить embed на "Ждите..."
        embed = discord.Embed(
            title="⚡ Быстрый тест",
            description="⏳ Ждите появления эмодзи...",
            color=discord.Color.orange()
        )
        embed.add_field(name="Ставка", value=f"{self.bet} 🪙", inline=True)

        await interaction.response.edit_message(embed=embed, view=None)

        # Задержка 1-3 секунды
        delay = random.uniform(1.0, 3.0)
        await asyncio.sleep(delay)

        # Показать эмодзи
        target_emoji = self.game.start()

        embed = discord.Embed(
            title="⚡ Быстрый тест",
            description=f"🎯 НАЖМИТЕ! {target_emoji}",
            color=discord.Color.red()
        )
        embed.add_field(name="Ставка", value=f"{self.bet} 🪙", inline=True)

        # Создать view с кнопкой для реакции
        view = ReflexTestReactionView(self.guild_id, self.user_id, self.bet, self.game, target_emoji)

        # Получить original message (нужно из interaction)
        original_message = await interaction.original_response()
        await original_message.edit(embed=embed, view=view)


class ReflexTestReactionView(View):
    """View для реакции на эмодзи."""

    def __init__(self, guild_id: int, user_id: int, bet: int, game: ReflexTestGame, target_emoji: str) -> None:
        super().__init__(timeout=10)  # 10 секунд на реакцию
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = game
        self.target_emoji = target_emoji

        react_button = Button(
            label=f"⚡ {target_emoji}",
            style=discord.ButtonStyle.danger,
            custom_id="reflex_react"
        )
        react_button.callback = self.on_react
        self.add_item(react_button)

    async def on_react(self, interaction: discord.Interaction) -> None:
        """Реагировать на эмодзи."""
        from storage.user_balance_store import user_balance_store

        result, message = self.game.attempt(self.target_emoji)

        # Создать embed
        if result == "correct":
            color = discord.Color.gold()
            title = "🎉 Победа!"
        elif result == "timeout":
            color = discord.Color.red()
            title = "😢 Медленно!"
        else:
            color = discord.Color.orange()
            title = "❌ Ошибка!"

        embed = discord.Embed(
            title=title,
            description=message,
            color=color
        )
        embed.add_field(name="Ставка", value=f"{self.bet} 🪙", inline=True)

        # Если игра окончена
        if self.game.game_over:
            if self.game.won:
                winnings = int(self.bet * self.game.get_multiplier())
                await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)
                embed.add_field(name="Выигрыш", value=f"{winnings} 🪙", inline=False)
                embed.set_footer(text=f"Множитель: {self.game.get_multiplier()}x")
            else:
                embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)

            self.stop()
            await interaction.response.edit_message(embed=embed, view=None)
        else:
            await interaction.response.edit_message(embed=embed, view=None)
