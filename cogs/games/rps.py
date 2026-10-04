"""Rock-Paper-Scissors game implementation with PvP and PvE modes."""
from config import replace_emojis

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from storage.economy import (
    check_balance,
    get_balance,
    hold_escrow,
    payout_winner,
    release_escrow,
)

logger = logging.getLogger(__name__)


class GameMode(Enum):
    """Game mode enum."""
    PVE = "pve"
    PVP = "pvp"


class GameState(Enum):
    """Game state enum."""
    INIT = "init"
    WAITING_PVE = "waiting_pve"
    WAITING_PVP = "waiting_pvp"
    PVP_CHOICE = "pvp_choice"
    RESOLVED_PVE = "resolved_pve"
    RESOLVED_PVP = "resolved_pvp"
    END = "end"


class Move(Enum):
    """Move enum with emoji mapping."""
    ROCK = ("rock", "🪨", "Камень")
    PAPER = ("paper", "📜", "Бумага")
    SCISSORS = ("scissors", "✂️", "Ножницы")
    
    def __init__(self, code: str, emoji: str, display_name: str):
        self.code = code
        self.emoji = emoji
        self.display_name = display_name
    
    @classmethod
    def from_code(cls, code: str) -> Optional["Move"]:
        """Get Move from code string."""
        for move in cls:
            if move.code == code:
                return move
        return None
    
    def beats(self, other: "Move") -> bool:
        """Check if this move beats another."""
        if self == Move.ROCK:
            return other == Move.SCISSORS
        elif self == Move.PAPER:
            return other == Move.ROCK
        elif self == Move.SCISSORS:
            return other == Move.PAPER
        return False


@dataclass
class GameSession:
    """Active game session data."""
    game_id: str
    mode: GameMode
    state: GameState
    initiator_id: int
    guild_id: int
    bet: int
    opponent_id: Optional[int] = None
    initiator_move: Optional[Move] = None
    opponent_move: Optional[Move] = None
    message_id: Optional[int] = None
    channel_id: Optional[int] = None


# Active games tracking
active_games: dict[str, GameSession] = {}
active_users: set[int] = set()


class RPSView(discord.ui.View):
    """Base view for RPS game with timeout."""
    
    def __init__(self, game_id: str, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.game_id = game_id


class PvEChoiceView(RPSView):
    """View for PvE move selection."""
    
    def __init__(self, game_id: str, bet: int):
        super().__init__(game_id, timeout=30)
        self.bet = bet
    
    @discord.ui.button(label="Камень", emoji="🪨", style=discord.ButtonStyle.primary, custom_id="rps:rock")
    async def btn_rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, Move.ROCK)
    
    @discord.ui.button(label="Бумага", emoji="📜", style=discord.ButtonStyle.primary, custom_id="rps:paper")
    async def btn_paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, Move.PAPER)
    
    @discord.ui.button(label="Ножницы", emoji="✂️", style=discord.ButtonStyle.primary, custom_id="rps:scissors")
    async def btn_scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, Move.SCISSORS)
    
    async def handle_choice(self, interaction: discord.Interaction, move: Move):
        """Handle player's move choice."""
        game = active_games.get(self.game_id)
        if not game or game.state != GameState.WAITING_PVE:
            await interaction.response.send_message(replace_emojis("❌ Игра недоступна."), ephemeral=True)
            return
        
        # Access Control: PvE mode - only creator can click
        if interaction.user.id != game.initiator_id:
            await interaction.response.send_message(
                replace_emojis("⚠️ Это не ваша игра! Запустите свою с помощью команды /rps"),
                ephemeral=True
            )
            return
        
        # Check if game already resolved (prevent double-click)
        if game.state != GameState.WAITING_PVE:
            await interaction.response.send_message(replace_emojis("❌ Игра уже завершена."), ephemeral=True)
            return
        
        # Update state to prevent multiple submissions
        game.state = GameState.RESOLVED_PVE
        
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        
        # Generate bot move (truly random with time-based seed)
        # Use secrets for cryptographically secure random choice
        import secrets
        # Combine time and secrets for maximum randomness
        seed = int(time.time() * 1000) + secrets.randbelow(1000)
        random.seed(seed)
        bot_move = random.choice(list(Move))
        # Reset seed to avoid affecting other random operations
        random.seed()
        logger.info(f"RPS PvE: Player chose {move.display_name}, Bot chose {bot_move.display_name}, Seed: {seed}")
        
        # Determine result
        if move == bot_move:
            result = "draw"
        elif move.beats(bot_move):
            result = "win"
        else:
            result = "loss"
        
        # Process economy
        guild_id = game.guild_id
        user_id = game.initiator_id
        bet = game.bet
        
        if result == "win":
            winnings = bet * 2
            await payout_winner(user_id, guild_id, winnings)
            balance_change = f"+{bet} {replace_emojis('🪙')}"
            title = replace_emojis("🎉 Победа в игре против ИИ!")
        elif result == "draw":
            await release_escrow(user_id, guild_id, bet)
            balance_change = f"0 {replace_emojis('🪙')} (возврат)"
            title = replace_emojis("🤝 Ничья!")
        else:  # loss
            balance_change = f"-{bet} {replace_emojis('🪙')}"
            title = replace_emojis("💥 Поражение против ИИ!")
        
        # Build result embed
        embed = discord.Embed(
            title=title,
            description=f"Ваш ход: {move.emoji} {move.display_name}\n"
                       f"Ход ИИ: {bot_move.emoji} {bot_move.display_name}\n\n"
                       f"{replace_emojis('💰')} Изменение баланса: {balance_change}",
            color=discord.Color.blue()
        )
        
        # Send public result message (not ephemeral)
        await interaction.edit_original_response(embed=embed, view=None)
        
        # Cleanup
        active_users.discard(user_id)
        if self.game_id in active_games:
            del active_games[self.game_id]


