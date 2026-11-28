"""Бизнес-логика «Ультиматума» 2.0.

Вся турнирная логика, регистрация (Соло/Дуо), голосования,
комнаты, ELO, финализация и восстановление после рестарта.
Источник истины — PostgreSQL (ult2_* таблицы).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import asyncpg
import discord

from storage.db import get_pool
from storage.vac_store import vac_store
from ultimatum.bracket import (
    Format, MatchStatus, Slot, TEMPLATES, entrant_count,
)
from ultimatum.elo import ult_elo_store
from ultimatum.store import (
    DuoInvite, UltEntrant, UltMatch, UltTournament, UltVote,
    ultimatum_store,
)

logger = logging.getLogger(__name__)

INVITE_TTL_SECONDS = 15 * 60
MAX_MATCH_KEY_LEN = 10

# Статусы матчей, в которых принимаются голоса
VOTABLE = (
    MatchStatus.READY.value,
    MatchStatus.IN_PROGRESS.value,
    MatchStatus.AWAITING_CONFIRMATION.value,
)


@dataclass
class VoteOutcome:
    kind: str  # 'voted' | 'completed' | 'disputed' | 'error'
    message: str = ""
    match: Optional[UltMatch] = None
    tournament: Optional[UltTournament] = None


@dataclass
class MatchFinalized:
    """Результат внутренней фиксации матча (для UI)."""
    match: UltMatch
    tournament: UltTournament
    winner: UltEntrant
    loser: UltEntrant
    tournament_finished: bool = False
    gf2_created: bool = False
    newly_ready: list[str] = field(default_factory=list)


def fmt_label(fmt: Format) -> str:
    return "СОЛО" if fmt is Format.SOLO else "ДУО"


def fmt_title(fmt: Format) -> str:
    return f"УЛЬТИМАТУМ — {fmt_label(fmt)}"


class UltService:
    def __init__(self, store=None, elo=None):
        self.store = store or ultimatum_store
        self.elo = elo or ult_elo_store
        # Повторный on_ready не должен запускать параллельные
        # resume — иначе возможны дубли сообщений до CAS-claim.
        self._resume_lock = asyncio.Lock()

    # ================= РЕГИСТРАЦИЯ =================

    async def _fresh_status(self, tid: str) -> Optional[str]:
        """Статус турнира из БД — переданные объекты могут быть stale."""
        t = await self.store.get_tournament(tid)
        return t.status if t else None

    @staticmethod
    async def _reg_lock(conn, tid: str) -> None:
        """Advisory lock, общий для всех регистрационных мутаций
        и для start() — сериализует их между собой.

        lock_timeout ограничивает ожидание: ожидающие держат
        соединения из пула, без таймаута возможен pool-deadlock,
        если критический участок start() превысит размер пула.
        """
        await conn.execute("SET LOCAL lock_timeout = '5000'")
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext($1))",
            f"ult2reg:{tid}",
        )

    @staticmethod
    async def _reg_guard(conn, tid: str) -> Optional[str]:
        """Проверка статуса ВНУТРИ транзакции под advisory lock.

        FOR UPDATE ждёт, пока параллельный start()/delete не
        закоммитит смену статуса — TOCTOU исключён. Возвращает
        текст ошибки или None, если регистрация ещё открыта.
        """
        row = await conn.fetchrow(
            "SELECT status FROM ult2_tournaments WHERE id=$1 FOR UPDATE",
            tid,
        )
        if row is None:
            return "❌ Турнир не найден."
        if row["status"] != "REGISTRATION":
            return "❌ Регистрация закрыта."
        return None

    async def register_solo(
        self, tournament: UltTournament, user: discord.User,
    ) -> str:
        """Записать игрока в Соло-турнир. Возвращает текст ответа."""
        if tournament.format is not Format.SOLO:
            return "❌ Это Дуо-турнир — используйте «Создать дуэт»."
        if await self._fresh_status(tournament.id) != "REGISTRATION":
            return "❌ Регистрация закрыта."
        existing = await self.store.find_entrant_by_member(tournament.id, user.id)
        if existing:
            return "❌ Вы уже участвуете в этом турнире."
        entrants = await self.store.get_entrants(tournament.id)
        if len(entrants) >= entrant_count(Format.SOLO):
            return "❌ Турнир заполнен (16/16)."
        # Одна транзакция: advisory lock + FOR UPDATE по статусу
        # (атомарно относительно start()/delete) + dup + лимит.
        await vac_store.ensure_schema()
        pool = await get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self._reg_lock(conn, tournament.id)
                    guard = await self._reg_guard(conn, tournament.id)
                    if guard:
                        return guard
                    dup = await conn.fetchval(
                        """
                        SELECT 1 FROM ult2_entrants
                        WHERE tournament_id=$1 AND $2 = ANY(member_ids)
                        """,
                        tournament.id, user.id,
                    )
                    if dup:
                        return "❌ Вы уже участвуете в этом турнире."
                    count = await conn.fetchval(
                        "SELECT COUNT(*) FROM ult2_entrants WHERE tournament_id=$1",
                        tournament.id,
                    )
                    if count >= entrant_count(Format.SOLO):
                        return "❌ Турнир заполнен (16/16)."
                    # VAC внутри транзакции — блокировка, выданная
                    # между проверкой в cog и записью, тоже отсечётся.
                    # to_regclass — fail-open, если схема VAC ещё не
                    # создана (свежая БД): не роняем регистрацию.
                    vac_uid = None
                    if await conn.fetchval(
                        "SELECT to_regclass('vac_bans')"
                    ) is not None:
                        vac_uid = await conn.fetchval(
                            """
                            SELECT user_id FROM vac_bans
                            WHERE guild_id=$1 AND user_id=$2
                              AND revoked_at IS NULL AND expired_at IS NULL
                              AND expires_at > now()
                            LIMIT 1
                            """,
                            tournament.guild_id, user.id,
                        )
                    if vac_uid is not None:
                        return "❌ У вас активен VAC — регистрация невозможна."
                    await conn.execute(
                        """
                        INSERT INTO ult2_entrants
                            (tournament_id, member_ids, member_names)
                        VALUES ($1, $2, $3::jsonb)
                        """,
                        tournament.id, [user.id],
                        __import__("json").dumps([user.display_name]),
                    )
                    await conn.execute(
                        """
                        INSERT INTO ult2_audit (tournament_id, actor_id, action)
                        VALUES ($1,$2,'register_solo')
                        """,
                        tournament.id, user.id,
                    )
        except asyncpg.UniqueViolationError:
            # Страховка: уникальный индекс member_ids[1]
            return "❌ Вы уже участвуете в этом турнире."
        n = len(await self.store.get_entrants(tournament.id))
        return f"✅ Вы в турнире! Участников: {n}/16"

    async def leave(
        self, tournament: UltTournament, user: discord.User,
    ) -> str:
        if await self._fresh_status(tournament.id) != "REGISTRATION":
            return "❌ Выйти можно только во время регистрации."
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._reg_lock(conn, tournament.id)
                guard = await self._reg_guard(conn, tournament.id)
                if guard:
                    return guard
                eid = await conn.fetchval(
                    """
                    SELECT id FROM ult2_entrants
                    WHERE tournament_id=$1 AND $2 = ANY(member_ids)
                    LIMIT 1
                    """,
                    tournament.id, user.id,
                )
                if not eid:
                    return "❌ Вы не участвуете в этом турнире."
                await conn.execute(
                    "DELETE FROM ult2_entrants WHERE id=$1", eid
                )
                await conn.execute(
                    """
                    INSERT INTO ult2_audit (tournament_id, actor_id, action)
                    VALUES ($1,$2,'leave')
                    """,
                    tournament.id, user.id,
                )
        return "✅ Вы покинули турнир."

    async def duo_create_invite(
        self, tournament: UltTournament, captain: discord.User,
        partner: discord.User,
    ) -> tuple[Optional[int], str]:
        """Создать приглашение в дуэт. Возвращает (invite_id, message)."""
        if tournament.format is not Format.DUO:
            return None, "❌ Это Соло-турнир."
        if await self._fresh_status(tournament.id) != "REGISTRATION":
            return None, "❌ Регистрация закрыта."
        if captain.id == partner.id:
            return None, "❌ Нельзя пригласить самого себя."

        await self.store.expire_invites(tournament.id)

        # Капитан уже в дуэте?
        cap_team = await self.store.find_entrant_by_member(tournament.id, captain.id)
        if cap_team:
            return None, "❌ Вы уже в дуэте. Сначала выйдите из команды."
        part_team = await self.store.find_entrant_by_member(
            tournament.id, partner.id
        )
        if part_team:
            return None, "❌ Этот игрок уже состоит в дуэте."

        # Активное приглашение?
        pending = await self.store.get_pending_invite_for(tournament.id, captain.id)
        if pending:
            if pending.captain_id == captain.id and pending.partner_id == partner.id:
                return pending.id, "⏳ Приглашение уже отправлено — ожидает ответа."
            return None, "❌ У вас уже есть активное приглашение. Отмените его сначала."
        pending_p = await self.store.get_pending_invite_for(tournament.id, partner.id)
        if pending_p:
            return None, "❌ У этого игрока уже есть активное приглашение."

        entrants = await self.store.get_entrants(tournament.id)
        if len(entrants) >= entrant_count(Format.DUO):
            return None, "❌ Лимит дуэтов исчерпан (8/8)."

        # VAC-проверки обоих игроков дуэта до подтверждения команды
        from services import vac_service
        for member in (captain, partner):
            vac = await vac_service.check_vac(tournament.guild_id, member.id)
            if vac is not None:
                ts = int(vac["expires_at"].timestamp())
                return None, (
                    f"❌ <@{member.id}> находится в VAC-блокировке.\n"
                    + vac_service.vac_blocked_text(
                        ts, reason=vac.get("reason"), level=vac.get("level"))
                )

        # Создание инвайта — в транзакции под тем же reg-lock:
        # статус, состав и pending-инвайты перепроверяются атомарно.
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._reg_lock(conn, tournament.id)
                guard = await self._reg_guard(conn, tournament.id)
                if guard:
                    return None, guard
                # Участие в дуэтах (повторно, внутри lock)
                dup = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM ult2_entrants
                    WHERE tournament_id=$1
                      AND (member_ids @> ARRAY[$2]::bigint[]
                           OR member_ids @> ARRAY[$3]::bigint[])
                    """,
                    tournament.id, captain.id, partner.id,
                )
                if dup:
                    return None, "❌ Один из игроков уже в дуэте."
                # Чужой/свой pending-инвайт с любым из игроков
                pend = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM ult2_duo_invites
                    WHERE tournament_id=$1 AND status='pending'
                      AND expires_at > now()
                      AND (captain_id IN ($2,$3)
                           OR partner_id IN ($2,$3))
                    """,
                    tournament.id, captain.id, partner.id,
                )
                if pend:
                    return None, (
                        "❌ У одного из игроков уже есть "
                        "активное приглашение."
                    )
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM ult2_entrants WHERE tournament_id=$1",
                    tournament.id,
                )
                if count >= entrant_count(Format.DUO):
                    return None, "❌ Лимит дуэтов исчерпан (8/8)."
                invite_id = await conn.fetchval(
                    """
                    INSERT INTO ult2_duo_invites
                        (tournament_id, captain_id, partner_id, partner_name,
                         status, expires_at)
                    VALUES ($1,$2,$3,$4,'pending',
                            now() + ($5 || ' seconds')::interval)
                    RETURNING id
                    """,
                    tournament.id, captain.id, partner.id,
                    partner.display_name, str(INVITE_TTL_SECONDS),
                )
                await conn.execute(
                    """
                    INSERT INTO ult2_audit
                        (tournament_id, actor_id, action, details)
                    VALUES ($1,$2,'duo_invite',$3::jsonb)
                    """,
                    tournament.id, captain.id,
                    __import__("json").dumps(
                        {"invite_id": invite_id, "partner": partner.id}
                    ),
                )
        return invite_id, "📨 Приглашение отправлено."

    async def duo_respond(
        self, invite: DuoInvite, user: discord.User, accept: bool,
    ) -> tuple[bool, str]:
        """Ответ на приглашение. Возвращает (успех, сообщение)."""
        tournament = await self.store.get_tournament(invite.tournament_id)
        if not tournament or tournament.status != "REGISTRATION":
            return False, "❌ Турнир уже не принимает регистрации."
        if invite.status != "pending":
            return False, "❌ Приглашение уже обработано или истекло."
        exp = invite.expires_at
        if exp is not None:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < datetime.now(timezone.utc):
                await self.store.set_invite_status(invite.id, "expired")
                return False, "❌ Срок приглашения истёк."
        if user.id != invite.partner_id:
            return False, "❌ Ответить может только приглашённый игрок."

        if not accept:
            await self.store.set_invite_status(invite.id, "declined")
            await self.store.log(
                tournament.id, user.id, "duo_decline",
                {"invite_id": invite.id},
            )
            return True, "❌ Приглашение отклонено."

        # Атомарная проверка + создание entrant'а
        await vac_store.ensure_schema()
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Сериализуем регистрации/принятия дуэтов в турнире
                await self._reg_lock(conn, tournament.id)
                # Статус турнира внутри lock — start() не может
                # вклиниться между проверкой и созданием дуэта.
                guard = await self._reg_guard(conn, tournament.id)
                if guard:
                    return False, guard
                # Повторная проверка статуса внутри транзакции
                row = await conn.fetchrow(
                    """
                    SELECT status FROM ult2_duo_invites
                    WHERE id=$1 FOR UPDATE
                    """,
                    invite.id,
                )
                if not row or row["status"] != "pending":
                    return False, "❌ Приглашение уже обработано."

                # Лимит команд
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM ult2_entrants WHERE tournament_id=$1",
                    tournament.id,
                )
                if count >= entrant_count(Format.DUO):
                    return False, "❌ Лимит дуэтов исчерпан (8/8)."

                # Игроки не должны быть в других дуэтах
                dup = await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM ult2_entrants
                    WHERE tournament_id=$1
                      AND (member_ids @> ARRAY[$2]::bigint[]
                           OR member_ids @> ARRAY[$3]::bigint[])
                    """,
                    tournament.id, invite.captain_id, user.id,
                )
                if dup:
                    return False, "❌ Один из игроков уже в дуэте."

                # VAC обоих игроков — прямо перед созданием дуэта,
                # чтобы блокировка, выданная после инвайта, тоже
                # не пропустила команду. to_regclass — fail-open при
                # отсутствии схемы VAC.
                vac_uid = None
                if await conn.fetchval(
                    "SELECT to_regclass('vac_bans')"
                ) is not None:
                    vac_uid = await conn.fetchval(
                        """
                        SELECT user_id FROM vac_bans
                        WHERE guild_id=$1 AND user_id = ANY($2::bigint[])
                          AND revoked_at IS NULL AND expired_at IS NULL
                          AND expires_at > now()
                        LIMIT 1
                        """,
                        tournament.guild_id,
                        [invite.captain_id, user.id],
                    )
                if vac_uid is not None:
                    return False, (
                        f"❌ <@{vac_uid}> находится в VAC-блокировке — "
                        "дуэт не может быть подтверждён."
                    )

                names = [f"<@{invite.captain_id}>", f"<@{user.id}>"]
                # Берём display_name через bot если доступен — иначе mention
                eid = await conn.fetchval(
                    """
                    INSERT INTO ult2_entrants
                        (tournament_id, seed, member_ids, member_names)
                    VALUES ($1, NULL, $2, $3::jsonb) RETURNING id
                    """,
                    tournament.id,
                    [invite.captain_id, user.id],
                    __import__("json").dumps(names),
                )
                await conn.execute(
                    "UPDATE ult2_duo_invites SET status='accepted' WHERE id=$1",
                    invite.id,
                )
                await conn.execute(
                    """
                    INSERT INTO ult2_audit (tournament_id, actor_id, action, details)
                    VALUES ($1,$2,'duo_accept',$3::jsonb)
                    """,
                    tournament.id, user.id,
                    __import__("json").dumps({"invite_id": invite.id, "entrant_id": eid}),
                )
        n = len(await self.store.get_entrants(tournament.id))
        return True, f"✅ Дуэт создан! Команд: {n}/8"

    async def duo_leave(self, tournament: UltTournament, user: discord.User) -> str:
        if await self._fresh_status(tournament.id) != "REGISTRATION":
            return "❌ После старта состав дуэта заблокирован."
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._reg_lock(conn, tournament.id)
                guard = await self._reg_guard(conn, tournament.id)
                if guard:
                    return "❌ После старта состав дуэта заблокирован."
                entrant = await conn.fetchrow(
                    """
                    SELECT id, member_ids FROM ult2_entrants
                    WHERE tournament_id=$1 AND $2 = ANY(member_ids)
                    LIMIT 1
                    """,
                    tournament.id, user.id,
                )
                if not entrant:
                    return "❌ Вы не состоите в дуэте."
                member_ids = list(entrant["member_ids"])
                # Отменить висящие приглашения обоих членов
                await conn.execute(
                    """
                    UPDATE ult2_duo_invites SET status='cancelled'
                    WHERE tournament_id=$1 AND status='pending'
                      AND (captain_id = ANY($2::bigint[])
                           OR partner_id = ANY($2::bigint[]))
                    """,
                    tournament.id, member_ids,
                )
                await conn.execute(
                    "DELETE FROM ult2_entrants WHERE id=$1", entrant["id"]
                )
                await conn.execute(
                    """
                    INSERT INTO ult2_audit (tournament_id, actor_id, action)
                    VALUES ($1,$2,'duo_leave')
                    """,
                    tournament.id, user.id,
                )
        return "✅ Дуэт распущен, слот освобождён."

    async def duo_cancel_invite(
        self, tournament: UltTournament, user: discord.User,
    ) -> str:
        pending = await self.store.get_pending_invite_for(tournament.id, user.id)
        if not pending or pending.captain_id != user.id:
            return "❌ Нет активного приглашения от вас."
        await self.store.set_invite_status(pending.id, "cancelled")
        return "✅ Приглашение отменено."

    # ================= СТАРТ =================

    async def start(
        self, tournament: UltTournament, bot: Optional[discord.Client] = None,
    ) -> tuple[bool, str]:
        """Идемпотентный старт: создаёт сетку один раз.

        Весь критический участок держит тот же advisory lock
        ``ult2reg:<tid>``, что и регистрационные мутации: пока
        старт идёт, join/leave/duo_* блокируются и затем видят
        финальный статус — гонки «успел зарегистрироваться
        после старта» нет.
        """
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await self._reg_lock(conn, tournament.id)
                # try_start атомарен — повторное нажатие вернёт False
                if not await self.store.try_start(tournament.id):
                    return False, "⏳ Турнир уже запущен."

                entrants = await self.store.get_entrants(tournament.id)
                need = entrant_count(tournament.format)
                if len(entrants) != need:
                    # Откат статуса — состав неполный
                    await self.store.set_status(tournament.id, "REGISTRATION")
                    return False, f"❌ Неполный состав: {len(entrants)}/{need}."

                # Посев по SR
                try:
                    await self._assign_seeds(tournament, entrants, bot)
                except Exception as e:
                    logger.error("Seeding failed for %s: %s", tournament.id, e)
                    await self.store.set_status(tournament.id, "REGISTRATION")
                    return False, "❌ Ошибка формирования посева."

                # Создать все матчи из шаблона
                try:
                    await self._create_bracket_matches(tournament)
                except Exception as e:
                    logger.error("Bracket creation failed for %s: %s", tournament.id, e)
                    await self.store.set_status(tournament.id, "REGISTRATION")
                    return False, "❌ Ошибка создания сетки."

                await self.store.log(tournament.id, tournament.created_by, "tournament_start")
                return True, "✅ Турнир начался!"

    async def _sr_for_members(
        self, guild_id: int, member_ids: list[int], bot=None,
    ) -> float:
        """Средний Skill Rating участников (0–10)."""
        from storage.player_stats_store import player_stats_store
        total = 0.0
        for uid in member_ids:
            try:
                stats = await player_stats_store.get(guild_id, uid)
                total += float(stats.skill_rating) if stats else 1.0
            except Exception:
                total += 1.0
        return total / len(member_ids) if member_ids else 1.0

    async def _assign_seeds(
        self, tournament: UltTournament, entrants: list[UltEntrant],
        bot=None,
    ) -> None:
        """Сортировка по SR desc; тай-брейк: больше матчей -> меньше id.

        Соло: SR игрока. Дуо: средний SR двух игроков.
        Посев фиксируется в БД и не меняется в ходе турнира.
        """
        scored = []
        for e in entrants:
            sr = await self._sr_for_members(
                tournament.guild_id, e.member_ids, bot
            )
            # стабильный тай-брейк: средний id членов asc
            tie = sum(e.member_ids) / len(e.member_ids)
            scored.append((sr, -tie, e))
        scored.sort(key=lambda x: (-x[0], -x[1]))
        for seed, (_, _, e) in enumerate(scored, 1):
            await self.store.update_entrant_seed(e.id, seed)

    async def _create_bracket_matches(self, tournament: UltTournament) -> None:
        """Создать строки ult2_matches из шаблона + разрешить U1."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                for spec in TEMPLATES[tournament.format]():
                    await conn.execute(
                        """
                        INSERT INTO ult2_matches
                            (tournament_id, key, bracket, round_name, gf_part,
                             status, side_a_src, side_b_src)
                        VALUES ($1,$2,$3,$4,$5,'WAITING',$6,$7)
                        ON CONFLICT (tournament_id, key) DO NOTHING
                        """,
                        tournament.id, spec.key, spec.bracket,
                        spec.round_name, spec.gf_part,
                        _slot_str(spec.a), _slot_str(spec.b),
                    )
                # Разрешить seed-слоты U1 и пометить READY
                await self._resolve_ready(conn, tournament.id)

    async def _resolve_ready(
        self, conn: asyncpg.Connection, tid: str,
    ) -> list[str]:
        """Заполнить пустые слоты из результатов матчей и перевести
        WAITING->READY матчи с обоими участниками. Возвращает ключи."""
        # Разрешение по source-строкам
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_a = COALESCE(m.entrant_a, src.winner_entrant)
            FROM ult2_matches src
            WHERE m.tournament_id=$1 AND src.tournament_id=$1
              AND m.side_a_src = 'winner:' || src.key
              AND src.status='COMPLETED' AND src.winner_entrant IS NOT NULL
              AND m.entrant_a IS NULL
            """,
            tid,
        )
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_b = COALESCE(m.entrant_b, src.winner_entrant)
            FROM ult2_matches src
            WHERE m.tournament_id=$1 AND src.tournament_id=$1
              AND m.side_b_src = 'winner:' || src.key
              AND src.status='COMPLETED' AND src.winner_entrant IS NOT NULL
              AND m.entrant_b IS NULL
            """,
            tid,
        )
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_a = COALESCE(m.entrant_a, src.loser_entrant)
            FROM ult2_matches src
            WHERE m.tournament_id=$1 AND src.tournament_id=$1
              AND m.side_a_src = 'loser:' || src.key
              AND src.status='COMPLETED' AND src.loser_entrant IS NOT NULL
              AND m.entrant_a IS NULL
            """,
            tid,
        )
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_b = COALESCE(m.entrant_b, src.loser_entrant)
            FROM ult2_matches src
            WHERE m.tournament_id=$1 AND src.tournament_id=$1
              AND m.side_b_src = 'loser:' || src.key
              AND src.status='COMPLETED' AND src.loser_entrant IS NOT NULL
              AND m.entrant_b IS NULL
            """,
            tid,
        )
        # seed:-слоты (только U1 на старте)
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_a = COALESCE(m.entrant_a, ea.id)
            FROM ult2_entrants ea
            WHERE m.tournament_id=$1 AND ea.tournament_id=$1
              AND m.side_a_src = 'seed:' || ea.seed::text
              AND m.entrant_a IS NULL
            """,
            tid,
        )
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_b = COALESCE(m.entrant_b, eb.id)
            FROM ult2_entrants eb
            WHERE m.tournament_id=$1 AND eb.tournament_id=$1
              AND m.side_b_src = 'seed:' || eb.seed::text
              AND m.entrant_b IS NULL
            """,
            tid,
        )
        # entrant:<id> — прямые ссылки (GF2)
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_a = COALESCE(m.entrant_a, substring(m.side_a_src from 9)::bigint)
            WHERE m.tournament_id=$1 AND m.side_a_src LIKE 'entrant:%'
              AND m.entrant_a IS NULL
            """,
            tid,
        )
        await conn.execute(
            """
            UPDATE ult2_matches m SET
                entrant_b = COALESCE(m.entrant_b, substring(m.side_b_src from 9)::bigint)
            WHERE m.tournament_id=$1 AND m.side_b_src LIKE 'entrant:%'
              AND m.entrant_b IS NULL
            """,
            tid,
        )
        rows = await conn.fetch(
            """
            UPDATE ult2_matches SET status='READY'
            WHERE tournament_id=$1 AND status='WAITING'
              AND entrant_a IS NOT NULL AND entrant_b IS NOT NULL
            RETURNING key
            """,
            tid,
        )
        return [r["key"] for r in rows]

    # ================= ГОЛОСОВАНИЕ =================

    async def cast_vote(
        self, match_db_id: int, user, side: str,
    ) -> VoteOutcome:
        """side: 'a'|'b' — за какую сторону проголосовал.
        Атомарно: голос + проверка консенсуса + фиксация."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                m = await self._lock_match(conn, match_db_id)
                if m is None:
                    return VoteOutcome("error", "Матч не найден.")
                if m["status"] == "COMPLETED":
                    return VoteOutcome("error", "Матч уже завершён.")
                if m["status"] == "CANCELLED":
                    return VoteOutcome("error", "Матч отменён.")
                if m["status"] not in VOTABLE:
                    return VoteOutcome("error", "Матч ещё не готов.")
                if m["entrant_a"] is None or m["entrant_b"] is None:
                    return VoteOutcome("error", "Матч ещё не заполнен.")

                ea = m["entrant_a"] if side == "a" else m["entrant_b"]
                eb = m["entrant_b"] if side == "a" else m["entrant_a"]
                voted_entrant = ea

                # voter должен быть участником одной из сторон
                voter_member = await conn.fetchval(
                    """
                    SELECT id FROM ult2_entrants
                    WHERE id IN ($1,$2) AND $3 = ANY(member_ids)
                    """,
                    m["entrant_a"], m["entrant_b"], user.id,
                )
                if voter_member is None:
                    return VoteOutcome("error", "Голосовать могут только участники матча.")

                await conn.execute(
                    """
                    INSERT INTO ult2_votes (match_id, user_id, voter_entrant, voted_entrant)
                    VALUES ($1,$2,$3,$4)
                    ON CONFLICT (match_id, user_id)
                    DO UPDATE SET voted_entrant=$4, voter_entrant=$3
                    """,
                    match_db_id, user.id, voter_member, voted_entrant,
                )
                # Фиксация голосов в движке-статусе
                await conn.execute(
                    "UPDATE ult2_matches SET status='AWAITING_CONFIRMATION' WHERE id=$1 AND status IN ('READY','IN_PROGRESS')",
                    match_db_id,
                )

                outcome = await self._evaluate_votes(conn, match_db_id)
                if outcome is not None:
                    return outcome
                return VoteOutcome("voted", "Голос учтён — ждём подтверждения второй стороны.")

    async def _lock_match(
        self, conn: asyncpg.Connection, match_db_id: int,
    ) -> Optional[asyncpg.Record]:
        return await conn.fetchrow(
            "SELECT * FROM ult2_matches WHERE id=$1 FOR UPDATE", match_db_id
        )

    async def _evaluate_votes(
        self, conn: asyncpg.Connection, match_db_id: int,
    ) -> Optional[VoteOutcome]:
        """Проверить консенсус. Возвращает VoteOutcome если матч
        завершён или ушёл в DISPUTED, иначе None."""
        m = await conn.fetchrow(
            "SELECT * FROM ult2_matches WHERE id=$1", match_db_id
        )
        if not m or m["status"] == "COMPLETED":
            return None
        ea, eb = m["entrant_a"], m["entrant_b"]
        if ea is None or eb is None:
            return None

        ent_a = await conn.fetchrow("SELECT * FROM ult2_entrants WHERE id=$1", ea)
        ent_b = await conn.fetchrow("SELECT * FROM ult2_entrants WHERE id=$1", eb)
        if not ent_a or not ent_b:
            return None

        votes = await conn.fetch(
            "SELECT * FROM ult2_votes WHERE match_id=$1", match_db_id
        )
        if not votes:
            return None

        members_a = set(ent_a["member_ids"])
        members_b = set(ent_b["member_ids"])

        def side_claim(members: set[int]) -> tuple[Optional[int], bool]:
            """(claim, conflict): claim — единогласный выбор стороны."""
            claims = {
                v["voted_entrant"] for v in votes if v["user_id"] in members
            }
            if len(claims) > 1:
                return None, True
            if not claims:
                return None, False
            return next(iter(claims)), False

        claim_a, conflict_a = side_claim(members_a)
        claim_b, conflict_b = side_claim(members_b)

        if conflict_a or conflict_b:
            await conn.execute(
                "UPDATE ult2_matches SET status='DISPUTED' WHERE id=$1",
                match_db_id,
            )
            await self.store.log(
                m["tournament_id"], 0, "vote_conflict",
                {"match_id": match_db_id}, conn=conn,
            )
            return VoteOutcome(
                "disputed",
                "⚠️ Участники одной из сторон проголосовали по-разному — спор передан организатору.",
            )

        if claim_a is not None and claim_b is not None:
            if claim_a == claim_b:
                fin = await self._finalize_match(
                    conn, match_db_id, claim_a, by_admin=False,
                )
                if fin is not None:
                    return VoteOutcome(
                        "completed",
                        f"✅ Матч завершён. Победитель: {fin.winner.label()}",
                        match=fin.match,
                        tournament=fin.tournament,
                    )
            else:
                await conn.execute(
                    "UPDATE ult2_matches SET status='DISPUTED' WHERE id=$1",
                    match_db_id,
                )
                await self.store.log(
                    m["tournament_id"], 0, "vote_dispute",
                    {"match_id": match_db_id}, conn=conn,
                )
                return VoteOutcome(
                    "disputed",
                    "⚠️ Стороны не согласны — результат передан организатору.",
                )
        return None

    async def _finalize_match(
        self, conn: asyncpg.Connection, match_db_id: int,
        winner_entrant_id: int, by_admin: bool = False,
        admin_id: int = 0,
    ) -> Optional[MatchFinalized]:
        """Атомарная фиксация результата + продвижение по сетке +
        ELO + финализация турнира при необходимости."""
        m = await self._lock_match(conn, match_db_id)
        if m is None:
            return None
        if m["status"] == "COMPLETED":
            return None  # идемпотентно
        if m["status"] == "CANCELLED":
            return None
        if m["entrant_a"] is None or m["entrant_b"] is None:
            return None
        if winner_entrant_id not in (m["entrant_a"], m["entrant_b"]):
            return None

        tid = m["tournament_id"]
        loser_id = m["entrant_b"] if winner_entrant_id == m["entrant_a"] else m["entrant_a"]

        winner_row = await conn.fetchrow(
            "SELECT * FROM ult2_entrants WHERE id=$1 FOR UPDATE", winner_entrant_id
        )
        loser_row = await conn.fetchrow(
            "SELECT * FROM ult2_entrants WHERE id=$1 FOR UPDATE", loser_id
        )
        if not winner_row or not loser_row:
            return None

        # Статистика сторон
        await conn.execute(
            """
            UPDATE ult2_entrants SET wins=wins+1 WHERE id=$1
            """,
            winner_entrant_id,
        )
        new_losses = loser_row["losses"] + 1
        new_status = "ELIMINATED" if new_losses >= 2 else "ACTIVE"
        await conn.execute(
            "UPDATE ult2_entrants SET losses=$2, status=$3 WHERE id=$1",
            loser_id, new_losses, new_status,
        )

        await conn.execute(
            """
            UPDATE ult2_matches SET
                status='COMPLETED', winner_entrant=$2, loser_entrant=$3,
                completed_at=now()
            WHERE id=$1
            """,
            match_db_id, winner_entrant_id, loser_id,
        )

        # Продвижение по сетке
        ready = await self._resolve_ready(conn, tid)

        # GF2 при необходимости
        gf2_created = False
        if m["key"] == "GF1":
            w_losses = winner_row["losses"]
            if w_losses > 0:
                # обе стороны с 1 поражением → нужен reset
                exists = await conn.fetchval(
                    "SELECT 1 FROM ult2_matches WHERE tournament_id=$1 AND key='GF2'",
                    tid,
                )
                if not exists:
                    await conn.execute(
                        """
                        INSERT INTO ult2_matches
                            (tournament_id, key, bracket, round_name, gf_part,
                             status, side_a_src, side_b_src,
                             entrant_a, entrant_b)
                        VALUES ($1,'GF2','GF','Гранд-финал · Решающий матч',2,
                                'READY',$2,$3,$4,$5)
                        """,
                        tid,
                        f"entrant:{m['entrant_a']}", f"entrant:{m['entrant_b']}",
                        m["entrant_a"], m["entrant_b"],
                    )
                    gf2_created = True

        # ELO — в той же транзакции, идемпотентно (тестовые турниры — без ELO)
        tournament = await self.store.get_tournament(tid)
        if tournament is not None and not tournament.is_test:
            try:
                await self.elo.apply_match(
                    conn,
                    match_db_id=match_db_id,
                    tournament_id=tid,
                    fmt=tournament.format.value,
                    winner_member_ids=list(winner_row["member_ids"]),
                    loser_member_ids=list(loser_row["member_ids"]),
                )
            except Exception as e:
                logger.error("ELO apply failed match %s: %s", match_db_id, e)

        finished = False
        if await self._tournament_finished(conn, tid):
            await self._finalize_tournament(conn, tid, admin_id or 0)
            finished = True

        if by_admin:
            await self.store.log(
                tid, admin_id, "admin_set_result",
                {"match_id": match_db_id, "winner": winner_entrant_id},
                conn=conn,
            )

        # Собрать объекты для ответа
        updated = await conn.fetchrow(
            "SELECT * FROM ult2_matches WHERE id=$1", match_db_id
        )
        if tournament is None:
            return None
        return MatchFinalized(
            match=_row_to_match(updated),
            tournament=tournament,
            winner=_row_to_entrant(winner_row),
            loser=_row_to_entrant(loser_row),
            tournament_finished=finished,
            gf2_created=gf2_created,
            newly_ready=ready,
        )

    async def _tournament_finished(
        self, conn: asyncpg.Connection, tid: str,
    ) -> bool:
        gf1 = await conn.fetchrow(
            "SELECT * FROM ult2_matches WHERE tournament_id=$1 AND key='GF1'",
            tid,
        )
        if not gf1 or gf1["status"] != "COMPLETED":
            return False
        # Победитель GF1 из нижней сетки? (losses>0 на момент финала)
        w = await conn.fetchrow(
            "SELECT losses FROM ult2_entrants WHERE id=$1",
            gf1["winner_entrant"],
        )
        if w and w["losses"] > 0:
            gf2 = await conn.fetchrow(
                "SELECT status FROM ult2_matches WHERE tournament_id=$1 AND key='GF2'",
                tid,
            )
            return bool(gf2 and gf2["status"] == "COMPLETED")
        return True

    async def _finalize_tournament(
        self, conn: asyncpg.Connection, tid: str, admin_id: int = 0,
    ) -> None:
        """Расставить места и закрыть турнир (в транзакции)."""
        matches = await conn.fetch(
            "SELECT * FROM ult2_matches WHERE tournament_id=$1", tid
        )
        entrants = await conn.fetch(
            "SELECT * FROM ult2_entrants WHERE tournament_id=$1", tid
        )
        t = await conn.fetchrow(
            "SELECT * FROM ult2_tournaments WHERE id=$1", tid
        )
        fmt = Format(t["format"])
        placements = _compute_placements(matches, entrants, fmt)

        for eid, (place, group) in placements.items():
            status = (
                "CHAMPION" if place == 1
                else "RUNNER_UP" if place == 2
                else "ELIMINATED"
            )
            await conn.execute(
                """
                UPDATE ult2_entrants
                SET final_place=$2, place_group=$3, status=$4
                WHERE id=$1
                """,
                eid, place, group, status,
            )

        champ = next((eid for eid, (p, _) in placements.items() if p == 1), None)
        await conn.execute(
            """
            UPDATE ult2_tournaments
            SET status='COMPLETED', completed_at=now(), champion_entrant_id=$2
            WHERE id=$1
            """,
            tid, champ,
        )
        await conn.execute(
            """
            INSERT INTO ult2_audit (tournament_id, actor_id, action, details)
            VALUES ($1,$2,'tournament_complete',$3::jsonb)
            """,
            tid, admin_id,
            __import__("json").dumps({"placements": {
                str(k): [v[0], v[1]] for k, v in placements.items()
            }}),
        )

    # ================= КОМНАТА =================

    async def set_room(
        self, match_db_id: int, user, room_id: str, room_password: str,
        is_org: bool = False,
    ) -> tuple[bool, str]:
        m = await self.store.get_match(match_db_id)
        if not m:
            return False, "Матч не найден."
        if m.status in ("COMPLETED", "CANCELLED"):
            return False, "Матч уже завершён — комнату изменить нельзя."
        # Права: участник матча или орг (проверка орга — снаружи)
        ent_a = await self.store.get_entrant(m.entrant_a) if m.entrant_a else None
        ent_b = await self.store.get_entrant(m.entrant_b) if m.entrant_b else None
        is_participant = any(
            e and user.id in e.member_ids for e in (ent_a, ent_b)
        )
        if not (is_participant or is_org):
            return False, "❌ Комнату могут указать только участники матча."
        await self.store.set_room(match_db_id, room_id.strip(), room_password.strip(), user.id)
        if m.status == MatchStatus.READY.value:
            await self.store.set_match_status(match_db_id, MatchStatus.IN_PROGRESS.value)
        return True, "✅ Данные комнаты сохранены."

    # ================= АДМИН =================

    async def admin_set_result(
        self, match_db_id: int, winner_entrant_id: int, admin,
    ) -> tuple[bool, str]:
        """Решение организатора по спорному/любому матчу."""
        m = await self.store.get_match(match_db_id)
        if not m:
            return False, "Матч не найден."
        if m.status == "COMPLETED":
            # Безопасная процедура: разрешено только если ни один
            # downstream-матч не завершён.
            if await self._has_completed_dependents(match_db_id):
                return False, (
                    "❌ Матч уже завершён, а зависимые матчи сыграны. "
                    "Автоматическая отмена небезопасна — требуется ручная "
                    "процедура исправления."
                )
            return False, "❌ Матч уже завершён."
        if m.status == "CANCELLED":
            return False, "Матч отменён."
        if m.entrant_a is None or m.entrant_b is None:
            return False, "Матч ещё не заполнен."
        if winner_entrant_id not in (m.entrant_a, m.entrant_b):
            return False, "Победитель должен быть одной из сторон."

        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                fin = await self._finalize_match(
                    conn, match_db_id, winner_entrant_id,
                    by_admin=True, admin_id=admin.id,
                )
        if fin is None:
            return False, "Не удалось зафиксировать результат."
        return True, f"✅ Результат установлен: победитель {fin.winner.label()}."

    async def _has_completed_dependents(self, match_db_id: int) -> bool:
        m = await self.store.get_match(match_db_id)
        if not m:
            return False
        matches = await self.store.get_matches(m.tournament_id)
        key = m.key
        for other in matches:
            if other.id == match_db_id or other.status != "COMPLETED":
                continue
            if (other.side_a_src == f"winner:{key}" or other.side_b_src == f"winner:{key}"
                    or other.side_a_src == f"loser:{key}" or other.side_b_src == f"loser:{key}"):
                return True
        return False

    async def admin_reopen_match(
        self, match_db_id: int, admin,
    ) -> tuple[bool, str]:
        """Вернуть DISPUTED/IN_PROGRESS матч в READY для повторного
        подтверждения (голоса очищаются)."""
        m = await self.store.get_match(match_db_id)
        if not m:
            return False, "Матч не найден."
        if m.status not in ("DISPUTED", "AWAITING_CONFIRMATION", "IN_PROGRESS"):
            return False, "❌ Матч не в состоянии спора/ожидания."
        if await self._has_completed_dependents(match_db_id):
            return False, "❌ Зависимые матчи уже сыграны — переоткрытие небезопасно."
        pool = await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE ult2_matches SET status='READY' WHERE id=$1",
                    match_db_id,
                )
                await conn.execute(
                    "DELETE FROM ult2_votes WHERE match_id=$1", match_db_id
                )
                await self.store.log(
                    m.tournament_id, admin.id, "admin_reopen",
                    {"match_id": match_db_id}, conn=conn,
                )
        return True, "✅ Матч возвращён на повторное подтверждение."

    # ================= ВОССТАНОВЛЕНИЕ =================

    async def resume_active(self, bot: discord.Client) -> None:
        """После рестарта: найти активные турниры и обновить карточки."""
        if self._resume_lock.locked():
            return
        async with self._resume_lock:
            await self._resume_active_locked(bot)

    async def _resume_active_locked(self, bot: discord.Client) -> None:
        try:
            tournaments = await self.store.list_active_all()
        except Exception as e:
            logger.error("resume_active: list failed: %s", e)
            return
        for t in tournaments:
            try:
                await self._refresh_tournament_messages(bot, t)
            except Exception as e:
                logger.error("resume refresh %s failed: %s", t.id, e)
        if tournaments:
            logger.info("Ультиматум: восстановлено турниров: %d", len(tournaments))

    async def _refresh_tournament_messages(
        self, bot: discord.Client, t: UltTournament,
    ) -> None:
        guild = bot.get_guild(t.guild_id)
        if not guild:
            return
        from views.ultimatum_view import refresh_all_ult_views
        await refresh_all_ult_views(bot, t)

    # ================= УДАЛЕНИЕ =================

    async def delete_tournament(
        self, tournament: UltTournament, bot=None,
    ) -> None:
        await self.store.log(
            tournament.id, tournament.created_by, "delete",
        )
        await self.store.delete_tournament(tournament.id)


# ---------- helpers ----------

def _slot_str(slot: Slot) -> str:
    return f"{slot.kind}:{slot.ref}"


def _row_to_match(r) -> UltMatch:
    return UltMatch(
        id=r["id"], tournament_id=r["tournament_id"], key=r["key"],
        bracket=r["bracket"], round_name=r["round_name"],
        gf_part=r["gf_part"], status=r["status"],
        side_a_src=r["side_a_src"], side_b_src=r["side_b_src"],
        entrant_a=r["entrant_a"], entrant_b=r["entrant_b"],
        winner_entrant=r["winner_entrant"], loser_entrant=r["loser_entrant"],
        room_id=r["room_id"], room_password=r["room_password"],
        room_updated_by=r["room_updated_by"], room_updated_at=r["room_updated_at"],
        created_at=r["created_at"], completed_at=r["completed_at"],
    )


def _decode_names(value) -> list[str]:
    if isinstance(value, str):
        try:
            import json as _j
            value = _j.loads(value)
        except Exception:
            return []
    return list(value or [])


def _row_to_entrant(r) -> UltEntrant:
    return UltEntrant(
        id=r["id"], tournament_id=r["tournament_id"], seed=r["seed"],
        member_ids=[int(x) for x in (r["member_ids"] or [])],
        member_names=_decode_names(r["member_names"]),
        wins=r["wins"], losses=r["losses"], status=r["status"],
        final_place=r["final_place"], place_group=r["place_group"],
    )


def _compute_placements(matches, entrants, fmt: Format):
    """Расчёт мест по завершённым матчам (зеркало bracket.compute_placements).

    matches/entrants — записи asyncpg. Возвращает
    {entrant_id: (final_place|None, place_group|None)}.
    """
    by_key = {m["key"]: m for m in matches}
    ent_by_id = {e["id"]: e for e in entrants}

    def loser_of(key):
        m = by_key.get(key)
        return m["loser_entrant"] if m and m["status"] == "COMPLETED" else None

    placements: dict[int, tuple[Optional[int], Optional[str]]] = {}

    gf1 = by_key.get("GF1")
    gf2 = by_key.get("GF2")
    if gf1 and gf1["status"] == "COMPLETED":
        w = ent_by_id.get(gf1["winner_entrant"])
        need_gf2 = bool(w and w["losses"] > 0)
        if not need_gf2 or (gf2 and gf2["status"] == "COMPLETED"):
            last = gf2 if need_gf2 else gf1
            if last and last["status"] == "COMPLETED":
                placements[last["winner_entrant"]] = (1, None)
                placements[last["loser_entrant"]] = (2, None)

    if fmt is Format.SOLO:
        groups = [
            ((3, 3), ["L6-1"]),
            ((4, 4), ["L5-1"]),
            ((5, 6), ["L4-1", "L4-2"]),
            ((7, 8), ["L3-1", "L3-2"]),
            ((9, 12), ["L2-1", "L2-2", "L2-3", "L2-4"]),
            ((13, 16), ["L1-1", "L1-2", "L1-3", "L1-4"]),
        ]
    else:
        groups = [
            ((3, 3), ["L4-1"]),
            ((4, 4), ["L3-1"]),
            ((5, 6), ["L2-1", "L2-2"]),
            ((7, 8), ["L1-1", "L1-2"]),
        ]

    for (lo, hi), keys in groups:
        label = f"{lo}–{hi}" if lo != hi else str(lo)
        exact = lo if lo == hi else None
        for k in keys:
            lid = loser_of(k)
            if lid and lid not in placements:
                placements[lid] = (exact, label)

    n = entrant_count(fmt)
    if len(placements) >= n or all(
        e["id"] in placements for e in entrants
    ):
        for e in entrants:
            if e["id"] not in placements:
                placements[e["id"]] = (None, f"{n}–{n}")
    return placements


ult_service = UltService()
