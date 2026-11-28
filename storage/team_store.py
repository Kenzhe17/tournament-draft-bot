"""Хранилище команд (Squad Hub 2.0) — PostgreSQL + JSON fallback.

PostgreSQL — источник истины. Критические операции (вступление,
исключение, передача капитанства, расформирование) выполняются
в транзакциях с блокировками строк и advisory-lock на пользователя,
поэтому гарантии держатся без опоры на asyncio.Lock.

Синхронные геттеры работают по кэшу ``_teams``, который обновляется
только после успешной записи в БД.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from config import DATA_DIR, DATABASE_URL
from models.team import Team, TEAM_MAX_MEMBERS
from storage.json_atomic import atomic_write_json

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

TEAM_DATA_FILE = DATA_DIR / "teams.json"

# Новые колонки teams (добавлены миграцией squad_schema)
_TEAM_COLS = (
    "team_id, team_name, password, password_hash, captain_id, guild_id, "
    "members, join_mode, recruiting, purpose, description, requirements, "
    "logo_url, created_at, updated_at, status_message_id, "
    "recruitment_channel_id, recruitment_message_id"
)


class TeamStoreError(Exception):
    """Ошибка операции с командой. ``code`` — машинный код причины."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class TeamStore:
    """Сохранение и загрузка команд с поддержкой PostgreSQL и JSON fallback."""

    def __init__(self) -> None:
        self._teams: dict[str, Team] = {}  # team_id -> Team
        # Снимок состава/капитана на момент последней успешной
        # операции — защита update_team от перезаписи roster
        # при алиасинге кэш-объектов.
        self._roster: dict[str, tuple[tuple[int, ...], int]] = {}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def _remember(self, team: Team) -> None:
        self._roster[team.team_id] = (
            tuple(team.members), team.captain_id)

    def _forget(self, team_id: str) -> None:
        self._roster.pop(team_id, None)

    @staticmethod
    def _pg_mode() -> bool:
        """PostgreSQL активен? Читаем dbmod.DATABASE_URL динамически —
        тесты подменяют его после импорта модуля."""
        try:
            import storage.db as dbmod
            return bool(dbmod.DATABASE_URL)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Загрузка / кэш
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Загрузить все команды из хранилища."""
        if self._pg_mode():
            # PostgreSQL loading is deferred to async context
            self._teams = {}
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
            for _t in loaded.values():
                self._remember(_t)
            logger.info("Загружено %d команд из JSON", len(self._teams))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.error("Ошибка загрузки команд из JSON: %s", exc)
            self._teams = {}

    @staticmethod
    def _row_to_team(row) -> Team:
        """asyncpg.Record -> Team (с миграцией legacy-пароля)."""
        data = {k: row[k] for k in row.keys()}
        members = data.get("members") or []
        data["members"] = [int(m) for m in members]
        for k in ("created_at", "updated_at"):
            v = data.get(k)
            if v is not None and hasattr(v, "isoformat"):
                data[k] = v.isoformat()
        for k in ("recruitment_channel_id", "recruitment_message_id",
                  "status_message_id"):
            v = data.get(k)
            data[k] = int(v) if v is not None else None
        req = data.get("requirements")
        if isinstance(req, str):
            try:
                data["requirements"] = json.loads(req)
            except (ValueError, TypeError):
                data["requirements"] = {}
        elif req is None:
            data["requirements"] = {}
        return Team.from_dict(data)

    async def load_from_postgres(self) -> None:
        """Async method to load teams from PostgreSQL after startup."""
        if not self._pg_mode():
            return
        try:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(f"SELECT {_TEAM_COLS} FROM teams")
                loaded = {}
                migrated = []
                for row in rows:
                    team = self._row_to_team(row)
                    loaded[row["team_id"]] = team
                    # Миграция legacy plaintext-пароля: from_dict
                    # захешировал его — персистим hash и затираем
                    # plaintext в БД (идемпотентно).
                    if row["password"] and not row["password_hash"]:
                        migrated.append(team)
                self._teams = loaded
                for _t in loaded.values():
                    self._remember(_t)
                for team in migrated:
                    try:
                        await self._upsert_pg(conn, team)
                    except Exception as exc:
                        logger.warning(
                            "password migration persist failed "
                            "for %s: %s", team.team_id, exc)
                if migrated:
                    logger.info("Migrated %d legacy team passwords",
                                len(migrated))
                logger.info("Загружено %d команд из PostgreSQL", len(self._teams))
        except Exception as exc:
            logger.error("Ошибка загрузки команд из PostgreSQL: %s", exc)
            self._teams = {}

    def _save(self) -> None:
        """Сохранить все команды в хранилище (JSON-режим)."""
        if not self._pg_mode():
            self._save_to_json()

    def _save_to_json(self) -> None:
        """Сохранить команды в JSON файл."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = {team_id: team.to_dict() for team_id, team in self._teams.items()}
        atomic_write_json(TEAM_DATA_FILE, raw)

    # ------------------------------------------------------------------
    # PostgreSQL write helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _pg() -> "asyncpg.Connection":
        from storage.db import get_pool
        return await get_pool()

    @classmethod
    async def _upsert_pg(cls, conn: "asyncpg.Connection", team: Team) -> None:
        """INSERT/UPDATE строки teams (вызывать внутри транзакции или
        на acquired connection)."""
        import json as _json
        from datetime import datetime

        def _ts(v):
            if isinstance(v, datetime):
                return v
            if isinstance(v, str) and v:
                try:
                    return datetime.fromisoformat(v)
                except ValueError:
                    pass
            return datetime.now()

        await conn.execute(
            f"""
            INSERT INTO teams ({_TEAM_COLS})
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
            ON CONFLICT (team_id) DO UPDATE SET
                team_name = EXCLUDED.team_name,
                password = EXCLUDED.password,
                password_hash = EXCLUDED.password_hash,
                captain_id = EXCLUDED.captain_id,
                guild_id = EXCLUDED.guild_id,
                members = EXCLUDED.members,
                join_mode = EXCLUDED.join_mode,
                recruiting = EXCLUDED.recruiting,
                purpose = EXCLUDED.purpose,
                description = EXCLUDED.description,
                requirements = EXCLUDED.requirements,
                logo_url = EXCLUDED.logo_url,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                status_message_id = EXCLUDED.status_message_id,
                recruitment_channel_id = EXCLUDED.recruitment_channel_id,
                recruitment_message_id = EXCLUDED.recruitment_message_id
            """,
            team.team_id, team.team_name, team.password,
            team.password_hash, team.captain_id, team.guild_id,
            list(team.members), team.join_mode, team.recruiting,
            team.purpose, team.description,
            _json.dumps(team.requirements or {}), team.logo_url,
            _ts(team.created_at), _ts(team.updated_at),
            team.status_message_id, team.recruitment_channel_id,
            team.recruitment_message_id,
        )

    async def _delete_pg(self, conn: "asyncpg.Connection", team_id: str) -> None:
        await conn.execute("DELETE FROM teams WHERE team_id = $1", team_id)

    # ------------------------------------------------------------------
    # Создание / обновление / удаление
    # ------------------------------------------------------------------

    async def create(self, team: Team) -> Team:
        """Создать новую команду.

        Raises:
            TeamStoreError: ``name_taken`` — название занято,
                ``name_invalid`` — невалидное название,
                ``already_in_team`` — капитан уже в команде.
        """
        from models.team import validate_team_name
        err = validate_team_name(team.team_name)
        if err:
            raise TeamStoreError("name_invalid", err)

        existing = self.get_by_member(team.captain_id)
        if existing is not None and existing.guild_id == team.guild_id:
            raise TeamStoreError("already_in_team",
                                 "Вы уже состоите в команде этого сервера")

        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # advisory lock на (guild,user): сериализуем создание
                    # и вступление одного пользователя в этом guild
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended($1, 0))",
                        f"squad-member:{team.guild_id}:{team.captain_id}",
                    )
                    clash = await conn.fetchval(
                        "SELECT team_id FROM teams WHERE guild_id = $1 "
                        "AND lower(team_name) = lower($2)",
                        team.guild_id, team.team_name,
                    )
                    if clash:
                        raise TeamStoreError(
                            "name_taken",
                            "Команда с таким названием уже существует")
                    member_clash = await conn.fetchval(
                        "SELECT team_id FROM teams WHERE guild_id = $1 "
                        "AND $2 = ANY(members)",
                        team.guild_id, team.captain_id,
                    )
                    if member_clash:
                        raise TeamStoreError(
                            "already_in_team",
                            "Вы уже состоите в команде этого сервера")
                    await self._upsert_pg(conn, team)
        else:
            for t in self._teams.values():
                if (t.guild_id == team.guild_id
                        and t.team_name.lower() == team.team_name.lower()):
                    raise TeamStoreError(
                        "name_taken",
                        "Команда с таким названием уже существует")

        self._teams[team.team_id] = team
        self._remember(team)
        self._save()
        return team

    def get(self, team_id: str) -> Team | None:
        """Получить команду по ID."""
        return self._teams.get(team_id)

    async def refresh_from_db(self, team_id: str) -> Team | None:
        """Refresh team from PostgreSQL database."""
        if self._pg_mode():
            try:
                from storage.db import get_pool
                pool = await get_pool()
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        f"SELECT {_TEAM_COLS} FROM teams WHERE team_id = $1",
                        team_id)
                    if row:
                        team = self._row_to_team(row)
                        self._teams[team_id] = team
                        self._remember(team)
                        return team
            except Exception as exc:
                logger.error("Ошибка загрузки команды из PostgreSQL: %s", exc)
        return self._teams.get(team_id)

    def get_by_guild(self, guild_id: int) -> list[Team]:
        """Получить все команды сервера."""
        return [t for t in self._teams.values() if t.guild_id == guild_id]

    def get_by_name(self, guild_id: int, team_name: str) -> Team | None:
        """Получить команду по названию в сервере (без учёта регистра)."""
        name = (team_name or "").strip().lower()
        for team in self._teams.values():
            if team.guild_id == guild_id and team.team_name.lower() == name:
                return team
        return None

    def get_by_captain(self, captain_id: int) -> Team | None:
        """Получить команду по капитану."""
        for team in self._teams.values():
            if team.captain_id == captain_id:
                return team
        return None

    def get_by_member(self, user_id: int) -> Team | None:
        """Получить команду по члену (по всем серверам)."""
        for team in self._teams.values():
            if user_id in team.members:
                return team
        return None

    def get_by_member_in_guild(self, guild_id: int,
                               user_id: int) -> Team | None:
        """Команда пользователя внутри конкретного сервера."""
        for team in self._teams.values():
            if team.guild_id == guild_id and user_id in team.members:
                return team
        return None

    async def delete(self, team_id: str) -> bool:
        """Удалить команду (legacy API; закрывает приглашения/заявки
        перед удалением — FK без каскада)."""
        if team_id not in self._teams:
            return False
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "DELETE FROM team_invites WHERE team_id=$1",
                        team_id)
                    await conn.execute(
                        "DELETE FROM team_applications WHERE team_id=$1",
                        team_id)
                    await self._delete_pg(conn, team_id)
        del self._teams[team_id]
        self._forget(team_id)
        self._save()
        return True

    # ------------------------------------------------------------------
    # Обновление метаданных
    # ------------------------------------------------------------------

    async def update_team(self, team: Team) -> bool:
        """Обновить метаданные команды (название, описание, настройки
        вступления и т.п.). Изменение состава/капитана через этот
        метод не допускается — используйте атомарные операции.

        Returns False, если команда зарегистрирована в активном
        турнире и попытка меняет защищённые поля.
        """
        team.updated_at = _now_iso()
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    # Актуальное состояние строки под блокировкой —
                    # защита от устаревших View и параллельных правок.
                    row = await conn.fetchrow(
                        f"SELECT {_TEAM_COLS} FROM teams "
                        "WHERE team_id = $1 FOR UPDATE", team.team_id)
                    if row is None:
                        return False
                    old = self._row_to_team(row)
                    if (sorted(old.members) != sorted(team.members)
                            or old.captain_id != team.captain_id):
                        # Восстанавливаем кэш-объект, чтобы мутация
                        # не «протекла» в get().
                        team.members = list(old.members)
                        team.captain_id = old.captain_id
                        raise TeamStoreError(
                            "roster_locked",
                            "Состав и капитан меняются только через "
                            "операции управления командой")
                    # Состав/капитан всегда берём из БД — update_team
                    # не должен затирать параллельные изменения.
                    team.members = list(old.members)
                    team.captain_id = old.captain_id
                    clash = await conn.fetchval(
                        "SELECT team_id FROM teams WHERE guild_id = $1 "
                        "AND lower(team_name) = lower($2) "
                        "AND team_id <> $3",
                        team.guild_id, team.team_name, team.team_id)
                    if clash:
                        raise TeamStoreError(
                            "name_taken",
                            "Команда с таким названием уже существует")
                    await self._upsert_pg(conn, team)
        else:
            old = self._teams.get(team.team_id)
            if old is None:
                return False
            snap = self._roster.get(team.team_id)
            if snap is not None and (
                    tuple(team.members) != snap[0]
                    or team.captain_id != snap[1]):
                team.members = list(snap[0])
                team.captain_id = snap[1]
                raise TeamStoreError(
                    "roster_locked",
                    "Состав и капитан меняются только через "
                    "операции управления командой")
            team.members = list(snap[0]) if snap else list(old.members)
            team.captain_id = snap[1] if snap else old.captain_id
            for t in self._teams.values():
                if (t.team_id != team.team_id
                        and t.guild_id == team.guild_id
                        and t.team_name.lower() == team.team_name.lower()):
                    raise TeamStoreError(
                        "name_taken",
                        "Команда с таким названием уже существует")

        self._teams[team.team_id] = team
        self._remember(team)
        self._save()
        return True

    # ------------------------------------------------------------------
    # Атомарные операции с составом (PostgreSQL)
    # ------------------------------------------------------------------

    @staticmethod
    def _member_lock_key(guild_id: int, user_id: int) -> str:
        return f"squad-member:{guild_id}:{user_id}"

    async def _lock_team_row(self, conn: "asyncpg.Connection",
                             team_id: str):
        return await conn.fetchrow(
            f"SELECT {_TEAM_COLS} FROM teams WHERE team_id = $1 FOR UPDATE",
            team_id)

    async def _team_in_active_tournament(self, guild_id: int,
                                         team_id: str) -> bool:
        from services.squad_service import is_team_in_active_tournament
        return await is_team_in_active_tournament(guild_id, team_id)

    async def join_team(self, team_id: str, user_id: int,
                        guild_id: int) -> Team:
        """Атомарно добавить пользователя в команду.

        Гарантии (в транзакции):
        - один пользователь — максимум одна команда в guild
          (advisory lock на (guild,user) + проверка всех команд);
        - максимум TEAM_MAX_MEMBERS участников (FOR UPDATE на строке
          команды);
        - команда существует, набор открыт и не заполнен.

        Raises TeamStoreError: team_not_found, team_full, already_in_team,
        recruiting_closed, roster_locked, user_in_other_team, db_error.
        """
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT pg_advisory_xact_lock("
                        "hashtextextended($1, 0))",
                        self._member_lock_key(guild_id, user_id),
                    )
                    row = await self._lock_team_row(conn, team_id)
                    if row is None or int(row["guild_id"]) != int(guild_id):
                        raise TeamStoreError("team_not_found",
                                             "Команда не найдена")
                    team = self._row_to_team(row)
                    if not team.recruiting:
                        raise TeamStoreError("recruiting_closed",
                                             "Набор в команду закрыт")
                    if team.is_full():
                        raise TeamStoreError("team_full",
                                             "Команда заполнена")
                    if user_id in team.members:
                        raise TeamStoreError("already_in_team",
                                             "Вы уже в этой команде")
                    other = await conn.fetchval(
                        "SELECT team_id FROM teams WHERE guild_id = $1 "
                        "AND $2 = ANY(members) AND team_id <> $3",
                        guild_id, user_id, team_id)
                    if other:
                        raise TeamStoreError(
                            "user_in_other_team",
                            "Вы уже состоите в другой команде")
                    if await self._team_in_active_tournament(
                            guild_id, team_id):
                        raise TeamStoreError(
                            "roster_locked",
                            "Команда зарегистрирована в активном "
                            "турнире — состав менять нельзя")
                    members = list(team.members) + [user_id]
                    await conn.execute(
                        "UPDATE teams SET members = $2, updated_at = now() "
                        "WHERE team_id = $1",
                        team_id, members)
                    team.members = members
                    team.updated_at = _now_iso()
                    self._teams[team_id] = team
                    self._remember(team)
            return team

        # JSON fallback — без транзакций, best-effort
        team = self._teams.get(team_id)
        if team is None or team.guild_id != guild_id:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if not team.recruiting:
            raise TeamStoreError("recruiting_closed",
                                 "Набор в команду закрыт")
        if team.is_full():
            raise TeamStoreError("team_full", "Команда заполнена")
        if team.is_member(user_id):
            raise TeamStoreError("already_in_team", "Вы уже в этой команде")
        for t in self._teams.values():
            if t.guild_id == guild_id and user_id in t.members:
                raise TeamStoreError(
                    "user_in_other_team",
                    "Вы уже состоите в другой команде")
        team.add_member(user_id)
        team.updated_at = _now_iso()
        self._save_to_json()
        return team

    async def remove_member(self, team_id: str, user_id: int,
                            *, actor_id: int | None = None,
                            guild_id: int | None = None) -> Team:
        """Атомарно исключить/вывести участника (не капитана)."""
        gid = guild_id
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await self._lock_team_row(conn, team_id)
                    if row is None:
                        raise TeamStoreError("team_not_found",
                                             "Команда не найдена")
                    team = self._row_to_team(row)
                    gid = int(team.guild_id)
                    if user_id == team.captain_id:
                        raise TeamStoreError(
                            "is_captain",
                            "Капитана нельзя исключить — "
                            "сначала передайте капитанство")
                    if user_id not in team.members:
                        raise TeamStoreError("not_member",
                                             "Игрок не в команде")
                    if await self._team_in_active_tournament(
                            gid, team_id):
                        raise TeamStoreError(
                            "roster_locked",
                            "Команда зарегистрирована в активном "
                            "турнире — состав менять нельзя")
                    members = [m for m in team.members if m != user_id]
                    await conn.execute(
                        "UPDATE teams SET members = $2, updated_at = now() "
                        "WHERE team_id = $1",
                        team_id, members)
                    team.members = members
                    team.updated_at = _now_iso()
                    self._teams[team_id] = team
                    self._remember(team)
            return team

        team = self._teams.get(team_id)
        if team is None:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if not team.remove_member(user_id):
            raise TeamStoreError("not_member", "Игрок не в команде")
        team.updated_at = _now_iso()
        self._save_to_json()
        return team

    async def leave_team(self, team_id: str, user_id: int) -> Team:
        """Участник покидает команду. Капитан должен сначала передать
        капитанство или расформировать команду."""
        team = self._teams.get(team_id)
        if team is not None and team.is_captain(user_id):
            raise TeamStoreError(
                "captain_must_transfer",
                "Капитан не может покинуть команду — передайте "
                "капитанство или расформируйте команду")
        return await self.remove_member(team_id, user_id)

    async def transfer_captain(self, team_id: str, new_captain_id: int,
                               actor_id: int) -> Team:
        """Передать капитанство участнику команды.

        ``actor_id`` — текущий капитан. Новый капитан должен подтвердить
        передачу отдельно (см. services.squad_service.pending_transfers).
        """
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await self._lock_team_row(conn, team_id)
                    if row is None:
                        raise TeamStoreError("team_not_found",
                                             "Команда не найдена")
                    team = self._row_to_team(row)
                    gid = int(team.guild_id)
                    if team.captain_id != actor_id:
                        raise TeamStoreError("not_captain",
                                             "Только капитан может "
                                             "передать капитанство")
                    if new_captain_id not in team.members:
                        raise TeamStoreError("not_member",
                                             "Игрок не в команде")
                    if new_captain_id == team.captain_id:
                        raise TeamStoreError("same_captain",
                                             "Игрок уже капитан")
                    if await self._team_in_active_tournament(
                            gid, team_id):
                        raise TeamStoreError(
                            "roster_locked",
                            "Команда зарегистрирована в активном "
                            "турнире — передача капитанства "
                            "заблокирована")
                    await conn.execute(
                        "UPDATE teams SET captain_id = $2, "
                        "updated_at = now() WHERE team_id = $1",
                        team_id, new_captain_id)
                    team.captain_id = new_captain_id
                    team.updated_at = _now_iso()
                    self._teams[team_id] = team
                    self._remember(team)
            return team

        team = self._teams.get(team_id)
        if team is None:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if team.captain_id != actor_id:
            raise TeamStoreError("not_captain",
                                 "Только капитан может передать капитанство")
        if new_captain_id not in team.members:
            raise TeamStoreError("not_member", "Игрок не в команде")
        if new_captain_id == team.captain_id:
            raise TeamStoreError("same_captain", "Игрок уже капитан")
        team.captain_id = new_captain_id
        team.updated_at = _now_iso()
        self._save_to_json()
        return team

    async def confirm_captain_transfer(self, team_id: str,
                                       new_captain_id: int) -> Team:
        """Новый капитан подтверждает передачу (после запроса старого
        капитана — pending хранится в squad_service)."""
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await self._lock_team_row(conn, team_id)
                    if row is None:
                        raise TeamStoreError("team_not_found",
                                             "Команда не найдена")
                    team = self._row_to_team(row)
                    gid = int(team.guild_id)
                    if new_captain_id not in team.members:
                        raise TeamStoreError("not_member",
                                             "Игрок не в команде")
                    if new_captain_id == team.captain_id:
                        raise TeamStoreError("same_captain",
                                             "Игрок уже капитан")
                    if await self._team_in_active_tournament(
                            gid, team_id):
                        raise TeamStoreError(
                            "roster_locked",
                            "Команда зарегистрирована в активном "
                            "турнире — передача капитанства "
                            "заблокирована")
                    await conn.execute(
                        "UPDATE teams SET captain_id = $2, "
                        "updated_at = now() WHERE team_id = $1",
                        team_id, new_captain_id)
                    team.captain_id = new_captain_id
                    team.updated_at = _now_iso()
                    self._teams[team_id] = team
                    self._remember(team)
            return team

        team = self._teams.get(team_id)
        if team is None:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if new_captain_id not in team.members:
            raise TeamStoreError("not_member", "Игрок не в команде")
        if new_captain_id == team.captain_id:
            raise TeamStoreError("same_captain", "Игрок уже капитан")
        team.captain_id = new_captain_id
        team.updated_at = _now_iso()
        self._save_to_json()
        return team

    async def disband(self, team_id: str, actor_id: int) -> str:
        """Атомарно расформировать команду.

        Возвращает team_name. Закрывает приглашения/заявки/LFG
        через squad_store (вызывается сервисным слоем).
        """
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await self._lock_team_row(conn, team_id)
                    if row is None:
                        raise TeamStoreError("team_not_found",
                                             "Команда не найдена")
                    team = self._row_to_team(row)
                    gid = int(team.guild_id)
                    if team.captain_id != actor_id:
                        raise TeamStoreError("not_captain",
                                             "Только капитан может "
                                             "расформировать команду")
                    if await self._team_in_active_tournament(
                            gid, team_id):
                        raise TeamStoreError(
                            "roster_locked",
                            "Команда зарегистрирована в активном "
                            "турнире — расформирование заблокировано")
                    # История действий остаётся в team_audit — строки
                    # приглашений/заявок удаляем, чтобы не блокировать
                    # удаление команды по FK.
                    await conn.execute(
                        "DELETE FROM team_invites WHERE team_id = $1",
                        team_id)
                    await conn.execute(
                        "DELETE FROM team_applications WHERE team_id = $1",
                        team_id)
                    await self._delete_pg(conn, team_id)
                    name = team.team_name
                    self._teams.pop(team_id, None)
            return name

        team = self._teams.get(team_id)
        if team is None:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if team.captain_id != actor_id:
            raise TeamStoreError("not_captain",
                                 "Только капитан может расформировать команду")
        name = team.team_name
        del self._teams[team_id]
        self._forget(team_id)
        self._save_to_json()
        return name

    # ------------------------------------------------------------------
    # Legacy-совместимость (BR/cashcup вызывают синхронно)
    # ------------------------------------------------------------------

    def is_member(self, team_id: str, user_id: int) -> bool:
        team = self._teams.get(team_id)
        return bool(team and team.is_member(user_id))

    def is_captain(self, team_id: str, user_id: int) -> bool:
        team = self._teams.get(team_id)
        return bool(team and team.is_captain(user_id))

    # ------------------------------------------------------------------
    # Легаси-методы, сохранённые для обратной совместимости вызовов
    # ------------------------------------------------------------------

    async def _save_to_postgres(self, team: Team) -> None:
        """Legacy: сохранение одной команды (не используется новым кодом)."""
        if not self._pg_mode():
            return
        try:
            pool = await self._pg()
            async with pool.acquire() as conn:
                await self._upsert_pg(conn, team)
        except Exception as exc:
            logger.error("Ошибка сохранения команды в PostgreSQL: %s", exc)

    async def _delete_from_postgres(self, team_id: str) -> None:
        if not self._pg_mode():
            return
        try:
            pool = await self._pg()
            async with pool.acquire() as conn:
                await self._delete_pg(conn, team_id)
        except Exception as exc:
            logger.error("Ошибка удаления команды из PostgreSQL: %s", exc)


# Глобальный экземпляр хранилища
team_store = TeamStore()


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().isoformat()
