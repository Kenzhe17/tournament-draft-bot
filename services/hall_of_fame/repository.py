"""SQL-запросы для Зала славы. Только чтение."""

from __future__ import annotations

from storage.db import get_pool

# Технические/тестовые ID: боты турниров используют отрицательные
# значения, тестовые игроки — маленькие положительные (< 1_000_000).
MIN_REAL_USER_ID = 1_000_000


async def table_exists(conn, name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name=$1)", name)


async def match_aggregates(guild_id: int) -> dict:
    """Агрегаты по match_history для одного guild."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await table_exists(conn, "match_history"):
            return {}
        top_kills = await conn.fetchrow(
            """
            SELECT user_id, kills, date, match_type, tournament_id
            FROM match_history
            WHERE guild_id=$1 AND user_id >= $2 AND kills > 0
            ORDER BY kills DESC, date DESC LIMIT 1
            """, guild_id, MIN_REAL_USER_ID)
        top_elo_gain = await conn.fetchrow(
            """
            SELECT user_id, elo_change, date, match_type, tournament_id
            FROM match_history
            WHERE guild_id=$1 AND user_id >= $2 AND elo_change > 0
            ORDER BY elo_change DESC, date DESC LIMIT 1
            """, guild_id, MIN_REAL_USER_ID)
        # Чемпионаты: победы в финальных матчах по уникальным турнирам
        champ_rows = await conn.fetch(
            """
            SELECT user_id, COUNT(DISTINCT tournament_id || '|' || match_index) AS cnt
            FROM match_history
            WHERE guild_id=$1 AND user_id >= $2
              AND match_type='final' AND result='win'
            GROUP BY user_id
            """, guild_id, MIN_REAL_USER_ID)
        match_counts = await conn.fetch(
            """
            SELECT user_id, COUNT(*) AS cnt
            FROM match_history
            WHERE guild_id=$1 AND user_id >= $2
            GROUP BY user_id
            """, guild_id, MIN_REAL_USER_ID)
    return {
        "top_kills": dict(top_kills) if top_kills else None,
        "top_elo_gain": dict(top_elo_gain) if top_elo_gain else None,
        "championships": {int(r["user_id"]): int(r["cnt"]) for r in champ_rows},
        "match_counts": {int(r["user_id"]): int(r["cnt"]) for r in match_counts},
    }


async def economy_leaders(guild_id: int) -> dict:
    """Баланс + банк по user_id (без двойного счёта — разные таблицы)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        wealth: dict[int, int] = {}
        if await table_exists(conn, "user_balance"):
            for r in await conn.fetch(
                "SELECT user_id, balance FROM user_balance WHERE guild_id=$1",
                guild_id):
                if int(r["user_id"]) >= MIN_REAL_USER_ID:
                    wealth[int(r["user_id"])] = wealth.get(int(r["user_id"]), 0) + int(r["balance"])
        if await table_exists(conn, "user_bank"):
            for r in await conn.fetch(
                "SELECT user_id, bank_balance FROM user_bank WHERE guild_id=$1",
                guild_id):
                if int(r["user_id"]) >= MIN_REAL_USER_ID:
                    wealth[int(r["user_id"])] = wealth.get(int(r["user_id"]), 0) + int(r["bank_balance"])
    return {"wealth": wealth}


async def voice_leader(guild_id: int) -> tuple[int, int] | None:
    """Максимальное накопленное голосовое время (секунды)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await table_exists(conn, "banner_voice_daily"):
            return None
        row = await conn.fetchrow(
            """
            SELECT user_id, SUM(seconds) AS total
            FROM banner_voice_daily
            WHERE guild_id=$1 AND user_id >= $2
            GROUP BY user_id ORDER BY total DESC LIMIT 1
            """, guild_id, MIN_REAL_USER_ID)
    return (int(row["user_id"]), int(row["total"])) if row else None


async def invite_leader(guild_id: int) -> tuple[int, int] | None:
    """Максимум подтверждённых приглашений (reward_status='rewarded')."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await table_exists(conn, "rep_invites"):
            return None
        row = await conn.fetchrow(
            """
            SELECT inviter_id, COUNT(*) AS cnt
            FROM rep_invites
            WHERE guild_id=$1 AND reward_status='rewarded'
              AND inviter_id IS NOT NULL AND inviter_id >= $2
            GROUP BY inviter_id ORDER BY cnt DESC LIMIT 1
            """, guild_id, MIN_REAL_USER_ID)
    return (int(row["inviter_id"]), int(row["cnt"])) if row else None


