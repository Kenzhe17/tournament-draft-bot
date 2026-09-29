"""Монетка - PvP/PvE игра."""

import discord
import secrets
import asyncio
from typing import Optional
from storage.user_balance_store import user_balance_store
from utils.embeds import replace_emojis

# Locks for atomic transactions
_user_locks = {}
_guild_locks = {}


def get_user_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    """Get or create a lock for user operations."""
    key = (guild_id, user_id)
    if key not in _user_locks:
        _user_locks[key] = asyncio.Lock()
    return _user_locks[key]


def get_guild_lock(guild_id: int) -> asyncio.Lock:
    """Get or create a lock for guild operations."""
    if guild_id not in _guild_locks:
        _guild_locks[guild_id] = asyncio.Lock()
    return _guild_locks[guild_id]


class CoinFlipGame:
    """Класс для хранения состояния игры."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        initiator_id: int,
        bet: int,
        opponent_id: Optional[int] = None,
        initiator_choice: Optional[str] = None
    ):
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.initiator_id = initiator_id
        self.bet = bet
        self.opponent_id = opponent_id
        self.initiator_choice = initiator_choice  # "heads" or "tails"
        self.opponent_choice: Optional[str] = None
        self.result: Optional[str] = None
        self.winner: Optional[int] = None
        self.is_active = True


class CoinFlipView(discord.ui.View):
    """View для игры Монетка."""

    def __init__(self, game: CoinFlipGame):
        super().__init__(timeout=60)
        self.game = game

    def create_game_embed(self, state: str = "menu", initiator_avatar: str = None) -> discord.Embed:
        """Создать embed для разных состояний игры."""
        embed = discord.Embed(
            title=f"{replace_emojis('a_sparkle')} **COIN FLIP | /coin_flip**",
            color=discord.Color.from_rgb(69, 69, 69)
        )

        if state == "menu":
            mode = "PvP" if self.game.opponent_id else "PvE"
            opponent = f"<@{self.game.opponent_id}>" if self.game.opponent_id else "Бот"

            if initiator_avatar:
                embed.set_thumbnail(url=initiator_avatar)

            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Информация о игре",
                value=f"• Режим: **{mode}**\n"
                      f"• Игрок: <@{self.game.initiator_id}>\n"
                      f"• Ставка: **{self.game.bet}** {replace_emojis('money')}\n"
                      f"• Множитель: **2.0x**",
                inline=False
            )

            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Правила",
                value="Выберите сторону монеты (PvE) или дождитесь ответа оппонента (PvP).",
                inline=False
            )

        elif state == "animating":
            embed.add_field(
                name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                value=f"• Монета подбрасывается... {replace_emojis('white_dots')}",
                inline=False
            )

        elif state == "result":
            if self.game.opponent_id:
                # PvP result
                result_text = "Орёл" if self.game.result == "heads" else "Решка"
                winner_emoji = "✅" if self.game.winner == self.game.initiator_id else "❌"
                embed.add_field(
                    name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                    value=f"• Выпало: **{result_text}**\n"
                          f"• Победитель: <@{self.game.winner}> (+**{self.game.bet * 2}** {replace_emojis('money')})",
                    inline=False
                )
            else:
                # PvE result
                initiator_text = "Орёл" if self.game.initiator_choice == "heads" else "Решка"
                result_text = "Орёл" if self.game.result == "heads" else "Решка"
                won = self.game.initiator_choice == self.game.result

                if won:
                    embed.add_field(
                        name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                        value=f"• Ваша ставка: **{initiator_text}**\n"
                              f"• Выпало: **{result_text}**\n"
                              f"• Итог: Вы выиграли **{self.game.bet * 2}** {replace_emojis('money')}!",
                        inline=False
                    )
                else:
                    embed.add_field(
                        name=f"{replace_emojis('white_dot')} {replace_emojis('white_arrow')} Результат",
                        value=f"• Ваша ставка: **{initiator_text}**\n"
                              f"• Выпало: **{result_text}**\n"
                              f"• Итог: Вы проиграли **{self.game.bet}** {replace_emojis('money')}.",
                        inline=False
                    )

        return embed


class CoinFlipHeadsButton(discord.ui.Button):
    """Кнопка выбора Орла (PvE)."""

    def __init__(self, game: CoinFlipGame, view: CoinFlipView):
        super().__init__(style=discord.ButtonStyle.primary, label="Орёл")
        self.game = game
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.initiator_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        self.game.initiator_choice = "heads"
        await start_game(interaction, self.game, self.view)


class CoinFlipTailsButton(discord.ui.Button):
    """Кнопка выбора Решки (PvE)."""

    def __init__(self, game: CoinFlipGame, view: CoinFlipView):
        super().__init__(style=discord.ButtonStyle.secondary, label="Решка")
        self.game = game
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.initiator_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        self.game.initiator_choice = "tails"
        await start_game(interaction, self.game, self.view)


class CoinFlipCancelButton(discord.ui.Button):
    """Кнопка отмены игры."""

    def __init__(self, game: CoinFlipGame):
        super().__init__(style=discord.ButtonStyle.danger, label="Отмена")
        self.game = game

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.initiator_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return

        # Return bet to initiator
        lock = get_user_lock(self.game.guild_id, self.game.initiator_id)
        async with lock:
            await user_balance_store.add_balance(
                self.game.guild_id,
                self.game.initiator_id,
                self.game.bet
            )

        self.game.is_active = False
        await interaction.response.edit_message(
            content="❌ Игра отменена. Ставка возвращена.",
            embed=None,
            view=None
        )


class CoinFlipAcceptButton(discord.ui.Button):
    """Кнопка принятия вызова (PvP)."""

    def __init__(self, game: CoinFlipGame, view: CoinFlipView):
        super().__init__(style=discord.ButtonStyle.success, label="Принять вызов")
        self.game = game
        self.view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.opponent_id:
            await interaction.response.send_message("❌ Вы не были вызваны.", ephemeral=True)
            return

        # Deduct bet from opponent
        lock = get_user_lock(self.game.guild_id, self.game.opponent_id)
        async with lock:
            balance = await user_balance_store.get_balance(self.game.guild_id, self.game.opponent_id)
            if balance < self.game.bet:
                await interaction.response.send_message(
                    f"❌ Недостаточно монет. У вас: {balance}",
                    ephemeral=True
                )
                return

            await user_balance_store.add_balance(
                self.game.guild_id,
                self.game.opponent_id,
                -self.game.bet
            )

        await start_game(interaction, self.game, self.view)


class CoinFlipDeclineButton(discord.ui.Button):
    """Кнопка отклонения вызова (PvP)."""

    def __init__(self, game: CoinFlipGame):
        super().__init__(style=discord.ButtonStyle.danger, label="Отклонить")
        self.game = game

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.game.opponent_id:
            await interaction.response.send_message("❌ Вы не были вызваны.", ephemeral=True)
            return

        # Return bet to initiator
        lock = get_user_lock(self.game.guild_id, self.game.initiator_id)
        async with lock:
            await user_balance_store.add_balance(
                self.game.guild_id,
                self.game.initiator_id,
                self.game.bet
            )

        self.game.is_active = False
        await interaction.response.edit_message(
            content="❌ Вызов отклонён. Ставка возвращена.",
            embed=None,
            view=None
        )


class CoinFlipPlayAgainButton(discord.ui.Button):
    """Кнопка сыграть снова."""

    def __init__(self, bet: int, opponent_id: Optional[int] = None):
        super().__init__(style=discord.ButtonStyle.primary, label="Сыграть снова")
        self.bet = bet
        self.opponent_id = opponent_id

    async def callback(self, interaction: discord.Interaction) -> None:
        """Перезапустить игру с теми же параметрами."""
        opponent = None
        if self.opponent_id:
            opponent = interaction.guild.get_member(self.opponent_id)

        # For play again, we need to edit the existing message
        # First create the game
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # Validate bet
        if self.bet < 10 or self.bet > 10000:
            await interaction.response.send_message(
                "❌ Ставка должна быть от 10 до 10,000 монет.",
                ephemeral=True
            )
            return

        # Check balance
        balance = await user_balance_store.get_balance(guild_id, user_id)
        if balance < self.bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance}",
                ephemeral=True
            )
            return

        # Deduct bet atomically
        lock = get_user_lock(guild_id, user_id)
        async with lock:
            balance = await user_balance_store.get_balance(guild_id, user_id)
            if balance < self.bet:
                await interaction.response.send_message(
                    f"❌ Недостаточно монет. У вас: {balance}",
                    ephemeral=True
                )
                return

            await user_balance_store.add_balance(guild_id, user_id, -self.bet)

        # Create game
        game = CoinFlipGame(
            guild_id=guild_id,
            channel_id=interaction.channel_id,
            initiator_id=user_id,
            bet=self.bet,
            opponent_id=opponent.id if opponent else None
        )

        # Create view
        view = CoinFlipView(game)

        if opponent:
            # PvP mode
            view.add_item(CoinFlipAcceptButton(game, view))
            view.add_item(CoinFlipDeclineButton(game))
        else:
            # PvE mode
            view.add_item(CoinFlipHeadsButton(game, view))
            view.add_item(CoinFlipTailsButton(game, view))

        view.add_item(CoinFlipCancelButton(game))

        embed = view.create_game_embed("menu", interaction.user.display_avatar.url)

        # Edit the existing message instead of sending new one
        await interaction.response.edit_message(embed=embed, view=view)


async def start_game(
    interaction: discord.Interaction,
    game: CoinFlipGame,
    view: CoinFlipView
) -> None:
    """Запустить игру с анимацией и результатом."""
    # Animation state
    game.is_active = False  # Prevent further interactions
    for item in view.children:
        item.disabled = True

    anim_embed = view.create_game_embed("animating")
    await interaction.response.edit_message(embed=anim_embed, view=view)

    # Wait for animation
    await asyncio.sleep(2)

    # Generate result
    game.result = secrets.choice(["heads", "tails"])

    # Determine winner
    if game.opponent_id:
        # PvP: Opponent automatically gets opposite side
        game.opponent_choice = "tails" if game.initiator_choice == "heads" else "heads"
        game.winner = game.initiator_id if game.initiator_choice == game.result else game.opponent_id
    else:
        # PvE: Already determined by choice
        game.winner = game.initiator_id if game.initiator_choice == game.result else None

    # Process payouts
    if game.opponent_id:
        # PvP
        winner_lock = get_user_lock(game.guild_id, game.winner)
        async with winner_lock:
            await user_balance_store.add_balance(
                game.guild_id,
                game.winner,
                game.bet * 2
            )
    else:
        # PvE
        if game.winner == game.initiator_id:
            lock = get_user_lock(game.guild_id, game.initiator_id)
            async with lock:
                await user_balance_store.add_balance(
                    game.guild_id,
                    game.initiator_id,
                    game.bet * 2
                )

    # Show result
    result_embed = view.create_game_embed("result")

    # Add play again button
    result_view = discord.ui.View(timeout=None)
    result_view.add_item(CoinFlipPlayAgainButton(game.bet, game.opponent_id))

    await interaction.edit_original_response(embed=result_embed, view=result_view)


async def create_coin_flip_game(
    interaction: discord.Interaction,
    bet: int,
    opponent: Optional[discord.Member] = None
) -> None:
    """Создать игру Монетка."""
    guild_id = interaction.guild_id
    user_id = interaction.user.id

    # Validate bet
    if bet < 10 or bet > 10000:
        await interaction.response.send_message(
            f"❌ Ставка должна быть от 10 до 10,000 монет.",
            ephemeral=True
        )
        return

    # Check balance
    balance = await user_balance_store.get_balance(guild_id, user_id)
    if balance < bet:
        await interaction.response.send_message(
            f"❌ Недостаточно монет. У вас: {balance}",
            ephemeral=True
        )
        return

    # Deduct bet atomically
    lock = get_user_lock(guild_id, user_id)
    async with lock:
        balance = await user_balance_store.get_balance(guild_id, user_id)
        if balance < bet:
            await interaction.response.send_message(
                f"❌ Недостаточно монет. У вас: {balance}",
                ephemeral=True
            )
            return

        await user_balance_store.add_balance(guild_id, user_id, -bet)

    # Create game
    game = CoinFlipGame(
        guild_id=guild_id,
        channel_id=interaction.channel_id,
        initiator_id=user_id,
        bet=bet,
        opponent_id=opponent.id if opponent else None
    )

    # Create view
    view = CoinFlipView(game)

    if opponent:
        # PvP mode
        view.add_item(CoinFlipAcceptButton(game, view))
        view.add_item(CoinFlipDeclineButton(game))
    else:
        # PvE mode
        view.add_item(CoinFlipHeadsButton(game, view))
        view.add_item(CoinFlipTailsButton(game, view))

    view.add_item(CoinFlipCancelButton(game))

    embed = view.create_game_embed("menu", interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed, view=view)
