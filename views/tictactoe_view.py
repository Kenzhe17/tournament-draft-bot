"""View для игры крестики-нолики."""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput, View, Button, button
from games.tictactoe import TicTacToeGame


class TicTacToeBetModal(Modal, title="❌⭕ Крестики-Нолики"):
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

        if bet < 20:
            await interaction.response.send_message("❌ Минимальная ставка: 20 🪙", ephemeral=True)
            return

        if bet > 500:
            await interaction.response.send_message("❌ Максимальная ставка: 500 🪙", ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        if balance < bet:
            await interaction.response.send_message("❌ Недостаточно монет!", ephemeral=True)
            return

        # Списать ставку
        await user_balance_store.add_balance(self.guild_id, self.user_id, -bet)

        # Создать игру (PvE)
        game = TicTacToeGame(is_pve=True)

        # Создать embed
        embed = discord.Embed(
            title="❌⭕ Крестики-Нолики",
            description="Вы играете за X. Выберите клетку (0-8)!",
            color=discord.Color.blue()
        )
        embed.add_field(name="Ставка", value=f"{bet} 🪙", inline=True)
        embed.add_field(name="Текущий ход", value="X (Вы)", inline=True)

        # Создать view
        view = TicTacToeGameView(self.guild_id, self.user_id, bet, game)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class TicTacToeGameView(View):
    """View для игры крестики-нолики."""

    def __init__(self, guild_id: int, user_id: int, bet: int, game: TicTacToeGame) -> None:
        super().__init__(timeout=300)  # 5 минут на игру
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = game

    @button(label="Сделать ход", style=discord.ButtonStyle.primary, emoji="🎯")
    async def make_move(self, interaction: discord.Interaction, button: Button) -> None:
        """Открыть modal для выбора клетки."""
        modal = TicTacToeMoveModal(self.guild_id, self.user_id, self.bet, self.game)
        await interaction.response.send_modal(modal)

    @button(label="Сдаться", style=discord.ButtonStyle.danger, emoji="🏳️")
    async def give_up(self, interaction: discord.Interaction, button: Button) -> None:
        """Сдаться и потерять ставку."""
        self.game.game_over = True
        embed = discord.Embed(
            title="❌⭕ Крестики-Нолики",
            description="😢 Вы сдались!",
            color=discord.Color.red()
        )
        embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)
        self.stop()
        await interaction.response.edit_message(embed=embed, view=None)


class TicTacToeMoveModal(Modal, title="🎯 Выберите клетку"):
    """Modal для выбора клетки."""

    position = TextInput(label="Клетка (0-8)", placeholder="Введите число от 0 до 8", min_length=1, max_length=1)

    def __init__(self, guild_id: int, user_id: int, bet: int, game: TicTacToeGame) -> None:
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.bet = bet
        self.game = game

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Обработать отправку формы."""
        from storage.user_balance_store import user_balance_store

        try:
            position = int(self.position.value)
        except ValueError:
            await interaction.response.send_message("❌ Введите число от 0 до 8!", ephemeral=True)
            return

        result, message = self.game.make_move(position)

        # Создать embed
        if result == "win":
            color = discord.Color.gold() if self.game.winner == "X" else discord.Color.red()
            title = "🎉 Победа!" if self.game.winner == "X" else "🤖 Бот победил!"
        elif result == "draw":
            color = discord.Color.greyple()
            title = "🤝 Ничья!"
        else:
            color = discord.Color.blue()
            title = "❌⭕ Крестики-Нолики"

        embed = discord.Embed(
            title=title,
            description=message,
            color=color
        )
        embed.add_field(name="Ставка", value=f"{self.bet} 🪙", inline=True)
        embed.add_field(name="Текущий ход", value=self.game.current_player, inline=True)

        # Если игра окончена
        if self.game.game_over:
            if self.game.winner == "X":
                winnings = int(self.bet * self.game.get_multiplier())
                await user_balance_store.add_balance(self.guild_id, self.user_id, winnings)
                embed.add_field(name="Выигрыш", value=f"{winnings} 🪙", inline=False)
                embed.set_footer(text=f"Множитель: {self.game.get_multiplier()}x")
            elif self.game.winner == "O":
                embed.add_field(name="Потеряно", value=f"{self.bet} 🪙", inline=False)
            else:  # Ничья
                await user_balance_store.add_balance(self.guild_id, self.user_id, self.bet)
                embed.add_field(name="Возврат", value=f"{self.bet} 🪙", inline=False)

            await interaction.response.edit_message(embed=embed, view=None)
        else:
            # Продолжить игру
            view = TicTacToeGameView(self.guild_id, self.user_id, self.bet, self.game)
            await interaction.response.edit_message(embed=embed, view=view)
