"""Модель данных Battle Royale турнира (Solo / Duo / Squad)."""

from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class BRTournamentPhase(str, Enum):
    """Фазы жизненного цикла BR турнира.

    REGISTRATION → DRAFT (составы после жеребьёвки) → PLAYING (карты)
    → COLLECT (сбор результатов) → RESULTS (опубликованный финал).
    COMPLETE/CANCELLED — архивные состояния.
    Значения bermuda/purgatory/kalahari сохранены для чтения legacy JSON.
    """

    REGISTRATION = "registration"
    DRAFT = "draft"
    PLAYING = "playing"
    COLLECT = "collect"
    RESULTS = "results"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    # legacy map-фазы (старые сохранения)
    BERMUDA = "bermuda"
    PURGATORY = "purgatory"
    KALAHARI = "kalahari"


class BRTournamentFormation(str, Enum):
    """Способ формирования составов: готовые составы или жеребьёвка."""

    RANDOM = "random"
    TEAM = "team"


class BRFormat(str, Enum):
    """Формат BR турнира — размер слота."""

    SOLO = "solo"
    DUO = "duo"
    SQUAD = "squad"


# Лимиты первой версии — константы правил формата.
BR_FORMAT_RULES: dict[str, dict[str, Any]] = {
    "solo": {
        "size": 1,
        "min_slots": 12,
        "max_slots": 48,
        "place_cap": 48,
        "label": "Solo",
    },
    "duo": {
        "size": 2,
        "min_slots": 5,
        "max_slots": 24,
        "place_cap": 24,
        "label": "Duo",
    },
    "squad": {
        "size": 4,
        "min_slots": 5,
        "max_slots": 12,
        "place_cap": 12,
        "label": "Squad",
    },
}

# Единая шкала очков за место (1–12), ниже 12-го — 0.
PLACE_POINTS = {
    1: 12, 2: 9, 3: 8, 4: 7, 5: 6, 6: 5,
    7: 4, 8: 3, 9: 2, 10: 1, 11: 0, 12: 0,
}

MAP_ROTATION = ["bermuda", "purgatory", "kalahari"]
MAP_NAMES_RU = {
    "bermuda": "Бермуды",
    "purgatory": "Чистилище",
    "kalahari": "Калахари",
}

# Фазы, в которых турнир ещё активен (удерживает игроков/команды).
ACTIVE_PHASES = {
    BRTournamentPhase.REGISTRATION,
    BRTournamentPhase.DRAFT,
    BRTournamentPhase.PLAYING,
    BRTournamentPhase.COLLECT,
    # legacy map-фазы
    BRTournamentPhase.BERMUDA,
    BRTournamentPhase.PURGATORY,
    BRTournamentPhase.KALAHARI,
}

# Приглашение Duo живёт до конца регистрации.
DUO_INVITE_TTL = 24 * 3600


def br_format_rules(fmt: str | BRFormat) -> dict[str, Any]:
    """Правила формата по значению ('solo'/'duo'/'squad')."""
    key = fmt.value if isinstance(fmt, BRFormat) else str(fmt)
    return BR_FORMAT_RULES.get(key, BR_FORMAT_RULES["squad"])


