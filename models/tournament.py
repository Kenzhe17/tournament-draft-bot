"""Модель данных турнира."""

from __future__ import annotations

import random
import discord
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Global bot instance for logging
_bot_instance = None

def set_bot_instance(bot):
    """Set the global bot instance for logging."""
    global _bot_instance
    _bot_instance = bot

def get_bot_instance():
    """Get the global bot instance for logging."""
    return _bot_instance


class TournamentSize(str, Enum):
    """Размер турнира."""

    EIGHT = "8"
    SIXTEEN = "16"
    THIRTY_TWO = "32"


class TournamentPhase(str, Enum):
    """Фазы жизненного цикла турнира."""

    SETUP = "setup"
    RULES_VOTING = "rules_voting"
    DRAFT = "draft"
    TEAMS = "teams"
    QUALIFIERS = "qualifiers"
    SEMIFINALS = "semifinals"
    FINAL = "final"
    COMPLETE = "complete"


class RegistrationState(str, Enum):
    """Состояние регистрации игроков."""

    CLOSED = "closed"
    OPEN = "open"
    ISOLATED = "isolated"


class FormationMode(str, Enum):
    """Режим формирования кругов."""

    SKILL = "skill"
    ELO = "elo"
    RANDOM = "random"
    RANDOM_AUTO = "random-auto"  # Автоматическое распределение по SR
    BALANCED_SR = "balanced-sr"  # Tournaments 2.0: 4 группы силы, мин. разница SR
    DRAFT = "draft"  # Tournaments 2.0: драфт капитанами (как elo/skill)


# Порядок выбора по кругам для разного количества капитанов
# Rotation Draft: круг 2 - прямой, круг 3 - обратный, круг 4 - с середины к концу, потом к началу
PICK_ORDERS: dict[int, dict[str, list[int] | int]] = {
    2: {
        "2": {"order": [0, 1], "auto": 1},  # Круг 2: 1 → 2
        "3": {"order": [1, 0], "auto": 0},  # Круг 3: 2 → 1
        "4": {"order": [1, 0], "auto": 0},  # Круг 4: 2 → 1
    },
    4: {
        "2": {"order": [0, 1, 2, 3], "auto": 3},  # Круг 2: 1 → 2 → 3 → 4
        "3": {"order": [1, 2, 3, 0], "auto": 0},  # Круг 3: 2 → 3 → 4 → 1
        "4": {"order": [3, 2, 0, 1], "auto": 0},  # Круг 4: 4 → 3 → 1 → 2
    },
    8: {
        "2": {"order": [0, 1, 2, 3, 4, 5, 6, 7], "auto": 7},  # Круг 2: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8
        "3": {"order": [4, 7, 3, 6, 2, 5, 1, 0], "auto": 0},  # Круг 3: 5 → 8 → 4 → 7 → 3 → 6 → 2 → 1
        "4": {"order": [5, 6, 7, 1, 0, 2, 3, 4], "auto": 0},  # Круг 4: 6 → 7 → 8 → 2 → 1 → 3 → 4 → 5
    },
    16: {
        "2": {"order": [0, 1, 2, 3], "auto": 3},  # Круг 2: 1 → 2 → 3 → 4
        "3": {"order": [1, 2, 3, 0], "auto": 0},  # Круг 3: 2 → 3 → 4 → 1
        "4": {"order": [3, 2, 0, 1], "auto": 0},  # Круг 4: 4 → 3 → 1 → 2
    },
    32: {
        "2": {"order": [0, 1, 2, 3, 4, 5, 6, 7], "auto": 7},  # Круг 2: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8
        "3": {"order": [4, 7, 3, 6, 2, 5, 1, 0], "auto": 0},  # Круг 3: 5 → 8 → 4 → 7 → 3 → 6 → 2 → 1
        "4": {"order": [5, 6, 7, 1, 0, 2, 3, 4], "auto": 0},  # Круг 4: 6 → 7 → 8 → 2 → 1 → 3 → 4 → 5
    },
}

# Порядок распределения для random-auto (по Skill Rating)
# Игроки всегда сортируются от высокого SR к низкому
# Меняется только порядок команд
RANDOM_AUTO_PICK_ORDERS: dict[int, dict[str, list[int]]] = {
    2: {  # 2 команды по 4 игрока
        "2": [0, 1],      # Сильные: 1 → 2
        "3": [1, 0],      # Средние: 2 → 1
        "4": [1, 0],      # Слабые: 2 → 1
    },
    4: {  # 4 команды по 4 игрока
        "2": [0, 1, 2, 3],           # Сильные: 1 → 2 → 3 → 4
        "3": [1, 2, 3, 0],           # Средние: 2 → 3 → 4 → 1
        "4": [3, 2, 0, 1],           # Слабые: 4 → 3 → 1 → 2
    },
    8: {  # 8 команд по 4 игрока
        "2": [0, 1, 2, 3, 4, 5, 6, 7],           # Сильные: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8
        "3": [4, 7, 3, 6, 2, 5, 1, 0],           # Средние: 5 → 8 → 4 → 7 → 3 → 6 → 2 → 1
        "4": [5, 6, 7, 1, 0, 2, 3, 4],           # Слабые: 6 → 7 → 8 → 2 → 1 → 3 → 4 → 5
    },
}

# Возможные пары полуфиналов (индексы команд 0-based)
SEMIFINAL_PAIRINGS: list[list[tuple[int, int]]] = [
    [(0, 1), (2, 3)],
    [(0, 2), (1, 3)],
    [(0, 3), (1, 2)],
]


@dataclass
class MatchContext:
    """Единый контекст одного матча (Tournaments 2.0).

    Определяет актуальные команды и ростеры из свежего состояния
    турнира. session_key — ключ OCR-сессии внутри турнира
    ("qualifier_0", "semifinal_1", "final_0"); для глобальных
    блокировок/логов/PG/ELO используется match_id с tournament_id.
    """

    tournament_id: str
    guild_id: int
    tournament_channel_id: int
    match_type: str  # "qualifier" | "semifinal" | "final"
    match_index: int
    team_a_index: int
    team_b_index: int
    team_a_player_ids: tuple[int, ...]
    team_b_player_ids: tuple[int, ...]
    session_key: str

    @property
    def match_id(self) -> str:
        return f"{self.tournament_id}_{self.session_key}"


