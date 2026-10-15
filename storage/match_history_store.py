"""Хранилище истории матчей игроков."""

import json
from pathlib import Path
from typing import List, Optional
from datetime import datetime

from models.match_history import MatchHistory, MatchHistoryList

DATA_DIR = Path("data")
HISTORY_FILE = DATA_DIR / "match_history.json"


class MatchHistoryStore:
    """Хранилище истории матчей в PostgreSQL."""

    def __init__(self) -> None:
        self._history: dict[str, MatchHistoryList] = {}
        self._use_db = True  # Использовать PostgreSQL
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self) -> None:
        """Загрузить историю из файла (fallback)."""
        if not HISTORY_FILE.exists():
            return

        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key, history_data in data.items():
                    history_list = MatchHistoryList(
                        guild_id=history_data["guild_id"],
                        user_id=history_data["user_id"],
                        matches=[MatchHistory.from_dict(m) for m in history_data.get("matches", [])]
                    )
                    self._history[key] = history_list
        except (json.JSONDecodeError, KeyError):
            self._history = {}

    def save(self) -> None:
        """Сохранить историю в файл (fallback)."""
        data = {
            key: {
                "guild_id": h.guild_id,
                "user_id": h.user_id,
                "matches": [m.to_dict() for m in h.matches]
            }
            for key, h in self._history.items()
        }
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def get(self, guild_id: int, user_id: int) -> List[MatchHistory]:
        """Получить историю матчей игрока, отсортированную по дате (от новых к старым)."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT guild_id, user_id, match_id, tournament_id, date,
                           match_type, match_index, team_index, team_name,
                           teammates, opponent_team_index, opponent_team_name,
                           opponents, kills, deaths, result, elo_change,
                           bet_amount, bet_win, bet_team
                    FROM match_history
                    WHERE guild_id = $1 AND user_id = $2
                    ORDER BY date DESC
                    """,
                    guild_id, user_id
                )
                return [
                    MatchHistory(
                        guild_id=row["guild_id"],
                        user_id=row["user_id"],
                        match_id=row["match_id"],
                        tournament_id=row["tournament_id"],
                        date=row["date"],
                        match_type=row["match_type"],
                        match_index=row["match_index"],
                        team_index=row["team_index"],
                        team_name=row["team_name"],
                        teammates=row["teammates"],
                        opponent_team_index=row["opponent_team_index"],
                        opponent_team_name=row["opponent_team_name"],
                        opponents=row["opponents"],
                        kills=row["kills"],
                        deaths=row["deaths"],
                        result=row["result"],
                        elo_change=row["elo_change"],
                        bet_amount=row.get("bet_amount", 0),
                        bet_win=row.get("bet_win", 0),
                        bet_team=row.get("bet_team", "")
                    )
                    for row in rows
                ]
        else:
            key = f"{guild_id}:{user_id}"
            history_list = self._history.get(key)
            if history_list:
                # Sort by date descending
                return sorted(history_list.matches, key=lambda m: m.date, reverse=True)
            return []

    async def add(self, match: MatchHistory) -> None:
        """Добавить запись о матче в историю."""
        if self._use_db:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO match_history
                    (guild_id, user_id, match_id, tournament_id, date,
                     match_type, match_index, team_index, team_name,
                     teammates, opponent_team_index, opponent_team_name,
                     opponents, kills, deaths, result, elo_change,
                     bet_amount, bet_win, bet_team)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
                    """,
                    match.guild_id, match.user_id, match.match_id, match.tournament_id,
                    match.date, match.match_type, match.match_index, match.team_index,
                    match.team_name, match.teammates, match.opponent_team_index,
                    match.opponent_team_name, match.opponents, match.kills, match.deaths,
                    match.result, match.elo_change, match.bet_amount, match.bet_win,
                    match.bet_team
                )
        else:
            key = f"{match.guild_id}:{match.user_id}"
            if key not in self._history:
                self._history[key] = MatchHistoryList(
                    guild_id=match.guild_id,
                    user_id=match.user_id,
                    matches=[]
                )
            self._history[key].matches.append(match)
            self.save()

    async def get_recent_stats(self, guild_id: int, user_id: int, limit: int = 10) -> dict:
        """Получить статистику за последние N матчей."""
        matches = await self.get(guild_id, user_id)
        recent_matches = matches[:limit]

        if not recent_matches:
            return {
                "total_matches": 0,
                "wins": 0,
                "losses": 0,
                "win_rate": 0.0,
                "total_kills": 0,
                "total_deaths": 0,
                "kd_ratio": 0.0,
                "avg_kills": 0.0
            }

        wins = sum(1 for m in recent_matches if m.result == "win")
        losses = sum(1 for m in recent_matches if m.result == "loss")
        total_kills = sum(m.kills for m in recent_matches)
        total_deaths = sum(m.deaths for m in recent_matches)

        kd_ratio = total_kills / total_deaths if total_deaths > 0 else 0.0
        avg_kills = total_kills / len(recent_matches) if recent_matches else 0.0
        win_rate = (wins / len(recent_matches) * 100) if recent_matches else 0.0

        return {
            "total_matches": len(recent_matches),
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_kills": total_kills,
            "total_deaths": total_deaths,
            "kd_ratio": kd_ratio,
            "avg_kills": avg_kills
        }


# Singleton instance
match_history_store = MatchHistoryStore()