def normalize_entry(raw: Any) -> dict[str, Any] | None:
    """Нормализовать запись результата карты.

    Возвращает {"place": int|None, "kills": int, "dns": bool} или None.
    Пустая запись (нет ключа) отличается от явного DNS.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("dns"):
        kills = raw.get("kills", 0)
        try:
            kills = max(0, min(50, int(kills)))
        except (TypeError, ValueError):
            kills = 0
        return {"place": None, "kills": kills, "dns": True}
    place = raw.get("place")
    kills = raw.get("kills", 0)
    try:
        place = int(place)
    except (TypeError, ValueError):
        return None
    try:
        kills = max(0, min(50, int(kills)))
    except (TypeError, ValueError):
        kills = 0
    return {"place": place, "kills": kills, "dns": False}


def _slot_member_ids(slot: dict) -> list[int]:
    ids = slot.get("member_ids")
    if ids:
        return [int(i) for i in ids]
    return []



@dataclass
class BRTournament:
    """Модель Battle Royale турнира."""

    id: str
    guild_id: int
    channel_id: int
    message_id: int | None = None
    formation: BRTournamentFormation = BRTournamentFormation.RANDOM
    phase: BRTournamentPhase = BRTournamentPhase.REGISTRATION
    format: BRFormat = BRFormat.SQUAD
    players: list[dict] = field(default_factory=list)  # [{"name","id"}] — все участники
    teams: list[dict] = field(default_factory=list)  # слоты/составы (см. _normalize_slot)
    registered_teams: list[str] = field(default_factory=list)  # team_id (squad team mode)
    role_id: int = 0
    room_channel_id: int = 0
    current_map: str = ""
    current_map_index: int = 0
    map_count: int = 3
    map_played: list[bool] = field(default_factory=list)
    team_rename_remaining: dict[int, bool] = field(default_factory=dict)
    room_data: dict[str, Any] = field(default_factory=dict)  # legacy: {"id","password"}
    rooms: dict[str, dict[str, str]] = field(default_factory=dict)  # "{map_idx}": {id,password}
    room_messages: dict[str, int] = field(default_factory=dict)  # card/notify message ids
    results_history: list[dict] = field(default_factory=list)
    results_published: bool = False
    published_at: str = ""
    games_finished_at: str = ""
    # Неизменяемый снимок итогов: publish_pending — заморожен для
    # публикации (редактирование закрыто), results_snapshot —
    # опубликованный/архивный результат (Discord и HoF читают его).
    publish_pending: list[dict] | None = None
    results_snapshot: list[dict] | None = None
    # Замечания миграции legacy-статистики для ведущего.
    migration_notes: list[str] = field(default_factory=list)
    view_page: int = -1  # страница публичной карточки итогов (-1 = сводка)
    cancel_reason: str = ""
    cancelled_by: int = 0
    is_test: bool = False
    legacy_scoring: bool = False
    created_at: str = ""
    team_rename_start_time: str = ""
    organizer_id: int = 0

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid.uuid4())
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if isinstance(self.format, str):
            try:
                self.format = BRFormat(self.format)
            except ValueError:
                self.format = BRFormat.SQUAD
        if isinstance(self.phase, str):
            try:
                self.phase = BRTournamentPhase(self.phase)
            except ValueError:
                self.phase = BRTournamentPhase.REGISTRATION
        n = max(0, int(self.map_count or 0))
        played = list(self.map_played or [])
        self.map_played = (played + [False] * n)[:n] if n else played

    # ---------- правила формата ----------

    @property
    def rules(self) -> dict[str, Any]:
        return br_format_rules(self.format)

    @property
    def slot_size(self) -> int:
        return int(self.rules["size"])

    @property
    def min_slots(self) -> int:
        return int(self.rules["min_slots"])

    @property
    def max_slots(self) -> int:
        return int(self.rules["max_slots"])

    @property
    def min_players(self) -> int:
        return self.min_slots * self.slot_size

    @property
    def max_players(self) -> int:
        return self.max_slots * self.slot_size

    @property
    def place_cap(self) -> int:
        return int(self.rules["place_cap"])

    @property
    def is_solo(self) -> bool:
        return self.format == BRFormat.SOLO

    @property
    def mode(self) -> str:
        """individual — solo и random-жеребьёвка; team — готовые составы."""
        if self.is_solo:
            return "individual"
        return self.formation.value

    @property
    def is_active(self) -> bool:
        return self.phase in ACTIVE_PHASES

    # ---------- карты ----------

    def get_map_sequence(self) -> list[str]:
        """Ротация Бермуды → Чистилище → Калахари с повторениями."""
        return [MAP_ROTATION[i % 3] for i in range(max(0, self.map_count))]

    def get_map_name_for_index(self, index: int) -> str:
        return MAP_ROTATION[index % 3]

    def current_map_name(self) -> str:
        return self.get_map_name_for_index(self.current_map_index)

    def get_stats_key(self, map_index: int) -> str:
        return f"map_{map_index}"

    def get_team_stats_for_map(self, team: dict, map_index: int) -> dict | None:
        stats = team.get("stats") or {}
        entry = stats.get(self.get_stats_key(map_index))
        if entry is None:
            # legacy-ключ по имени карты — только если имя встречается
            # в расписании один раз, иначе очки «переехали» бы на повтор.
            name = self.get_map_name_for_index(map_index)
            if self.get_map_sequence().count(name) == 1:
                entry = stats.get(name)
        return normalize_entry(entry)

    # ---------- слоты ----------

    def filled_slots(self) -> list[dict]:
        """Подтверждённые слоты (без ожидающих приглашений Duo)."""
        return [s for s in self.teams if not s.get("pending_invitee_id")]

    def pending_slots(self) -> list[dict]:
        return [s for s in self.teams if s.get("pending_invitee_id")]

    def slot_member_ids(self, slot: dict) -> list[int]:
        return _slot_member_ids(slot)

    def slot_display_name(self, slot: dict) -> str:
        return slot.get("name") or f"Слот {slot.get('index', 0) + 1}"

    def find_slot_by_user(self, user_id: int) -> tuple[int, dict] | None:
        for i, slot in enumerate(self.teams):
            if user_id in _slot_member_ids(slot):
                return i, slot
        return None

    def find_slot_by_uid(self, uid: str) -> tuple[int, dict] | None:
        for i, slot in enumerate(self.teams):
            if slot.get("uid") == uid:
                return i, slot
        return None

    def player_in_other_active_br(self, user_id: int) -> bool:
        """Игрок уже занят активным BR на этом сервере (кроме себя)."""
        try:
            from storage.br_store import br_store
            for other in br_store.get_by_guild(self.guild_id):
                if other.id == self.id or not other.is_active:
                    continue
                if any(p.get("id") == user_id for p in other.players):
                    return True
                for slot in other.teams:
                    if user_id in _slot_member_ids(slot):
                        return True
        except Exception as exc:  # pragma: no cover - защитный контур
            logger.warning("BR player_in_other_active_br failed: %s", exc)
        return False

    def _display_name_for(self, user_id: int, fallback: str = "") -> str:
        for p in self.players:
            if p.get("id") == user_id:
                return p.get("name") or fallback
        return fallback or f"User_{user_id}"

    def _remember_player(self, name: str, user_id: int) -> None:
        for p in self.players:
            if p.get("id") == user_id:
                p["name"] = name or p.get("name", "")
                return
        self.players.append({"name": name, "id": user_id})
        if not self.organizer_id and len(self.players) == 1:
            self.organizer_id = user_id

    def add_player(self, name: str, user_id: int) -> bool:
        """Совместимый API: добавить участника.

        Solo — создаёт слот; random — в общий пул; team-режимы — False
        (используйте register_team_slot / create_duo_invite).
        """
        ok, _err = self.register_player(name, user_id)
        return ok

    def register_player(self, name: str, user_id: int) -> tuple[bool, str]:
        """Регистрация участника на форматах individual/random.

        Returns:
            (ok, error_message)
        """
        if self.phase != BRTournamentPhase.REGISTRATION:
            return False, "Регистрация закрыта."
        if self.mode == "team":
            return False, "В этом формате регистрируются готовые составы."
        if self.find_slot_by_user(user_id) or any(
            p.get("id") == user_id for p in self.players
        ):
            return False, "Вы уже зарегистрированы."
        if self.player_in_other_active_br(user_id):
            return False, "Вы уже участвуете в другом активном BR на этом сервере."

        if self.is_solo:
            if len(self.filled_slots()) >= self.max_slots:
                return False, f"Лимит участников: {self.max_slots}."
            self._remember_player(name, user_id)
            self.teams.append(self._new_slot(
                name or f"Игрок-{user_id % 10000:04d}",
                [user_id], {user_id: name}, None,
            ))
            return True, ""

        # random duo/squad — пул игроков до жеребьёвки
        if len(self.players) >= self.max_players:
            return False, f"Лимит участников: {self.max_players}."
        self._remember_player(name, user_id)
        return True, ""

    def _purge_players(self, user_ids: list[int]) -> None:
        ids = set(user_ids)
        self.players = [p for p in self.players if p.get("id") not in ids]

    def _new_slot(
        self,
        name: str,
        member_ids: list[int],
        display_names: dict[int, str],
        team_id: str | None,
        captain_id: int | None = None,
    ) -> dict:
        idx = len(self.teams)
        cap = captain_id if captain_id is not None else (member_ids[0] if member_ids else 0)
        cap_name = display_names.get(cap, name)
        member_names = [
            display_names.get(i, self._display_name_for(i)) for i in member_ids
        ]
        return {
            "index": idx,
            "uid": str(uuid.uuid4()),
            "name": name,
            "captain": cap_name,
            "captain_id": cap,
            "member_ids": list(member_ids),
            "display_names": {str(k): v for k, v in display_names.items()},
            "players": [n for n in member_names if n != cap_name],
            "team_id": team_id,
            "pending_invitee_id": None,
            "stats": {},
        }

    def create_duo_invite(
        self, captain_id: int, captain_name: str, invitee_id: int
    ) -> tuple[bool, str, int]:
        """Duo team: капитан создаёт пару с ожидающим напарником.

        Returns:
            (ok, error, slot_index)
        """
        if self.format != BRFormat.DUO or self.mode != "team":
            return False, "Приглашения доступны только в формате Duo (команды).", -1
        if self.phase != BRTournamentPhase.REGISTRATION:
            return False, "Регистрация закрыта.", -1
        if self.find_slot_by_user(captain_id):
            return False, "Вы уже в составе.", -1
        if self.find_slot_by_user(invitee_id):
            return False, "Напарник уже зарегистрирован.", -1
        if invitee_id == captain_id:
            return False, "Нельзя пригласить себя.", -1
        if self.player_in_other_active_br(captain_id) or self.player_in_other_active_br(invitee_id):
            return False, "Один из игроков уже участвует в другом активном BR.", -1
        filled = len(self.filled_slots())
        if filled >= self.max_slots:
            return False, f"Лимит пар: {self.max_slots}.", -1

        self._remember_player(captain_name, captain_id)
        slot = self._new_slot(
            f"Пара {filled + 1}", [captain_id], {captain_id: captain_name}, None,
            captain_id=captain_id,
        )
        slot["pending_invitee_id"] = invitee_id
        slot["invite_expires"] = time.time() + DUO_INVITE_TTL
        self.teams.append(slot)
        return True, "", len(self.teams) - 1

    def respond_duo_invite(
        self, slot_index: int, user_id: int, user_name: str, accept: bool
    ) -> tuple[bool, str]:
        """Напарник подтверждает или отклоняет приглашение."""
        if self.phase != BRTournamentPhase.REGISTRATION:
            return False, "Регистрация закрыта."
        if not (0 <= slot_index < len(self.teams)):
            return False, "Приглашение не найдено."
        slot = self.teams[slot_index]
        if slot.get("pending_invitee_id") != user_id:
            return False, "Это приглашение адресовано другому игроку."
        exp = slot.get("invite_expires") or 0
        if time.time() > exp:
            self.teams.pop(slot_index)
            return False, "Срок приглашения истёк."
        if accept:
            if self.find_slot_by_user(user_id):
                return False, "Вы уже зарегистрированы."
            if self.player_in_other_active_br(user_id):
                return False, "Вы участвуете в другом активном BR."
            filled = len(self.filled_slots())
            if filled >= self.max_slots:
                return False, f"Лимит пар: {self.max_slots}."
            self._remember_player(user_name, user_id)
            slot["member_ids"].append(user_id)
            slot.setdefault("display_names", {})[str(user_id)] = user_name
            slot["players"] = [
                slot["display_names"].get(str(i), self._display_name_for(i))
                for i in slot["member_ids"]
                if i != slot.get("captain_id")
            ]
            slot["pending_invitee_id"] = None
            slot["invite_expires"] = None
            return True, "Заявка подтверждена — пара зарегистрирована."
        self.teams.pop(slot_index)
        return True, "Приглашение отклонено."

    def register_team_slot(
        self,
        team_id: str,
        team_name: str,
        captain_id: int,
        member_ids: list[int],
        display_names: dict[int, str],
    ) -> tuple[bool, str]:
        """Squad team: регистрация готового состава из /team со снимком.

        Returns:
            (ok, error)
        """
        if self.format != BRFormat.SQUAD or self.mode != "team":
            return False, "Готовые составы доступны только в формате Squad (команды)."
        if self.phase != BRTournamentPhase.REGISTRATION:
            return False, "Регистрация закрыта."
        if team_id in self.registered_teams:
            return False, "Команда уже зарегистрирована."
        if len(self.filled_slots()) >= self.max_slots:
            return False, f"Лимит отрядов: {self.max_slots}."
        if len(member_ids) != self.slot_size:
            return False, f"В отряде должно быть {self.slot_size} игрока."
        for uid in member_ids:
            if self.find_slot_by_user(uid):
                return False, f"Игрок <@{uid}> уже зарегистрирован."
            if self.player_in_other_active_br(uid):
                return False, f"<@{uid}> участвует в другом активном BR."
        for uid in member_ids:
            self._remember_player(display_names.get(uid, ""), uid)
        slot = self._new_slot(
            team_name, member_ids, display_names, team_id, captain_id=captain_id
        )
        self.teams.append(slot)
        self.registered_teams.append(team_id)
        return True, ""

    def unregister_team(self, team_id: str) -> bool:
        """Капитан снимает регистрацию готового отряда до старта."""
        if self.phase != BRTournamentPhase.REGISTRATION:
            return False
        if team_id not in self.registered_teams:
            return False
        self.registered_teams.remove(team_id)
        removed = [s for s in self.teams if s.get("team_id") == team_id]
        self.teams = [s for s in self.teams if s.get("team_id") != team_id]
        for s in removed:
            self._purge_players(_slot_member_ids(s))
        return True

    def remove_player(self, user_id: int) -> bool:
        """Участник выходит до старта.

        Solo/Random — убирает игрока из пула/слота. Duo — роспуск пары.
        Squad team — весь отряд выходит (как раньше).
        """
        if self.phase != BRTournamentPhase.REGISTRATION:
            return False
        found = self.find_slot_by_user(user_id)
        if found:
            idx, slot = found
            member_ids = _slot_member_ids(slot)
            if slot.get("pending_invitee_id") and member_ids == [user_id]:
                self.teams.pop(idx)
                self._purge_players(member_ids)
                return True
            # Вышел кто-то из готового состава — освобождаем слот целиком
            team_id = slot.get("team_id")
            self.teams.pop(idx)
            self._purge_players(member_ids)
            if team_id and team_id in self.registered_teams:
                self.registered_teams.remove(team_id)
            return True
        for i, p in enumerate(self.players):
            if p.get("id") == user_id:
                self.players.pop(i)
                return True
        return False

    def kick_slot(self, slot_index: int, reason: str = "") -> bool:
        """Организатор снимает слот с причиной (до старта)."""
        if self.phase != BRTournamentPhase.REGISTRATION:
            return False
        if not (0 <= slot_index < len(self.teams)):
            return False
        slot = self.teams.pop(slot_index)
        team_id = slot.get("team_id")
        self._purge_players(_slot_member_ids(slot))
        if team_id and team_id in self.registered_teams:
            self.registered_teams.remove(team_id)
        self.results_history.append({
            "ts": datetime.now().isoformat(),
            "action": "kick_slot",
            "slot": slot.get("name", ""),
            "reason": reason,
        })
        return True

    def clear_players(self) -> None:
        self.players = []

    # ---------- жеребьёвка / старт ----------

    def draw_error(self) -> str:
        """Проверить готовность к жеребьёвке/старту. '' — можно."""
        if self.mode == "team":
            # legacy squad-team может иметь только registered_teams без слотов
            n = len(self.filled_slots()) or len(self.registered_teams)
            if n < self.min_slots:
                return f"Нужно минимум {self.min_slots} составов (сейчас {n})."
            if n > self.max_slots:
                return f"Лимит составов: {self.max_slots} (сейчас {n})."
            return ""
        # individual/random
        n = len(self.players)
        if self.is_solo:
            n = len(self.filled_slots())
            if n < self.min_slots:
                return (f"Нужно минимум {self.min_slots} игроков "
                        f"(сейчас {n}, добавьте {self.min_slots - n}).")
            return ""
        if n < self.min_players:
            return (f"Нужно минимум {self.min_players} игроков "
                    f"(сейчас {n}, добавьте {self.min_players - n}).")
        if n % self.slot_size != 0:
            need = self.slot_size - (n % self.slot_size)
            return (f"Игроков {n}: для формата нужно кратно {self.slot_size}. "
                    f"Добавьте {need} или снимите {self.slot_size - need}.")
        if n // self.slot_size > self.max_slots:
            return f"Лимит слотов: {self.max_slots} (игроков {n})."
        return ""

    def draw(self) -> tuple[bool, str]:
        """Сформировать слоты. Random — перемешать пул; team — уже есть."""
        err = self.draw_error()
        if err:
            return False, err
        if self.mode == "team":
            # неподтверждённые приглашения Duo не становятся составами
            self.teams = [s for s in self.teams if not s.get("pending_invitee_id")]
            # legacy: слоты могли быть не созданы — достраиваем из /team
            if not self.filled_slots() and self.registered_teams:
                self._materialize_registered_slots()
            for i, s in enumerate(self.teams):
                s["index"] = i
            self.phase = BRTournamentPhase.DRAFT
            self.team_rename_start_time = datetime.now().isoformat()
            return True, ""
        if self.is_solo:
            # Solo: слоты уже созданы по одному, имена не нужны.
            self.phase = BRTournamentPhase.PLAYING
            self._enter_first_map()
            return True, ""
        # random duo/squad
        pool = [p for p in self.players]
        random.shuffle(pool)
        self.teams = []
        self.team_rename_remaining = {}
        for i in range(0, len(pool), self.slot_size):
            group = pool[i:i + self.slot_size]
            ids = [p["id"] for p in group]
            names = {p["id"]: p["name"] for p in group}
            label = "Пара" if self.format == BRFormat.DUO else f"Отряд {i // self.slot_size + 1}"
            self.teams.append(self._new_slot(
                f"{label} {i // self.slot_size + 1}", ids, names, None,
            ))
            self.team_rename_remaining[i // self.slot_size] = True
        self.phase = BRTournamentPhase.DRAFT
        self.team_rename_start_time = datetime.now().isoformat()
        return True, ""

    def _materialize_registered_slots(self) -> None:
        """Legacy squad-team: собрать слоты из registered_teams (/team)."""
        try:
            from storage.team_store import team_store
        except Exception:
            return
        name_by_id = {p.get("id"): p.get("name", "") for p in self.players}
        for team_id in list(self.registered_teams):
            team = team_store.get(team_id)
            if not team:
                continue
            member_ids = [int(m) for m in team.members]
            if len(member_ids) != self.slot_size:
                continue
            names = {uid: (name_by_id.get(uid) or f"User_{uid}") for uid in member_ids}
            slot = self._new_slot(team.team_name, member_ids, names, team_id,
                                  captain_id=team.captain_id)
            self.teams.append(slot)

    def begin_games(self) -> None:
        """Переход из DRAFT к играм («Игры идут»)."""
        if self.phase == BRTournamentPhase.DRAFT:
            self.phase = BRTournamentPhase.PLAYING
            self._enter_first_map()

    def _enter_first_map(self) -> None:
        self.current_map_index = 0
        self.current_map = self.current_map_name()
        if len(self.map_played) < self.map_count:
            self.map_played = (self.map_played + [False] * self.map_count)[: self.map_count]

    def finish_games(self) -> bool:
        """PLAYING → COLLECT: «Все N карт сыграны».

        Единственный переход к приватному вводу результатов.
        Идемпотентно: повторный вызов ничего не меняет.
        """
        if self.phase != BRTournamentPhase.PLAYING:
            return False
        self.phase = BRTournamentPhase.COLLECT
        self.games_finished_at = datetime.now().isoformat()
        return True

    def schedule_lines(self) -> list[str]:
        """Расписание карт для публичной карточки (1..N)."""
        return [
            f"{i + 1}. {MAP_NAMES_RU.get(m, m)}"
            for i, m in enumerate(self.get_map_sequence())
        ]

    def room_credentials(self) -> dict[str, str] | None:
        """Актуальные ID/пароль комнаты (одно на весь турнир)."""
        data = self.room_data or {}
        rid = str(data.get("id") or "").strip()
        pwd = str(data.get("password") or "").strip()
        if not rid or not pwd:
            return None
        return {"id": rid, "password": pwd}

    def can_view_room(self, user_id: int) -> bool:
        """Право видеть данные комнаты.

        Участникам Solo/Duo/Random и всем членам Random-составов; в
        Squad team — только капитанам готовых отрядов.
        """
        found = self.find_slot_by_user(user_id)
        if not found:
            return False
        _idx, slot = found
        if slot.get("pending_invitee_id"):
            return False
        if self.mode == "team" and self.format == BRFormat.SQUAD:
            return slot.get("captain_id") == user_id
        return True

    # ---------- результаты ----------

    def set_result_entry(
        self,
        map_index: int,
        slot_index: int,
        *,
        place: int | None = None,
        kills: int = 0,
        dns: bool = False,
        clear: bool = False,
        by: int = 0,
    ) -> tuple[bool, str]:
        """Записать/изменить/очистить результат слота на карте.

        clear=True удаляет запись (пустая запись ≠ DNS).
        Returns:
            (ok, error)
        """
        if self.phase != BRTournamentPhase.COLLECT:
            return False, "Ввод результатов доступен только после последней карты."
        if self.publish_pending is not None or self.results_snapshot is not None:
            return False, "Итоги зафиксированы для публикации — правки закрыты."
        if not (0 <= map_index < self.map_count):
            return False, "Нет такой карты."
        if not (0 <= slot_index < len(self.teams)):
            return False, "Нет такого слота."
        entry: dict | None
        if clear:
            entry = None
        elif dns:
            entry = {"place": None, "kills": 0, "dns": True}
        else:
            if place is None:
                return False, "Укажите место или отметьте DNS."
            place = int(place)
            if place < 1 or place > self.place_cap:
                return False, f"Место должно быть от 1 до {self.place_cap}."
            kills = int(kills)
            if kills < 0 or kills > 50:
                return False, "Киллы должны быть от 0 до 50."
            # одно место на карту — только одному слоту
            for j, other in enumerate(self.teams):
                if j == slot_index:
                    continue
                oe = normalize_entry((other.get("stats") or {}).get(self.get_stats_key(map_index)))
                if oe and not oe["dns"] and oe["place"] == place:
                    return False, (
                        f"Место {place} на карте {map_index + 1} уже занято "
                        f"слотом «{self.slot_display_name(other)}»."
                    )
            entry = {"place": place, "kills": kills, "dns": False}

        slot = self.teams[slot_index]
        stats = slot.setdefault("stats", {})
        key = self.get_stats_key(map_index)
        old = normalize_entry(stats.get(key))
        if entry is None:
            stats.pop(key, None)
        else:
            stats[key] = dict(entry)
        self.results_history.append({
            "ts": datetime.now().isoformat(),
            "by": by,
            "action": "set_result",
            "map": map_index,
            "slot": self.slot_display_name(slot),
            "old": old,
            "new": entry,
        })
        return True, ""

    def set_results_batch(
        self, map_index: int, entries: list[dict], by: int = 0
    ) -> tuple[bool, list[str]]:
        """Атомарный пакетный ввод по карте.

        entries: [{"slot": int, "place": int|None, "kills": int, "dns": bool}]
        Returns:
            (ok, errors) — при ошибке ничего не сохраняется.
        """
        if self.phase != BRTournamentPhase.COLLECT:
            return False, ["Ввод результатов доступен только после завершения игр."]
        if self.publish_pending is not None or self.results_snapshot is not None:
            return False, ["Итоги зафиксированы для публикации — правки закрыты."]
        errors: list[str] = []
        seen_slots: set[int] = set()
        seen_places: dict[int, int] = {}
        prepared: list[tuple[int, dict]] = []
        for e in entries:
            try:
                si = int(e.get("slot"))
            except (TypeError, ValueError):
                errors.append(f"Строка без номера слота: {e!r}")
                continue
            label = f"слот {si + 1}"
            if not (0 <= si < len(self.teams)):
                errors.append(f"{label}: слот не существует")
                continue
            if si in seen_slots:
                errors.append(f"{label}: слот указан дважды")
                continue
            seen_slots.add(si)
            slot = self.teams[si]
            label = f"слот «{self.slot_display_name(slot)}»"
            if e.get("clear"):
                prepared.append((si, None))
                continue
            if e.get("dns"):
                prepared.append((si, {"place": None, "kills": 0, "dns": True}))
                continue
            try:
                place = int(e.get("place"))
            except (TypeError, ValueError):
                errors.append(f"{label}: нет места (или укажите DNS)")
                continue
            if place < 1 or place > self.place_cap:
                errors.append(f"{label}: место {place} вне диапазона 1–{self.place_cap}")
                continue
            try:
                kills = int(e.get("kills", 0))
            except (TypeError, ValueError):
                errors.append(f"{label}: киллы должны быть целым 0–50")
                continue
            if kills < 0 or kills > 50:
                errors.append(f"{label}: киллы {kills} вне диапазона 0–50")
                continue
            if place in seen_places:
                errors.append(
                    f"{label}: место {place} уже назначено слоту "
                    f"«{self.slot_display_name(self.teams[seen_places[place]])}»"
                )
                continue
            seen_places[place] = si
            prepared.append((si, {"place": place, "kills": kills, "dns": False}))
        if errors:
            return False, errors
        # сверка с уже сохранёнными местами вне пакета
        key = self.get_stats_key(map_index)
        for j, other in enumerate(self.teams):
            if j in seen_slots:
                continue
            oe = normalize_entry((other.get("stats") or {}).get(key))
            if not oe or oe["dns"]:
                continue
            for si, entry in prepared:
                if entry and entry.get("place") == oe["place"]:
                    errors.append(
                        f"место {oe['place']} на карте {map_index + 1} уже занято "
                        f"слотом «{self.slot_display_name(other)}»"
                    )
        if errors:
            return False, errors
        # обмен местами поддерживается: пакет перезаписывает строки
        for si, entry in prepared:
            slot = self.teams[si]
            stats = slot.setdefault("stats", {})
            old = normalize_entry(stats.get(key))
            if entry is None:
                stats.pop(key, None)
            else:
                stats[key] = dict(entry)
            self.results_history.append({
                "ts": datetime.now().isoformat(),
                "by": by,
                "action": "batch_result",
                "map": map_index,
                "slot": self.slot_display_name(slot),
                "old": old,
                "new": entry,
            })
        return True, []

    def results_status(self) -> tuple[int, int]:
        """(заполнено, ожидается) записей по всем картам."""
        expected = len(self.teams) * self.map_count
        filled = 0
        for slot in self.teams:
            for i in range(self.map_count):
                if self.get_team_stats_for_map(slot, i) is not None:
                    filled += 1
        return filled, expected

    def results_complete(self) -> bool:
        filled, expected = self.results_status()
        return expected > 0 and filled >= expected

    # ---------- подсчёт ----------

    def _slot_points(self, slot: dict, map_idx: int) -> int:
        entry = self.get_team_stats_for_map(slot, map_idx)
        if not entry or entry["dns"]:
            return 0
        return PLACE_POINTS.get(entry["place"], 0) + entry["kills"]

    def _slot_boyoos(self, slot: dict) -> int:
        n = 0
        for i in range(self.map_count):
            e = self.get_team_stats_for_map(slot, i)
            if e and not e["dns"] and e["place"] == 1:
                n += 1
        return n

    def _slot_total_kills(self, slot: dict) -> int:
        return sum(
            (self.get_team_stats_for_map(slot, i) or {}).get("kills", 0)
            for i in range(self.map_count)
        )

    def _slot_last_place(self, slot: dict) -> int:
        """Место на последней карте; DNS/пусто — хуже любого сыгранного."""
        e = self.get_team_stats_for_map(slot, self.map_count - 1)
        if not e or e["dns"] or not e["place"]:
            return self.place_cap + 1
        return e["place"]

    def freeze_results(self) -> tuple[bool, str]:
        """Зафиксировать неизменяемый снимок итогов для публикации.

        Вызывается под блокировкой в фазе COLLECT. После фиксации
        правки результатов закрыты; повторный вызов возвращает тот же
        снимок (безопасный повтор/двойной клик).
        """
        if self.phase != BRTournamentPhase.COLLECT:
            return False, "Турнир не в фазе сбора результатов."
        if self.results_published or self.results_snapshot is not None:
            return False, "Итоги уже опубликованы."
        if self.publish_pending is not None:
            return True, ""
        if not self.results_complete():
            filled, expected = self.results_status()
            return False, f"Не все записи заполнены: {filled}/{expected}."
        self.publish_pending = self.calculate_final_results()
        return True, ""

    def commit_publish(self) -> bool:
        """Зафиксировать снимок как опубликованный итог (→ RESULTS).

        Идемпотентно: без publish_pending или не в COLLECT — False.
        """
        if self.phase != BRTournamentPhase.COLLECT or self.publish_pending is None:
            return False
        self.results_snapshot = self.publish_pending
        self.publish_pending = None
        self.phase = BRTournamentPhase.RESULTS
        self.results_published = True
        self.published_at = datetime.now().isoformat()
        self.view_page = -1
        return True

    def calculate_final_results(self) -> list[dict]:
        """Итоговая таблица.

        Если есть зафиксированный снимок (pending или published) —
        возвращается именно он: Discord, архив и Hall of Fame читают
        одни и те же данные. Иначе считается из статистики.
        Новые BR: очки → тай-брейк (Booyah → киллы → место последней
        карты → исходный слот). Legacy-результаты: историческая
        сортировка по очкам.
        """
        snap = self.results_snapshot or self.publish_pending
        if snap is not None:
            return [dict(r) for r in snap]
        results = []
        for i, slot in enumerate(self.teams):
            total = sum(self._slot_points(slot, m) for m in range(self.map_count))
            per_map = {}
            for m in range(self.map_count):
                e = self.get_team_stats_for_map(slot, m)
                if e:
                    per_map[self.get_stats_key(m)] = {
                        "place": e["place"], "kills": e["kills"], "dns": e["dns"],
                        "map": self.get_map_name_for_index(m),
                    }
            results.append({
                "slot_index": i,
                "team_index": i,  # совместимость со старым кодом
                "name": self.slot_display_name(slot),
                "captain": slot.get("captain", ""),
                "member_ids": _slot_member_ids(slot),
                "total_points": total,
                "map_stats": per_map,
                "booyahs": self._slot_boyoos(slot),
                "total_kills": self._slot_total_kills(slot),
                "last_place": self._slot_last_place(slot),
            })
        if self.legacy_scoring:
            results.sort(key=lambda x: x["total_points"], reverse=True)
        else:
            results.sort(key=lambda x: (
                -x["total_points"],
                -x["booyahs"],
                -x["total_kills"],
                x["last_place"],
                x["slot_index"],
            ))
        for rank, r in enumerate(results, 1):
            r["rank"] = rank
        return results

    def winner_slot(self) -> dict | None:
        res = self.calculate_final_results()
        if not res:
            return None
        return self.teams[res[0]["slot_index"]]

    # ---------- rename ----------

    def rename_slot(self, slot_index: int, new_name: str, user_id: int) -> bool:
        """Капитан один раз называет свой слот (Duo/Squad Random)."""
        if self.phase not in (BRTournamentPhase.DRAFT, BRTournamentPhase.PLAYING,
                              BRTournamentPhase.COLLECT):
            return False
        if not (0 <= slot_index < len(self.teams)):
            return False
        if not self.team_rename_remaining.get(slot_index, False):
            return False
        slot = self.teams[slot_index]
        if slot.get("captain_id") != user_id:
            return False
        slot["name"] = new_name
        self.team_rename_remaining[slot_index] = False
        return True

    # ---------- serialize ----------

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "formation": self.formation.value,
            "phase": self.phase.value,
            "format": self.format.value,
            "players": self.players,
            "teams": self.teams,
            "registered_teams": self.registered_teams,
            "role_id": self.role_id,
            "room_channel_id": self.room_channel_id,
            "current_map": self.current_map,
            "current_map_index": self.current_map_index,
            "map_count": self.map_count,
            "map_played": self.map_played,
            "team_rename_remaining": self.team_rename_remaining,
            "room_data": self.room_data,
            "rooms": self.rooms,
            "room_messages": self.room_messages,
            "results_history": self.results_history,
            "results_published": self.results_published,
            "published_at": self.published_at,
            "games_finished_at": self.games_finished_at,
            "publish_pending": self.publish_pending,
            "results_snapshot": self.results_snapshot,
            "migration_notes": self.migration_notes,
            "view_page": self.view_page,
            "cancel_reason": self.cancel_reason,
            "cancelled_by": self.cancelled_by,
            "is_test": self.is_test,
            "legacy_scoring": self.legacy_scoring,
            "created_at": self.created_at,
            "team_rename_start_time": self.team_rename_start_time,
            "organizer_id": self.organizer_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BRTournament:
        """Создать объект из JSON. Старый BR читается как Squad."""
        has_new_schema = "format" in data
        fmt = data.get("format", "squad")
        phase_raw = data.get("phase", "registration")

        # Маппинг legacy-фаз на новые
        phase_map = {
            "bermuda": BRTournamentPhase.PLAYING,
            "purgatory": BRTournamentPhase.PLAYING,
            "kalahari": BRTournamentPhase.PLAYING,
        }
        if phase_raw in phase_map:
            phase = phase_map[phase_raw]
        elif phase_raw == "results":
            # legacy: результаты уже были показаны — считаем опубликованными
            phase = BRTournamentPhase.RESULTS
        else:
            try:
                phase = BRTournamentPhase(phase_raw)
            except ValueError:
                phase = BRTournamentPhase.REGISTRATION

        current_map_index = data.get("current_map_index", 0)
        if current_map_index == 0 and phase_raw != "registration":
            if phase_raw == "purgatory":
                current_map_index = 1
            elif phase_raw == "kalahari":
                current_map_index = 2

        map_count = int(data.get("map_count", 3) or 3)
        map_played = data.get("map_played") or []
        if not map_played:
            map_played = [False] * map_count

        teams = [_normalize_slot(s, i) for i, s in enumerate(data.get("teams", []))]
        teams = [t for t in teams if t]
        # индексы пересчитать
        for i, t in enumerate(teams):
            t["index"] = i

        t = cls(
            id=data.get("id", ""),
            guild_id=data.get("guild_id", 0),
            channel_id=data.get("channel_id", 0),
            message_id=data.get("message_id"),
            formation=BRTournamentFormation(data.get("formation", "random")),
            phase=phase,
            format=fmt,
            players=data.get("players", []),
            teams=teams,
            registered_teams=data.get("registered_teams", []),
            role_id=data.get("role_id", 0),
            room_channel_id=data.get("room_channel_id", 0),
            current_map=data.get("current_map", ""),
            current_map_index=current_map_index,
            map_count=map_count,
            map_played=map_played,
            team_rename_remaining=data.get("team_rename_remaining", {}),
            room_data=data.get("room_data", {}),
            rooms=data.get("rooms", {}),
            room_messages=data.get("room_messages", {}),
            results_history=data.get("results_history", []),
            results_published=bool(data.get(
                "results_published",
                phase in (BRTournamentPhase.RESULTS, BRTournamentPhase.COMPLETE))),
            published_at=data.get("published_at", ""),
            games_finished_at=data.get("games_finished_at", ""),
            publish_pending=data.get("publish_pending"),
            results_snapshot=data.get("results_snapshot"),
            migration_notes=list(data.get("migration_notes") or []),
            view_page=(int(data["view_page"]) if data.get("view_page") is not None else -1),
            cancel_reason=data.get("cancel_reason", ""),
            cancelled_by=data.get("cancelled_by", 0),
            is_test=bool(data.get("is_test", False)),
            # legacy_scoring сохраняем явно: после первого save в JSON
            # появляется "format", и без поля флаг терялся бы при reload.
            legacy_scoring=bool(data.get("legacy_scoring", not has_new_schema)),
            created_at=data.get("created_at", ""),
            team_rename_start_time=data.get("team_rename_start_time", ""),
            organizer_id=data.get("organizer_id", 0),
        )
        # legacy: достроить member_ids из имён, где возможно
        if not has_new_schema:
            t._backfill_member_ids()
        t._migrate_stat_keys()
        return t

    def _migrate_stat_keys(self) -> None:
        """Миграция статистики с ключей по имени карты на map_N.

        - Явный map_N всегда в приоритете — имя-ключ его не перекрывает.
        - Имя, встречающееся в расписании ровно один раз, мигрирует
          в map_N (исходная запись сохраняется, map_N читается первым).
        - Неоднозначный/вне-расписания ключ НЕ копируется: исходная
          запись остаётся, ведущему показывается замечание для сверки.
        Идемпотентно: после миграции map_N существует и повторный
        прогон ничего не меняет.
        """
        seq = self.get_map_sequence()
        notes: list[str] = []
        for slot in self.teams:
            stats = slot.get("stats") or {}
            if not isinstance(stats, dict):
                continue
            label = self.slot_display_name(slot)
            for name in MAP_ROTATION:
                if name not in stats:
                    continue
                idxs = [i for i, m in enumerate(seq) if m == name]
                if len(idxs) == 1:
                    target = self.get_stats_key(idxs[0])
                    if target not in stats:
                        stats[target] = stats[name]
                else:
                    times = "не встречается" if not idxs else f"встречается {len(idxs)} раз(а)"
                    notes.append(
                        f"Слот «{label}»: результат «{MAP_NAMES_RU.get(name, name)}» "
                        f"({name}) не привязан к карте — имя {times} в расписании "
                        f"из {self.map_count}; сверьте и введите вручную."
                    )
        if notes:
            self.migration_notes = notes

    def _backfill_member_ids(self) -> None:
        """Legacy-слоты: восстановить member_ids по именам из players."""
        id_by_name = {p.get("name"): p.get("id") for p in self.players}
        for slot in self.teams:
            if slot.get("member_ids"):
                continue
            ids = []
            names = {}
            for nm in [slot.get("captain", "")] + list(slot.get("players", [])):
                uid = id_by_name.get(nm)
                if uid is not None and uid not in ids:
                    ids.append(uid)
                    names[uid] = nm
            if ids:
                slot["member_ids"] = ids
                slot["captain_id"] = slot.get("captain_id") or (ids[0] if ids else 0)
                slot["display_names"] = {str(k): v for k, v in names.items()}


def _normalize_slot(raw: Any, index: int) -> dict | None:
    """Привести legacy/новый слот к единой схеме."""
    if not isinstance(raw, dict):
        return None
    slot = dict(raw)
    slot.setdefault("index", index)
    slot.setdefault("uid", f"{index}")
    slot.setdefault("name", raw.get("name") or f"Команда {index + 1}")
    slot.setdefault("captain", raw.get("captain", ""))
    slot.setdefault("players", list(raw.get("players", [])))
    slot.setdefault("stats", dict(raw.get("stats", {})))
    slot.setdefault("team_id", raw.get("team_id"))
    slot.setdefault("member_ids", list(raw.get("member_ids", [])))
    slot.setdefault("display_names", dict(raw.get("display_names", {})))
    slot.setdefault("captain_id", raw.get("captain_id", 0))
    slot.setdefault("pending_invitee_id", raw.get("pending_invitee_id"))
    slot.setdefault("invite_expires", raw.get("invite_expires"))
    return slot
