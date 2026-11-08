"""Rating calculator — новая система ELO турниров (FACEIT-style).

Формула для каждого игрока:

    final_elo = base_elo + performance_modifier + rank_bonus

    base_elo: зависит от результата матча и ожидаемой вероятности
              победы команды (по среднему Skill Rating)
    performance_modifier: место игрока по Performance Score среди всех
              8 участников матча
    rank_bonus: +N для игроков вне TOP-N ELO-лидерборда

Затем результат ограничивается: победитель +[min..max],
проигравший -[min..max], и округляется один раз до int.

ELO и Skill Rating — независимые рейтинги. Win Rate не используется.
"""

import logging
from typing import Any

from utils.elo_config import ELO_CONFIG

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Сила команд
# ---------------------------------------------------------------------------

def calculate_team_skill_rating(skill_ratings: list[float]) -> float:
    """Средний Skill Rating команды (шкала 0-10)."""
    if not skill_ratings:
        return 0.0
    return sum(skill_ratings) / len(skill_ratings)


def calculate_expected_win_probability(
    team_a_sr: float, team_b_sr: float
) -> tuple[float, float]:
    """Вероятности победы команд A и B по разнице Skill Rating.

    expected_a = 1 / (1 + 10 ** ((sr_b - sr_a) / SCALE))
    """
    scale = ELO_CONFIG["skill_rating_scale"]
    expected_a = 1.0 / (1.0 + 10.0 ** ((team_b_sr - team_a_sr) / scale))
    return expected_a, 1.0 - expected_a


# ---------------------------------------------------------------------------
# Базовое начисление
# ---------------------------------------------------------------------------

def calculate_base_elo(expected_win: float, won: bool) -> float:
    """Базовое изменение ELO до индивидуальных поправок.

    Победа:  20 + 10 * (1 - expected_win)
    Поражение: -(20 + 10 * expected_win)
    где expected_win — ожидаемая вероятность победившей команды.
    """
    if won:
        return 20.0 + 10.0 * (1.0 - expected_win)
    return -(20.0 + 10.0 * expected_win)


# ---------------------------------------------------------------------------
# Индивидуальный вклад
# ---------------------------------------------------------------------------

def calculate_player_performance(
    kills: int, deaths: int, assists: int, damage: int
) -> float:
    """Performance Score игрока за матч."""
    cfg = ELO_CONFIG
    return (
        kills * cfg["kill_weight"]
        + assists * cfg["assist_weight"]
        - deaths * cfg["death_weight"]
        + damage / cfg["damage_divisor"]
    )


def calculate_performance_modifiers(scores: list[float]) -> list[int]:
    """Поправки ELO по индивидуальному вкладу относительно всех игроков матча.

    Сравнение каждого Performance Score со всеми остальными:
      - максимум матча            → "best"           (+5)
      - минимум матча             → "worst"          (-5)
      - выше среднего по матчу    → "above_average"  (+3)
      - ниже среднего по матчу    → "below_average"  (-3)
      - ровно среднее / все равны  → "average"        (0)

    Одинаковый score → одинаковая поправка.
    """
    cfg = ELO_CONFIG
    mods = cfg["performance_modifiers"]
    if not scores:
        return []

    hi = max(scores)
    lo = min(scores)
    if hi == lo:
        # Все сыграли одинаково — средний вклад
        return [mods["average"]] * len(scores)

    mean = sum(scores) / len(scores)

    result: list[int] = []
    for score in scores:
        if score == hi:
            result.append(mods["best"])
        elif score == lo:
            result.append(mods["worst"])
        elif score > mean:
            result.append(mods["above_average"])
        elif score < mean:
            result.append(mods["below_average"])
        else:
            result.append(mods["average"])
    return result


# ---------------------------------------------------------------------------
# TOP-N защита
# ---------------------------------------------------------------------------

def calculate_rank_bonus(leaderboard_rank: int) -> int:
    """Бонус для игроков вне TOP-N ELO-лидерборда (ранг до матча)."""
    cfg = ELO_CONFIG["rank_protection"]
    if not cfg["enabled"]:
        return 0
    if leaderboard_rank > cfg["top_limit"]:
        return cfg["bonus"]
    return 0


# ---------------------------------------------------------------------------
# Итоговое изменение
# ---------------------------------------------------------------------------

def clamp_elo_change(elo_change: float, won: bool) -> float:
    """Ограничить изменение ELO диапазоном +/-[min_match_elo, max_match_elo]."""
    cfg = ELO_CONFIG
    lo = cfg["min_match_elo"]
    hi = cfg["max_match_elo"]
    if won:
        return max(lo, min(hi, elo_change))
    return min(-lo, max(-hi, elo_change))


def calculate_final_elo(
    base_elo: float,
    performance_modifier: int,
    rank_bonus: int,
    won: bool,
) -> int:
    """Итоговое изменение ELO: сумма → clamp → округление (один раз)."""
    total = base_elo + performance_modifier + rank_bonus
    return int(round(clamp_elo_change(total, won)))


# ---------------------------------------------------------------------------
# Оркестратор: расчёт изменений для всего матча
# ---------------------------------------------------------------------------

