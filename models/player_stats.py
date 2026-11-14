"""Модель статистики игроков."""

from dataclasses import dataclass, field
from typing import Any


def calculate_sr(
    kills: int, deaths: int, assists: int, damage: int, games: int
) -> float:
    """Skill Rating 0-10 — классическая формула r!z3: только K/D и AVG Kills.

    assists/damage принимаются для совместимости сигнатуры и не
    участвуют в расчёте (K/D — единственная игровая статистика).
    """
    if games <= 0:
        return 1.0

    games_multiplier = games / 3.0 if games < 3 else 1.0

    safe_deaths = deaths if deaths > 0 else 1
    kd_ratio = kills / safe_deaths
    avg_kills = kills / games

    raw_index = (kd_ratio * 60) + (avg_kills * 8)

    # Нормализация: raw 30 → 1.0, raw 220 → 10.0
    rating = 1.0 + ((raw_index - 30) / (220 - 30)) * 9.0
    rating *= games_multiplier

    return round(max(1.0, min(rating, 10.0)), 1)


@dataclass
class PlayerStats:
    """Статистика игрока по всем турнирам."""

    guild_id: int
    user_id: int
    name: str
    elo: int = 1000
    wins: int = 0
    finals: int = 0
    games: int = 0
    current_streak: int = 0
    best_win_streak: int = 0
    best_loss_streak: int = 0
    bio: str = ""  # Короткое описание профиля
    avatar_url: str = ""  # URL аватара профиля
    streak_bonus_coins: int = 0  # Монеты полученные за серии побед

    # New fields for detailed rating system
    total_kills: int = 0
    total_deaths: int = 0
    total_assists: int = 0
    total_damage: int = 0
    best_match_kills: int = 0
    total_elo_change: int = 0
    last_elo_change: int = 0  # Sum of all ELO changes
    last_pet_elo_bonus: int = 0  # Pet bonus applied to last ELO change

    # Stats for last 20 matches only
    last_20_kills: int = 0
    last_20_deaths: int = 0
    last_20_wins: int = 0
    last_20_games: int = 0

    # New fields for level and XP system
    xp: int = 0
    level: int = 1
    xp_to_next_level: int = 250
    total_earnings: int = 0  # Total coins earned
    tournament_participations: int = 0
    description: str = ""  # User profile description

    # Социальные поля профиля
    reputation: int = 0  # Репутация (Reputation 2.0; игровыми матчами не изменяется)
    birthday: str = ""  # День рождения (ДД.ММ.ГГГГ)
    spouse_id: int = 0  # Discord ID супруга/и (0 = не в браке)

    # Daily tournament coin limit
    daily_tournament_coins: int = 0  # Coins earned from tournaments today
    daily_coins_date: str = ""  # Date string for daily tracking (YYYY-MM-DD)

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в словарь."""
        return {
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "name": self.name,
            "elo": self.elo,
            "wins": self.wins,
            "finals": self.finals,
            "games": self.games,
            "current_streak": self.current_streak,
            "best_win_streak": self.best_win_streak,
            "best_loss_streak": self.best_loss_streak,
            "bio": self.bio,
            "avatar_url": self.avatar_url,
            "streak_bonus_coins": self.streak_bonus_coins,
            "total_kills": self.total_kills,
            "total_deaths": self.total_deaths,
            "total_assists": self.total_assists,
            "total_damage": self.total_damage,
            "best_match_kills": self.best_match_kills,
            "total_elo_change": self.total_elo_change,
            "last_elo_change": self.last_elo_change,
            "last_pet_elo_bonus": self.last_pet_elo_bonus,
            "last_20_kills": self.last_20_kills,
            "last_20_deaths": self.last_20_deaths,
            "last_20_wins": self.last_20_wins,
            "last_20_games": self.last_20_games,
            "xp": self.xp,
            "level": self.level,
            "xp_to_next_level": self.xp_to_next_level,
            "total_earnings": self.total_earnings,
            "tournament_participations": self.tournament_participations,
            "description": self.description,
            "daily_tournament_coins": self.daily_tournament_coins,
            "daily_coins_date": self.daily_coins_date,
            "reputation": self.reputation,
            "birthday": self.birthday,
            "spouse_id": self.spouse_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerStats":
        """Десериализация из словарЯ."""
        level = data.get("level", 1)
        return cls(
            guild_id=data.get("guild_id", 0),
            user_id=data.get("user_id", 0),
            name=data.get("name", "Unknown"),
            elo=data.get("elo", 1000),
            wins=data.get("wins", 0),
            finals=data.get("finals", 0),
            games=data.get("games", 0),
            current_streak=data.get("current_streak", 0),
            best_win_streak=data.get("best_win_streak", 0),
            best_loss_streak=data.get("best_loss_streak", 0),
            bio=data.get("bio", ""),
            avatar_url=data.get("avatar_url", ""),
            streak_bonus_coins=data.get("streak_bonus_coins", 0),
            total_kills=data.get("total_kills", 0),
            total_deaths=data.get("total_deaths", 0),
            total_assists=data.get("total_assists", 0),
            total_damage=data.get("total_damage", 0),
            best_match_kills=data.get("best_match_kills", 0),
            total_elo_change=data.get("total_elo_change", 0),
            last_elo_change=data.get("last_elo_change", 0),
            last_pet_elo_bonus=data.get("last_pet_elo_bonus", 0),
            last_20_kills=data.get("last_20_kills", 0),
            last_20_deaths=data.get("last_20_deaths", 0),
            last_20_wins=data.get("last_20_wins", 0),
            last_20_games=data.get("last_20_games", 0),
            xp=data.get("xp", 0),
            level=level,
            xp_to_next_level=cls._calculate_xp_to_next_level(level),
            total_earnings=data.get("total_earnings", 0),
            tournament_participations=data.get("tournament_participations", 0),
            description=data.get("description", ""),
            daily_tournament_coins=data.get("daily_tournament_coins", 0),
            daily_coins_date=data.get("daily_coins_date", ""),
            reputation=data.get("reputation", 0),
            birthday=data.get("birthday", ""),
            spouse_id=data.get("spouse_id", 0),
        )

    @property
    def avg_kills(self) -> float:
        """Среднее количество убийств за игру."""
        return self.total_kills / self.games if self.games > 0 else 0.0

    @property
    def avg_deaths(self) -> float:
        """Среднее количество смертей за игру."""
        return self.total_deaths / self.games if self.games > 0 else 0.0

    @property
    def kd_ratio(self) -> float:
        """Соотношение убийств к смертям."""
        return self.total_kills / self.total_deaths if self.total_deaths > 0 else 0.0

    @property
    def kda_ratio(self) -> float:
        """K+A/D — убийства + ассисты к смертям."""
        deaths = self.total_deaths if self.total_deaths > 0 else 1
        return (self.total_kills + self.total_assists) / deaths

    @property
    def avg_damage(self) -> float:
        """Средний урон за игру."""
        return self.total_damage / self.games if self.games > 0 else 0.0

    @property
    def avg_elo_change(self) -> float:
        """Среднее изменение ELO за игру."""
        return self.total_elo_change / self.games if self.games > 0 else 0.0

    @property
    def win_rate(self) -> float:
        """Процент побед."""
        return (self.wins / self.games * 100) if self.games > 0 else 0.0

    @property
    def skill_rating(self) -> float:
        """Skill Rating 0-10: только K/D и avg kills (классика r!z3)."""
        return calculate_sr(
            self.total_kills,
            self.total_deaths,
            self.total_assists,
            self.total_damage,
            self.games,
        )

    def add_xp(self, amount: int) -> tuple[int, int]:
        """Добавить XP и автоматически повысить уровень. Возвращает (новый уровень, уровень до)."""
        self.xp += amount

        levels_gained = 0
        old_level = self.level

        # Check for level ups
        while self.xp >= self.xp_to_next_level:
            self.xp -= self.xp_to_next_level
            self.level += 1
            levels_gained += 1
            # Calculate XP needed for next level based on current level
            self.xp_to_next_level = self._calculate_xp_to_next_level(self.level)

        return self.level, old_level

    @staticmethod
    def _calculate_xp_to_next_level(level: int) -> int:
        """Calculate XP needed for next level based on current level."""
        if level <= 10:
            return 250
        elif level <= 20:
            return 270
        elif level <= 30:
            return 300
        elif level <= 40:
            return 330
        elif level <= 50:
            return 360
        elif level <= 60:
            return 390
        elif level <= 70:
            return 420
        elif level <= 80:
            return 450
        elif level <= 90:
            return 470
        else:
            return 500

    def get_level_progress(self) -> tuple[int, int]:
        """Получить прогресс до следующего уровня (текущий XP, максимум)."""
        return self.xp, self.xp_to_next_level

    def get_rank_title(self) -> str:
        """Получить название ранга по уровню (единая таблица utils.ranks)."""
        from utils.ranks import rank_name

        return rank_name(self.level)
