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
        """Legacy-вариант для вызовов вне транзакции (best-effort)."""
        from services.squad_service import is_team_in_active_tournament
        return await is_team_in_active_tournament(guild_id, team_id)

    # ------------------------------------------------------------------
    # Fail-closed проверка активных турниров ВНУТРИ транзакции.
    # Не использует вложенных acquire из пула: BR — in-memory,
    # Cash Cup — прямой SQL на том же conn с FOR UPDATE (сериализует
    # с cashcup_store.mutate, который тоже берёт строку FOR UPDATE).
    # ------------------------------------------------------------------

    @staticmethod
    async def _tournament_blocked_on(conn: "asyncpg.Connection",
                                    guild_id: int,
                                    team_id: str) -> str | None:
        """None — команда свободна; str — текст причины блокировки.

        Любая ошибка проверки (кроме отсутствия таблицы cashcup)
        → TeamStoreError("check_failed") — fail-closed: опасная
        операция запрещена до восстановления проверки."""
        # BR — in-memory store, соединений не требует
        try:
            from models.br_tournament import BRTournamentPhase
            from storage.br_store import br_store
            for brt in br_store.all():
                if (brt and not getattr(brt, "is_test", False)
                        and int(getattr(brt, "guild_id", 0))
                        == int(guild_id)
                        and team_id in (brt.registered_teams or [])
                        and brt.phase not in (
                            BRTournamentPhase.COMPLETE,
                            BRTournamentPhase.CANCELLED)):
                    return ("Команда зарегистрирована в активном "
                            "BR-турнире — состав менять нельзя")
        except Exception as exc:
            logger.warning("BR protection check failed: %s", exc)
            raise TeamStoreError(
                "check_failed",
                "Не удалось проверить статус турнира — операция "
                "временно запрещена") from exc

        # Cash Cup — та же БД, запрос на текущем соединении.
        # FOR UPDATE сериализует с регистрацией в турнире
        # (cashcup_store.mutate держит строку FOR UPDATE): либо мы
        # ждём её commit и видим регистрацию, либо регистрация
        # ждёт нас и увидит уже изменённый состав.
        # to_regclass не падает при отсутствии таблицы — прямой
        # SELECT по несуществующей таблице абортировал бы всю
        # транзакцию (InFailedSQLTransaction).
        try:
            exists = await conn.fetchval(
                "SELECT to_regclass('cashcup_tournaments')")
            if not exists:
                return None
            rows = await conn.fetch(
                "SELECT id, state FROM cashcup_tournaments "
                "WHERE guild_id = $1 AND status IN "
                "('registration','running') FOR UPDATE",
                guild_id)
        except Exception as exc:
            logger.warning("CashCup protection check failed: %s",
                           exc)
            raise TeamStoreError(
                "check_failed",
                "Не удалось проверить статус турнира — операция "
                "временно запрещена") from exc
        for r in rows:
            raw = r["state"]
            try:
                state = json.loads(raw) if isinstance(raw, str) \
                    else dict(raw)
            except (ValueError, TypeError):
                continue
            for t in state.get("teams") or []:
                if t.get("br_team_id") == team_id:
                    return ("Команда зарегистрирована в активном "
                            "CASH CUP — состав менять нельзя")
        return None

    @staticmethod
    def _check_requirements(team: Team, *,
                            sr: float | None,
                            lfg: Any) -> str | None:
        """Обязательные требования команды. Возвращает текст ошибки
        или None. SR=None — данных нет."""
        req = team.requirements or {}
        min_sr = req.get("min_sr")
        if min_sr:
            try:
                min_sr_f = float(min_sr)
            except (TypeError, ValueError) as exc:
                # Битые требования — fail-closed, а не «пропустить».
                raise TeamStoreError(
                    "check_failed",
                    "Требования команды повреждены — joining "
                    "временно недоступен") from exc
            if sr is None:
                return (f"Требуется SR от {min_sr} — у вас нет "
                        "данных об SR. Покажите капитану профиль.")
            if sr < min_sr_f:
                return (f"Требуется SR от {min_sr} — ваш SR "
                        f"{sr:.1f}.")
        dev = req.get("device")
        if dev:
            lfg_dev = getattr(lfg, "device", "") if lfg else ""
            if not lfg_dev:
                return ("Требуется устройство "
                        f"{dev} — у вас оно не указано. "
                        "Заполните профиль в Squad Hub → «Ищу "
                        "команду».")
            if lfg_dev != dev:
                return ("Требуется устройство "
                        f"{dev} — у вас {lfg_dev}.")
        role = req.get("role")
        if role:
            lfg_role = getattr(lfg, "play_role", "") if lfg else ""
            if lfg_role and lfg_role != role:
                return (f"Предпочтительная роль: {role} — у вас "
                        f"{lfg_role}.")
        if req.get("ff_uid_required"):
            ff_uid = getattr(lfg, "ff_uid", "") if lfg else ""
            if not ff_uid:
                return ("Требуется подтверждённый Free Fire UID — "
                        "укажите его в профиле поиска.")
        return None

    async def join_team(self, team_id: str, user_id: int,
                        guild_id: int, *,
                        via: str = "open",
                        password: str = "",
                        invite_id: int | None = None,
                        application_id: int | None = None,
                        sr: float | None = None,
                        ) -> Team:
        """Единый транзакционный механизм вступления (P0.3).

        ``via``: 'open' | 'password' | 'invite' | 'application'.
        Все проверки — внутри одной транзакции на одном соединении:
        advisory lock пользователя → блокировка строки команды →
        актуальные guild/team → режим вступления → валидность и срок
        приглашения/заявки → полномочия → требования → вместимость →
        отсутствие другого состава → блокировка турниром → UPDATE →
        фиксация invite/application → закрытие остальных заявок/
        приглашений → LFG off → аудит. При любой ошибке — rollback.
        Кэш обновляется только после COMMIT.
        """
        if via not in ("open", "password", "invite", "application"):
            raise TeamStoreError("bad_via", "Неизвестный способ "
                                 "вступления")
        team: Team
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    team = await self._join_tx(
                        conn, team_id, user_id, guild_id,
                        via=via, password=password,
                        invite_id=invite_id,
                        application_id=application_id, sr=sr)
            # Кэш — только после успешного COMMIT.
            self._teams[team.team_id] = team
            self._remember(team)
            self._save()
            return team
        return await self._join_json(
            team_id, user_id, guild_id, via=via, password=password,
            invite_id=invite_id, application_id=application_id,
            sr=sr)

    async def _join_json(self, team_id: str, user_id: int,
                         guild_id: int, *, via: str,
                         password: str, invite_id: int | None,
                         application_id: int | None,
                         sr: float | None) -> Team:
        """JSON-fallback: best-effort без транзакций (dev-режим)."""
        team = self._teams.get(team_id)
        if team is None or team.guild_id != guild_id:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if via == "open" and (team.join_mode != "open"
                              or not team.recruiting):
            raise TeamStoreError("recruiting_closed",
                                 "Открытое вступление недоступно")
        if via == "password":
            if team.join_mode != "password":
                raise TeamStoreError("recruiting_closed",
                                     "Вход по паролю недоступен")
            if not team.check_password(password):
                raise TeamStoreError("bad_password", "Неверный пароль")
        if via == "application" and (team.join_mode != "applications"
                                     or not team.recruiting):
            raise TeamStoreError("mode_changed",
                                 "Капитан изменил тип вступления")
        if team.is_full():
            raise TeamStoreError("team_full", "Команда заполнена")
        if team.is_member(user_id):
            raise TeamStoreError("already_in_team",
                                 "Вы уже в этой команде")
        for t in self._teams.values():
            if t.guild_id == guild_id and user_id in t.members:
                raise TeamStoreError(
                    "user_in_other_team",
                    "Вы уже состоите в другой команде")
        lfg = None
        try:
            from storage import squad_store as _ss
            import asyncio
            lfg = await _ss.get_lfg(guild_id, user_id)
        except Exception:
            pass
        req_err = self._check_requirements(team, sr=sr, lfg=lfg)
        if req_err:
            raise TeamStoreError("requirements", req_err)
        if await self._team_in_active_tournament(guild_id, team_id):
            raise TeamStoreError(
                "roster_locked",
                "Команда зарегистрирована в активном турнире — "
                "состав менять нельзя")
        team.add_member(user_id)
        team.updated_at = _now_iso()
        self._save_to_json()
        return team

    async def _join_tx(self, conn, team_id: str, user_id: int,
                       guild_id: int, *, via: str, password: str,
                       invite_id: int | None,
                       application_id: int | None,
                       sr: float | None) -> Team:
        from storage import squad_store as _ss

        # 1. Advisory lock на (guild,user) — сериализуем все
        #    вступления пользователя в этом сервере.
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
            self._member_lock_key(guild_id, user_id))
        # 2. Блокировка строки команды + актуальность.
        row = await self._lock_team_row(conn, team_id)
        if row is None or int(row["guild_id"]) != int(guild_id):
            raise TeamStoreError("team_not_found", "Команда не найдена")
        team = self._row_to_team(row)
        gid = int(team.guild_id)

        # 3–5. Режим вступления + валидность invite/application.
        if via == "open":
            if team.join_mode != "open" or not team.recruiting:
                raise TeamStoreError(
                    "recruiting_closed",
                    "Открытое вступление в эту команду недоступно")
        elif via == "password":
            # Пароль — самостоятельное согласие капитана: флаг
            # recruiting (публичный набор) для входа по паролю
            # не требуется — иначе legacy-команды с паролем
            # потеряли бы возможность пополнения состава.
            if team.join_mode != "password":
                raise TeamStoreError(
                    "recruiting_closed",
                    "Вход по паролю для этой команды недоступен")
            if not team.check_password(password):
                raise TeamStoreError("bad_password",
                                     "Неверный пароль")
        elif via == "invite":
            if invite_id is None:
                raise TeamStoreError("invite_required",
                                     "Приглашение не указано")
            inv = await _ss.get_invite_on(conn, invite_id)
            if (inv is None or inv.status != "pending"
                    or inv.is_expired
                    or inv.invited_user_id != user_id
                    or inv.team_id != team_id
                    or inv.guild_id != gid):
                raise TeamStoreError(
                    "invite_invalid",
                    "Приглашение недействительно или истекло")
        elif via == "application":
            if team.join_mode != "applications" or not team.recruiting:
                raise TeamStoreError(
                    "mode_changed",
                    "Капитан изменил тип вступления — "
                    "подайте заявку заново")
            if application_id is None:
                raise TeamStoreError("application_required",
                                     "Заявка не указана")
            app = await _ss.get_application_on(conn, application_id)
            if (app is None or app.status != "pending"
                    or app.is_expired
                    or app.user_id != user_id
                    or app.team_id != team_id
                    or app.guild_id != gid):
                raise TeamStoreError(
                    "application_invalid",
                    "Заявка недействительна или истекла")

        # 6. Вместимость и членство.
        if team.is_full():
            raise TeamStoreError("team_full", "Команда заполнена")
        if user_id in team.members:
            raise TeamStoreError("already_in_team",
                                 "Вы уже в этой команде")
        other = await conn.fetchval(
            "SELECT team_id FROM teams WHERE guild_id = $1 "
            "AND $2 = ANY(members) AND team_id <> $3",
            gid, user_id, team_id)
        if other:
            raise TeamStoreError(
                "user_in_other_team",
                "Вы уже состоите в другой команде")

        # 7. Требования капитана (SR передан снаружи — stats store
        #    использует свой pool; устройство/роль/FF UID — из team_lfg
        #    на этом же соединении).
        lfg = await _ss.get_lfg_on(conn, gid, user_id)
        req_err = self._check_requirements(
            team, sr=sr, lfg=lfg)
        if req_err:
            raise TeamStoreError("requirements", req_err)

        # 8. Блокировка активным турниром (fail-closed).
        reason = await self._tournament_blocked_on(conn, gid, team_id)
        if reason:
            raise TeamStoreError("roster_locked", reason)

        # 9. Добавить участника.
        members = list(team.members) + [user_id]
        await conn.execute(
            "UPDATE teams SET members = $2, updated_at = now() "
            "WHERE team_id = $1", team_id, members)

        # 10–13. Фиксация invite/application + закрытие остальных
        # заявок/приглашений + LFG off + аудит.
        if via == "invite" and invite_id is not None:
            ok = await _ss.resolve_invite_on(
                conn, invite_id, "accepted", require_unexpired=True)
            if not ok:
                raise TeamStoreError(
                    "invite_invalid",
                    "Приглашение уже обработано")
        if via == "application" and application_id is not None:
            ok = await _ss.resolve_application_on(
                conn, application_id, "accepted",
                require_unexpired=True)
            if not ok:
                raise TeamStoreError(
                    "application_invalid",
                    "Заявка уже обработана")
        await _ss.close_user_membership_on(conn, gid, user_id,
                                           "joined_team")
        if team.is_full() or len(members) >= 4:
            await _ss.mark_team_full_applications_on(conn, team_id)
        await _ss.audit_on(
            conn, gid, team_id, user_id, "team_join",
            metadata={"via": via, "invite_id": invite_id,
                      "application_id": application_id})

        # Объект с актуальным составом — кэш обновится после COMMIT.
        team.members = members
        team.updated_at = _now_iso()
        return team

    async def remove_member(self, team_id: str, user_id: int,
                            *, actor_id: int | None = None,
                            guild_id: int | None = None) -> Team:
        """Атомарно исключить/вывести участника (не капитана)."""
        gid = guild_id
        team: Team
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
                    reason = await self._tournament_blocked_on(
                        conn, gid, team_id)
                    if reason:
                        raise TeamStoreError("roster_locked", reason)
                    members = [m for m in team.members if m != user_id]
                    await conn.execute(
                        "UPDATE teams SET members = $2, updated_at = now() "
                        "WHERE team_id = $1",
                        team_id, members)
                    team.members = members
                    team.updated_at = _now_iso()
            self._teams[team_id] = team
            self._remember(team)
            self._save()
            return team

        team = self._teams.get(team_id)
        if team is None:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if team.is_captain(user_id):
            raise TeamStoreError(
                "is_captain",
                "Капитана нельзя исключить — сначала передайте "
                "капитанство")
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
                    reason = await self._tournament_blocked_on(
                        conn, gid, team_id)
                    if reason:
                        raise TeamStoreError("roster_locked", reason)
                    await conn.execute(
                        "UPDATE teams SET captain_id = $2, "
                        "updated_at = now() WHERE team_id = $1",
                        team_id, new_captain_id)
                    team.captain_id = new_captain_id
                    team.updated_at = _now_iso()
            self._teams[team_id] = team
            self._remember(team)
            self._save()
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
                    reason = await self._tournament_blocked_on(
                        conn, gid, team_id)
                    if reason:
                        raise TeamStoreError("roster_locked", reason)
                    await conn.execute(
                        "UPDATE teams SET captain_id = $2, "
                        "updated_at = now() WHERE team_id = $1",
                        team_id, new_captain_id)
                    team.captain_id = new_captain_id
                    team.updated_at = _now_iso()
            self._teams[team_id] = team
            self._remember(team)
            self._save()
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
        team: Team
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
                        raise TeamStoreError(
                            "not_captain",
                            "Только капитан может расформировать "
                            "команду")
                    reason = await self._tournament_blocked_on(
                        conn, gid, team_id)
                    if reason:
                        raise TeamStoreError("roster_locked", reason)
                    await self._delete_team_rows(conn, team_id)
                    name = team.team_name
            self._teams.pop(team_id, None)
            self._forget(team_id)
            self._save()
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

    @staticmethod
    async def _delete_team_rows(conn: "asyncpg.Connection",
                                team_id: str) -> None:
        """Удалить команду и связанные invites/applications в
        текущей транзакции (FK без каскада). История — в team_audit."""
        await conn.execute(
            "DELETE FROM team_invites WHERE team_id = $1", team_id)
        await conn.execute(
            "DELETE FROM team_applications WHERE team_id = $1",
            team_id)
        await conn.execute(
            "DELETE FROM teams WHERE team_id = $1", team_id)

    async def admin_disband(self, team_id: str, guild_id: int,
                            actor_id: int) -> str:
        """Административное расформирование: без проверки капитана,
        но с проверкой guild и fail-closed защитой турниров.
        Возвращает team_name."""
        if self._pg_mode():
            pool = await self._pg()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    row = await self._lock_team_row(conn, team_id)
                    if row is None or int(row["guild_id"]) != int(
                            guild_id):
                        raise TeamStoreError("team_not_found",
                                             "Команда не найдена")
                    team = self._row_to_team(row)
                    gid = int(team.guild_id)
                    reason = await self._tournament_blocked_on(
                        conn, gid, team_id)
                    if reason:
                        raise TeamStoreError("roster_locked", reason)
                    await self._delete_team_rows(conn, team_id)
                    from storage import squad_store as _ss
                    await _ss.audit_on(
                        conn, gid, team_id, actor_id,
                        "admin_disband",
                        metadata={"team_name": team.team_name})
                    name = team.team_name
            self._teams.pop(team_id, None)
            self._forget(team_id)
            self._save()
            return name

        team = self._teams.get(team_id)
        if team is None or team.guild_id != guild_id:
            raise TeamStoreError("team_not_found", "Команда не найдена")
        if await self._team_in_active_tournament(guild_id, team_id):
            raise TeamStoreError(
                "roster_locked",
                "Команда зарегистрирована в активном турнире")
        name = team.team_name
        del self._teams[team_id]
        self._forget(team_id)
        self._save_to_json()
        return name

    @staticmethod
    def _snapshot_matches(snap: dict, team: Team) -> bool:
        """Снимок предпросмотра vs актуальная команда.

        Сравниваем updated_at (parse-aware: naive считается UTC)
        и состав. Поля, которых нет в снимке, не проверяются."""
        snap_updated = str(snap.get("updated_at") or "")
        if snap_updated:
            a = TeamStore._parse_ts(snap_updated)
            b = TeamStore._parse_ts(team.updated_at or "")
            if a is not None and b is not None:
                if a != b:
                    return False
            elif snap_updated[:26] != (team.updated_at or "")[:26]:
                return False
        snap_members = snap.get("members")
        if snap_members is not None and sorted(
                int(m) for m in snap_members) != sorted(team.members):
            return False
        return True

    @staticmethod
    def _parse_ts(value: str):
        """ISO → aware datetime. Naive трактуем как локальное время:
        Team.created_at/updated_at пишутся через datetime.now()
        (naive local), а asyncpg при записи в timestamptz считает
        naive локальным — иначе снапшот и строка БД «разъедутся»."""
        from datetime import datetime
        try:
            v = value.replace("Z", "+00:00")
            dt = datetime.fromisoformat(v)
            if dt.tzinfo is None:
                dt = dt.astimezone()
            return dt
        except (ValueError, TypeError):
            return None

    async def disband_many(self, guild_id: int,
                           snapshots: list[dict],
                           actor_id: int) -> list[dict]:
        """Массовый сброс команд ОДНОЙ транзакцией (P0.6).

        ``snapshots`` — список {"team_id", "updated_at"} из
        предпросмотра. В транзакции каждая команда блокируется
        FOR UPDATE (в порядке team_id — детерминированный порядок
        против deadlock), сверяется с snapshot (updated_at) и с
        защитой турниров. Любое расхождение → rollback всей
        операции (TeamStoreError "list_changed"/"tournament_started").

        Возвращает список {"team_id","team_name","members",
        "recruitment_channel_id","recruitment_message_id"}.
        """
        if not snapshots:
            return []
        if not self._pg_mode():
            raise TeamStoreError(
                "no_pg", "Массовый сброс требует PostgreSQL")
        ids = sorted(s["team_id"] for s in snapshots)
        pool = await self._pg()
        removed: list[dict] = []
        async with pool.acquire() as conn:
            async with conn.transaction():
                for snap in sorted(snapshots,
                                   key=lambda s: s["team_id"]):
                    tid = snap["team_id"]
                    row = await self._lock_team_row(conn, tid)
                    if row is None or int(row["guild_id"]) != int(
                            guild_id):
                        raise TeamStoreError(
                            "list_changed",
                            "Состав команд изменился — требуется "
                            "новый предпросмотр")
                    team = self._row_to_team(row)
                    # Сверка с подтверждённым снимком
                    if not self._snapshot_matches(snap, team):
                        raise TeamStoreError(
                            "list_changed",
                            "Команда «%s» изменилась после "
                            "предпросмотра — подтвердите снова"
                            % team.team_name)
                    reason = await self._tournament_blocked_on(
                        conn, guild_id, tid)
                    if reason:
                        raise TeamStoreError(
                            "tournament_started",
                            f"Команда «{team.team_name}» вошла в "
                            "активный турнир — подтвердите снова")
                    await self._delete_team_rows(conn, tid)
                    from storage import squad_store as _ss
                    await _ss.audit_on(
                        conn, guild_id, tid, actor_id,
                        "admin_mass_disband",
                        metadata={"team_name": team.team_name,
                                  "members": list(team.members)})
                    removed.append({
                        "team_id": tid,
                        "team_name": team.team_name,
                        "members": list(team.members),
                        "recruitment_channel_id":
                            team.recruitment_channel_id,
                        "recruitment_message_id":
                            team.recruitment_message_id,
                    })
        # После COMMIT — обновить кэш
        for item in removed:
            tid = item["team_id"]
            self._teams.pop(tid, None)
            self._forget(tid)
        self._save()
        return removed

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