def compute_match_elo_changes(
    match_players: list[dict[str, Any]],
    team_sr_values: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    """Рассчитать ELO-изменения для всех игроков матча.

    match_players — список словарей:
        name: str
        team: str            # "a" или "b"
        won: bool            # True если команда игрока победила
        kills/deaths/assists/damage: int
        skill_rating: float  # SR игрока (0-10)
        elo_rank: int        # место в ELO-лидерборде ДО матча (1-based, 999=unknown)

    Возвращает {name: breakdown} где breakdown содержит все составляющие
    для логирования и отображения.
    """
    cfg = ELO_CONFIG

    # 1. Средний SR команд (можно передать roster-wide извне)
    team_sr: dict[str, float] = {}
    for team in {p["team"] for p in match_players}:
        if team_sr_values and team in team_sr_values:
            team_sr[team] = team_sr_values[team]
        else:
            srs = [p["skill_rating"] for p in match_players if p["team"] == team]
            team_sr[team] = calculate_team_skill_rating(srs)

    # 2. Вероятности победы команд
    teams = sorted(team_sr.keys())
    expected: dict[str, float] = {}
    if len(teams) == 2:
        exp_a, exp_b = calculate_expected_win_probability(
            team_sr[teams[0]], team_sr[teams[1]]
        )
        expected[teams[0]] = exp_a
        expected[teams[1]] = exp_b
    else:
        for t in teams:
            expected[t] = 0.5

    # 3. Performance Score всех игроков и индивидуальные поправки
    scores = [
        calculate_player_performance(
            p.get("kills", 0),
            p.get("deaths", 0),
            p.get("assists", 0),
            p.get("damage", 0),
        )
        for p in match_players
    ]
    perf_mods = calculate_performance_modifiers(scores)

    results: dict[str, dict[str, Any]] = {}
    for p, score, perf_mod in zip(match_players, scores, perf_mods):
        won = p["won"]
        # Ожидаемая вероятность победившей команды:
        # своя, если выиграл; команды соперника, если проиграл
        expected_win = expected[p["team"]] if won else (1.0 - expected[p["team"]])

        base = calculate_base_elo(expected_win, won)
        rank_bonus = calculate_rank_bonus(p.get("elo_rank", 999))
        elo_change = calculate_final_elo(base, perf_mod, rank_bonus, won)

        results[p["name"]] = {
            "elo_change": elo_change,
            "won": won,
            "expected_win": round(expected_win, 4),
            "base_elo": round(base, 2),
            "performance_score": round(score, 2),
            "performance_modifier": perf_mod,
            "rank_bonus": rank_bonus,
            "elo_rank": p.get("elo_rank", 999),
            "team_sr": round(team_sr[p["team"]], 2),
        }

        logger.info(
            "ELO calc %s: won=%s exp_win=%.3f base=%.1f perf=%+.0f(score=%.2f) "
            "rank_bonus=%+d(rank=%s) -> %+d",
            p["name"], won, expected_win, base, perf_mod, score,
            rank_bonus, p.get("elo_rank", 999), elo_change,
        )

    return results


# ---------------------------------------------------------------------------
# Обновление статистики игрока после матча (без изменений логики)
# ---------------------------------------------------------------------------

def update_player_stats_from_match(
    stats,
    kills: int,
    deaths: int,
    elo_change: int,
    team_won: bool,
    assists: int = 0,
    damage: int = 0,
):
    """
    Update player stats after a match.

    Args:
        stats: Current player stats (PlayerStats)
        kills: Kills in this match
        deaths: Deaths in this match
        elo_change: ELO change from this match
        team_won: Whether the player's team won
        assists: Assists in this match
        damage: Damage in this match

    Returns:
        Updated player stats
    """
    # Update total kills, deaths, assists and damage
    stats.total_kills += kills
    stats.total_deaths += deaths
    stats.total_assists += assists
    stats.total_damage += damage

    # Update best match kills
    if kills > stats.best_match_kills:
        stats.best_match_kills = kills

    # Update best win streak
    if stats.current_streak > stats.best_win_streak:
        stats.best_win_streak = stats.current_streak

    # Check for win streak bonus (3 wins = +50 coins)
    if stats.current_streak >= 3 and stats.current_streak % 3 == 0:
        stats.streak_bonus_coins = getattr(stats, 'streak_bonus_coins', 0) + 50

    # Update total ELO change
    stats.total_elo_change += elo_change

    # Update last ELO change
    stats.last_elo_change = elo_change

    # Update current ELO
    stats.elo += elo_change

    # Update wins/finals
    if team_won:
        stats.wins += 1

    # Репутация: +1 за участие в матче, +2 бонус за победу
    stats.reputation = getattr(stats, "reputation", 0) + 1 + (2 if team_won else 0)

    # Update streak
    if team_won:
        if stats.current_streak > 0:
            stats.current_streak += 1
        else:
            stats.current_streak = 1
        if stats.current_streak > stats.best_win_streak:
            stats.best_win_streak = stats.current_streak
    else:
        if stats.current_streak < 0:
            stats.current_streak -= 1
        else:
            stats.current_streak = -1
        if abs(stats.current_streak) > stats.best_loss_streak:
            stats.best_loss_streak = abs(stats.current_streak)

    return stats


async def get_leaderboard_rank(guild_id: int, user_id: int) -> int:
    """
    Calculate player's rank in leaderboard by ELO.

    Args:
        guild_id: Discord guild ID
        user_id: Player's user ID

    Returns:
        Rank (1-based, 999 if unknown or not found)
    """
    from storage.player_stats_store import player_stats_store

    all_stats = await player_stats_store.get_all(guild_id)

    if not all_stats:
        return 999

    # Sort by ELO descending
    sorted_stats = sorted(all_stats, key=lambda x: x.elo, reverse=True)

    # Find player's rank
    for rank, stats in enumerate(sorted_stats, start=1):
        if stats.user_id == user_id:
            return rank

    return 999  # Not found
