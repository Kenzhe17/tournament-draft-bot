"""Rock-Paper-Scissors game implementation with PvP and PvE modes."""

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
    p1_message_id: Optional[int] = None
    p2_message_id: Optional[int] = None


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
            await interaction.response.send_message("❌ Игра недоступна.", ephemeral=True)
            return
        
        if interaction.user.id != game.initiator_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return
        
        # Check if game already resolved (prevent double-click)
        if game.state != GameState.WAITING_PVE:
            await interaction.response.send_message("❌ Игра уже завершена.", ephemeral=True)
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
            balance_change = f"+{bet} 🪙"
            title = "🎉 Победа в игре против ИИ!"
        elif result == "draw":
            await release_escrow(user_id, guild_id, bet)
            balance_change = "0 🪙 (возврат)"
            title = "🤝 Ничья!"
        else:  # loss
            balance_change = f"-{bet} 🪙"
            title = "💥 Поражение против ИИ!"
        
        # Build result embed
        embed = discord.Embed(
            title=title,
            description=f"Ваш ход: {move.emoji} {move.display_name}\n"
                       f"Ход ИИ: {bot_move.emoji} {bot_move.display_name}\n\n"
                       f"💰 Изменение баланса: {balance_change}",
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
    
    def __init__(self, game_id: str, initiator_id: int, opponent_id: int, bet: int):
        super().__init__(game_id, timeout=60)
        self.initiator_id = initiator_id
        self.opponent_id = opponent_id
        self.bet = bet
    
    @discord.ui.button(label="Принять", emoji="⚔️", style=discord.ButtonStyle.success, custom_id="rps:accept")
    async def btn_accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_accept(interaction)
    
    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger, custom_id="rps:decline")
    async def btn_decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_decline(interaction)
    
    async def handle_accept(self, interaction: discord.Interaction):
        """Handle challenge acceptance."""
        game = active_games.get(self.game_id)
        if not game or game.state != GameState.WAITING_PVP:
            await interaction.response.send_message("❌ Вызов недоступен.", ephemeral=True)
            return
        
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Этот вызов не для вас.", ephemeral=True)
            return
        
        # Check if opponent is already in a game
        if self.opponent_id in active_users:
            await interaction.response.send_message("❌ Вы уже участвуете в игре.", ephemeral=True)
            return
        
        # Hold escrow for opponent
        escrow_success = await hold_escrow(self.opponent_id, game.guild_id, self.bet)
        if not escrow_success:
            await interaction.response.send_message("❌ Недостаточно баланса для ставки.", ephemeral=True)
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
        
        if interaction.user.id != self.opponent_id:
            await interaction.response.send_message("❌ Этот вызов не для вас.", ephemeral=True)
            return
        
        # Refund initiator
        await release_escrow(self.initiator_id, game.guild_id, self.bet)
        
        # Update message
        embed = discord.Embed(
            title="❌ Вызов отклонён",
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
        """Send choice views to both players."""
        channel = interaction.channel
        
        # View for initiator
        view_p1 = PvPChoiceView(self.game_id, self.initiator_id, self.opponent_id, self.bet, is_initiator=True)
        embed_p1 = discord.Embed(
            title="⚔️ Выберите ваш ход",
            description=f"Ваша ставка: {self.bet} 🪙\n"
                          f"Соперник: <@{self.opponent_id}>\n"
                          f"Сделайте выбор. Результат будет опубликован в чате!",
            color=discord.Color.blue()
        )
        msg_p1 = await channel.send(f"<@{self.initiator_id}>", embed=embed_p1, view=view_p1)
        game.p1_message_id = msg_p1.id
        
        # View for opponent
        view_p2 = PvPChoiceView(self.game_id, self.opponent_id, self.initiator_id, self.bet, is_initiator=False)
        embed_p2 = discord.Embed(
            title="⚔️ Выберите ваш ход",
            description=f"Ваша ставка: {self.bet} 🪙\n"
                          f"Соперник: <@{self.initiator_id}>\n"
                          f"Сделайте выбор. Результат будет опубликован в чате!",
            color=discord.Color.blue()
        )
        msg_p2 = await channel.send(f"<@{self.opponent_id}>", embed=embed_p2, view=view_p2)
        game.p2_message_id = msg_p2.id
        
        # Update game with message IDs
        game.channel_id = channel.id


class PvPChoiceView(RPSView):
    """View for PvP move selection."""
    
    def __init__(self, game_id: str, user_id: int, opponent_id: int, bet: int, is_initiator: bool):
        super().__init__(game_id, timeout=40)
        self.user_id = user_id
        self.opponent_id = opponent_id
        self.bet = bet
        self.is_initiator = is_initiator
    
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
            await interaction.response.send_message("❌ Игра недоступна.", ephemeral=True)
            return
        
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Это не ваша игра.", ephemeral=True)
            return
        
        # Disable buttons
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        
        # Record move
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
            balance_change_p1 = "0 🪙 (возврат)"
            balance_change_p2 = "0 🪙 (возврат)"
            title = "🤝 Ничья!"
        else:
            await payout_winner(winner_id, guild_id, winner_payout)
            loser_id = game.opponent_id if winner_id == game.initiator_id else game.initiator_id
            # Loser already lost escrow, no action needed
            balance_change_winner = f"+{winner_payout - bet} 🪙"
            balance_change_loser = f"-{bet} 🪙"
            title = f"⚔️ Итоги дуэли: <@{game.initiator_id}> vs <@{game.opponent_id}>"
        
        # Build result embed
        if result == "draw":
            description = (f"<@{game.initiator_id}>: {p1_move.emoji} {p1_move.display_name}\n"
                          f"<@{game.opponent_id}>: {p2_move.emoji} {p2_move.display_name}\n\n"
                          f"🤝 Ничья! Оба игрока получают возврат ставки.\n"
                          f"💰 <@{game.initiator_id}>: {balance_change_p1}\n"
                          f"💰 <@{game.opponent_id}>: {balance_change_p2}")
        else:
            winner_name = f"<@{winner_id}>"
            description = (f"<@{game.initiator_id}>: {p1_move.emoji} {p1_move.display_name}\n"
                          f"<@{game.opponent_id}>: {p2_move.emoji} {p2_move.display_name}\n\n"
                          f"🏆 Победитель: {winner_name}!\n"
                          f"💰 Выигрыш: {winner_payout} 🪙 (комиссия 5%)")
        
        embed = discord.Embed(
            title=title,
            description=description,
            color=discord.Color.gold()
        )
        
        # Delete choice messages
        try:
            channel = interaction.channel
            if game.p1_message_id:
                msg = await channel.fetch_message(game.p1_message_id)
                await msg.delete()
            if game.p2_message_id:
                msg = await channel.fetch_message(game.p2_message_id)
                await msg.delete()
        except Exception as e:
            logger.error(f"Error deleting choice messages: {e}")
        
        # Edit challenge message with result
        if game.message_id:
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
            await interaction.followup.send("❌ Ставка должна быть больше 0.", ephemeral=False)
            return
        
        # Check if user is already in a game
        if user_id in active_users:
            await interaction.followup.send("❌ Вы уже участвуете в игре.", ephemeral=False)
            return
        
        # Check balance
        if not await check_balance(user_id, guild_id, bet):
            await interaction.followup.send("❌ Недостаточно баланса.", ephemeral=False)
            return
        
        # Determine mode
        if opponent:
            # PvP mode
            if opponent.id == user_id:
                await interaction.followup.send("❌ Нельзя играть против себя.", ephemeral=False)
                return
            
            if opponent.bot:
                await interaction.followup.send("❌ Нельзя играть против ботов.", ephemeral=False)
                return
            
            if opponent.id in active_users:
                await interaction.followup.send("❌ Соперник уже участвует в игре.", ephemeral=False)
                return
            
            mode = GameMode.PVP
        else:
            # PvE mode
            mode = GameMode.PVE
        
        # Hold escrow
        escrow_success = await hold_escrow(user_id, guild_id, bet)
        if not escrow_success:
            await interaction.followup.send("❌ Не удалось удержать ставку.", ephemeral=True)
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
            opponent_id=opponent.id if opponent else None
        )
        active_games[game_id] = game
        active_users.add(user_id)
        
        try:
            if mode == GameMode.PVE:
                # PvE mode
                game.state = GameState.WAITING_PVE
                view = PvEChoiceView(game_id, bet)
                embed = discord.Embed(
                    title="🎮 Камень-Ножницы-Бумага | Игра против ИИ",
                    description=f"Ставка: {bet} 🪙\n"
                                  f"Сделайте ваш ход, выбрав одну из кнопок ниже. У вас есть 30 секунд!",
                    color=discord.Color.blue()
                )
                await interaction.followup.send(embed=embed, view=view, ephemeral=False)
            else:
                # PvP mode
                game.state = GameState.WAITING_PVP
                active_users.add(opponent.id)
                
                # Hold escrow for opponent (they need to accept first)
                opponent_escrow = await hold_escrow(opponent.id, guild_id, bet)
                if not opponent_escrow:
                    await release_escrow(user_id, guild_id, bet)
                    active_users.discard(user_id)
                    active_users.discard(opponent.id)
                    del active_games[game_id]
                    await interaction.followup.send("❌ У соперника недостаточно баланса.", ephemeral=True)
                    return
                
                view = PvPChallengeView(game_id, user_id, opponent.id, bet)
                total_pot = bet * 2 * 0.95
                embed = discord.Embed(
                    title="⚔️ Вызов на дуэль: Камень-Ножницы-Бумага",
                    description=f"<@{user_id}> вызывает <@{opponent.id}> на дуэль!\n\n"
                                  f"💰 Ставка: {bet} 🪙\n"
                                  f"🏆 Призовой фонд: {total_pot} 🪙 (комиссия 5%)\n\n"
                                  f"<@{opponent.id}>, примите вызов в течение 60 секунд.",
                    color=discord.Color.gold()
                )
                msg = await interaction.followup.send(embed=embed, view=view)
                game.message_id = msg.id
                game.channel_id = interaction.channel.id
        
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
            await interaction.followup.send("❌ Произошла ошибка при запуске игры.", ephemeral=True)


async def setup(bot: commands.Bot):
    """Load the cog."""
    await bot.add_cog(RPSCog(bot))