@dataclass
class Tournament:
    """Состояние одного турнира на сервере."""

    guild_id: int
    channel_id: int
    message_id: int = 0
    id: str = field(init=False)  # Unique tournament ID
    creation_time: str = field(init=False, default_factory=lambda: str(int(__import__('time').time() * 1000)))
    betting_results: dict[str, int] = field(default_factory=dict)  # Store betting payouts {user_name: payout}

    def __post_init__(self):
        # Use guild_id + creation_time for unique tournament ID
        self.id = f"{self.guild_id}_{self.creation_time}"

    # Настройка
    size: TournamentSize = TournamentSize.EIGHT
    registration: RegistrationState = RegistrationState.CLOSED
    formation_mode: FormationMode = FormationMode.DRAFT

    # Выбранные правила матча (BRAZIL/KAZAKHSTAN) — голосование
    # после «Старт» в /tournament create, до формирования команд.
    ruleset: str = "kazakhstan"  # r!z3 CLASSIC: только Kazakhstan
    rules_vote: dict = field(default_factory=dict)
    captains: list[str] = field(default_factory=list)  # Display names
    circle1: list[str] = field(default_factory=list)  # Captain circle (display names)
    circle2: list[str] = field(default_factory=list)
    circle3: list[str] = field(default_factory=list)
    circle4: list[str] = field(default_factory=list)

    # Players pool for RANDOM mode (instead of circles)
    players_pool: list[str] = field(default_factory=list)  # List of all player names

    phase: TournamentPhase = TournamentPhase.SETUP
    is_test: bool = False
    start_time: str = ""  # ISO format timestamp when tournament started
    created_at: str = field(init=False, default_factory=lambda: __import__('datetime').datetime.now().isoformat())  # ISO format timestamp when tournament was created

    # Лимиты для кругов (True = лимит включен, False = без лимита)
    circle_limits_enabled: dict[int, bool] = field(default_factory=lambda: {2: True, 3: True, 4: True})

    # Названия команд (индекс команды -> название)
    team_names: dict[int, str] = field(default_factory=dict)
    team_names_changed_teams: set[int] = field(default_factory=set)  # Индексы команд которые уже изменили название

    # Отображение имени на user_id для статистики
    player_user_ids: dict[str, int] = field(default_factory=dict)

    # Сообщение драфта для пинга капитанов
    draft_message_id: int = 0  # ID сообщения с пингом текущего капитана
    draft_pick_start_time: str = ""  # ISO format timestamp when current pick started
    draft_pick_duration: int = 60  # Seconds for each pick (default 60s)

    # Организатор турнира (user_id)
    organizer_id: int = 0

    # MM поиск который создал этот турнир (channel_id)
    mm_search_channel_id: int = 0

    # Канал матча cs-4x4-match-N, созданный на этапе ready-check
    mm_preset_channel_id: int = 0

    @property
    def captain_count(self) -> int:
        """Количество капитанов на основе размера турнира."""
        if self.size == TournamentSize.EIGHT:
            return 2
        elif self.size == TournamentSize.SIXTEEN:
            return 4
        else:  # 32 players
            return 8

    def circle_limit(self, circle: int) -> int:
        """Лимит игроков для круга на основе размера турнира и настроек."""
        if circle == 1:
            return self.captain_count  # circle1 = captains
        elif circle == 4:
            # circle4 unlimited by default, but can be limited
            if self.circle_limits_enabled.get(4, True):
                return self.captain_count
            return float('inf')
        else:
            # circle2, circle3
            if self.circle_limits_enabled.get(circle, True):
                return self.captain_count
            return float('inf')

    # Драфт
    captain_order: list[int] = field(default_factory=list)
    picks: dict[str, dict[str, str]] = field(default_factory=dict)
    current_circle: int = 2
    pick_index: int = 0
    available: dict[str, list[str]] = field(default_factory=dict)

    # Команды и плей-офф
    teams: list[dict[str, Any]] = field(default_factory=list)
    qualifier_matches: list[tuple[int, int]] = field(default_factory=list)
    qualifier_winners: list[int | None] = field(default_factory=list)
    semifinal_matches: list[tuple[int, int]] = field(default_factory=list)
    semifinal_winners: list[int | None] = field(default_factory=list)
    semifinal_pending_winners: list[int | None] = field(default_factory=list)  # Pending confirmation
    final_teams: list[int] = field(default_factory=list)
    eliminated_teams: set[int] = field(default_factory=set)  # Индексы команд которые проиграли

    # Кто открывает каждый матч: {(match_type, match_index): user_id}
    match_openers: dict[tuple[str, int], int] = field(default_factory=dict)

    # Данные о комнатах (ID и пароль)
    qualifier_rooms: dict[int, dict[str, str]] = field(default_factory=dict)  # {match_index: {"id": "123", "password": "abc"}}
    semifinal_rooms: dict[int, dict[str, str]] = field(default_factory=dict)  # {match_index: {"id": "123", "password": "abc"}}
    final_room: dict[str, str] = field(default_factory=dict)  # {"id": "123", "password": "abc"}
    winner_team_index: int | None = None
    final_pending_winner: int | None = None  # Pending confirmation

    # Temporary match statistics storage (before confirmation)
    # Format: match_id -> {player_name: {"kills": int, "deaths": int, "circle": int}}
    temp_match_stats: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)

    # Match IDs whose ELO was already awarded (idempotency guard)
    processed_elo_matches: set[str] = field(default_factory=set)

    # OCR-сессии результатов матчей (скриншот → ручное сопоставление игроков)
    # key: "{match_type}_{match_index}"
    ocr_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Выделенные каналы матчей (MM-флоу): channel_id -> {"match_type","match_index"}
    # Скриншот, присланный в такой канал, автоматически привязывается к матчу.
    match_channels: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Карточки матчей обычных турниров: "{match_type}_{match_index}" ->
    # {"public_message_id","public_channel_id","work_message_id",
    #  "work_channel_id","sig"}. Публичная карточка — в канале турнира,
    # рабочая — в OCR-канале (одно сообщение на матч, редактируется).
    match_cards: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Betting system - new timer-based system
    betting_phase_start_time: float | None = None  # When betting started for current phase
    betting_phase: str | None = None  # "qualifiers", "semifinal", "final"
    betting_duration: int = 180  # 3 minutes in seconds
    
    # Legacy betting_open field (kept for backward compatibility)
    betting_open: bool = False
    # Store bets temporarily in tournament (persisted in database)
    # Format: match_id -> list of Bet objects

    # --- Свойства ---

    @property
    def all_players(self) -> set[str]:
        """Все добавленные игроки (круги 1–4) - player names."""
        return set(self.circle1 + self.circle2 + self.circle3 + self.circle4)

    def distribute_players_random_auto(self, players_with_sr: list[tuple[str, float]]) -> list[list[str]]:
        """Распределить игроков по SR для random-auto формата.
        
        Args:
            players_with_sr: List of (player_name, skill_rating) tuples
            
        Returns:
            List of teams (each team is list of player names)
        """
        # Сортировать по SR от высокого к низшему
        sorted_players = sorted(players_with_sr, key=lambda x: x[1], reverse=True)
        player_names = [p[0] for p in sorted_players]
        
        num_teams = self.captain_count
        players_per_team = len(player_names) // num_teams
        
        # Разделить на 4 группы
        group_size = num_teams
        groups = [
            player_names[i:i + group_size] 
            for i in range(0, len(player_names), group_size)
        ]
        
        # Инициализировать команды
        teams = [[] for _ in range(num_teams)]
        
        # Круг 1 - капитаны (первые N игроков)
        for i, captain in enumerate(groups[0]):
            teams[i].append(captain)
        
        # Круги 2-4 - распределение по RANDOM_AUTO_PICK_ORDERS
        for round_num in [2, 3, 4]:
            if round_num - 1 < len(groups):
                group = groups[round_num - 1]
                order = RANDOM_AUTO_PICK_ORDERS.get(num_teams, {}).get(str(round_num), [])
                
                for team_idx, player_idx in enumerate(order):
                    if player_idx < len(group):
                        teams[team_idx].append(group[player_idx])
        
        return teams

    @property
    def is_setup_complete(self) -> bool:
        """Готов ли турнир к запуску драфта."""
        # Use circle1 as captains (they are the same)
        if len(self.circle1) != self.captain_count:
            return False

        # Check circle2, circle3, circle4 - all need at least captain_count players
        for circle in [2, 3, 4]:
            circle_list = self.circle_list(circle)
            if len(circle_list) < self.captain_count:
                return False

        return True

    def is_team_name_editable(self, team_index: int) -> bool:
        """Можно ли редактировать название конкретной команды."""
        # Team names can only be edited in bracket phases and only once per team
        bracket_phases = [TournamentPhase.QUALIFIERS, TournamentPhase.SEMIFINALS, TournamentPhase.FINAL]
        return self.phase in bracket_phases and team_index not in self.team_names_changed_teams

    def get_player_team_index(self, user_id: int) -> int | None:
        """Получить индекс команды игрока по его user_id.

        Args:
            user_id: Discord ID игрока

        Returns:
            Индекс команды или None если игрок не в команде
        """
        # Найти имя игрока по user_id
        player_name = None
        for name, uid in self.player_user_ids.items():
            if uid == user_id:
                player_name = name
                break

        if not player_name:
            return None

        # Найти команду игрока
        for team_idx, team in enumerate(self.teams):
            all_team_players = [team["captain"]] + team["players"]
            if player_name in all_team_players:
                return team_idx

        return None

    def start_betting_phase(self, phase: str) -> None:
        """Start betting for a specific phase (qualifier/semifinal/final)."""
        import time
        self.betting_phase = phase
        self.betting_phase_start_time = time.time()
        self.betting_open = True  # Legacy field for backward compatibility

    def is_betting_open(self) -> bool:
        """Check if betting is currently open (within 3-minute window)."""
        import time
        if self.betting_phase_start_time is None:
            return False
        elapsed = time.time() - self.betting_phase_start_time
        return elapsed < self.betting_duration

    def get_betting_remaining_time(self) -> int:
        """Get remaining betting time in seconds."""
        import time
        if self.betting_phase_start_time is None:
            return 0
        elapsed = time.time() - self.betting_phase_start_time
        remaining = max(0, self.betting_duration - int(elapsed))
        return remaining

    def circle_list(self, circle: int) -> list[str]:
        """Получить список игроков круга (display names)."""
        return getattr(self, f"circle{circle}")

    def set_circle_list(self, circle: int, players: list[str]) -> None:
        """Установить список игроков круга (display names)."""
        setattr(self, f"circle{circle}", players)

    def get_circle_counts(self) -> dict[int, int]:
        """Получить количество игроков в каждом круге."""
        return {
            1: len(self.circle1),
            2: len(self.circle2),
            3: len(self.circle3),
            4: len(self.circle4),
        }

    def shuffle_circles(self) -> None:
        """Случайно перераспределить игроков внутри каждого круга."""
        random.shuffle(self.circle1)
        random.shuffle(self.circle2)
        random.shuffle(self.circle3)
        random.shuffle(self.circle4)

    # --- Добавление игроков ---

    def add_players(self, names: list[str]) -> tuple[list[str], list[str]]:
        """
        Добавить игроков в круги 1→2→3→4 по порядку заполнения.
        Circle4 без лимита. Возвращает (добавленные, отклонённые).
        """
        added: list[str] = []
        rejected: list[str] = []

        for name in names:
            name = name.strip()
            if not name:
                continue

            # For RANDOM/BALANCED_SR modes, add to players_pool instead of circles
            if self.formation_mode in (FormationMode.RANDOM, FormationMode.BALANCED_SR):
                if name in self.players_pool:
                    rejected.append(name)
                    continue

                if len(self.players_pool) < int(self.size.value):
                    self.players_pool.append(name)
                    added.append(name)
                else:
                    rejected.append(name)
                continue

            # For ELO/SKILL modes, use circles
            if name in self.all_players:
                rejected.append(name)
                continue

            placed = False
            # Circle1, circle2, circle3 - по captain_count игроков
            # Circle4 - без лимита
            for circle in range(1, 5):
                circle_players = self.circle_list(circle)
                # Circle4 без лимита, остальные по captain_count
                if circle == 4 or len(circle_players) < self.captain_count:
                    circle_players.append(name)
                    self.set_circle_list(circle, circle_players)
                    added.append(name)
                    placed = True
                    break

            if not placed:
                rejected.append(name)

        return added, rejected

    def add_player_to_circle(self, circle: int, name: str, user_id: int | None = None, store=None) -> bool:
        """Добавить игрока в конкретный круг с проверкой конфликтов.
        
        Args:
            store: JsonStore instance for cross-tournament conflict checking
        """
        name = name.strip()
        import logging
        logging.info(f"add_player_to_circle: circle={circle}, name='{name}', user_id={user_id}")
        logging.info(f"  all_players: {self.all_players}")
        logging.info(f"  name in all_players: {name in self.all_players}")

        # Check conflicts if store provided
        if store and user_id:
            all_tournaments = store.get_by_guild(self.guild_id)
            logging.info(f"  Checking {len(all_tournaments)} other tournaments in guild")
            for other_tournament in all_tournaments:
                # Skip self
                if other_tournament.id == self.id:
                    continue

                logging.info(f"  Checking tournament {other_tournament.id} (phase: {other_tournament.phase.value})")
                # Check if player is active in other tournament
                if other_tournament.is_player_active(user_id):
                    logging.warning(f"  FAILED: player is active in another tournament (ID: {other_tournament.id})")
                    return False  # Conflict detected

        # Note: BR tournaments are separate systems and don't conflict with regular tournaments

        if not name or name in self.all_players:
            logging.warning(f"  FAILED: name is empty or already in all_players")
            return False

        circle_players = self.circle_list(circle)
        limit = self.circle_limit(circle)
        logging.info(f"  circle_players: {circle_players}, limit={limit}, len={len(circle_players)}")

        # Check limit if not unlimited
        if limit != float('inf') and len(circle_players) >= limit:
            logging.warning(f"  FAILED: circle is full")
            return False

        circle_players.append(name)
        self.set_circle_list(circle, circle_players)
        logging.info(f"  SUCCESS: added to circle {circle}")

        # Register user_id if provided
        # Only update if user_id > 0 (real player) to avoid overwriting with bot IDs
        if user_id is not None:
            existing_id = self.player_user_ids.get(name)
            if existing_id is None or (existing_id <= 0 and user_id > 0):
                # Only set if no existing ID, or existing is bot and new is real
                self.player_user_ids[name] = user_id
                logging.info(f"  Set user_id for {name}: {user_id}")
            else:
                logging.info(f"  Kept existing user_id for {name}: {existing_id} (not overwriting with {user_id})")

        return True

    def player_name_for_uid(self, user_id: int) -> str | None:
        """Отображаемое имя игрока по Discord user_id (или None)."""
        for name, uid in self.player_user_ids.items():
            if uid == user_id:
                return name
        return None

    def remove_player_uid(self, user_id: int) -> bool:
        """Удалить игрока по Discord user_id из кругов и players_pool.

        В отличие от remove_player (поиск по имени без учёта регистра)
        оперирует точной записью player_user_ids — идентичность
        по user_id, одинаковые display_name не затрагиваются.
        """
        name = self.player_name_for_uid(user_id)
        if name is None:
            return False

        removed = False
        for circle in range(1, 5):
            circle_players = self.circle_list(circle)
            if name in circle_players:
                circle_players.remove(name)
                self.set_circle_list(circle, circle_players)
                removed = True

        if name in self.players_pool:
            self.players_pool.remove(name)
            removed = True

        if removed and name in self.player_user_ids:
            del self.player_user_ids[name]

        return removed

    def remove_player(self, name: str) -> bool:
        """Удалить игрока из любого круга и команды. Возвращает True если найден и удален."""
        import logging
        logger = logging.getLogger(__name__)

        name = name.strip()
        removed = False

        # Remove from circles
        for circle in range(1, 5):
            circle_players = self.circle_list(circle)
            # Case-insensitive search
            for i, player in enumerate(circle_players):
                if player.strip().lower() == name.lower():
                    circle_players.pop(i)
                    self.set_circle_list(circle, circle_players)
                    removed = True
                    logger.info(f"remove_player: removed '{name}' from circle {circle}, tournament_id={self.id}")

        # Remove from teams if in TEAMS phase or later
        user_id = self.player_user_ids.get(name)
        if user_id:
            for team_idx, team in enumerate(self.teams):
                # Remove from captain
                if team.get("captain") == name:
                    team["captain"] = ""
                    removed = True
                    logger.info(f"remove_player: removed '{name}' as captain from team {team_idx}, tournament_id={self.id}")

                # Remove from players list
                if name in team.get("players", []):
                    team["players"].remove(name)
                    removed = True
                    logger.info(f"remove_player: removed '{name}' from team {team_idx} players, tournament_id={self.id}")

        # Remove from player_user_ids if removed from circles or teams
        if removed and name in self.player_user_ids:
            del self.player_user_ids[name]
            logger.info(f"remove_player: removed '{name}' from player_user_ids, tournament_id={self.id}")

        if removed:
            logger.info(f"remove_player: successfully removed '{name}' (user_id={user_id}) from tournament {self.id}")

        return removed

    # --- Формирование кругов по ELO ---

    async def distribute_by_elo(self, guild_id: int) -> None:
        """Распределить игроков по кругам на основе ELO."""
        from storage.player_stats_store import player_stats_store
        import logging
        logger = logging.getLogger(__name__)

        # Collect all players from all circles
        all_players = []
        for circle in range(1, 5):
            circle_list = self.circle_list(circle)
            for player_name in circle_list:
                user_id = self.player_user_ids.get(player_name, 0)
                all_players.append((player_name, user_id))

        logger.info(f"distribute_by_elo: collected {len(all_players)} players, real_player_ids={[uid for name, uid in all_players if uid > 0]}")

        # Get ELO for each player (only for real players)
        players_with_elo = []
        for player_name, user_id in all_players:
            if user_id > 0:
                stats = await player_stats_store.get(guild_id, user_id)
                elo = stats.elo if stats else 1000  # Default ELO for new players
                players_with_elo.append((player_name, user_id, elo))
            else:
                # Bot players get default ELO
                players_with_elo.append((player_name, user_id, 1000))

        # Sort by ELO (descending)
        players_with_elo.sort(key=lambda x: x[2], reverse=True)

        # Clear all circles
        self.circle1 = []
        self.circle2 = []
        self.circle3 = []
        self.circle4 = []

        # Distribute to circles based on tournament size
        captain_count = self.captain_count

        # Top players become captains (circle1)
        for i in range(captain_count):
            if i < len(players_with_elo):
                player_name, user_id, _ = players_with_elo[i]
                self.circle1.append(player_name)
                self.player_user_ids[player_name] = user_id

        # Next group goes to circle2
        for i in range(captain_count, captain_count * 2):
            if i < len(players_with_elo):
                player_name, user_id, _ = players_with_elo[i]
                self.circle2.append(player_name)
                self.player_user_ids[player_name] = user_id

        # Next group goes to circle3
        for i in range(captain_count * 2, captain_count * 3):
            if i < len(players_with_elo):
                player_name, user_id, _ = players_with_elo[i]
                self.circle3.append(player_name)
                self.player_user_ids[player_name] = user_id

        # All remaining players go to circle4
        for i in range(captain_count * 3, len(players_with_elo)):
            player_name, user_id, _ = players_with_elo[i]
            self.circle4.append(player_name)
            self.player_user_ids[player_name] = user_id

        # Clean up player_user_ids: only keep players who are in circles
        circle_player_names = set(self.circle1 + self.circle2 + self.circle3 + self.circle4)
        new_player_user_ids = {}
        for name, uid in self.player_user_ids.items():
            if name in circle_player_names:
                new_player_user_ids[name] = uid

        self.player_user_ids = new_player_user_ids
        logger.info(f"distribute_by_elo: cleaned player_user_ids, kept {len(new_player_user_ids)} players, real_player_ids={[uid for name, uid in new_player_user_ids.items() if uid > 0]}")

    async def distribute_by_skill_rating(self, guild_id: int) -> None:
        """Распределить игроков по кругам на основе Skill Rating."""
        from storage.player_stats_store import player_stats_store
        import logging
        logger = logging.getLogger(__name__)

        # Collect all players from all circles
        all_players = []
        for circle in range(1, 5):
            circle_list = self.circle_list(circle)
            for player_name in circle_list:
                user_id = self.player_user_ids.get(player_name, 0)
                all_players.append((player_name, user_id))

        logger.info(f"distribute_by_skill_rating: collected {len(all_players)} players, real_player_ids={[uid for name, uid in all_players if uid > 0]}")
        
        # Get Skill Rating for each player (only for real players)
        players_with_rating = []
        all_ratings = []  # Для расчёта среднего по серверу

        for player_name, user_id in all_players:
            if user_id > 0:
                stats = await player_stats_store.get(guild_id, user_id)
                if stats:
                    try:
                        skill_rating = stats.skill_rating
                        if stats.games >= 3:
                            all_ratings.append(skill_rating)
                    except (AttributeError, Exception):
                        skill_rating = 1.0  # Default if skill_rating not available
                else:
                    skill_rating = 1.0  # Default for new players
                players_with_rating.append((player_name, user_id, skill_rating))
            else:
                # Bot players get default rating
                players_with_rating.append((player_name, user_id, 1.0))
        
        # Рассчитать среднее Skill Rating сервера (для игроков с 3+ катками)
        server_avg_rating = 5.0  # Default if no data
        if all_ratings:
            server_avg_rating = sum(all_ratings) / len(all_ratings)
        
        logger.info(f"distribute_by_skill_rating: server_avg_rating={server_avg_rating}, players_with_rating={len(players_with_rating)}")
        
        # Заменить Skill Rating на среднее по серверу для игроков с менее чем 3 катками
        final_players_with_rating = []
        for player_name, user_id, skill_rating in players_with_rating:
            stats = await player_stats_store.get(guild_id, user_id)
            if stats and stats.games < 3:
                # Игрок с менее чем 3 катками - даём среднее по серверу
                final_players_with_rating.append((player_name, user_id, server_avg_rating))
            else:
                # Игрок с 3+ катками - используем его Skill Rating
                final_players_with_rating.append((player_name, user_id, skill_rating))

        # Sort by Skill Rating (descending)
        final_players_with_rating.sort(key=lambda x: x[2], reverse=True)
        
        logger.info(f"distribute_by_skill_rating: sorted players: {[(p[0], p[2]) for p in final_players_with_rating]}")

        # Clear all circles
        self.circle1 = []
        self.circle2 = []
        self.circle3 = []
        self.circle4 = []

        # Distribute to circles based on tournament size
        captain_count = self.captain_count
        logger.info(f"distribute_by_skill_rating: captain_count={captain_count}")

        # Top players become captains (circle1)
        for i in range(captain_count):
            if i < len(final_players_with_rating):
                player_name, user_id, _ = final_players_with_rating[i]
                self.circle1.append(player_name)
                self.player_user_ids[player_name] = user_id
        
        logger.info(f"distribute_by_skill_rating: circle1={self.circle1}")

        # Next group goes to circle2
        for i in range(captain_count, captain_count * 2):
            if i < len(final_players_with_rating):
                player_name, user_id, _ = final_players_with_rating[i]
                self.circle2.append(player_name)
                self.player_user_ids[player_name] = user_id
        
        logger.info(f"distribute_by_skill_rating: circle2={self.circle2}")

        # Next group goes to circle3
        for i in range(captain_count * 2, captain_count * 3):
            if i < len(final_players_with_rating):
                player_name, user_id, _ = final_players_with_rating[i]
                self.circle3.append(player_name)
                self.player_user_ids[player_name] = user_id
        
        logger.info(f"distribute_by_skill_rating: circle3={self.circle3}")

        # All remaining players go to circle4
        for i in range(captain_count * 3, len(final_players_with_rating)):
            player_name, user_id, _ = final_players_with_rating[i]
            self.circle4.append(player_name)
            self.player_user_ids[player_name] = user_id

        logger.info(f"distribute_by_skill_rating: circle4={self.circle4}")

        # Clean up player_user_ids: only keep players who are in circles
        circle_player_names = set(self.circle1 + self.circle2 + self.circle3 + self.circle4)
        new_player_user_ids = {}
        for name, uid in self.player_user_ids.items():
            if name in circle_player_names:
                new_player_user_ids[name] = uid

        self.player_user_ids = new_player_user_ids
        logger.info(f"distribute_by_skill_rating: cleaned player_user_ids, kept {len(new_player_user_ids)} players, real_player_ids={[uid for name, uid in new_player_user_ids.items() if uid > 0]}")

    # --- Случайное распределение (без кругов) ---

    def distribute_randomly(self) -> None:
        """Случайно распределить игроков по командам без кругов."""
        import logging
        logger = logging.getLogger(__name__)

        # Collect all players from players_pool (RANDOM mode)
        all_players = []
        for player_name in self.players_pool:
            user_id = self.player_user_ids.get(player_name, 0)
            all_players.append((player_name, user_id))

        logger.info(f"distribute_randomly: collected {len(all_players)} players, real_player_ids={[uid for name, uid in all_players if uid > 0]}")

        # Shuffle players randomly
        random.shuffle(all_players)

        # Distribute players into teams of 4
        captain_count = self.captain_count
        players_per_team = 4  # Fixed 4 players per team
        total_teams = len(all_players) // players_per_team

        logger.info(f"distribute_randomly: creating {total_teams} teams, captain_count={captain_count}")

        # Create teams
        self.teams = []
        for team_idx in range(total_teams):
            start_idx = team_idx * players_per_team
            end_idx = start_idx + players_per_team

            team_players = all_players[start_idx:end_idx]

            # Randomly select captain from team players
            random.shuffle(team_players)
            captain_name, captain_user_id = team_players[0]
            remaining_players = team_players[1:]

            # Create team data
            team_players_names = [p[0] for p in team_players]
            team_data = {
                "captain": captain_name,
                "players": team_players_names,
                "circle1": captain_name,
                "circle2": remaining_players[0][0] if len(remaining_players) > 0 else "",
                "circle3": remaining_players[1][0] if len(remaining_players) > 1 else "",
                "circle4": remaining_players[2][0] if len(remaining_players) > 2 else "",
            }

            self.teams.append(team_data)

            # Update player_user_ids for all team members
            self.player_user_ids[captain_name] = captain_user_id
            for player_name, user_id in remaining_players:
                self.player_user_ids[player_name] = user_id

        logger.info(f"distribute_randomly: created {len(self.teams)} teams")

        # Clear circles and players_pool since we don't use them in RANDOM mode
        self.circle1 = []
        self.circle2 = []
        self.circle3 = []
        self.circle4 = []
        self.players_pool = []

        # Clean up player_user_ids: only keep players who are in teams
        # Remove players who were in players_pool but not selected for teams
        team_player_names = set()
        for team in self.teams:
            team_player_names.add(team["captain"])
            team_player_names.update(team["players"])

        # Rebuild player_user_ids with only team players
        new_player_user_ids = {}
        for name, uid in self.player_user_ids.items():
            if name in team_player_names:
                new_player_user_ids[name] = uid

        self.player_user_ids = new_player_user_ids
        logger.info(f"distribute_randomly: cleaned player_user_ids, kept {len(new_player_user_ids)} players, real_player_ids={[uid for name, uid in new_player_user_ids.items() if uid > 0]}")

        # Skip TEAMS phase and go directly to bracket generation
        logger.info(f"distribute_randomly: calling generate_bracket, current phase={self.phase.value}")
        self.generate_bracket()
        logger.info(f"distribute_randomly: after generate_bracket, new phase={self.phase.value}")

    async def distribute_balanced_sr(self, guild_id: int) -> str | None:
        """Balanced SR (Tournaments 2.0): команды по 4 игрока, по одному
        из каждой из четырёх групп силы, минимум разницы суммарного SR.

        Возвращает None при успехе или текст ошибки (дубли Discord ID,
        недостаточно игроков, невалидный SR-набор).
        """
        from storage.player_stats_store import player_stats_store
        import logging
        logger = logging.getLogger(__name__)

        # Все игроки из общего пула регистрации (без кругов)
        all_players: list[tuple[str, int]] = [
            (player_name, self.player_user_ids.get(player_name, 0))
            for player_name in self.players_pool
        ]

        if not all_players:
            return "❌ Нет игроков для распределения."

        # Отказ при дублях Discord ID
        from utils.balanced_sr import duplicate_user_ids, balanced_sr_teams
        name_to_uid = {name: uid for name, uid in all_players}
        dups = duplicate_user_ids(name_to_uid)
        if dups:
            dup_names = [n for n, u in name_to_uid.items() if u in dups]
            return (
                "❌ Balanced SR: обнаружены дубли Discord ID "
                f"({', '.join(f'<@{u}>' for u in dups)}): "
                f"{', '.join(dup_names)}. Удалите дубли и повторите."
            )

        # Skill Rating для реальных игроков; ботам и новичкам — дефолт
        players_with_rating: list[tuple[str, int, float]] = []
        all_ratings: list[float] = []
        for player_name, user_id in all_players:
            skill_rating = 1.0
            if user_id > 0:
                stats = await player_stats_store.get(guild_id, user_id)
                if stats is not None:
                    try:
                        sr = float(stats.skill_rating)
                        skill_rating = sr
                        if getattr(stats, "games", 0) >= 3:
                            all_ratings.append(sr)
                    except (AttributeError, TypeError, ValueError):
                        pass
            players_with_rating.append((player_name, user_id, skill_rating))

        server_avg_rating = (
            sum(all_ratings) / len(all_ratings) if all_ratings else 5.0
        )

        # Игроки с менее чем 3 катками получают средний SR сервера
        players_with_sr: list[tuple[str, float]] = []
        for player_name, user_id, skill_rating in players_with_rating:
            stats = await player_stats_store.get(guild_id, user_id) if user_id > 0 else None
            if stats is not None and getattr(stats, "games", 0) < 3:
                players_with_sr.append((player_name, server_avg_rating))
            else:
                players_with_sr.append((player_name, skill_rating))

        teams_names = balanced_sr_teams(
            players_with_sr, self.player_user_ids
        )
        if teams_names is None:
            return "❌ Balanced SR: нужно минимум 8 игроков (число должно делиться на 4)."

        # Команды в стандартном формате (как distribute_randomly)
        self.teams = []
        for team_idx, members in enumerate(teams_names):
            captain_name = members[0]
            rest = members[1:]
            team_data = {
                "captain": captain_name,
                "players": members,
                "circle1": captain_name,
                "circle2": rest[0] if len(rest) > 0 else "",
                "circle3": rest[1] if len(rest) > 1 else "",
                "circle4": rest[2] if len(rest) > 2 else "",
            }
            self.teams.append(team_data)

        # Круги и пул не используются — команды построены напрямую
        self.circle1 = []
        self.circle2 = []
        self.circle3 = []
        self.circle4 = []
        self.players_pool = []

        team_player_names = set()
        for team in self.teams:
            team_player_names.add(team["captain"])
            team_player_names.update(team["players"])
        self.player_user_ids = {
            name: uid for name, uid in self.player_user_ids.items()
            if name in team_player_names
        }

        logger.info(
            f"distribute_balanced_sr: {len(self.teams)} teams, "
            f"players={len(team_player_names)}, tournament_id={self.id}"
        )

        self.generate_bracket()
        return None

    # --- Драфт ---

    def start_draft(self) -> None:
        """Начать драфт без перемешивания капитанов."""
        # Sync captains with circle1
        self.captains = list(self.circle1)

        # captain_order is fixed [0, 1, 2, 3...] for PICK_ORDERS to work
        self.captain_order = list(range(self.captain_count))
        self.picks = {str(i): {} for i in range(self.captain_count)}
        self.current_circle = 2
        self.pick_index = 0

        # Record start time if not already set
        if not self.start_time:
            from datetime import datetime
            self.start_time = datetime.now().isoformat()

        # Initialize available players for circles 2, 3, 4
        # All players from circle4 are available for draft
        self.available = {
            "2": list(self.circle2),
            "3": list(self.circle3),
            "4": list(self.circle4),  # All players from circle4
        }

        self.phase = TournamentPhase.DRAFT
        self.reset_pick_timer()

    def reset_pick_timer(self) -> None:
        """Сбросить таймер текущего выбора."""
        from datetime import datetime
        self.draft_pick_start_time = datetime.now().isoformat()

    def get_draft_pick_remaining_time(self) -> int:
        """Получить оставшееся время для текущего выбора в секундах."""
        # Handle old tournaments without the field
        draft_pick_start_time = getattr(self, 'draft_pick_start_time', '')
        draft_pick_duration = getattr(self, 'draft_pick_duration', 60)
        
        if not draft_pick_start_time:
            return draft_pick_duration
        
        from datetime import datetime
        start = datetime.fromisoformat(draft_pick_start_time)
        elapsed = (datetime.now() - start).total_seconds()
        remaining = int(draft_pick_duration - elapsed)
        return max(0, remaining)

    def draft_pick_deadline_unix(self) -> int | None:
        """Unix-время (секунды) окончания текущего выбора.

        Для Discord <t:...:R>: дедлайн вычисляется один раз из
        сохранённого draft_pick_start_time + draft_pick_duration и
        не пересчитывается от «сейчас» — после рестарта значение
        то же, что до него. None — таймер не запущен/невалиден.
        """
        from datetime import datetime
        start_raw = getattr(self, "draft_pick_start_time", "") or ""
        if not start_raw:
            return None
        try:
            start = datetime.fromisoformat(start_raw)
        except (ValueError, TypeError):
            return None
        # Naive ISO пишется локальным datetime.now() — timestamp()
        # трактует naive как локальное время, что совпадает с
        # elapsed-логикой get_draft_pick_remaining_time.
        duration = int(getattr(self, "draft_pick_duration", 60) or 60)
        return int(start.timestamp()) + duration

    def pick_random_player(self) -> tuple[int, str] | None:
        """Случайно выбрать игрока для текущего капитана.
        Возвращает (position, player_name) или None если нет доступных игроков.
        """
        picker_pos = self.current_picker_position()
        if picker_pos is None:
            return None
        
        key = str(self.current_circle)
        available = self.available.get(key, [])
        if not available:
            return None
        
        # Randomly select a player
        player = random.choice(available)
        return (picker_pos, player)

    def current_picker_position(self) -> int | None:
        """Позиция капитана, который сейчас выбирает."""
        if self.phase != TournamentPhase.DRAFT:
            return None

        # Check if current circle has available players
        key = str(self.current_circle)
        if key not in self.available or not self.available[key]:
            return None

        circle_orders = PICK_ORDERS.get(self.captain_count, {})
        order_data = circle_orders.get(key, {})
        order = order_data.get("order", [])
        if self.pick_index < len(order):
            # Return the captain index directly from pick order
            return order[self.pick_index]
        return None

    def pick_player(self, position: int, player: str) -> None:
        """Зафиксировать выбор игрока капитаном на позиции position."""
        key = str(self.current_circle)
        # Store picks by position in captain_order
        self.picks[str(position)][key] = player
        self.available[key].remove(player)
        self.reset_pick_timer()  # Reset timer for next pick

    def advance_after_pick(self) -> bool:
        """
        Перейти к следующему шагу драфта.
        Возвращает True, если драфт завершён.
        """
        # Check if current circle has no more players
        key = str(self.current_circle)
        remaining = self.available.get(key, [])
        if not remaining:
            # All players picked, advance to next phase
            return self._advance_circle()

        circle_orders = PICK_ORDERS.get(self.captain_count, {})
        order_data = circle_orders.get(key, {})
        order = order_data.get("order", [])
        self.pick_index += 1

        if self.pick_index >= len(order):
            return self._advance_circle()

        return False


    def _advance_circle(self) -> bool:
        """Перейти к следующему кругу или завершить драфт."""
        self.pick_index = 0

        if self.current_circle >= 4:
            self._build_teams()
            # Skip TEAMS phase and go directly to bracket generation
            self.generate_bracket()
            return True

        self.current_circle += 1
        return False

    def _build_teams(self) -> None:
        """Сформировать команды из результатов драфта."""
        self.teams = []

        for pos in range(self.captain_count):
            captain_idx = self.captain_order[pos]
            captain_name = self.captains[captain_idx]
            picks = self.picks[str(pos)]

            # Collect all players from all circles
            players = []
            if captain_name:
                players.append(captain_name)
            for circle in ["2", "3", "4"]:
                pick = picks.get(circle, "")
                if pick:
                    players.append(pick)

            team_data = {
                "captain": captain_name,
                "players": players,
                "circle1": captain_name,  # Captain is in circle1
                "circle2": picks.get("2", ""),
                "circle3": picks.get("3", ""),
                "circle4": picks.get("4", ""),
            }

            self.teams.append(team_data)

        # Notify captains about statistics
        try:
            from config import replace_emojis
            bot = get_bot_instance()
            if bot:
                guild = bot.get_guild(self.guild_id)
                if guild:
                    channel = guild.get_channel(self.channel_id)
                    if channel:
                        # Build captain mentions
                        captain_mentions = []
                        for team in self.teams:
                            captain_id = self.player_user_ids.get(team["captain"])
                            if captain_id:
                                captain_mentions.append(f"<@{captain_id}>")

                        # Commented out - not sending captain notification
                        # if captain_mentions:
                        #     message = (
                        #         f" **КОМАНДЫ СФОРМИРОВАНЫ!** \n\n"
                        #         f" **Капитаны:** {', '.join(captain_mentions)}\n\n"
                        #         f" **ВАЖНО:** Не забудьте заполнить статистику матчей!\n"
                        #         f" После каждого матча нажмите на результат и введите:\n"
                        #         f"Убийства (kills) для каждой команды\n"
                        #         f"Это необходимо для правильного расчета ELO и статистики\n\n"
                        #         f" Удачи в турнире! "
                        #     )
                        #     import asyncio
                        #     asyncio.create_task(channel.send(message))
        except Exception:
            pass  # Don't break tournament build if notification fails

    def generate_bracket(self) -> None:
        """Сгенерировать сетку на основе размера турнира."""
        import logging
        logger = logging.getLogger(__name__)

        # Only generate if we have teams
        if not self.teams:
            logger.warning("generate_bracket: no teams available")
            return

        logger.info(f"generate_bracket: tournament size={self.size.value}, teams={len(self.teams)}")

        if self.size == TournamentSize.EIGHT:
            # 8 players: straight to final
            self.final_teams = [0, 1]  # First two teams
            self.phase = TournamentPhase.FINAL
            logger.info(f"generate_bracket: set phase to FINAL")
            # Start betting phase for final
            self.start_betting_phase("final")
        elif self.size == TournamentSize.SIXTEEN:
            # 16 players: semifinals + final
            self.generate_semifinals()
            logger.info(f"generate_bracket: called generate_semifinals, phase={self.phase.value}")
        else:
            # 32 players: qualifiers + semifinals + final
            self.generate_qualifiers()
            logger.info(f"generate_bracket: called generate_qualifiers, phase={self.phase.value}")

        # Assign match openers and send notification
        self._assign_match_openers()

    def _assign_match_openers(self) -> None:
        """Назначить кто открывает каждый матч и отправить уведомление."""
        try:
            from config import replace_emojis
            bot = get_bot_instance()
            if not bot:
                return

            guild = bot.get_guild(self.guild_id)
            if not guild:
                return

            channel = guild.get_channel(self.channel_id)
            if not channel:
                return

            # Dictionary to store match -> opener assignments
            self.match_openers = {}  # {(match_type, match_index): user_id}

            # Assign for qualifiers if they exist
            if hasattr(self, 'qualifier_matches') and self.qualifier_matches:
                for i, (team_a, team_b) in enumerate(self.qualifier_matches):
                    # Get all players from both teams
                    players = []
                    for team_idx in [team_a, team_b]:
                        if team_idx < len(self.teams):
                            team = self.teams[team_idx]
                            players.append(team["captain"])
                            players.extend(team["players"])

                    # Randomly select opener
                    if players:
                        opener_name = random.choice(players)
                        opener_id = self.player_user_ids.get(opener_name)
                        if opener_id:
                            self.match_openers[("qualifier", i)] = opener_id

            # Assign for semifinals if they exist
            if hasattr(self, 'semifinal_matches') and self.semifinal_matches:
                for i, (team_a, team_b) in enumerate(self.semifinal_matches):
                    players = []
                    for team_idx in [team_a, team_b]:
                        if team_idx < len(self.teams):
                            team = self.teams[team_idx]
                            players.append(team["captain"])
                            players.extend(team["players"])

                    if players:
                        opener_name = random.choice(players)
                        opener_id = self.player_user_ids.get(opener_name)
                        if opener_id:
                            self.match_openers[("semifinal", i)] = opener_id

            # Assign for final if it exists
            if hasattr(self, 'final_teams') and self.final_teams:
                team_a, team_b = self.final_teams
                players = []
                for team_idx in [team_a, team_b]:
                    if team_idx < len(self.teams):
                        team = self.teams[team_idx]
                        players.append(team["captain"])
                        players.extend(team["players"])

                if players:
                    opener_name = random.choice(players)
                    opener_id = self.player_user_ids.get(opener_name)
                    if opener_id:
                        self.match_openers[("final", 0)] = opener_id

        except Exception as e:
            import logging
            logging.error(f"Error assigning match openers: {e}", exc_info=True)

    def generate_qualifiers(self) -> None:
        """Сгенерировать отборочные матчи для 32 игроков."""
        # All 8 teams play qualifiers: 4 matches, 4 winners advance to semifinals
        # Teams 0 vs 1, 2 vs 3, 4 vs 5, 6 vs 7 - winners advance to semifinals
        self.qualifier_matches = [(0, 1), (2, 3), (4, 5), (6, 7)]
        self.qualifier_winners = [None, None, None, None]
        self.phase = TournamentPhase.QUALIFIERS
        # Start betting phase for qualifiers
        self.start_betting_phase("qualifiers")

    def set_qualifier_winner(self, match_index: int, team_index: int) -> bool:
        """
        Записать победителя отборочного матча (pending confirmation).
        Возвращает True, если все отборочные матчи завершены.
        """
        self.qualifier_winners[match_index] = team_index
        # Don't auto-advance to semifinals - wait for confirmation with stats
        return False

    def confirm_qualifier_winner(self, match_index: int, team_index: int) -> bool:
        """
        Подтвердить победителя отборочного матча с сохранением статистики.
        Возвращает True, если все отборочные матчи завершены.
        """
        # Determine loser team
        match = self.qualifier_matches[match_index]
        loser_idx = match[0] if match[1] == team_index else match[1]
        
        # Mark loser as eliminated
        self.eliminate_team(loser_idx)
        
        # Winner is already set in qualifier_winners, this just confirms
        # Statistics should be saved before calling this
        # Clear pending winner for this match
        if match_index in range(len(self.qualifier_winners)):
            self.qualifier_winners[match_index] = team_index

        if all(w is not None for w in self.qualifier_winners):
            self.generate_semifinals_from_qualifiers()
            return True
        return False

    async def confirm_qualifier_winner_async(self, match_index: int, team_index: int) -> bool:
        """
        Асинхронная версия подтверждения победителя.
        Статистика и ELO обрабатываются через process_match_result в views.
        """
        # Confirm winner
        return self.confirm_qualifier_winner(match_index, team_index)

    def generate_semifinals_from_qualifiers(self) -> None:
        """Сгенерировать полуфиналы из победителей отборочных."""
        # 4 qualifier winners advance to semifinals: 2 matches
        # Winners of qualifiers[0] vs qualifiers[1], qualifiers[2] vs qualifiers[3]
        winners = self.qualifier_winners
        self.semifinal_matches = [(winners[0], winners[1]), (winners[2], winners[3])]
        self.semifinal_winners = [None, None]
        self.semifinal_pending_winners = [None, None]
        self.phase = TournamentPhase.SEMIFINALS
        # Start betting phase for semifinals
        self.start_betting_phase("semifinal")

    def generate_semifinals(self) -> None:
        """Случайно сгенерировать пары полуфиналов."""
        pairing = random.choice(SEMIFINAL_PAIRINGS)
        self.semifinal_matches = list(pairing)
        self.semifinal_winners = [None, None]
        self.semifinal_pending_winners = [None, None]
        self.phase = TournamentPhase.SEMIFINALS
        # Start betting phase for semifinals
        self.start_betting_phase("semifinal")

    def set_semifinal_winner(self, match_index: int, team_index: int) -> bool:
        """
        Записать победителя полуфинала (pending confirmation).
        Возвращает True, если оба полуфинала завершены.
        """
        self.semifinal_pending_winners[match_index] = team_index
        # Don't auto-advance to final - wait for confirmation with stats
        return False

    def confirm_semifinal_winner(self, match_index: int, team_index: int) -> bool:
        """
        Подтвердить победителя полуфинала с сохранением статистики.
        Возвращает True, если оба полуфинала завершены.
        """
        # Determine loser team
        match = self.semifinal_matches[match_index]
        loser_idx = match[0] if match[1] == team_index else match[1]
        
        # Mark loser as eliminated
        self.eliminate_team(loser_idx)
        
        # Move from pending to confirmed
        self.semifinal_winners[match_index] = team_index
        self.semifinal_pending_winners[match_index] = None

        if all(w is not None for w in self.semifinal_winners):
            self.generate_final_from_semifinals()
            return True
        return False

    async def confirm_semifinal_winner_async(self, match_index: int, team_index: int) -> bool:
        """
        Асинхронная версия подтверждения победителя.
        Статистика и ELO обрабатываются через process_match_result в views.
        """
        # Confirm winner
        return self.confirm_semifinal_winner(match_index, team_index)

    def generate_final_from_semifinals(self) -> None:
        """Сгенерировать финал из победителей полуфиналов."""
        # Переход к финалу: пары + фаза + сброс pending-состояния
        self.final_teams = [
            w for w in self.semifinal_winners if w is not None
        ][:2]
        self.final_pending_winner = None
        self.phase = TournamentPhase.FINAL
        # Start betting phase for final
        self.start_betting_phase("final")

    def set_final_winner(self, team_index: int) -> None:
        """Записать победителя финала (pending confirmation)."""
        self.final_pending_winner = team_index
        # Don't set phase to COMPLETE yet, wait for confirmation

    async def confirm_final_winner(self, team_index: int) -> None:
        """Подтвердить победителя финала (идемпотентно)."""
        # Повторное подтверждение не должно перезаписывать результат
        # или запускать побочные эффекты завершения второй раз.
        if self.phase == TournamentPhase.COMPLETE:
            return
        # Determine loser team
        loser_idx = self.final_teams[0] if self.final_teams[1] == team_index else self.final_teams[1]
        
        # Mark loser as eliminated
        self.eliminate_team(loser_idx)

        self.winner_team_index = team_index
        self.phase = TournamentPhase.COMPLETE

        # Unlock MM search channel if this tournament was created from MM
        if self.mm_search_channel_id > 0:
            try:
                bot = get_bot_instance()
                if bot and hasattr(bot, 'mm_cog'):
                    from cogs.mm import LOCKED_CHANNELS
                    if self.channel_id in LOCKED_CHANNELS:
                        del LOCKED_CHANNELS[self.channel_id]
                        import logging
                        logging.info(f"Channel {self.channel_id} unlocked after tournament completion")
            except Exception as e:
                import logging
                logging.warning(f"Error unlocking MM channel: {e}")

        # Reset MM search if this tournament was created from MM
        if self.mm_search_channel_id > 0:
            try:
                from storage.mm_search_store import mm_search_store
                search = mm_search_store.get(self.guild_id, self.mm_search_channel_id)
                if search:
                    # Search is already cleared, but we can verify
                    pass
            except Exception as e:
                import logging
                logging.warning(f"Error resetting MM search: {e}")

        # Update organizer setup embed if it exists
        try:
            from config import TOURNAMENT_CHANNELS
            from storage.organizer_store import organizer_store
            bot = get_bot_instance()
            if bot:
                guild = bot.get_guild(self.guild_id)
                if guild:
                    for channel_id in TOURNAMENT_CHANNELS:
                        orgs = organizer_store.get(self.guild_id, channel_id)
                        if orgs and orgs.message_id:
                            try:
                                update_channel = guild.get_channel(channel_id)
                                if update_channel:
                                    update_message = await update_channel.fetch_message(orgs.message_id)
                                    from views.organizer_view import build_multi_channel_embed, OrganizerManagementButton
                                    from discord.ui import View
                                    new_embed = build_multi_channel_embed(guild)
                                    new_view = View()
                                    new_view.add_item(OrganizerManagementButton(self.guild_id, channel_id, guild))
                                    from utils.design import compose
                                    await update_message.edit(view=compose(new_embed, new_view))
                            except Exception:
                                pass
        except Exception:
            pass

    def calculate_earnings(self, player_name: str, position: int) -> int:
        """Рассчитать награды за турнир по позиции."""
        # Base rewards
        if position == 1:  # Winner
            return 100
        elif position == 2:  # 2nd place
            return 50
        elif position == 3:  # 3rd place
            return 30
        elif position <= 4:  # Finalist (4th place)
            return 20
        else:  # Participation
            return 20

    async def distribute_earnings(self, guild_id: int) -> dict[str, int]:
        """Распределить награды всем участникам турнира. Возвращает словарь {player_name: earnings}."""
        from storage.user_balance_store import user_balance_store
        from storage.player_stats_store import player_stats_store
        from datetime import datetime

        earnings_map = {}
        today = datetime.now().strftime("%Y-%m-%d")
        DAILY_LIMIT = 500

        # Distribute to all players based on their final position
        for player_name, user_id in self.player_user_ids.items():
            # Determine position based on winner and teams
            position = 5  # Default: participation
            if self.winner_team_index is not None:
                # Check if player is on winning team
                winner_team = self.teams[self.winner_team_index] if self.winner_team_index < len(self.teams) else {}
                if player_name in winner_team.values():
                    position = 1
                else:
                    # Check other teams for 2nd, 3rd, 4th place
                    for team_idx, team in enumerate(self.teams):
                        if team_idx != self.winner_team_index and player_name in team.values():
                            position = team_idx + 2  # 2nd, 3rd, 4th place
                            break

            # Calculate earnings
            earnings = self.calculate_earnings(player_name, position)
            
            # Check daily limit
            stats = await player_stats_store.get(guild_id, user_id)
            if stats:
                # Reset daily counter if it's a new day
                if stats.daily_coins_date != today:
                    stats.daily_tournament_coins = 0
                    stats.daily_coins_date = today
                
                # Calculate remaining allowance
                remaining = DAILY_LIMIT - stats.daily_tournament_coins
                if remaining <= 0:
                    earnings = 0  # Already hit daily limit
                elif earnings > remaining:
                    earnings = remaining  # Cap at remaining allowance
                
                # Update daily counter
                stats.daily_tournament_coins += earnings
                await player_stats_store.set(stats)
            
            earnings_map[player_name] = earnings

            # Add to balance (only if earnings > 0)
            if earnings > 0:
                await user_balance_store.add_balance(guild_id, user_id, earnings)

            # Update player stats total earnings and XP
            if stats:
                stats.total_earnings += earnings
                stats.tournament_participations += 1

                # Add XP rewards
                xp_reward = 50  # Base XP for tournament participation
                if position == 1:  # Winner
                    xp_reward += 100
                elif position <= 3:  # Top 3
                    xp_reward += 75
                elif position <= 4:  # Finalist
                    xp_reward += 50

                stats.add_xp(xp_reward)
                await player_stats_store.set(stats)

        return earnings_map

    def is_player_active(self, user_id: int) -> bool:
        """Проверить активен ли игрок в этом турнире.

        Игрок активен если:
        - Фаза SETUP или DRAFT и он зарегистрирован (в all_players)
        - Фаза > DRAFT и он выбран в команду (в teams) или в players_pool (RANDOM mode)
        - Команда еще не проиграла (не в eliminated_teams)
        - Турнир еще не завершен (не COMPLETE)
        """
        import logging
        logger = logging.getLogger(__name__)

        # COMPLETE phase: nobody is active
        if self.phase == TournamentPhase.COMPLETE:
            logger.info(f"is_player_active: user_id={user_id}, tournament_id={self.id}, phase=COMPLETE, is_active=False")
            return False

        # Phase SETUP or DRAFT: check if registered
        if self.phase in [TournamentPhase.SETUP, TournamentPhase.DRAFT]:
            # Check if user_id is in player_user_ids (real players only, not bots)
            # Also check if user_id > 0 to exclude bot players (negative IDs)
            is_active = user_id in self.player_user_ids.values() and user_id > 0
            logger.info(f"is_player_active: user_id={user_id}, tournament_id={self.id}, phase={self.phase.value}, is_active={is_active}, real_player_ids={[uid for uid in self.player_user_ids.values() if uid > 0]}")
            return is_active

        # Phase > DRAFT: check if in a team that hasn't been eliminated
        if self.phase in [TournamentPhase.TEAMS, TournamentPhase.QUALIFIERS,
                          TournamentPhase.SEMIFINALS, TournamentPhase.FINAL]:
            # For RANDOM mode, also check players_pool
            if self.formation_mode == FormationMode.RANDOM:
                for player_name in self.players_pool:
                    if self.player_user_ids.get(player_name) == user_id and user_id > 0:
                        logger.info(f"is_player_active: user_id={user_id}, tournament_id={self.id}, phase={self.phase.value}, is_active=True (in players_pool)")
                        return True  # Player in players_pool

            # Find which team the player is in
            for team_idx, team in enumerate(self.teams):
                if team_idx in self.eliminated_teams:
                    continue  # Team already eliminated

                # Check if player is in this team
                all_team_players = [team["captain"]] + team["players"]
                for player_name in all_team_players:
                    if self.player_user_ids.get(player_name) == user_id and user_id > 0:
                        logger.info(f"is_player_active: user_id={user_id}, tournament_id={self.id}, phase={self.phase.value}, is_active=True (in team {team_idx})")
                        return True  # Player in active team

        logger.info(f"is_player_active: user_id={user_id}, tournament_id={self.id}, phase={self.phase.value}, is_active=False")
        return False  # Player not active

    def eliminate_team(self, team_index: int) -> None:
        """Отметить команду как проигравшую."""
        self.eliminated_teams.add(team_index)

    # --- Сериализация ---

    def to_dict(self) -> dict[str, Any]:
        """Преобразовать в словарь для JSON."""
        return {
            "id": self.id,
            "creation_time": self.creation_time,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "size": self.size.value,
            "registration": self.registration.value,
            "formation_mode": self.formation_mode.value,
            "ruleset": self.ruleset,
            "rules_vote": self.rules_vote,
            "captains": self.captains,
            "circle1": self.circle1,
            "circle2": self.circle2,
            "circle3": self.circle3,
            "circle4": self.circle4,
            "players_pool": self.players_pool,
            "phase": self.phase.value,
            "is_test": self.is_test,
            "start_time": self.start_time,
            "created_at": self.created_at,
            "circle_limits_enabled": self.circle_limits_enabled,
            "team_names": self.team_names,
            "team_names_changed_teams": list(self.team_names_changed_teams),
            "player_user_ids": self.player_user_ids,
            "draft_message_id": self.draft_message_id,
            "draft_pick_start_time": self.draft_pick_start_time,
            "draft_pick_duration": self.draft_pick_duration,
            "captain_order": self.captain_order,
            "picks": self.picks,
            "current_circle": self.current_circle,
            "pick_index": self.pick_index,
            "available": self.available if hasattr(self, 'available') else {},
            "teams": self.teams,
            "match_openers": [
                [mt, mi, uid]
                for (mt, mi), uid in getattr(self, "match_openers", {}).items()
            ],
            "qualifier_matches": [list(m) for m in self.qualifier_matches],
            "qualifier_winners": self.qualifier_winners,
            "semifinal_matches": [list(m) for m in self.semifinal_matches],
            "semifinal_winners": self.semifinal_winners,
            "semifinal_pending_winners": self.semifinal_pending_winners,
            "final_teams": self.final_teams,
            "final_pending_winner": self.final_pending_winner,
            "temp_match_stats": self.temp_match_stats,
            "betting_results": self.betting_results,
            "betting_open": self.betting_open,
            "betting_phase": self.betting_phase,
            "betting_phase_start_time": self.betting_phase_start_time,
            "betting_duration": self.betting_duration,
            "qualifier_rooms": self.qualifier_rooms,
            "semifinal_rooms": self.semifinal_rooms,
            "final_room": self.final_room,
            "winner_team_index": self.winner_team_index,
            "organizer_id": self.organizer_id,
            "eliminated_teams": list(self.eliminated_teams),
            "mm_search_channel_id": self.mm_search_channel_id,
            "mm_preset_channel_id": self.mm_preset_channel_id,
            "processed_elo_matches": list(self.processed_elo_matches),
            "match_channels": self.match_channels,
            "match_cards": self.match_cards,
            "ocr_sessions": self.ocr_sessions,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Tournament:
        """Восстановить из словаря JSON."""
        t = cls(
            guild_id=data["guild_id"],
            channel_id=data["channel_id"],
            message_id=data.get("message_id", 0),
            size=TournamentSize(data.get("size", "8")),
            registration=RegistrationState(data.get("registration", "closed")),
            # Старые сохранения без ключа — турниры эпохи ELO;
            # не меняем их режим. Новые турниры всегда пишут ключ.
            formation_mode=FormationMode(data.get("formation_mode", "elo")),
            ruleset=data.get("ruleset", ""),
            rules_vote=data.get("rules_vote", {}),
            captains=data.get("captains", []),
            circle1=data.get("circle1", []),
            circle2=data.get("circle2", []),
            circle3=data.get("circle3", []),
            circle4=data.get("circle4", []),
            players_pool=data.get("players_pool", []),
            phase=TournamentPhase(data.get("phase", "setup")),
            is_test=data.get("is_test", False),
            start_time=data.get("start_time", ""),
            circle_limits_enabled=data.get("circle_limits_enabled", {2: True, 3: True, 4: True}),
            team_names=data.get("team_names", {}),
            team_names_changed_teams=set(data.get("team_names_changed_teams", [])),
            player_user_ids=data.get("player_user_ids", {}),
            draft_message_id=data.get("draft_message_id", 0),
            draft_pick_start_time=data.get("draft_pick_start_time", ""),
            draft_pick_duration=data.get("draft_pick_duration", 60),
            captain_order=data.get("captain_order", []),
            picks=data.get("picks", {}),
            current_circle=data.get("current_circle", 2),
            pick_index=data.get("pick_index", 0),
            available=data.get("available", {}),
            teams=data.get("teams", []),
            semifinal_pending_winners=data.get("semifinal_pending_winners", []),
            final_pending_winner=data.get("final_pending_winner"),
            temp_match_stats=data.get("temp_match_stats", {}),
            betting_results=data.get("betting_results", {}),
            betting_open=data.get("betting_open", False),
            betting_phase=data.get("betting_phase"),
            betting_phase_start_time=data.get("betting_phase_start_time"),
            betting_duration=data.get("betting_duration", 180),
            qualifier_matches=[
                tuple(m) for m in data.get("qualifier_matches", [])
            ],
            qualifier_winners=data.get("qualifier_winners", []),
            semifinal_matches=[
                tuple(m) for m in data.get("semifinal_matches", [])
            ],
            semifinal_winners=data.get("semifinal_winners", []),
            final_teams=data.get("final_teams", []),
            qualifier_rooms=data.get("qualifier_rooms", {}),
            semifinal_rooms=data.get("semifinal_rooms", {}),
            final_room=data.get("final_room", {}),
            winner_team_index=data.get("winner_team_index"),
            organizer_id=data.get("organizer_id", 0),
            eliminated_teams=set(data.get("eliminated_teams", [])),
            mm_search_channel_id=data.get("mm_search_channel_id", 0),
            mm_preset_channel_id=data.get("mm_preset_channel_id", 0),
            processed_elo_matches=set(data.get("processed_elo_matches", [])),
            match_channels=data.get("match_channels", {}),
            match_cards=data.get("match_cards", {}),
            ocr_sessions=data.get("ocr_sessions", {}),
        )
        # Set created_at after initialization since it's init=False
        t.created_at = data.get("created_at", __import__('datetime').datetime.now().isoformat())
        # Restore stable tournament identity — иначе __post_init__
        # генерирует новый id при каждом рестарте (creation_time init=False).
        t.creation_time = data.get("creation_time") or t.creation_time
        t.id = data.get("id") or f"{t.guild_id}_{t.creation_time}"
        # match_openers: сериализован как [[match_type, match_index, uid], ...]
        t.match_openers = {
            (str(mt), int(mi)): int(uid)
            for mt, mi, uid in data.get("match_openers", [])
        }
        return t


def _team_roster_names(team: dict[str, Any]) -> list[str]:
    """Имена игроков команды: players[] или circle1-4."""
    players = [p for p in team.get("players", []) if p]
    if players:
        return players
    return [
        team.get(f"circle{c}") for c in range(1, 5)
        if team.get(f"circle{c}")
    ]


def resolve_match_context(
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> MatchContext:
    """Разрешить актуальные команды и ростеры матча из свежего
    состояния турнира (Tournaments 2.0).

    Args:
        tournament: актуальный Tournament (перечитанный из store).
        match_type: "qualifier" | "semifinal" | "final".
        match_index: индекс матча внутри этапа.

    Returns:
        MatchContext с team indices, Discord ID ростеров и session_key.

    Raises:
        ValueError: невалидный match_type/index или отсутствующие команды.
    """
    if match_type == "qualifier":
        matches = tournament.qualifier_matches
    elif match_type == "semifinal":
        matches = tournament.semifinal_matches
    elif match_type == "final":
        matches = [tuple(tournament.final_teams[:2])] if len(tournament.final_teams) >= 2 else []
    else:
        raise ValueError(f"unknown match_type: {match_type}")

    if match_index < 0 or match_index >= len(matches):
        raise ValueError(
            f"match_index {match_index} out of range for {match_type} "
            f"({len(matches)} matches)"
        )

    team_a_index, team_b_index = matches[match_index]
    teams = tournament.teams or []
    if not (0 <= team_a_index < len(teams) and 0 <= team_b_index < len(teams)):
        raise ValueError(
            f"team indices ({team_a_index}, {team_b_index}) out of range "
            f"for {len(teams)} teams"
        )

    def _roster_ids(team_index: int) -> tuple[int, ...]:
        team = teams[team_index]
        ids = []
        for name in _team_roster_names(team):
            uid = tournament.player_user_ids.get(name, 0)
            if uid:
                ids.append(int(uid))
        return tuple(ids)

    session_key = f"{match_type}_{match_index}"
    return MatchContext(
        tournament_id=tournament.id,
        guild_id=tournament.guild_id,
        tournament_channel_id=tournament.channel_id,
        match_type=match_type,
        match_index=match_index,
        team_a_index=team_a_index,
        team_b_index=team_b_index,
        team_a_player_ids=_roster_ids(team_a_index),
        team_b_player_ids=_roster_ids(team_b_index),
        session_key=session_key,
    )
