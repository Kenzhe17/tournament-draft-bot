"""BracketService — генерация и продвижение сеток турниров.

Форматы: single_elimination, double_elimination, round_robin.
Узлы BracketNode хранят связи winner_to/loser_to для автоведущего.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from arbiter.enums import Bracket, TournamentFormat
from arbiter.models import BracketNode, TournamentEntry, dumps, loads, new_id, utcnow
from arbiter.services.org_service import org_service
from storage.db import get_pool

logger = logging.getLogger(__name__)


class BracketError(Exception):
    pass


def _row_to_node(r: asyncpg.Record) -> BracketNode:
    return BracketNode(
        id=r["id"], tournament_id=r["tournament_id"], bracket=Bracket(r["bracket"]),
        round_index=r["round_index"], slot_index=r["slot_index"], label=r["label"],
        best_of=r["best_of"],
        team_a_entry_id=r["team_a_entry_id"], team_b_entry_id=r["team_b_entry_id"],
        team_a_name=r["team_a_name"], team_b_name=r["team_b_name"],
        team_a_source=r["team_a_source"], team_b_source=r["team_b_source"],
        team_a_score=r["team_a_score"], team_b_score=r["team_b_score"],
        team_a_result=r["team_a_result"], team_b_result=r["team_b_result"],
        winner_slot=r["winner_slot"], status=r["status"], match_id=r["match_id"],
        winner_to_node_id=r["winner_to_node_id"], winner_to_slot=r["winner_to_slot"],
        loser_to_node_id=r["loser_to_node_id"], loser_to_slot=r["loser_to_slot"],
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _row_to_entry(r: asyncpg.Record) -> TournamentEntry:
    return TournamentEntry(
        id=r["id"], tournament_id=r["tournament_id"], team_id=r["team_id"],
        team_name=r["team_name"], seed=r["seed"],
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _next_power_of_two(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


class BracketService:
    # ---------- entries ----------

    async def add_entry(self, tournament_id: str, team_name: str,
                        *, team_id: str | None = None, seed: int = 0) -> TournamentEntry:
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT COUNT(*) FROM arb_tournament_entries WHERE tournament_id=$1",
                tournament_id)
            entry = TournamentEntry(tournament_id=tournament_id, team_id=team_id,
                                    team_name=team_name.strip(),
                                    seed=seed or existing + 1)
            await conn.execute(
                "INSERT INTO arb_tournament_entries (id, tournament_id, team_id, team_name, "
                "seed, created_at, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7)",
                entry.id, tournament_id, team_id, entry.team_name, entry.seed,
                utcnow(), utcnow())
            await org_service._audit(conn, None, None, "tournament.entry_add",
                                     "tournament", tournament_id,
                                     {"team": entry.team_name, "seed": entry.seed})
        return entry

    async def list_entries(self, tournament_id: str) -> list[TournamentEntry]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM arb_tournament_entries WHERE tournament_id=$1 ORDER BY seed",
                tournament_id)
            return [_row_to_entry(r) for r in rows]

    async def remove_entry(self, entry_id: str) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM arb_tournament_entries WHERE id=$1 RETURNING tournament_id",
                entry_id)
            return row is not None

    # ---------- nodes ----------

    async def _insert_node(self, conn: asyncpg.Connection, node: BracketNode) -> BracketNode:
        await conn.execute(
            "INSERT INTO arb_bracket_nodes (id, tournament_id, bracket, round_index, "
            "slot_index, label, best_of, team_a_entry_id, team_b_entry_id, "
            "team_a_name, team_b_name, team_a_source, team_b_source, "
            "team_a_score, team_b_score, team_a_result, team_b_result, winner_slot, "
            "status, match_id, winner_to_node_id, winner_to_slot, loser_to_node_id, "
            "loser_to_slot, created_at, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,"
            "$21,$22,$23,$24,$25,$26)",
            node.id, node.tournament_id, node.bracket.value, node.round_index,
            node.slot_index, node.label, node.best_of, node.team_a_entry_id,
            node.team_b_entry_id, node.team_a_name, node.team_b_name,
            node.team_a_source, node.team_b_source, node.team_a_score,
            node.team_b_score, node.team_a_result, node.team_b_result,
            node.winner_slot, node.status, node.match_id, node.winner_to_node_id,
            node.winner_to_slot, node.loser_to_node_id, node.loser_to_slot,
            utcnow(), utcnow())
        return node

    async def list_nodes(self, tournament_id: str,
                         bracket: Bracket | None = None) -> list[BracketNode]:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if bracket:
                rows = await conn.fetch(
                    "SELECT * FROM arb_bracket_nodes WHERE tournament_id=$1 AND bracket=$2 "
                    "ORDER BY round_index, slot_index", tournament_id, bracket.value)
            else:
                rows = await conn.fetch(
                    "SELECT * FROM arb_bracket_nodes WHERE tournament_id=$1 "
                    "ORDER BY bracket, round_index, slot_index", tournament_id)
            return [_row_to_node(r) for r in rows]

    async def get_node(self, node_id: str) -> BracketNode | None:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM arb_bracket_nodes WHERE id=$1", node_id)
            return _row_to_node(row) if row else None

    async def _update_node(self, conn: asyncpg.Connection, node: BracketNode,
                           **fields: Any) -> None:
        updates = {k: v for k, v in fields.items()}
        sets = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
        values = []
        for v in updates.values():
            if isinstance(v, Bracket):
                v = v.value
            values.append(v)
        await conn.execute(
            f"UPDATE arb_bracket_nodes SET {sets}, updated_at=$1 WHERE id=${len(updates)+2}",
            utcnow(), *values, node.id)

    # ---------- generation ----------

    async def generate_single_elimination(self, tournament_id: str,
                                          entries: list[TournamentEntry],
                                          best_of: int = 1,
                                          third_place: bool = False) -> list[BracketNode]:
        """Single elimination bracket с byes."""
        n = len(entries)
        if n < 2:
            raise BracketError("Нужно минимум 2 команды")
        size = _next_power_of_two(n)
        rounds = size.bit_length() - 1

        # Посев: seed чередуется для баланса (1 vs size, 2 vs size-1...)
        seeded = sorted(entries, key=lambda e: e.seed)
        bracket_order: list[TournamentEntry | None] = [None] * size
        # стандартный порядок посева
        order = self._seed_order(size)
        for i, e in enumerate(seeded):
            bracket_order[order[i]] = e

        nodes: list[BracketNode] = []
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Round 0
                r0_nodes = []
                for i in range(0, size, 2):
                    a = bracket_order[i]
                    b = bracket_order[i + 1]
                    node = BracketNode(
                        tournament_id=tournament_id, bracket=Bracket.WINNERS,
                        round_index=0, slot_index=i // 2, best_of=best_of,
                        team_a_entry_id=a.id if a else None,
                        team_b_entry_id=b.id if b else None,
                        team_a_name=a.team_name if a else None,
                        team_b_name=b.team_name if b else None,
                        status="pending",
                    )
                    # bye handling
                    if a and not b:
                        node.status = "bye"
                        node.winner_slot = "teamA"
                        node.team_a_score = 0
                    elif b and not a:
                        node.status = "bye"
                        node.winner_slot = "teamB"
                    nodes.append(node)
                    await self._insert_node(conn, node)
                    r0_nodes.append(node)

                # Upper rounds
                prev = r0_nodes
                round_idx = 1
                while len(prev) > 1:
                    curr = []
                    for i in range(0, len(prev), 2):
                        left, right = prev[i], prev[i + 1]
                        node = BracketNode(
                            tournament_id=tournament_id, bracket=Bracket.WINNERS,
                            round_index=round_idx, slot_index=i // 2, best_of=best_of,
                            status="pending",
                            label=("Grand Final" if len(prev) == 2 and not third_place
                                   else f"Round {round_idx + 1}"),
                        )
                        # winner routing
                        node.winner_to_node_id = None  # set after creation
                        nodes.append(node)
                        await self._insert_node(conn, node)
                        curr.append(node)
                    prev = curr
                    round_idx += 1

                # Set winner_to links
                by_round: dict[int, list[BracketNode]] = {}
                for nd in nodes:
                    by_round.setdefault(nd.round_index, []).append(nd)
                for rd, rnodes in by_round.items():
                    if rd == 0:
                        continue
                    prev_round = by_round.get(rd - 1, [])
                    for i, node in enumerate(rnodes):
                        # winners of matches 2i and 2i+1 feed this node
                        if 2 * i < len(prev_round):
                            node.team_a_source = prev_round[2 * i].id
                        if 2 * i + 1 < len(prev_round):
                            node.team_b_source = prev_round[2 * i + 1].id
                        # find next round node
                        next_round = by_round.get(rd + 1, [])
                        if i // 2 < len(next_round):
                            target = next_round[i // 2]
                            # slot depends on position within pair
                            node.winner_to_node_id = target.id
                            node.winner_to_slot = "teamA" if i % 2 == 0 else "teamB"
                            await self._update_node(conn, node,
                                                    winner_to_node_id=target.id,
                                                    winner_to_slot=node.winner_to_slot)

                # Final node champion routing: mark GF
                if by_round:
                    max_rd = max(by_round)
                    finals = by_round[max_rd]
                    if finals:
                        finals[0].label = "Grand Final"
                        await self._update_node(conn, finals[0], label="Grand Final")

                # Third place: losers of semifinals
                if third_place and rounds >= 2:
                    semis = by_round.get(rounds - 2, [])
                    if len(semis) >= 2:
                        tp = BracketNode(
                            tournament_id=tournament_id, bracket=Bracket.THIRD_PLACE,
                            round_index=0, slot_index=0, best_of=best_of,
                            label="Third Place", status="pending",
                        )
                        # losers of semis feed TP
                        for i, semi in enumerate(semis[:2]):
                            semi.loser_to_node_id = tp.id if False else None  # set below
                        await self._insert_node(conn, tp)
                        for i, semi in enumerate(semis[:2]):
                            await self._update_node(conn, semi,
                                                    loser_to_node_id=tp.id,
                                                    loser_to_slot="teamA" if i == 0 else "teamB")
                        tp.team_a_source = semis[0].id
                        tp.team_b_source = semis[1].id
                        await self._update_node(conn, tp,
                                                team_a_source=semis[0].id,
                                                team_b_source=semis[1].id)
                        nodes.append(tp)
        return nodes

    async def generate_round_robin(self, tournament_id: str,
                                   entries: list[TournamentEntry],
                                   best_of: int = 1) -> list[BracketNode]:
        """Round robin: каждый с каждым, круговой метод Berger."""
        n = len(entries)
        if n < 2:
            raise BracketError("Нужно минимум 2 команды")
        if n % 2 == 1:
            entries = entries + [TournamentEntry(tournament_id=tournament_id,
                                                 team_name="BYE", seed=0)]
            n += 1
        rounds = n - 1
        nodes: list[BracketNode] = []
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                arr = list(entries)
                for r in range(rounds):
                    for i in range(n // 2):
                        a = arr[i]
                        b = arr[n - 1 - i]
                        if a.team_name == "BYE" or b.team_name == "BYE":
                            continue
                        node = BracketNode(
                            tournament_id=tournament_id, bracket=Bracket.ROUND_ROBIN,
                            round_index=r, slot_index=i, best_of=best_of,
                            team_a_entry_id=a.id, team_b_entry_id=b.id,
                            team_a_name=a.team_name, team_b_name=b.team_name,
                            status="pending",
                        )
                        nodes.append(node)
                        await self._insert_node(conn, node)
                    # rotate
                    arr = [arr[0]] + [arr[-1]] + arr[1:-1]
        return nodes

    @staticmethod
    def _seed_order(size: int) -> list[int]:
        """Порядок посева для balanced bracket (1, size, size/2+1, size/2, ...)."""
        if size == 1:
            return [0]
        half = size // 2
        prev = BracketService._seed_order(half)
        order = []
        for i in prev:
            order.append(i)
            order.append(size - 1 - i)
        # reorder to top/bottom halves
        return order

    # ---------- progression ----------

    async def complete_node(self, node_id: str, winner_slot: str,
                            team_a_score: int = 0, team_b_score: int = 0,
                            team_a_result: str | None = None,
                            team_b_result: str | None = None) -> BracketNode:
        """Завершить узел и продвинуть победителя/проигравшего."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT * FROM arb_bracket_nodes WHERE id=$1 FOR UPDATE", node_id)
                if not row:
                    raise BracketError("Узел не найден")
                node = _row_to_node(row)
                if node.status == "complete":
                    raise BracketError("Узел уже завершён")
                if winner_slot not in ("teamA", "teamB"):
                    raise BracketError("winner_slot: teamA|teamB")

                winner_name = node.team_a_name if winner_slot == "teamA" else node.team_b_name
                loser_name = node.team_b_name if winner_slot == "teamA" else node.team_a_name
                winner_entry = node.team_a_entry_id if winner_slot == "teamA" else node.team_b_entry_id
                loser_entry = node.team_b_entry_id if winner_slot == "teamA" else node.team_a_entry_id

                await conn.execute(
                    "UPDATE arb_bracket_nodes SET team_a_score=$1, team_b_score=$2, "
                    "team_a_result=$3, team_b_result=$4, winner_slot=$5, status='complete', "
                    "updated_at=$6 WHERE id=$7",
                    team_a_score, team_b_score, team_a_result, team_b_result,
                    winner_slot, utcnow(), node_id)

                # Winner propagation
                if node.winner_to_node_id:
                    slot_col = f"team_{node.winner_to_slot[4].lower()}_name"
                    entry_col = f"team_{node.winner_to_slot[4].lower()}_entry_id"
                    await conn.execute(
                        f"UPDATE arb_bracket_nodes SET {slot_col}=$1, {entry_col}=$2, "
                        f"status=CASE WHEN status='pending' THEN 'ready' ELSE status END, "
                        f"updated_at=$3 WHERE id=$4",
                        winner_name, winner_entry, utcnow(), node.winner_to_node_id)

                # Loser propagation (double elim / third place)
                if node.loser_to_node_id:
                    slot_col = f"team_{node.loser_to_slot[4].lower()}_name"
                    entry_col = f"team_{node.loser_to_slot[4].lower()}_entry_id"
                    await conn.execute(
                        f"UPDATE arb_bracket_nodes SET {slot_col}=$1, {entry_col}=$2, "
                        f"status=CASE WHEN status='pending' THEN 'ready' ELSE status END, "
                        f"updated_at=$3 WHERE id=$4",
                        loser_name, loser_entry, utcnow(), node.loser_to_node_id)

                await org_service._audit(conn, None, None, "bracket.node_complete",
                                         "bracket_node", node_id,
                                         {"winner": winner_slot, "winner_name": winner_name})

                # Check tournament completion
                await self._maybe_complete_tournament(conn, node.tournament_id)

        return await self.get_node(node_id)

    async def _maybe_complete_tournament(self, conn: asyncpg.Connection,
                                         tournament_id: str) -> None:
        """Если все узлы завершены/bye — пометить турнир complete."""
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE status IN ('complete','bye')) AS done "
            "FROM arb_bracket_nodes WHERE tournament_id=$1", tournament_id)
        if row and row["total"] > 0 and row["total"] == row["done"]:
            # champion = winner of grand final / last winners node
            gf = await conn.fetchrow(
                "SELECT team_a_name, team_b_name, winner_slot FROM arb_bracket_nodes "
                "WHERE tournament_id=$1 AND bracket='winners' "
                "ORDER BY round_index DESC LIMIT 1", tournament_id)
            champion = None
            if gf and gf["winner_slot"]:
                champion = gf["team_a_name"] if gf["winner_slot"] == "teamA" else gf["team_b_name"]
            await conn.execute(
                "UPDATE arb_tournaments SET status='complete', champion_name=$1, "
                "completed_at=$2, updated_at=$3 WHERE id=$4",
                champion, utcnow(), utcnow(), tournament_id)

    async def standings_round_robin(self, tournament_id: str) -> list[dict]:
        """Таблица round robin: wins/losses/score."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT team_a_name, team_b_name, team_a_score, team_b_score, winner_slot "
                "FROM arb_bracket_nodes WHERE tournament_id=$1 AND bracket='round_robin' "
                "AND status='complete'", tournament_id)
        table: dict[str, dict] = {}
        for r in rows:
            for name, score, opp_score, won in (
                (r["team_a_name"], r["team_a_score"], r["team_b_score"], r["winner_slot"] == "teamA"),
                (r["team_b_name"], r["team_b_score"], r["team_a_score"], r["winner_slot"] == "teamB"),
            ):
                t = table.setdefault(name, {"name": name, "wins": 0, "losses": 0,
                                            "maps_w": 0, "maps_l": 0})
                if won:
                    t["wins"] += 1; t["maps_w"] += score; t["maps_l"] += opp_score
                elif r["winner_slot"]:
                    t["losses"] += 1; t["maps_w"] += score; t["maps_l"] += opp_score
        return sorted(table.values(), key=lambda t: (-t["wins"], -(t["maps_w"] - t["maps_l"])))


bracket_service = BracketService()
