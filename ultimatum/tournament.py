"""Tournament logic for Ultimatum Solo."""

import logging
from typing import List, Optional

from ultimatum.models import (
    Tournament, Player, Match, Round,
    TournamentStatus, PlayerStatus, MatchStatus, MatchType, RoundStatus
)
from ultimatum.store import UltimatumStore
from ultimatum.matchmaking import Matchmaker
from ultimatum.elo_store import elo_store

logger = logging.getLogger(__name__)


class UltimatumTournament:
    """Main tournament logic controller."""

    def __init__(self, store: UltimatumStore):
        self.store = store
        self.matchmaker = Matchmaker(store)

    async def start_tournament(self, tournament_id: str, bot=None) -> None:
        """Start tournament after registration is complete."""
        tournament = await self.store.get_tournament(tournament_id)
        if not tournament:
            raise ValueError("Tournament not found")

        players = await self.store.get_players(tournament_id)
        if len(players) != 16:
            raise ValueError(f"Need exactly 16 players, got {len(players)}")

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.IN_PROGRESS, current_round=1
        )

        # Create first round
        await self.create_round(tournament_id, 1, bot)

        logger.info(f"Tournament {tournament_id} started with {len(players)} players")
        
        # Send round embed to tournament channel
        if bot and tournament.guild_id:
            guild = bot.get_guild(tournament.guild_id)
            if guild:
                from views.ultimatum_view import build_ultimatum_round_embed, build_ultimatum_round_view, round_messages
                try:
                    new_embed = await build_ultimatum_round_embed(tournament_id, 1, guild)
                    new_view = await build_ultimatum_round_view(bot, tournament_id, 1)
                    
                    # Find tournament channel with Ultimatum setup embed
                    setup_message = None
                    target_channel = None
                    
                    for channel in guild.text_channels:
                        async for msg in channel.history(limit=50):
                            if msg.embeds and msg.embeds[0].title:
                                title = msg.embeds[0].title
                                if "УЛЬТИМАТУМ" in title and "РЕГИСТРАЦИЯ" in title:
                                    setup_message = msg
                                    target_channel = channel
                                    break
                        if setup_message:
                            break
                    
                    if target_channel and setup_message:
                        # Send round message first
                        round_message = await target_channel.send(embed=new_embed, view=new_view)
                        logger.info(f"Sent round message: {round_message.id}")
                        
                        # Track the round message
                        round_messages[guild.id] = (target_channel.id, round_message.id)
                        
                        # Delete setup message after round message is sent
                        await setup_message.delete()
                        logger.info("Deleted setup message after start")
                    else:
                        logger.warning("Could not find Ultimatum setup embed to replace")
                        # Try to send to same channel as tournament is registered
                        # or find by searching for tournament_id in message content
                except Exception as e:
                    logger.error(f"Error sending round embed: {e}", exc_info=True)

    async def create_round(self, tournament_id: str, round_number: int, bot=None) -> None:
        """Create a new round with matches."""
        tournament = await self.store.get_tournament(tournament_id)
        if not tournament:
            raise ValueError("Tournament not found")

        # Get active players
        players = await self.store.get_active_players(tournament_id)

        if len(players) < 2:
            logger.warning(f"Not enough active players for round {round_number}: {len(players)}")
            return

        # Check for grand final condition
        if await self._should_create_grand_final(tournament_id, players):
            await self._create_grand_final(tournament_id, players, bot)
            return

        # Create round in database (handle duplicate key error)
        try:
            await self.store.create_round(tournament_id, round_number)
        except Exception as e:
            if "duplicate key" in str(e).lower():
                logger.warning(f"Round {round_number} already exists for tournament {tournament_id}, skipping creation")
                return
            raise

        # Create matches using matchmaking
        player_pairs = await self.matchmaker.create_matches(
            tournament_id, players, round_number
        )

        # Create match records
        for p1_id, p2_id in player_pairs:
            await self.store.create_match(
                tournament_id, round_number, p1_id, p2_id, MatchType.NORMAL
            )
            # Add to match history
            await self.store.add_match_history(tournament_id, p1_id, p2_id, round_number)

        # Update round status
        await self.store.update_round_status(
            tournament_id, round_number, RoundStatus.IN_PROGRESS
        )

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.ROUND_IN_PROGRESS, round_number
        )

        # Add tournament role to all players in matches
        if bot and tournament.guild_id:
            guild = bot.get_guild(tournament.guild_id)
            if guild:
                role = guild.get_role(1567580142744895561)
                if role:
                    for p1_id, p2_id in player_pairs:
                        try:
                            member1 = guild.get_member(p1_id)
                            member2 = guild.get_member(p2_id)
                            if member1:
                                await member1.add_roles(role)
                            if member2:
                                await member2.add_roles(role)
                        except Exception as e:
                            logger.error(f"Error adding role to player: {e}")

        logger.info(f"Round {round_number} created with {len(player_pairs)} matches")

    async def complete_match(
        self,
        match_id: int,
        winner_id: int,
        loser_id: int,
        bot=None
    ) -> None:
        """Complete a match and update player stats."""
        logger.info(f"Completing match {match_id}: winner={winner_id}, loser={loser_id}")
        
        # Get match details
        match = await self.store.get_match_by_id(match_id)
        if not match:
            raise ValueError("Match not found")

        logger.info(f"Match details: tournament_id={match.tournament_id}, round={match.round_number}, type={match.match_type}")

        # Update match in database
        await self.store.complete_match(match_id, winner_id, loser_id)

        # Get players
        winner = await self.store.get_player_by_discord_id(match.tournament_id, winner_id)
        loser = await self.store.get_player_by_discord_id(match.tournament_id, loser_id)

        if not winner or not loser:
            raise ValueError("Player not found")

        # Update player stats (wins/losses)
        await self.store.update_player_stats(
            match.tournament_id,
            winner_id,
            winner.wins + 1,
            winner.losses
        )

        await self.store.update_player_stats(
            match.tournament_id,
            loser_id,
            loser.wins,
            loser.losses + 1
        )

        logger.info(f"Player stats updated: winner={winner_id} ({winner.wins+1}:{winner.losses}), loser={loser_id} ({loser.wins}:{loser.losses+1})")

        # Check if loser should be eliminated (2 losses)
        if loser.losses + 1 >= 2:
            logger.info(f"Player {loser_id} eliminated (2 losses)")
            await self.eliminate_player(match.tournament_id, loser_id, match.round_number, bot)

        # Update Elo ratings (only for normal matches, not for byes)
        if match.match_type == MatchType.NORMAL:
            try:
                await elo_store.update_elo(
                    match_id,
                    match.tournament_id,
                    winner_id,
                    loser_id,
                    winner.username,
                    loser.username
                )
                logger.info(f"Elo updated for match {match_id}")
            except Exception as e:
                logger.error(f"Failed to update Elo: {e}")
        elif match.match_type == MatchType.GRAND_FINAL:
            # Grand final: determine champion and runner-up
            logger.info("Grand final match completed, calling _complete_grand_final")
            await self._complete_grand_final(match.tournament_id, winner_id, loser_id, bot)

        # Check if round is complete (skip for grand final)
        if match.match_type != MatchType.GRAND_FINAL:
            logger.info(f"Checking round {match.round_number} completion for tournament {match.tournament_id}")
            await self._check_round_completion(match.tournament_id, match.round_number, bot)

    async def _complete_grand_final(
        self,
        tournament_id: str,
        winner_id: int,
        loser_id: int,
        bot=None
    ) -> None:
        """Complete grand final and determine champion."""
        # Set champion status
        await self.store.update_player_stats(
            tournament_id,
            winner_id,
            None,
            None,
            PlayerStatus.CHAMPION
        )

        # Set runner-up status
        await self.store.update_player_stats(
            tournament_id,
            loser_id,
            None,
            None,
            PlayerStatus.GRAND_FINAL_LOSER
        )

        # Finalize tournament with rankings
        await self.finalize_tournament(tournament_id, bot)

        logger.info(f"Grand final completed: Champion {winner_id}, Runner-up {loser_id}")

    async def _check_round_completion(self, tournament_id: str, round_number: int, bot=None) -> None:
        """Check if all matches in round are complete and start next round."""
        matches = await self.store.get_matches(tournament_id, round_number)

        logger.info(f"Checking round {round_number} completion for tournament {tournament_id}")
        logger.info(f"Total matches: {len(matches)}, Completed: {sum(1 for m in matches if m.status == MatchStatus.COMPLETED)}")

        # Check if all matches are completed
        all_complete = all(m.status == MatchStatus.COMPLETED for m in matches)

        if not all_complete:
            logger.info(f"Round {round_number} not complete yet for tournament {tournament_id}")
            return

        # Mark round as completed
        await self.store.update_round_status(
            tournament_id, round_number, RoundStatus.COMPLETED
        )

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.ROUND_COMPLETED
        )

        logger.info(f"Round {round_number} completed for tournament {tournament_id}")

        # Start next round automatically
        next_round = round_number + 1
        logger.info(f"Starting round {next_round} for tournament {tournament_id}")
        try:
            await self.create_round(tournament_id, next_round, bot)
            logger.info(f"Round {next_round} created successfully")
        except Exception as e:
            logger.error(f"Error creating round {next_round}: {e}", exc_info=True)
            return
        
        # Update round embed to show new round
        if bot:
            from views.ultimatum_view import update_round_embed, round_messages
            tournament = await self.store.get_tournament(tournament_id)
            if tournament and tournament.guild_id:
                guild = bot.get_guild(tournament.guild_id)
                if guild and guild.id in round_messages:
                    try:
                        await update_round_embed(guild, tournament_id, next_round, bot)
                        logger.info(f"Round embed updated to round {next_round}")
                    except Exception as e:
                        logger.error(f"Error updating round embed: {e}", exc_info=True)

    async def _should_create_grand_final(
        self,
        tournament_id: str,
        players: List[Player]
    ) -> bool:
        """Check if grand final conditions are met.

        Grand final scenarios:
        1. 4-0 Direct Pass: One player has 4-0 record, other emerges from 3-1 bracket
        2. 5-1 Qualifier: Both players have 1 loss, but only 2 remain

        Grand final is created when:
        - Exactly 2 active players remain
        - AND (one has 4-0 OR both have >= 4 wins with max 1 loss)
        """
        if len(players) != 2:
            return False

        p1, p2 = players[0], players[1]

        # Scenario 1: 4-0 Direct Pass
        # One player has perfect 4-0, other has any record
        has_4_0 = (p1.wins == 4 and p1.losses == 0) or (p2.wins == 4 and p2.losses == 0)
        if has_4_0:
            return True

        # Scenario 2: 5-1 Qualifier
        # Both players have at least 4 wins and max 1 loss
        # (This happens when both come from 3-1 bracket and win their matches)
        both_strong = (
            p1.wins >= 4 and p1.losses <= 1 and
            p2.wins >= 4 and p2.losses <= 1
        )
        if both_strong:
            return True

        return False

    async def _create_grand_final(
        self,
        tournament_id: str,
        players: List[Player],
        bot=None
    ) -> None:
        """Create grand final match."""
        tournament = await self.store.get_tournament(tournament_id)
        
        # Sort players by wins, then by fewer losses
        sorted_players = sorted(
            players,
            key=lambda p: (-p.wins, p.losses)
        )
        player1, player2 = sorted_players[0], sorted_players[1]

        # The 4-0 player gets direct pass status
        if player1.wins == 4 and player1.losses == 0:
            direct_pass = player1
            qualifier = player2
        elif player2.wins == 4 and player2.losses == 0:
            direct_pass = player2
            qualifier = player1
        else:
            # Neither has 4-0, this shouldn't happen with current logic
            direct_pass = sorted_players[0]
            qualifier = sorted_players[1]

        # Set grand finalist status for both
        await self.store.update_player_stats(
            tournament_id, direct_pass.discord_id, direct_pass.wins, direct_pass.losses,
            PlayerStatus.GRAND_FINALIST
        )
        await self.store.update_player_stats(
            tournament_id, qualifier.discord_id, qualifier.wins, qualifier.losses,
            PlayerStatus.GRAND_FINALIST
        )

        # Create grand final match
        await self.store.create_match(
            tournament_id, 999, direct_pass.discord_id, qualifier.discord_id, MatchType.GRAND_FINAL
        )

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.GRAND_FINAL
        )

        # Add tournament role to grand final players
        if bot and tournament and tournament.guild_id:
            guild = bot.get_guild(tournament.guild_id)
            if guild:
                role = guild.get_role(1567580142744895561)
                if role:
                    try:
                        member1 = guild.get_member(direct_pass.discord_id)
                        member2 = guild.get_member(qualifier.discord_id)
                        if member1:
                            await member1.add_roles(role)
                        if member2:
                            await member2.add_roles(role)
                    except Exception as e:
                        logger.error(f"Error adding role to grand final player: {e}")

        logger.info(
            f"Grand final created: {direct_pass.username} ({direct_pass.wins}:{direct_pass.losses}) vs "
            f"{qualifier.username} ({qualifier.wins}:{qualifier.losses})"
        )

    async def eliminate_player(
        self,
        tournament_id: str,
        discord_id: int,
        elimination_round: Optional[int] = None,
        bot=None
    ) -> None:
        """Eliminate a player (2 losses)."""
        tournament = await self.store.get_tournament(tournament_id)
        round_num = elimination_round or tournament.current_round

        await self.store.update_player_stats(
            tournament_id, discord_id, None, None,
            PlayerStatus.ELIMINATED, round_num
        )
        
        # Remove tournament role from eliminated player
        if bot and tournament and tournament.guild_id:
            guild = bot.get_guild(tournament.guild_id)
            if guild:
                role = guild.get_role(1567580142744895561)
                if role:
                    try:
                        member = guild.get_member(discord_id)
                        if member:
                            await member.remove_roles(role)
                    except Exception as e:
                        logger.error(f"Error removing role from eliminated player: {e}")
        
        logger.info(f"Player {discord_id} eliminated from tournament {tournament_id} in round {round_num}")

    async def finalize_tournament(self, tournament_id: str, bot=None) -> None:
        """Finalize tournament and calculate rankings 1-16.

        Ranking criteria (in order):
        1. Status (Champion > Grand Final Loser > Grand Finalist > Eliminated > Active)
        2. Total wins (more wins = better)
        3. Total losses (fewer losses = better)
        4. Elimination round (later = better, for eliminated players)
        """
        tournament = await self.store.get_tournament(tournament_id)
        
        # Get all players
        players = await self.store.get_players(tournament_id)

        # Sort by status priority, then wins, then losses, then elimination round
        status_priority = {
            PlayerStatus.CHAMPION: 0,
            PlayerStatus.GRAND_FINAL_LOSER: 1,
            PlayerStatus.GRAND_FINALIST: 2,
            PlayerStatus.ELIMINATED: 3,
            PlayerStatus.ACTIVE: 4,
        }

        sorted_players = sorted(
            players,
            key=lambda p: (
                status_priority.get(p.status, 99),
                -p.wins,
                p.losses if p.losses else 0,
                -(p.elimination_round if p.elimination_round else 0)  # Later elimination = better
            )
        )

        # Assign places 1-16
        for idx, player in enumerate(sorted_players):
            place = idx + 1
            await self.store.update_player_final_place(
                tournament_id, player.discord_id, place
            )

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.COMPLETED
        )

        # Remove tournament role from all players
        if bot and tournament and tournament.guild_id:
            guild = bot.get_guild(tournament.guild_id)
            if guild:
                role = guild.get_role(1567580142744895561)
                if role:
                    for player in players:
                        try:
                            member = guild.get_member(player.discord_id)
                            if member:
                                await member.remove_roles(role)
                        except Exception as e:
                            logger.error(f"Error removing role from player {player.discord_id}: {e}")

        logger.info(f"Tournament {tournament_id} finalized with {len(sorted_players)} players ranked")

        # Update tournament status
        await self.store.update_tournament_status(
            tournament_id, TournamentStatus.COMPLETED
        )

        logger.info(f"Tournament {tournament_id} finalized with {len(players)} players")
