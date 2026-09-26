"""Модель статистики игроков."""

from dataclasses import dataclass, field
from typing import Any


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
    best_match_kills: int = 0
    total_elo_change: int = 0
    last_elo_change: int = 0  # Sum of all ELO changes

    # Stats for last 20 matches only
    last_20_kills: int = 0
    last_20_deaths: int = 0
    last_20_wins: int = 0
    last_20_games: int = 0

    # New fields for level and XP system
    xp: int = 0
    level: int = 1
    xp_to_next_level: int = 100
    total_earnings: int = 0  # Total coins earned
    tournament_participations: int = 0
    description: str = ""  # User profile description

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
            "best_match_kills": self.best_match_kills,
            "total_elo_change": self.total_elo_change,
            "last_elo_change": self.last_elo_change,
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
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PlayerStats":
        """Десериализация из словарЯ."""
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
            best_match_kills=data.get("best_match_kills", 0),
            total_elo_change=data.get("total_elo_change", 0),
            last_elo_change=data.get("last_elo_change", 0),
            last_20_kills=data.get("last_20_kills", 0),
            last_20_deaths=data.get("last_20_deaths", 0),
            last_20_wins=data.get("last_20_wins", 0),
            last_20_games=data.get("last_20_games", 0),
            xp=data.get("xp", 0),
            level=data.get("level", 1),
            xp_to_next_level=data.get("xp_to_next_level", 100),
            total_earnings=data.get("total_earnings", 0),
            tournament_participations=data.get("tournament_participations", 0),
            description=data.get("description", ""),
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
    def avg_elo_change(self) -> float:
        """Среднее изменение ELO за игру."""
        return self.total_elo_change / self.games if self.games > 0 else 0.0

    @property
    def win_rate(self) -> float:
        """Процент побед."""
        return (self.wins / self.games * 100) if self.games > 0 else 0.0

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
            # Calculate XP needed for next level: 100 * level * (level + 1) / 2
            self.xp_to_next_level = int(100 * self.level * (self.level + 1) / 2)

        return self.level, old_level

    def get_level_progress(self) -> tuple[int, int]:
        """Получить прогресс до следующего уровня (текущий XP, максимум)."""
        return self.xp, self.xp_to_next_level

    def get_rank_title(self) -> str:
        """Получить название ранга по уровню."""
        if self.level >= 100:
            return "GrandMaster"
        elif self.level >= 93:
            return "Expert I"
        elif self.level >= 86:
            return "Expert II"
        elif self.level >= 80:
            return "Expert III"
        elif self.level >= 73:
            return "Master I"
        elif self.level >= 66:
            return "Master II"
        elif self.level >= 60:
            return "Master III"
        elif self.level >= 54:
            return "Diamond I"
        elif self.level >= 48:
            return "Diamond II"
        elif self.level >= 42:
            return "Diamond III"
        elif self.level >= 37:
            return "Platinum I"
        elif self.level >= 32:
            return "Platinum II"
        elif self.level >= 27:
            return "Platinum III"
        elif self.level >= 23:
            return "Gold I"
        elif self.level >= 19:
            return "Gold II"
        elif self.level >= 15:
            return "Gold III"
        elif self.level >= 12:
            return "Silver I"
        elif self.level >= 9:
            return "Silver II"
        elif self.level >= 6:
            return "Silver III"
        elif self.level >= 4:
            return "Bronze I"
        elif self.level >= 2:
            return "Bronze II"
        else:
            return "Bronze III"