async def ultimatum_records() -> dict:
    """Рекорды Ультиматума из ult_elo_ratings (Соло, новая система).

    Фолбэк на легаси elo_ratings, если новых рейтингов ещё нет.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        new_table = await table_exists(conn, "ult_elo_ratings")
        if new_table:
            top = await conn.fetchrow(
                """
                SELECT discord_id, elo AS peak_elo, elo, wins, losses,
                       rated_matches
                FROM ult_elo_ratings WHERE format='solo' AND discord_id >= $1
                ORDER BY peak_elo DESC LIMIT 1
                """, MIN_REAL_USER_ID)
            top_winrate = await conn.fetchrow(
                """
                SELECT discord_id, wins, rated_matches
                FROM ult_elo_ratings
                WHERE format='solo' AND discord_id >= $1 AND rated_matches >= 10
                ORDER BY (wins::float / NULLIF(rated_matches,0)) DESC LIMIT 1
                """, MIN_REAL_USER_ID)
            top_duels = await conn.fetchrow(
                """
                SELECT discord_id, rated_matches
                FROM ult_elo_ratings WHERE format='solo' AND discord_id >= $1
                ORDER BY rated_matches DESC LIMIT 1
                """, MIN_REAL_USER_ID)
            top_wins = await conn.fetchrow(
                """
                SELECT discord_id, wins
                FROM ult_elo_ratings WHERE format='solo' AND discord_id >= $1
                ORDER BY wins DESC LIMIT 1
                """, MIN_REAL_USER_ID)
        elif await table_exists(conn, "elo_ratings"):
            top = await conn.fetchrow(
                """
                SELECT discord_id, username, peak_elo, elo, wins, losses, rated_matches
                FROM elo_ratings WHERE discord_id >= $1
                ORDER BY peak_elo DESC LIMIT 1
                """, MIN_REAL_USER_ID)
            top_winrate = await conn.fetchrow(
                """
                SELECT discord_id, username, wins, rated_matches
                FROM elo_ratings
                WHERE discord_id >= $1 AND rated_matches >= 10
                ORDER BY (wins::float / NULLIF(rated_matches,0)) DESC LIMIT 1
                """, MIN_REAL_USER_ID)
            top_duels = await conn.fetchrow(
                """
                SELECT discord_id, username, rated_matches
                FROM elo_ratings WHERE discord_id >= $1
                ORDER BY rated_matches DESC LIMIT 1
                """, MIN_REAL_USER_ID)
            top_wins = await conn.fetchrow(
                """
                SELECT discord_id, username, wins
                FROM elo_ratings WHERE discord_id >= $1
                ORDER BY wins DESC LIMIT 1
                """, MIN_REAL_USER_ID)
        else:
            top = top_winrate = top_duels = top_wins = None
    def _d(r):
        return dict(r) if r else None
    return {
        "top_elo": _d(top), "top_winrate": _d(top_winrate),
        "top_duels": _d(top_duels), "top_wins": _d(top_wins),
    }


async def br_wins_leader(guild_id: int) -> tuple[int, int] | None:
    """Лидер по победам в BR (из br_tournaments.json через br_store)."""
    try:
        from storage.br_store import br_store
        counts: dict[int, int] = {}
        for t in br_store.get_by_guild(guild_id):
            phase = getattr(t, "phase", None)
            phase_val = getattr(phase, "value", phase)
            if phase_val not in ("results", "complete"):
                continue
            # Завершённый турнир: есть результаты команд
            results = t.calculate_final_results() if hasattr(t, "calculate_final_results") else []
            if not results:
                continue
            winner = results[0]
            # team -> players ids через players list (name->id)
            id_by_name = {p["name"]: p["id"] for p in getattr(t, "players", [])}
            team_idx = winner.get("team_index")
            team = t.teams[team_idx] if team_idx is not None and team_idx < len(t.teams) else None
            if team is None:
                continue
            member_names = [team.get("captain", "")] + list(team.get("players", []))
            for nm in member_names:
                uid = id_by_name.get(nm)
                if uid and uid >= MIN_REAL_USER_ID:
                    counts[uid] = counts.get(uid, 0) + 1
        if not counts:
            return None
        uid, cnt = max(counts.items(), key=lambda kv: kv[1])
        return uid, cnt
    except Exception:
        return None