class PvPChallengeView(RPSView):
    """View for PvP challenge acceptance."""
    
    def __init__(self, game_id: str, initiator_id: int, opponent_id: Optional[int], bet: int, channel_id: int, message_id: int, bot: commands.Bot):
        super().__init__(game_id, timeout=60)
        self.initiator_id = initiator_id
        self.opponent_id = opponent_id  # Can be None for open challenges
        self.bet = bet
        self.channel_id = channel_id
        self.message_id = message_id
        self.bot_instance = bot  # Store bot instance for timeout handler
    
    async def on_timeout(self) -> None:
        """Handle timeout - auto-decline the challenge."""
        game = active_games.get(self.game_id)
        if not game:
            return
        
        # Refund initiator
        await release_escrow(self.initiator_id, game.guild_id, self.bet)
        
        # Refund opponent if they accepted (escrow held)
        if self.opponent_id and self.opponent_id in active_users:
            await release_escrow(self.opponent_id, game.guild_id, self.bet)
        
        # Update message
        try:
            channel = self.bot_instance.get_channel(self.channel_id)
            if channel:
                msg = await channel.fetch_message(self.message_id)
                embed = discord.Embed(
                    title=replace_emojis("⏰ Время истекло"),
                    description=f"Вызов истёк и был автоматически отменён.",
                    color=discord.Color.orange()
                )
                try:
                    await msg.edit(embed=embed, view=None)
                except discord.errors.NotFound:
                    # Message was already deleted
                    pass
        except Exception as e:
            logger.error(f"Error editing timeout message: {e}")
        
        # Cleanup
        active_users.discard(self.initiator_id)
        if self.opponent_id:
            active_users.discard(self.opponent_id)
        if self.game_id in active_games:
            del active_games[self.game_id]
    
    @discord.ui.button(label="Принять", emoji="⚔️", style=discord.ButtonStyle.success, custom_id="rps:accept")
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_accept(interaction)
    
    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger, custom_id="rps:decline")
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_decline(interaction)
    
    @discord.ui.button(label="Отменить", emoji="🚫", style=discord.ButtonStyle.secondary, custom_id="rps:cancel")
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_cancel(interaction)
    
    async def handle_accept(self, interaction: discord.Interaction):
        """Handle challenge acceptance."""
        game = active_games.get(self.game_id)
        if not game or game.state != GameState.WAITING_PVP:
            try:
                await interaction.response.send_message(replace_emojis("❌ Вызов недоступен."), ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        
        # Access Control: Anyone except creator can accept
        if interaction.user.id == self.initiator_id:
            try:
                await interaction.response.send_message(
                    replace_emojis("⚠️ Вы не можете принять свой собственный вызов!"),
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                pass
            return
        
        # If it's a direct challenge, only the invited opponent can accept
        if self.opponent_id and interaction.user.id != self.opponent_id:
            try:
                await interaction.response.send_message(
                    replace_emojis("⚠️ Этот вызов предназначен для другого игрока!"),
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                pass
            return
        
        # Check if user is already in a game
        if interaction.user.id in active_users:
            try:
                await interaction.response.send_message(replace_emojis("❌ Вы уже участвуете в игре."), ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        
        # Update opponent to whoever accepted (open challenge)
        new_opponent_id = interaction.user.id
        new_opponent = interaction.guild.get_member(new_opponent_id)
        
        if not new_opponent:
            try:
                await interaction.response.send_message(replace_emojis("❌ Не удалось найти пользователя."), ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        
        if new_opponent.bot:
            try:
                await interaction.response.send_message(replace_emojis("❌ Нельзя играть против ботов."), ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        
        # Hold escrow for new opponent
        escrow_success = await hold_escrow(new_opponent_id, game.guild_id, self.bet)
        if not escrow_success:
            try:
                await interaction.response.send_message(replace_emojis("❌ Недостаточно баланса для ставки."), ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            return
        
        # Update game state with new opponent
        game.state = GameState.PVP_CHOICE
        game.opponent_id = new_opponent_id
        active_users.add(new_opponent_id)
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except discord.errors.InteractionResponded:
            pass
        
        # Send choice views to both players
        await self.send_choice_views(interaction, game)
        
        # Hold escrow for opponent
        escrow_success = await hold_escrow(self.opponent_id, game.guild_id, self.bet)
        if not escrow_success:
            await interaction.response.send_message(replace_emojis("❌ Недостаточно баланса для ставки."), ephemeral=True)
            # Refund initiator
            await release_escrow(self.initiator_id, game.guild_id, self.bet)
            active_users.discard(self.initiator_id)
            del active_games[self.game_id]
            return
        
        # Update game state
        game.state = GameState.PVP_CHOICE
        game.opponent_id = self.opponent_id
        active_users.add(self.opponent_id)
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        
        # Send choice views to both players
        await self.send_choice_views(interaction, game)
    
    async def handle_decline(self, interaction: discord.Interaction):
        """Handle challenge decline."""
        game = active_games.get(self.game_id)
        if not game:
            return
        
        # Access Control: Only the challenged opponent can decline
        if self.opponent_id and interaction.user.id != self.opponent_id:
            try:
                await interaction.response.send_message(
                    replace_emojis("⚠️ Только вызванный игрок может отклонить вызов!"),
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                pass
            return
        
        # For open challenges, anyone except creator can decline
        if not self.opponent_id and interaction.user.id == self.initiator_id:
            try:
                await interaction.response.send_message(
                    replace_emojis("⚠️ Создатель не может отклонить свой вызов. Используйте отмену!"),
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                pass
            return
        
        # Refund initiator and opponent if escrow held
        await release_escrow(self.initiator_id, game.guild_id, self.bet)
        if self.opponent_id:
            await release_escrow(self.opponent_id, game.guild_id, self.bet)
        
        # Update message
        embed = discord.Embed(
            title=replace_emojis("❌ Вызов отклонён"),
            description=f"<@{interaction.user.id}> отклонил вызов от <@{self.initiator_id}>.",
            color=discord.Color.red()
        )
        
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.errors.InteractionResponded:
            pass
        
        # Cleanup
        active_users.discard(self.initiator_id)
        if self.opponent_id:
            active_users.discard(self.opponent_id)
        if self.game_id in active_games:
            del active_games[self.game_id]
    
    async def handle_cancel(self, interaction: discord.Interaction):
        """Handle challenge cancellation by creator."""
        game = active_games.get(self.game_id)
        if not game:
            return
        
        # Access Control: Only creator can cancel
        if interaction.user.id != self.initiator_id:
            try:
                await interaction.response.send_message(
                    replace_emojis("⚠️ Только создатель вызова может его отменить!"),
                    ephemeral=True
                )
            except discord.errors.InteractionResponded:
                pass
            return
        
        # Refund initiator and opponent if escrow held
        await release_escrow(self.initiator_id, game.guild_id, self.bet)
        if self.opponent_id:
            await release_escrow(self.opponent_id, game.guild_id, self.bet)
        
        # Update message
        embed = discord.Embed(
            title=replace_emojis("🚫 Вызов отменён"),
            description=f"<@{self.initiator_id}> отменил свой вызов.",
            color=discord.Color.orange()
        )
        
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.errors.InteractionResponded:
            pass
        
        # Cleanup
        active_users.discard(self.initiator_id)
        if self.opponent_id:
            active_users.discard(self.opponent_id)
        if self.game_id in active_games:
            del active_games[self.game_id]
        
        # Refund initiator
        await release_escrow(self.initiator_id, game.guild_id, self.bet)
        
        # Update message
        embed = discord.Embed(
            title=replace_emojis("❌ Вызов отклонён"),
            description=f"<@{self.opponent_id}> отклонил вызов от <@{self.initiator_id}>.",
            color=discord.Color.red()
        )
        
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        
        # Cleanup
        active_users.discard(self.initiator_id)
        if self.game_id in active_games:
            del active_games[self.game_id]
    
    async def send_choice_views(self, interaction: discord.Interaction, game: GameSession):
        """Update main message to show choice phase."""
        # Instead of sending new messages, update the main challenge message
        view = PvPChoiceView(self.game_id, self.initiator_id, self.opponent_id, self.bet, is_initiator=None, is_main=True, bot=self.bot_instance)
        embed = discord.Embed(
            title=replace_emojis("⚔️ Выберите ваш ход"),
            description=f"<@{self.initiator_id}> vs <@{self.opponent_id}>\n\n"
                          f"{replace_emojis('💰')} Ставка: {self.bet} {replace_emojis('🪙')}\n"
                          f"{replace_emojis('⏱️')} У вас есть 40 секунд чтобы сделать выбор!\n\n"
                          f"Сделайте выбор. Результат будет опубликован здесь!",
            color=discord.Color.blue()
        )
        
        try:
            channel = self.bot_instance.get_channel(self.channel_id)
            if channel and self.message_id:
                msg = await channel.fetch_message(self.message_id)
                await msg.edit(embed=embed, view=view)
        except Exception as e:
            logger.error(f"Error updating main message: {e}")


class PvPChoiceView(RPSView):
    """View for PvP move selection."""
    
    def __init__(self, game_id: str, user_id: int, opponent_id: int, bet: int, is_initiator: Optional[bool] = None, is_main: bool = False, bot: Optional[commands.Bot] = None):
        super().__init__(game_id, timeout=40)
        self.user_id = user_id
        self.opponent_id = opponent_id
        self.bet = bet
        self.is_initiator = is_initiator
        self.is_main = is_main  # If True, this is the main message with buttons for both
        self.bot_instance = bot  # Store bot instance for message access
    
    @discord.ui.button(label="Камень", emoji="🪨", style=discord.ButtonStyle.primary, custom_id="rps:rock")
    async def btn_rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, Move.ROCK)
    
    @discord.ui.button(label="Бумага", emoji="📜", style=discord.ButtonStyle.primary, custom_id="rps:paper")
    async def btn_paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, Move.PAPER)
    
    @discord.ui.button(label="Ножницы", emoji="✂️", style=discord.ButtonStyle.primary, custom_id="rps:scissors")
    async def btn_scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, Move.SCISSORS)
    
    async def handle_choice(self, interaction: discord.Interaction, move: Move):
        """Handle player's move choice."""
        game = active_games.get(self.game_id)
        if not game or game.state != GameState.PVP_CHOICE:
            await interaction.response.send_message(replace_emojis("❌ Игра недоступна."), ephemeral=True)
            return
        
        # Access Control: Only registered participants can click
        if self.is_main:
            # Main message mode - both can click, but only their own choice
            if interaction.user.id != self.user_id and interaction.user.id != self.opponent_id:
                await interaction.response.send_message(
                    replace_emojis("⚠️ Вы не являетесь участником этой дуэли!"),
                    ephemeral=True
                )
                return
        else:
            # Individual message mode - only the owner can click
            if interaction.user.id != self.user_id:
                await interaction.response.send_message(
                    replace_emojis("⚠️ Это не ваши кнопки! Используйте свои кнопки для выбора хода."),
                    ephemeral=True
                )
                return
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        
        # Record move
        if self.is_main:
            # Main message mode - need to determine which player made the move
            if interaction.user.id == self.user_id:
                game.initiator_move = move
            else:
                game.opponent_move = move
            
            # Check if both players have chosen
            if game.initiator_move and game.opponent_move:
                await self.resolve_game(interaction, game)
            else:
                # Update message to show waiting for other player
                waiting_for = self.opponent_id if interaction.user.id == self.user_id else self.user_id
                embed = discord.Embed(
                    title=replace_emojis("⚔️ Ожидание выбора..."),
                    description=f"<@{waiting_for}> делает свой выбор...",
                    color=discord.Color.yellow()
                )
                try:
                    await interaction.response.edit_message(embed=embed, view=self)
                except discord.errors.InteractionResponded:
                    pass
        else:
            # Individual message mode (deprecated but kept for compatibility)
            await interaction.response.edit_message(view=self)
            
            if self.is_initiator:
                game.initiator_move = move
            else:
                game.opponent_move = move
            
            # Check if both players have chosen
            if game.initiator_move and game.opponent_move:
                await self.resolve_game(interaction, game)
    
    async def resolve_game(self, interaction: discord.Interaction, game: GameSession):
        """Resolve PvP game."""
        p1_move = game.initiator_move
        p2_move = game.opponent_move
        
        # Determine result
        if p1_move == p2_move:
            result = "draw"
            winner_id = None
        elif p1_move.beats(p2_move):
            result = "p1_win"
            winner_id = game.initiator_id
        else:
            result = "p2_win"
            winner_id = game.opponent_id
        
        # Process economy
        guild_id = game.guild_id
        bet = game.bet
        total_pot = bet * 2
        winner_payout = int(total_pot * 0.95)  # 5% house fee
        
        if result == "draw":
            await release_escrow(game.initiator_id, guild_id, bet)
            await release_escrow(game.opponent_id, guild_id, bet)
            balance_change_p1 = f"0 {replace_emojis('🪙')} (возврат)"
            balance_change_p2 = f"0 {replace_emojis('🪙')} (возврат)"
            title = replace_emojis("🤝 Ничья!")
        else:
            await payout_winner(winner_id, guild_id, winner_payout)
            loser_id = game.opponent_id if winner_id == game.initiator_id else game.initiator_id
            # Loser already lost escrow, no action needed
            balance_change_winner = f"+{winner_payout - bet} {replace_emojis('🪙')}"
            balance_change_loser = f"-{bet} {replace_emojis('🪙')}"
            title = f"{replace_emojis('⚔️')} Итоги дуэли: <@{game.initiator_id}> vs <@{game.opponent_id}>"
        
        # Build result embed
        if result == "draw":
            description = (f"<@{game.initiator_id}>: {p1_move.emoji} {p1_move.display_name}\n"
                          f"<@{game.opponent_id}>: {p2_move.emoji} {p2_move.display_name}\n\n"
                          f"{replace_emojis('🤝')} Ничья! Оба игрока получают возврат ставки.\n"
                          f"{replace_emojis('💰')} <@{game.initiator_id}>: {balance_change_p1}\n"
                          f"{replace_emojis('💰')} <@{game.opponent_id}>: {balance_change_p2}")
        else:
            winner_name = f"<@{winner_id}>"
            description = (f"<@{game.initiator_id}>: {p1_move.emoji} {p1_move.display_name}\n"
                          f"<@{game.opponent_id}>: {p2_move.emoji} {p2_move.display_name}\n\n"
                          f"{replace_emojis('🏆')} Победитель: {winner_name}!\n"
                          f"{replace_emojis('💰')} Выигрыш: {winner_payout} {replace_emojis('🪙')} (комиссия 5%)")
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold()
        )
        
        # Update main message with result (no need to delete messages anymore)
        if game.message_id:
            try:
                # Get bot instance from the view
                channel = interaction.client.get_channel(game.channel_id)
                if channel:
                    msg = await channel.fetch_message(game.message_id)
                    await msg.edit(embed=embed, view=None)
            except Exception as e:
                logger.error(f"Error editing main message with result: {e}")
            try:
                msg = await channel.fetch_message(game.message_id)
                await msg.edit(embed=embed, view=None)
            except Exception as e:
                logger.error(f"Error editing challenge message: {e}")
        
        # Cleanup
        active_users.discard(game.initiator_id)
        active_users.discard(game.opponent_id)
        if self.game_id in active_games:
            del active_games[self.game_id]


class RPSCog(commands.Cog):
    """Rock-Paper-Scissors game cog."""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    @app_commands.command(name="rps", description="Камень-Ножницы-Бумага")
    @app_commands.describe(bet="Ставка в монетах", opponent="Соперник (для PvP)")
    async def rps(self, interaction: discord.Interaction, bet: int, opponent: Optional[discord.Member] = None):
        """Start RPS game."""
        await interaction.response.defer(ephemeral=False)
        
        user_id = interaction.user.id
        guild_id = interaction.guild_id
        
        # Validate bet
        if bet <= 0:
            await interaction.followup.send(replace_emojis("❌ Ставка должна быть больше 0."), ephemeral=False)
            return
        
        # Check if user is already in a game
        if user_id in active_users:
            await interaction.followup.send(replace_emojis("❌ Вы уже участвуете в игре."), ephemeral=False)
            return
        
        # Check balance
        if not await check_balance(user_id, guild_id, bet):
            await interaction.followup.send(replace_emojis("❌ Недостаточно баланса."), ephemeral=False)
            return
        
        # Determine mode
        if opponent:
            # PvP mode - direct challenge
            if opponent.id == user_id:
                await interaction.followup.send(replace_emojis("❌ Нельзя играть против себя."), ephemeral=False)
                return
            
            if opponent.bot:
                await interaction.followup.send(replace_emojis("❌ Нельзя играть против ботов."), ephemeral=False)
                return
            
            if opponent.id in active_users:
                await interaction.followup.send(replace_emojis("❌ Соперник уже участвует в игре."), ephemeral=False)
                return
            
            mode = GameMode.PVP
            opponent_id = opponent.id
        else:
            # PvE mode
            mode = GameMode.PVE
            opponent_id = None
        
        # Hold escrow
        escrow_success = await hold_escrow(user_id, guild_id, bet)
        if not escrow_success:
            await interaction.followup.send(replace_emojis("❌ Не удалось удержать ставку."), ephemeral=True)
            return
        
        # Create game session
        game_id = f"rps_{user_id}_{guild_id}_{asyncio.get_event_loop().time()}"
        game = GameSession(
            game_id=game_id,
            mode=mode,
            state=GameState.INIT,
            initiator_id=user_id,
            guild_id=guild_id,
            bet=bet,
            opponent_id=opponent_id
        )
        active_games[game_id] = game
        active_users.add(user_id)
        
        try:
            if mode == GameMode.PVE:
                # PvE mode
                game.state = GameState.WAITING_PVE
                view = PvEChoiceView(game_id, bet)
                embed = discord.Embed(
                    title=replace_emojis("🎮 Камень-Ножницы-Бумага | Игра против ИИ"),
                    description=f"Ставка: {bet} {replace_emojis('🪙')}\n"
                                  f"Сделайте ваш ход, выбрав одну из кнопок ниже. У вас есть 30 секунд!",
                    color=discord.Color.blue()
                )
                msg = await interaction.followup.send(embed=embed, view=view, ephemeral=False)

                # Create thread for the game
                thread_name = f"🎮 RPS - {interaction.user.display_name}"
                try:
                    thread = await msg.create_thread(
                        name=thread_name,
                        auto_archive_duration=60
                    )
                    await thread.send(f"{replace_emojis('white_arrow')} Игра началась! Используйте кнопки выше для взаимодействия.")
                except Exception as e:
                    logger.error(f"Failed to create thread: {e}")
            else:
                # PvP mode
                game.state = GameState.WAITING_PVP
                
                if opponent_id:
                    # Direct challenge - hold escrow for opponent immediately to check balance
                    opponent_escrow = await hold_escrow(opponent_id, guild_id, bet)
                    if not opponent_escrow:
                        await release_escrow(user_id, guild_id, bet)
                        active_users.discard(user_id)
                        del active_games[game_id]
                        await interaction.followup.send(replace_emojis("❌ У соперника недостаточно баланса."), ephemeral=True)
                        return
                    # Opponent is not added to active_users yet (only when they accept)
                    
                    view = PvPChallengeView(game_id, user_id, opponent_id, bet, interaction.channel_id, 0, self.bot)
                    total_pot = bet * 2 * 0.95
                    embed = discord.Embed(
                        title=replace_emojis("⚔️ Вызов на дуэль: Камень-Ножницы-Бумага"),
                        description=f"<@{user_id}> вызывает <@{opponent_id}> на дуэль!\n\n"
                                      f"{replace_emojis('💰')} Ставка: {bet} {replace_emojis('🪙')}\n"
                                      f"{replace_emojis('🏆')} Призовой фонд: {total_pot} {replace_emojis('🪙')} (комиссия 5%)\n\n"
                                      f"<@{opponent_id}>, примите вызов в течение 60 секунд.",
                        color=discord.Color.gold()
                    )
                    msg = await interaction.followup.send(embed=embed, view=view)
                    game.message_id = msg.id
                    game.channel_id = interaction.channel.id
                    # Update view with message_id after sending
                    view.message_id = msg.id

                    # Create thread for the game
                    thread_name = f"⚔️ RPS - {interaction.user.display_name} vs {opponent.display_name}"
                    try:
                        thread = await msg.create_thread(
                            name=thread_name,
                            auto_archive_duration=60
                        )
                        await thread.send(f"{replace_emojis('white_arrow')} Вызов отправлен! Используйте кнопки выше для взаимодействия.")
                    except Exception as e:
                        logger.error(f"Failed to create thread: {e}")
                else:
                    # Open challenge - anyone can accept
                    view = PvPChallengeView(game_id, user_id, None, bet, interaction.channel_id, 0, self.bot)
                    total_pot = bet * 2 * 0.95
                    embed = discord.Embed(
                        title=replace_emojis("⚔️ Открытый вызов: Камень-Ножницы-Бумага"),
                        description=f"<@{user_id}> ищет соперника на дуэль!\n\n"
                                      f"{replace_emojis('💰')} Ставка: {bet} {replace_emojis('🪙')}\n"
                                      f"{replace_emojis('🏆')} Призовой фонд: {total_pot} {replace_emojis('🪙')} (комиссия 5%)\n\n"
                                      f'Нажмите "Принять" чтобы принять вызов в течение 60 секунд.',
                        color=discord.Color.gold()
                    )
                    msg = await interaction.followup.send(embed=embed, view=view)
                    game.message_id = msg.id
                    game.channel_id = interaction.channel.id
                    # Update view with message_id after sending
                    view.message_id = msg.id

                    # Create thread for the game
                    thread_name = f"⚔️ RPS - {interaction.user.display_name} (открытый вызов)"
                    try:
                        thread = await msg.create_thread(
                            name=thread_name,
                            auto_archive_duration=60
                        )
                        await thread.send(f"{replace_emojis('white_arrow')} Открытый вызов! Используйте кнопки выше для взаимодействия.")
                    except Exception as e:
                        logger.error(f"Failed to create thread: {e}")
        
        except Exception as e:
            logger.error(f"Error starting RPS game: {e}", exc_info=True)
            # Refund on error
            await release_escrow(user_id, guild_id, bet)
            if mode == GameMode.PVP and opponent:
                await release_escrow(opponent.id, guild_id, bet)
            active_users.discard(user_id)
            if mode == GameMode.PVP and opponent:
                active_users.discard(opponent.id)
            if game_id in active_games:
                del active_games[game_id]
            await interaction.followup.send(replace_emojis("❌ Произошла ошибка при запуске игры."), ephemeral=True)


async def setup(bot: commands.Bot):
    """Load the cog."""
    await bot.add_cog(RPSCog(bot))
