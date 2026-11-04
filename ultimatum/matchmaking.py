"""Matchmaking algorithm for Ultimatum Solo Tournament."""

import logging
from dataclasses import dataclass
from typing import List, Tuple, Set, Optional
from collections import defaultdict

from ultimatum.models import Player

logger = logging.getLogger(__name__)


@dataclass
class Pair:
    """A potential match pair."""
    player1: Player
    player2: Player
    weight: float = 0.0


class Matchmaker:
    """Matchmaking algorithm for tournament."""

    def __init__(self, store):
        self.store = store

    async def create_matches(
        self,
        tournament_id: str,
        players: List[Player],
        round_number: int
    ) -> List[Tuple[int, int]]:
        """
        Create optimal matches for players.

        Returns list of (player1_id, player2_id) tuples.
        """
        if len(players) < 2:
            logger.warning(f"Not enough players for matching: {len(players)}")
            return []

        # Group players by win-loss record
        groups = self._group_by_record(players)

        # Log groups
        for record, group_players in sorted(groups.items()):
            logger.info(f"Group {record}: {len(group_players)} players")

        # Create pairs
        pairs = await self._create_pairs(tournament_id, groups, round_number)

        # Validate pairs
        if not self._validate_pairs(pairs, players):
            logger.error("Invalid pairs created - player appears in multiple matches")
            return []

        logger.info(f"Created {len(pairs)} pairs for round {round_number}")
        return pairs

    def _group_by_record(self, players: List[Player]) -> dict[str, List[Player]]:
        """Group players by win-loss record."""
        groups = defaultdict(list)
        for player in players:
            record = f"{player.wins}-{player.losses}"
            groups[record].append(player)
        return dict(groups)

    async def _create_pairs(
        self,
        tournament_id: str,
        groups: dict[str, List[Player]],
        round_number: int
    ) -> List[Tuple[int, int]]:
        """Create pairs from grouped players."""
        pairs = []
        used_players: Set[int] = set()

        # Sort groups by record (highest wins first)
        sorted_records = sorted(groups.keys(), key=lambda r: (
            -int(r.split('-')[0]),  # Descending wins
            int(r.split('-')[1])   # Ascending losses
        ))

        for record in sorted_records:
            group_players = groups[record]
            available = [p for p in group_players if p.discord_id not in used_players]

            if len(available) < 2:
                # Try to merge with adjacent group
                if len(available) == 1:
                    merged_pair = await self._merge_with_adjacent(
                        tournament_id, available[0], groups, sorted_records, record, used_players
                    )
                    if merged_pair:
                        pairs.append(merged_pair)
                        used_players.add(merged_pair[0])
                        used_players.add(merged_pair[1])
                continue

            # Create pairs within group, avoiding rematches
            group_pairs = await self._create_group_pairs(
                tournament_id, available, round_number
            )

            for p1_id, p2_id in group_pairs:
                pairs.append((p1_id, p2_id))
                used_players.add(p1_id)
                used_players.add(p2_id)

        return pairs

    async def _create_group_pairs(
        self,
        tournament_id: str,
        players: List[Player],
        round_number: int
    ) -> List[Tuple[int, int]]:
        """Create pairs within a group, avoiding rematches."""
        if len(players) < 2:
            return []

        # Generate all possible pairs
        all_pairs = []
        for i in range(len(players)):
            for j in range(i + 1, len(players)):
                p1, p2 = players[i], players[j]
                weight = await self._calculate_pair_weight(
                    tournament_id, p1, p2
                )
                all_pairs.append(Pair(p1, p2, weight))

        # Sort by weight (lower is better)
        all_pairs.sort(key=lambda p: p.weight)

        # Greedy selection avoiding conflicts
        pairs = []
        used: Set[int] = set()

        for pair in all_pairs:
            if pair.player1.discord_id not in used and pair.player2.discord_id not in used:
                pairs.append((pair.player1.discord_id, pair.player2.discord_id))
                used.add(pair.player1.discord_id)
                used.add(pair.player2.discord_id)

        return pairs

    async def _calculate_pair_weight(
        self,
        tournament_id: str,
        player1: Player,
        player2: Player
    ) -> float:
        """
        Calculate weight for a pair.

        Lower weight = better match.
        Factors:
        - Same record (0 weight bonus)
        - Close win difference
        - Close loss difference
        - Rematch penalty (high weight)
        """
        weight = 0.0

        # Record match bonus (negative = better)
        if player1.wins == player2.wins and player1.losses == player2.losses:
            weight -= 10.0

        # Win difference penalty
        win_diff = abs(player1.wins - player2.wins)
        weight += win_diff * 2.0

        # Loss difference penalty
        loss_diff = abs(player1.losses - player2.losses)
        weight += loss_diff * 2.0

        # Rematch penalty (very high)
        if await self.store.have_played_before(
            tournament_id, player1.discord_id, player2.discord_id
        ):
            weight += 100.0

        return weight

    async def _merge_with_adjacent(
        self,
        tournament_id: str,
        player: Player,
        groups: dict[str, List[Player]],
        sorted_records: List[str],
        current_record: str,
        used_players: Set[int]
    ) -> Optional[Tuple[int, int]]:
        """Try to merge a single player with adjacent group."""
        current_idx = sorted_records.index(current_record)

        # Check previous and next groups
        candidates = []

        for offset in [-1, 1]:
            idx = current_idx + offset
            if 0 <= idx < len(sorted_records):
                adjacent_record = sorted_records[idx]
                adjacent_players = [
                    p for p in groups[adjacent_record]
                    if p.discord_id not in used_players
                ]
                if adjacent_players:
                    candidates.extend(adjacent_players)

        if not candidates:
            return None

        # Find best candidate
        best_candidate = None
        best_weight = float('inf')

        for candidate in candidates:
            weight = await self._calculate_pair_weight(
                tournament_id, player, candidate
            )
            if weight < best_weight:
                best_weight = weight
                best_candidate = candidate

        if best_candidate:
            # Check for rematch warning
            if await self.store.have_played_before(
                tournament_id, player.discord_id, best_candidate.discord_id
            ):
                logger.warning(
                    f"Rematch warning: {player.username} vs {best_candidate.username}"
                )

            return (player.discord_id, best_candidate.discord_id)

        return None

    def _validate_pairs(
        self,
        pairs: List[Tuple[int, int]],
        original_players: List[Player]
    ) -> bool:
        """Validate that pairs are correct."""
        # Check no player appears twice
        player_counts = defaultdict(int)
        for p1_id, p2_id in pairs:
            player_counts[p1_id] += 1
            player_counts[p2_id] += 1

        for player_id, count in player_counts.items():
            if count > 1:
                logger.error(f"Player {player_id} appears in {count} pairs")
                return False

        # Check all players in pairs are in original list
        original_ids = {p.discord_id for p in original_players}
        for p1_id, p2_id in pairs:
            if p1_id not in original_ids or p2_id not in original_ids:
                logger.error(f"Pair contains invalid player: {p1_id}, {p2_id}")
                return False

        return True
