"""Хранилище команд для BR турнира с поддержкой PostgreSQL и JSON fallback."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from config import DATA_DIR, DATABASE_URL
from models.team import Team

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

TEAM_DATA_FILE = DATA_DIR / "teams.json"


class TeamStore:
    """Сохранение и загрузка команд с поддержкой PostgreSQL и JSON fallback."""

    def __init__(self) -> None:
        self._teams: dict[str, Team] = {}  # team_id -> Team
        self._use_postgres = bool(DATABASE_URL)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        """Загрузить все команды из хранилища."""
        if self._use_postgres:
            self._load_from_postgres()
        else:
            self._load_from_json()

    def _load_from_json(self) -> None:
        """Загрузить команды из JSON файла."""
        if not TEAM_DATA_FILE.exists():
            self._teams = {}
            return

        try:
            with open(TEAM_DATA_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)

            loaded = {}
            for team_id, data in raw.items():
                loaded[team_id] = Team.from_dict(data)

            self._teams = loaded
            logger.info("Загружено %d команд из JSON", len(self._teams))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки команд из JSON: %s", exc)
            self._teams = {}

    async def _load_from_postgres(self) -> None:
        """Загрузить команды из PostgreSQL."""
        try:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("SELECT * FROM teams")
                loaded = {}
                for row in rows:
                    data = {
                        "team_id": row["team_id"],
                        "team_name": row["team_name"],
                        "password": row["password"],
                        "captain_id": row["captain_id"],
                        "guild_id": row["guild_id"],
                        "members": row["members"],
                        "created_at": row["created_at"].isoformat() if row["created_at"] else "",
                    }
                    loaded[row["team_id"]] = Team.from_dict(data)
                self._teams = loaded
                logger.info("Загружено %d команд из PostgreSQL", len(self._teams))
        except Exception as exc:
            logger.error("Ошибка загрузки команд из PostgreSQL: %s", exc)
            self._teams = {}

    def _save(self) -> None:
        """Сохранить все команды в хранилище."""
        if self._use_postgres:
            # PostgreSQL saves are done immediately on each operation
            pass
        else:
            self._save_to_json()

    def _save_to_json(self) -> None:
        """Сохранить команды в JSON файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {team_id: team.to_dict() for team_id, team in self._teams.items()}
        with open(TEAM_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)

    async def _save_to_postgres(self, team: Team) -> None:
        """Сохранить команду в PostgreSQL."""
        try:
            from storage.db import get_pool
            from datetime import datetime
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Convert created_at string to datetime if needed
                created_at = team.created_at
                if isinstance(created_at, str):
                    try:
                        created_at = datetime.fromisoformat(created_at)
                    except ValueError:
                        created_at = datetime.now()

                await conn.execute("""
                    INSERT INTO teams (team_id, team_name, password, captain_id, guild_id, members, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    ON CONFLICT (team_id) DO UPDATE SET
                        team_name = EXCLUDED.team_name,
                        password = EXCLUDED.password,
                        captain_id = EXCLUDED.captain_id,
                        guild_id = EXCLUDED.guild_id,
                        members = EXCLUDED.members,
                        created_at = EXCLUDED.created_at
                """, team.team_id, team.team_name, team.password, team.captain_id,
                    team.guild_id, team.members, created_at)
        except Exception as exc:
            logger.error("Ошибка сохранения команды в PostgreSQL: %s", exc)

    async def _delete_from_postgres(self, team_id: str) -> None:
        """Удалить команду из PostgreSQL."""
        try:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM teams WHERE team_id = $1", team_id)
        except Exception as exc:
            logger.error("Ошибка удаления команды из PostgreSQL: %s", exc)

    async def create(self, team: Team) -> Team:
        """Создать новую команду.

        Args:
            team: Объект команды

        Returns:
            Созданная команда
        """
        self._teams[team.team_id] = team
        if self._use_postgres:
            await self._save_to_postgres(team)
        else:
            self._save_to_json()
        return team

    def get(self, team_id: str) -> Team | None:
        """Получить команду по ID.

        Args:
            team_id: ID команды

        Returns:
            Объект команды или None
        """
        return self._teams.get(team_id)

    def get_by_guild(self, guild_id: int) -> list[Team]:
        """Получить все команды сервера.

        Args:
            guild_id: ID сервера

        Returns:
            Список команд
        """
        return [team for team in self._teams.values() if team.guild_id == guild_id]

    def get_by_name(self, guild_id: int, team_name: str) -> Team | None:
        """Получить команду по названию в сервере.

        Args:
            guild_id: ID сервера
            team_name: Название команды

        Returns:
            Объект команды или None
        """
        for team in self._teams.values():
            if team.guild_id == guild_id and team.team_name == team_name:
                return team
        return None

    def get_by_captain(self, captain_id: int) -> Team | None:
        """Получить команду по капитану.

        Args:
            captain_id: ID капитана

        Returns:
            Объект команды или None
        """
        for team in self._teams.values():
            if team.captain_id == captain_id:
                return team
        return None

    def get_by_member(self, user_id: int) -> Team | None:
        """Получить команду по члену.

        Args:
            user_id: ID пользователя

        Returns:
            Объект команды или None
        """
        for team in self._teams.values():
            if user_id in team.members:
                return team
        return None

    async def delete(self, team_id: str) -> bool:
        """Удалить команду.

        Args:
            team_id: ID команды

        Returns:
            True если успешно, False если команда не найдена
        """
        if team_id not in self._teams:
            return False

        del self._teams[team_id]
        if self._use_postgres:
            await self._delete_from_postgres(team_id)
        else:
            self._save_to_json()
        return True

    async def add_member(self, team_id: str, user_id: int) -> bool:
        """Добавить члена в команду.

        Args:
            team_id: ID команды
            user_id: ID пользователя

        Returns:
            True если успешно, False если команда не найдена, лимит превышен или уже в команде
        """
        team = self._teams.get(team_id)
        if not team:
            return False

        if not team.add_member(user_id):
            return False

        if self._use_postgres:
            await self._save_to_postgres(team)
        else:
            self._save_to_json()
        return True

    async def remove_member(self, team_id: str, user_id: int) -> bool:
        """Удалить члена из команды.

        Args:
            team_id: ID команды
            user_id: ID пользователя

        Returns:
            True если успешно, False если команда не найдена, пользователь не найден или это капитан
        """
        team = self._teams.get(team_id)
        if not team:
            return False

        if not team.remove_member(user_id):
            return False

        if self._use_postgres:
            await self._save_to_postgres(team)
        else:
            self._save_to_json()
        return True

    def is_member(self, team_id: str, user_id: int) -> bool:
        """Проверить, является ли пользователь членом команды.

        Args:
            team_id: ID команды
            user_id: ID пользователя

        Returns:
            True если пользователь в команде
        """
        team = self._teams.get(team_id)
        if not team:
            return False
        return team.is_member(user_id)

    def is_captain(self, team_id: str, user_id: int) -> bool:
        """Проверить, является ли пользователь капитаном команды.

        Args:
            team_id: ID команды
            user_id: ID пользователя

        Returns:
            True если пользователь капитан
        """
        team = self._teams.get(team_id)
        if not team:
            return False
        return team.is_captain(user_id)

    async def update_team(self, team: Team) -> bool:
        """Обновить команду.

        Args:
            team: Объект команды

        Returns:
            True если успешно
        """
        if team.team_id not in self._teams:
            return False

        self._teams[team.team_id] = team
        if self._use_postgres:
            await self._save_to_postgres(team)
        else:
            self._save_to_json()
        return True


# Глобальный экземпляр хранилища
team_store = TeamStore()
