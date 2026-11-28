"""«Ультиматум» 2.0 — Double Elimination (Соло 1×1 / Дуо 2×2).

Команды живут в группе /tournament ультиматум *.
Все кнопки/селекты/модалки с custom_id "ult2:*" обрабатываются
здесь через on_interaction — переживают перезапуск бота.
"""

from __future__ import annotations

import logging
import random
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from config import GUILD_ID, ULT_ROOM_CHANNEL_ID, get_emoji
from ultimatum.bracket import Format, entrant_count
from ultimatum.service import ult_service, fmt_title, VOTABLE
from ultimatum.store import (
    DuoInvite, UltMatch, UltTournament, ultimatum_store,
)
from utils.design import DesignView, btn, colors, em, muted, text_card
from utils.permissions import is_org, is_org_check
from views.ultimatum_view import (
    CID, build_bracket_view, build_matches_panel, build_match_detail,
    build_org_menu, build_registration_view, load_context,
    post_or_update_bracket, refresh_all_ult_views,
)

logger = logging.getLogger(__name__)

ultimatum_group = app_commands.Group(
    name="ультиматум",
    description="Ультиматум — Double Elimination (Соло и Дуо)",
)

_fmt_choices = [
    app_commands.Choice(name="Соло (1×1)", value="solo"),
    app_commands.Choice(name="Дуо (2×2)", value="duo"),
]


def has_guild_tag(user) -> bool:
    return bool(user.primary_guild and user.primary_guild.id == GUILD_ID)


async def _can_join(t: UltTournament, user) -> tuple[bool, str]:
    if t.is_open or has_guild_tag(user):
        return True, ""
    return False, f"Закрытый турнир — нужен тег {get_emoji('guild_tag')}r!z3"


async def _vac_check(guild_id: int, *user_ids: int) -> Optional[str]:
    """VAC-проверка всех переданных игроков (solo и duo).

    Возвращает текст отказа с причиной/окончанием для первого
    заблокированного или None.
    """
    from services import vac_service
    for uid in user_ids:
        vac = await vac_service.check_vac(guild_id, int(uid))
        if vac is not None:
            ts = int(vac["expires_at"].timestamp())
            who = f"<@{uid}> " if len(user_ids) > 1 else ""
            return who + vac_service.vac_blocked_text(
                ts, reason=vac.get("reason"), level=vac.get("level"))
    return None


def _tid_num(tid: str) -> int:
    try:
        return int(tid[:8], 16)
    except Exception:
        return 0


class UltimatumRoomModal(discord.ui.Modal, title="Данные комнаты"):
    """Ввод ID комнаты и пароля для матча."""

    def __init__(self, match_db_id: int):
        super().__init__(
            title="Данные комнаты",
            custom_id=f"ult2room:{match_db_id}",
            timeout=300,
        )
        self.match_db_id = match_db_id
        self.room_input = discord.ui.TextInput(
            label="ID комнаты", placeholder="Например: 123456789", max_length=32,
        )
        self.pass_input = discord.ui.TextInput(
            label="Пароль", placeholder="Пароль комнаты", max_length=32,
            required=False,
        )
        self.add_item(self.room_input)
        self.add_item(self.pass_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # discord.py уже распарсил значения TextInput в item'ы
        room_id = (self.room_input.value or "").strip()
        room_password = (self.pass_input.value or "").strip()
        if room_id:
            await ultimatum_room_submit(
                interaction, self.match_db_id, room_id, room_password
            )
        else:
            await interaction.response.send_message(
                "Укажите ID комнаты.", ephemeral=True
            )


async def ultimatum_room_submit(
    interaction: discord.Interaction, match_db_id: int,
    room_id: str, room_password: str,
) -> None:
    if not room_id.strip():
        await interaction.response.send_message(
            "Укажите ID комнаты.", ephemeral=True
        )
        return
    from utils.permissions import is_org_check
    ok, msg = await ult_service.set_room(
        match_db_id, interaction.user, room_id, room_password or "",
        is_org=bool(interaction.guild) and is_org_check(
            interaction.user, interaction.guild
        ),
    )
    if not ok:
        await interaction.response.send_message(msg, ephemeral=True)
        return
    await interaction.response.send_message(f"{msg}", ephemeral=True)

    m = await ultimatum_store.get_match(match_db_id)
    # Уведомить соперника приватно
    try:
        if m:
            ents = {}
            for eid in (m.entrant_a, m.entrant_b):
                e = await ultimatum_store.get_entrant(eid) if eid else None
                if e:
                    ents[eid] = e
            for eid, e in ents.items():
                if interaction.user.id in e.member_ids:
                    continue
                for uid in e.member_ids:
                    try:
                        user = await interaction.client.fetch_user(uid)
                        await user.send(
                            f"Комната для вашего матча **{m.key}** "
                            f"({m.round_name}):\n"
                            f"ID: `{m.room_id or room_id}`\n"
                            f"Пароль: `{m.room_password or room_password or '—'}`"
                        )
                    except Exception:
                        pass
    except Exception as e:
        logger.warning("room notify failed: %s", e)

    # Публикация ID комнаты (без пароля) в настроенный канал
    await _publish_room_public(interaction, m, room_id)


async def _publish_room_public(interaction, m: Optional[UltMatch], room_id: str) -> None:
    """ID комнаты — в ULT_ROOM_CHANNEL_ID. Пароль НЕ публикуем."""
    if not ULT_ROOM_CHANNEL_ID or m is None:
        return
    try:
        channel = interaction.client.get_channel(ULT_ROOM_CHANNEL_ID)
        if channel is None:
            guild = interaction.guild
            if guild:
                channel = await guild.fetch_channel(ULT_ROOM_CHANNEL_ID)
        if channel is None:
            return
        await channel.send(
            f"**{m.key}** ({m.round_name}): комната `{room_id}` — "
            "пароль доступен участникам матча."
        )
    except Exception as e:
        logger.warning("room public post failed: %s", e)


class UltimatumCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = ult_service

    async def cog_load(self) -> None:
        from cogs.groups import bind_commands
        from cogs.tournament import tournament_group
        if ultimatum_group.parent is None:
            try:
                tournament_group.add_command(ultimatum_group)
            except Exception as e:
                logger.warning("ultimatum group attach failed: %s", e)
        bind_commands(ultimatum_group, self, [
            "создать", "удалить", "сетка", "статус", "история",
            "исправить", "test_fill", "test_sim",
        ])

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------

    @ultimatum_group.command(name="создать", description="Создать турнир Ультиматум")
    @app_commands.describe(формат="Соло 1×1 или Дуо 2×2")
    @app_commands.choices(формат=_fmt_choices)
    @is_org()
    async def ult_create(
        self, interaction: discord.Interaction,
        формат: app_commands.Choice[str],
    ) -> None:
        fmt = Format(формат.value)
        existing = await ultimatum_store.get_active_tournament(
            interaction.guild_id, fmt
        )
        if existing:
            await interaction.response.send_message(
                f"Уже есть активный турнир ({fmt_title(fmt)}). "
                "Завершите или удалите его.",
                ephemeral=True,
            )
            return

        name = f"{fmt_title(fmt)} #{interaction.created_at.strftime('%Y%m%d%H%M')}"
        try:
            t = await ultimatum_store.create_tournament(
                fmt, interaction.guild_id, name, interaction.user.id,
                channel_id=interaction.channel_id,
            )
        except Exception:
            # uq_ult2_active: параллельное создание того же формата
            await interaction.response.send_message(
                f"Уже есть активный турнир ({fmt_title(fmt)}).",
                ephemeral=True,
            )
            return
        ctx = await load_context(t.id)
        if ctx is None:
            await interaction.response.send_message("Ошибка создания.", ephemeral=True)
            return
        _, _, _, entrants, invites = ctx
        view = build_registration_view(t, entrants, invites)
        msg = await interaction.channel.send(view=view)
        await ultimatum_store.set_message_ids(t.id, message_id=msg.id)
        await ultimatum_store.log(t.id, interaction.user.id, "create")

        await interaction.response.send_message(
            f"Турнир **{name}** создан! Карточка регистрации ниже.",
            ephemeral=True,
        )
        logger.info("Ult2 %s created by %s", t.id, interaction.user.id)

    @ultimatum_group.command(name="удалить", description="Удалить активный турнир Ультиматум")
    @is_org()
    @app_commands.describe(формат="Формат турнира")
    @app_commands.choices(формат=_fmt_choices)
    async def ult_delete(
        self, interaction: discord.Interaction,
        формат: app_commands.Choice[str],
    ) -> None:
        fmt = Format(формат.value)
        t = await ultimatum_store.get_active_tournament(interaction.guild_id, fmt)
        if not t:
            await interaction.response.send_message(
                "Нет активного турнира этого формата.", ephemeral=True
            )
            return
        await self.service.delete_tournament(t, self.bot)
        await interaction.response.send_message(
            f"Турнир **{t.name}** удалён.", ephemeral=True
        )

    @ultimatum_group.command(name="сетка", description="Показать турнирную сетку")
    @app_commands.describe(формат="Формат турнира")
    @app_commands.choices(формат=_fmt_choices)
    async def ult_bracket(
        self, interaction: discord.Interaction,
        формат: app_commands.Choice[str],
    ) -> None:
        fmt = Format(формат.value)
        t = await ultimatum_store.get_active_tournament(interaction.guild_id, fmt)
        if not t:
            await interaction.response.send_message(
                "Нет активного турнира этого формата.", ephemeral=True
            )
            return
        await post_or_update_bracket(self.bot, t)
        await interaction.response.send_message(
            "Сетка обновлена в канале турнира.", ephemeral=True
        )

    @ultimatum_group.command(name="статус", description="Статус турнира Ультиматум")
    @app_commands.describe(формат="Формат турнира")
    @app_commands.choices(формат=_fmt_choices)
    async def ult_status(
        self, interaction: discord.Interaction,
        формат: app_commands.Choice[str],
    ) -> None:
        fmt = Format(формат.value)
        t = await ultimatum_store.get_active_tournament(interaction.guild_id, fmt)
        if not t:
            await interaction.response.send_message(
                "Нет активного турнира этого формата.", ephemeral=True
            )
            return
        ctx = await load_context(t.id)
        if ctx is None:
            await interaction.response.send_message("Ошибка загрузки.", ephemeral=True)
            return
        _, ent_map, matches, entrants, _ = ctx
        total = len(matches)
        done = sum(1 for m in matches if m.status == "COMPLETED")
        active = [m for m in matches if m.status in VOTABLE or m.status == "DISPUTED"]
        view = DesignView(
            title=f"{t.name}", accent=colors.TOURNAMENT, timeout=120,
        )
        view.text(
            f"**Формат:** {'Соло' if fmt is Format.SOLO else 'Дуо'}\n"
            f"**Статус:** {t.status}\n"
            f"**Матчи:** {done}/{total} завершено\n"
            f"**Активных:** {len(active)}\n"
            f"**Участников:** {len(entrants)}"
        )
        if active:
            view.sep()
            view.text("**Текущие матчи:**")
            for m in sorted(active, key=lambda x: x.key)[:10]:
                ea, eb = ent_map.get(m.entrant_a or -1), ent_map.get(m.entrant_b or -1)
                view.text(
                    f"• {m.key}: {ea.label() if ea else '?'} — {eb.label() if eb else '?'} ({m.status})"
                )
        await interaction.response.send_message(view=view, ephemeral=True)

    @ultimatum_group.command(name="история", description="История завершённых турниров Ультиматум")
    @app_commands.describe(формат="Формат (необязательно)")
    @app_commands.choices(формат=_fmt_choices)
    async def ult_history(
        self, interaction: discord.Interaction,
        формат: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        fmt = Format(формат.value) if формат else None
        history = await ultimatum_store.list_history(
            interaction.guild_id, fmt, limit=10
        )
        view = DesignView(
            title="История Ультиматума", accent=colors.TOURNAMENT, timeout=120,
        )
        if not history:
            view.text(muted("Завершённых турниров пока нет."))
        else:
            for t in history:
                champ = "?"
                if t.champion_entrant_id:
                    e = await ultimatum_store.get_entrant(t.champion_entrant_id)
                    if e:
                        champ = e.label()
                when = t.completed_at.strftime("%d.%m.%Y") if t.completed_at else "?"
                view.text(
                    f"• **{t.name}** — {'Соло' if t.format is Format.SOLO else 'Дуо'} — "
                    f"чемпион: {champ} ({when})"
                )
        await interaction.response.send_message(view=view, ephemeral=True)

    @ultimatum_group.command(name="исправить", description="Административное исправление результата матча")
    @is_org()
    @app_commands.describe(формат="Формат турнира")
    @app_commands.choices(формат=_fmt_choices)
    async def ult_fix(
        self, interaction: discord.Interaction,
        формат: app_commands.Choice[str],
    ) -> None:
        fmt = Format(формат.value)
        t = await ultimatum_store.get_active_tournament(interaction.guild_id, fmt)
        if not t:
            await interaction.response.send_message(
                "Нет активного турнира этого формата.", ephemeral=True
            )
            return
        await _open_fix_panel(interaction, t)

    # ------------------------------------------------------------------
    # Тестовые команды (для /tournament test)
    # ------------------------------------------------------------------

    @ultimatum_group.command(name="test_fill", description="Заполнить Ультиматум тестовыми игроками")
    @is_org()
    @app_commands.describe(формат="Формат (по умолчанию — активный)")
    @app_commands.choices(формат=_fmt_choices)
    async def test_fill(
        self, interaction: discord.Interaction,
        формат: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        fmt = Format(формат.value) if формат else None
        if fmt is None:
            for f in (Format.SOLO, Format.DUO):
                t = await ultimatum_store.get_active_tournament(
                    interaction.guild_id, f
                )
                if t:
                    fmt = f
                    break
        if fmt is None:
            await interaction.response.send_message(
                "Нет активного турнира. Сначала создайте его.",
                ephemeral=True,
            )
            return
        t = await ultimatum_store.get_active_tournament(interaction.guild_id, fmt)
        if not t:
            await interaction.response.send_message(
                "Нет активного турнира этого формата.", ephemeral=True
            )
            return
        if t.status != "REGISTRATION":
            await interaction.response.send_message(
                "Турнир не в фазе регистрации.", ephemeral=True
            )
            return

        need = entrant_count(fmt)
        entrants = await ultimatum_store.get_entrants(t.id)
        if len(entrants) >= need:
            await interaction.response.send_message(
                f"Уже заполнен ({len(entrants)}/{need}).", ephemeral=True
            )
            return

        base = 9_200_000 + (_tid_num(t.id) % 900) * 100
        added = 0
        if fmt is Format.SOLO:
            for i in range(len(entrants), need):
                uid = base + i
                try:
                    await ultimatum_store.add_entrant(
                        t.id, [uid], [f"TestP{i+1}"],
                    )
                    added += 1
                except Exception:
                    pass
        else:
            teams_needed = need - len(entrants)
            for i in range(teams_needed):
                idx = len(entrants) + i
                await ultimatum_store.add_entrant(
                    t.id,
                    [base + idx * 2, base + idx * 2 + 1],
                    [f"BotA{idx+1}", f"BotB{idx+1}"],
                )
                added += 1

        # Тестовое заполнение — турнир не должен менять ELO
        await ultimatum_store.set_is_test(t.id, True)

        # VAC-sweep + start
        await interaction.response.send_message(
            f"Добавлено {added}. Запускаю турнир...", ephemeral=True
        )
        t = await ultimatum_store.get_tournament(t.id)
        ok, msg = await self.service.start(t, self.bot)
        if ok:
            await post_or_update_bracket(self.bot, t)
            await refresh_all_ult_views(self.bot, t)
        await interaction.followup.send(msg, ephemeral=True)

    @ultimatum_group.command(name="test_sim", description="Автотест: заполнить и доиграть турнир случайно")
    @is_org()
    @app_commands.describe(формат="Формат")
    @app_commands.choices(формат=_fmt_choices)
    async def test_sim(
        self, interaction: discord.Interaction,
        формат: app_commands.Choice[str],
    ) -> None:
        fmt = Format(формат.value)
        # Найти или создать тестовый турнир
        t = await ultimatum_store.get_active_tournament(interaction.guild_id, fmt)
        if t is None:
            t = await ultimatum_store.create_tournament(
                fmt, interaction.guild_id,
                f"TEST {fmt_title(fmt)}", interaction.user.id,
                channel_id=interaction.channel_id, is_test=True,
            )
        elif t.status == "COMPLETED":
            await interaction.response.send_message(
                "Предыдущий тестовый турнир завершён — удалите его.", ephemeral=True
            )
            return
        t.is_test = True

        # Заполнить при необходимости
        need = entrant_count(fmt)
        entrants = await ultimatum_store.get_entrants(t.id)
        if t.status == "REGISTRATION" and len(entrants) < need:
            base = 9_300_000 + (_tid_num(t.id) % 900) * 100
            if fmt is Format.SOLO:
                for i in range(len(entrants), need):
                    await ultimatum_store.add_entrant(
                        t.id, [base + i], [f"Sim{i+1}"],
                    )
            else:
                for i in range(len(entrants), need):
                    await ultimatum_store.add_entrant(
                        t.id,
                        [base + i * 2, base + i * 2 + 1],
                        [f"SimA{i+1}", f"SimB{i+1}"],
                    )
        await ultimatum_store.set_is_test(t.id, True)
        t = await ultimatum_store.get_tournament(t.id)
        ok, msg = await self.service.start(t, self.bot)
        if not ok:
            await interaction.response.send_message(msg, ephemeral=True)
            return

        # Автопрохождение: случайный победитель в каждом готовом матче
        await interaction.response.send_message(
            "Автосимуляция запущена...", ephemeral=True
        )
        played = 0
        guard = 0
        while guard < 200:
            guard += 1
            matches = await ultimatum_store.get_matches(t.id)
            ready = [m for m in matches if m.status in VOTABLE]
            if not ready:
                break
            m = ready[0]
            winner = random.choice([m.entrant_a, m.entrant_b])
            # Прямая фиксация через транзакцию сервиса
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                async with conn.transaction():
                    await self.service._finalize_match(
                        conn, m.id, winner, by_admin=False,
                    )
            played += 1
        t = await ultimatum_store.get_tournament(t.id)
        matches = await ultimatum_store.get_matches(t.id)
        done = sum(1 for x in matches if x.status == "COMPLETED")
        await interaction.followup.send(
            f"Симуляция завершена: сыграно {played} матчей, "
            f"статус турнира: {t.status}, завершено {done}/{len(matches)}.",
            ephemeral=True,
        )
        await post_or_update_bracket(self.bot, t)

    # ------------------------------------------------------------------
    # Восстановление после рестарта
    # ------------------------------------------------------------------

    async def cog_load_resume(self) -> None:
        """Вызывается из bot.on_ready — восстановить активные турниры."""
        try:
            await self.service.resume_active(self.bot)
        except Exception as e:
            logger.error("ult resume failed: %s", e)

    # ------------------------------------------------------------------
    # Диспетчер взаимодействий (custom_id "ult2:*")
    # ------------------------------------------------------------------

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        if not interaction.data:
            return
        cid = interaction.data.get("custom_id") or ""
        if not cid.startswith(f"{CID}:"):
            return
        try:
            await self._dispatch(interaction, cid)
        except Exception as e:
            logger.error("ult2 dispatch %s failed: %s", cid, e, exc_info=True)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Внутренняя ошибка. Попробуйте позже.", ephemeral=True
                    )
            except Exception:
                pass

    async def _dispatch(self, interaction: discord.Interaction, cid: str) -> None:
        parts = cid.split(":")
        # ult2:<action>[:args...]
        action = parts[1] if len(parts) > 1 else ""
        rest = parts[2:]

        # Модалка сабмита не приходит как custom_id кнопки — Discord
        # шлёт custom_id модалки. Мы используем префикс ult2room:.
        if cid.startswith("ult2room:"):
            return  # обрабатывается в Modal.on_submit

        if action == "join":
            await self._act_join(interaction, rest[0])
        elif action == "leave":
            await self._act_leave(interaction, rest[0])
        elif action == "duo":
            await self._act_duo(interaction, rest)
        elif action == "org":
            await self._act_org(interaction, rest)
        elif action == "br":
            await self._act_bracket(interaction, rest)
        elif action == "match":
            await self._act_match(interaction, rest, interaction.data)
        elif action == "vote":
            await self._act_vote(interaction, int(rest[0]), rest[1])
        elif action == "room":
            await self._act_room(interaction, int(rest[0]))
        elif action == "fixwin":
            await self._act_fixwin(interaction, int(rest[0]), rest[1])
        elif action == "reopen":
            await self._act_reopen(interaction, int(rest[0]))
        elif action == "fixpick":
            await self._act_fixpick(interaction, rest[0], interaction.data)
        else:
            await interaction.response.send_message(
                "Неизвестное действие.", ephemeral=True
            )

    # ---------------- регистрация ----------------

    async def _get_tournament(self, tid: str) -> Optional[UltTournament]:
        return await ultimatum_store.get_tournament(tid)

    async def _act_join(self, interaction, tid: str) -> None:
        t = await self._get_tournament(tid)
        if not t:
            return await interaction.response.send_message("Турнир не найден.", ephemeral=True)
        ok, why = await _can_join(t, interaction.user)
        if not ok:
            return await interaction.response.send_message(why, ephemeral=True)
        vac = await _vac_check(t.guild_id, interaction.user.id)
        if vac:
            return await interaction.response.send_message(vac, ephemeral=True)
        msg = await self.service.register_solo(t, interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)
        t = await self._get_tournament(tid)
        await refresh_all_ult_views(self.bot, t)

    async def _act_leave(self, interaction, tid: str) -> None:
        t = await self._get_tournament(tid)
        if not t:
            return await interaction.response.send_message("Турнир не найден.", ephemeral=True)
        if t.format is Format.DUO:
            msg = await self.service.duo_leave(t, interaction.user)
        else:
            msg = await self.service.leave(t, interaction.user)
        await interaction.response.send_message(msg, ephemeral=True)
        t = await self._get_tournament(tid)
        await refresh_all_ult_views(self.bot, t)

    # ---------------- дуэты ----------------

    async def _act_duo(self, interaction, rest: list[str]) -> None:
        sub = rest[0] if rest else ""
        tid = rest[1] if len(rest) > 1 else ""
        if sub == "new":
            t = await self._get_tournament(tid)
            if not t or t.format is not Format.DUO:
                return await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            # Уже в дуэте?
            ent = await ultimatum_store.find_entrant_by_member(t.id, interaction.user.id)
            if ent:
                return await interaction.response.send_message(
                    "Вы уже в дуэте.", ephemeral=True
                )
            pending = await ultimatum_store.get_pending_invite_for(t.id, interaction.user.id)
            if pending:
                return await interaction.response.send_message(
                    f"У вас уже есть приглашение "
                    f"({'исходящее' if pending.captain_id == interaction.user.id else 'входящее'}) "
                    f"для <@{pending.partner_id or pending.captain_id}>.",
                    ephemeral=True,
                )
            view = DesignView(title="Выбор напарника", accent=colors.TOURNAMENT, timeout=180)
            view.text("Выберите второго игрока дуэта:")
            sel = discord.ui.UserSelect(
                placeholder="Выберите напарника",
                custom_id=f"{CID}:duo:pick:{t.id}",
                min_values=1, max_values=1,
            )
            view.row(sel)
            await interaction.response.send_message(view=view, ephemeral=True)

        elif sub == "pick":
            t = await self._get_tournament(tid)
            if not t:
                return await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            values = interaction.data.get("values") or []
            if not values:
                return await interaction.response.send_message("Игрок не выбран.", ephemeral=True)
            partner_id = int(values[0])
            partner = interaction.client.get_user(partner_id)
            if partner is None:
                try:
                    partner = await interaction.client.fetch_user(partner_id)
                except Exception:
                    return await interaction.response.send_message(
                        "Не удалось найти этого пользователя.", ephemeral=True
                    )
            inv_id, msg = await self.service.duo_create_invite(
                t, interaction.user, partner
            )
            await interaction.response.send_message(msg, ephemeral=True)
            if inv_id is None:
                return
            # DM приглашение; если закрыты — fallback в канал турнира
            try:
                puser = await interaction.client.fetch_user(partner_id)
                iview = _invite_view(t, inv_id, interaction.user.id)
                dm = await puser.send(view=iview)
                await _set_invite_message(inv_id, dm.id)
            except Exception as e:
                logger.warning("duo invite DM failed: %s", e)
                try:
                    await _post_invite_to_channel(
                        interaction, t, inv_id, interaction.user.id,
                        partner_id,
                    )
                    await interaction.followup.send(
                        "Личные сообщения игрока закрыты — "
                        "приглашение опубликовано в канале турнира.",
                        ephemeral=True,
                    )
                except Exception as e2:
                    logger.warning("duo invite channel fallback failed: %s", e2)
                    await interaction.followup.send(
                        "Не удалось доставить приглашение: у игрока "
                        "закрыты DM, а канал турнира недоступен. "
                        "Отправьте приглашение позже.",
                        ephemeral=True,
                    )
            await refresh_all_ult_views(self.bot, t)

        elif sub in ("accept", "decline"):
            iid = int(rest[1]) if len(rest) > 1 else 0
            inv = await ultimatum_store.get_invite(iid)
            if not inv:
                return await interaction.response.send_message("Приглашение не найдено.", ephemeral=True)
            ok, msg = await self.service.duo_respond(inv, interaction.user, sub == "accept")
            await interaction.response.send_message(msg, ephemeral=True)
            t = await self._get_tournament(inv.tournament_id)
            if t:
                await refresh_all_ult_views(self.bot, t)
                # Обновить DM-сообщение (со свежим статусом)
                fresh = await ultimatum_store.get_invite(iid)
                await _update_invite_dm(interaction.client, fresh or inv)

        elif sub == "leave":
            t = await self._get_tournament(tid)
            if not t:
                return await interaction.response.send_message("Турнир не найден.", ephemeral=True)
            # Сначала роспуск дуэта; если игрок не в команде —
            # отменить его висящее приглашение.
            msg = await self.service.duo_leave(t, interaction.user)
            cancelled_inv = None
            if "не состоите" in msg:
                pending = await ultimatum_store.get_pending_invite_for(
                    t.id, interaction.user.id
                )
                msg = await self.service.duo_cancel_invite(t, interaction.user)
                if pending and pending.captain_id == interaction.user.id:
                    cancelled_inv = pending
            await interaction.response.send_message(msg, ephemeral=True)
            if cancelled_inv is not None:
                cancelled_inv.status = "cancelled"
                await _update_invite_dm(interaction.client, cancelled_inv)
            await refresh_all_ult_views(self.bot, t)

    # ---------------- орг ----------------

    async def _act_org(self, interaction, rest: list[str]) -> None:
        sub = rest[0] if rest else ""
        tid = rest[1] if len(rest) > 1 else ""
        if not is_org_check(interaction.user, interaction.guild):
            return await interaction.response.send_message(
                "Только организаторы.", ephemeral=True
            )
        t = await self._get_tournament(tid)
        if not t:
            return await interaction.response.send_message("Турнир не найден.", ephemeral=True)

        if sub == "menu" or sub == "":
            await interaction.response.send_message(
                view=build_org_menu(t), ephemeral=True
            )
        elif sub == "start":
            await interaction.response.defer(ephemeral=True)
            # VAC-sweep
            entrants = await ultimatum_store.get_entrants(t.id)
            all_ids = [uid for e in entrants for uid in e.member_ids]
            vac_ids = set()
            if all_ids:
                from services import vac_service
                vac_ids = await vac_service.vac_active_ids(t.guild_id, all_ids)
            if vac_ids:
                for e in entrants:
                    if any(uid in vac_ids for uid in e.member_ids):
                        await ultimatum_store.remove_entrant(e.id)
                await interaction.followup.send(
                    "Исключены участники с активным VAC. Нажмите «Старт» ещё раз.",
                    ephemeral=True,
                )
                return
            ok, msg = await self.service.start(t, self.bot)
            await interaction.followup.send(msg, ephemeral=True)
            if ok:
                t = await self._get_tournament(tid)
                await post_or_update_bracket(self.bot, t)
                await refresh_all_ult_views(self.bot, t)
        elif sub == "open":
            await interaction.response.defer(ephemeral=True)
            t = await self._get_tournament(tid)
            await ultimatum_store.set_open(t.id, not t.is_open)
            t = await self._get_tournament(tid)
            await interaction.followup.send(
                f"Регистрация {'открыта' if t.is_open else 'закрыта'}.",
                ephemeral=True,
            )
            await refresh_all_ult_views(self.bot, t)
        elif sub == "delete":
            await interaction.response.defer(ephemeral=True)
            await self.service.delete_tournament(t, self.bot)
            await interaction.followup.send("Турнир удалён.", ephemeral=True)
            # Удалить сообщения
            try:
                for mid in (t.message_id, t.bracket_message_id):
                    if mid:
                        ch = self.bot.get_channel(t.channel_id)
                        if ch:
                            msg = await ch.fetch_message(mid)
                            await msg.delete()
            except Exception:
                pass
        elif sub == "fix":
            await interaction.response.defer(ephemeral=True)
            await _open_fix_panel(interaction, t, followup=True)

    # ---------------- сетка ----------------

    async def _act_bracket(self, interaction, rest: list[str]) -> None:
        sub = rest[0] if rest else ""
        tid = rest[1] if len(rest) > 1 else ""
        t = await self._get_tournament(tid)
        if not t:
            return await interaction.response.send_message("Турнир не найден.", ephemeral=True)
        if sub == "refresh":
            await refresh_all_ult_views(self.bot, t)
            return await interaction.response.send_message(
                "Сетка обновлена.", ephemeral=True
            )
        # open / matches / png / default → ephemeral bracket
        ctx = await load_context(t.id)
        if ctx is None:
            return await interaction.response.send_message("Ошибка.", ephemeral=True)
        _, ent_map, matches, _, _ = ctx
        if sub == "png":
            await interaction.response.defer(ephemeral=True)
            try:
                from ultimatum.bracket_png import render_bracket_png
                png = render_bracket_png(t, matches, ent_map)
                await interaction.followup.send(
                    file=discord.File(
                        __import__("io").BytesIO(png),
                        filename=f"ult_{t.id[:6]}.png",
                    ),
                    ephemeral=True,
                )
            except Exception as e:
                logger.error("bracket png failed: %s", e, exc_info=True)
                await interaction.followup.send(
                    "Не удалось сгенерировать изображение сетки.",
                    ephemeral=True,
                )
            return
        if sub == "matches":
            view = build_matches_panel(t, matches, ent_map)
        else:
            view = build_bracket_view(t, matches, ent_map)
        await interaction.response.send_message(view=view, ephemeral=True)

    # ---------------- матчи ----------------

    async def _act_match(self, interaction, rest: list[str], data: dict) -> None:
        sub = rest[0] if rest else ""
        tid = rest[1] if len(rest) > 1 else ""
        t = await self._get_tournament(tid)
        if not t:
            return await interaction.response.send_message("Турнир не найден.", ephemeral=True)
        ctx = await load_context(t.id)
        if ctx is None:
            return await interaction.response.send_message("Ошибка.", ephemeral=True)
        _, ent_map, matches, _, _ = ctx
        viewer_is_org = is_org_check(interaction.user, interaction.guild)

        async def _detail(mid: int):
            m = next((x for x in matches if x.id == mid), None)
            if m is None:
                return None
            votes = await ultimatum_store.get_votes(mid)
            return build_match_detail(
                t, m, ent_map, votes,
                viewer_id=interaction.user.id, is_org=viewer_is_org,
            )

        if sub == "pick":
            values = data.get("values") or []
            if values:
                view = await _detail(int(values[0]))
                if view is None:
                    return await interaction.response.send_message("Матч не найден.", ephemeral=True)
                return await interaction.response.send_message(view=view, ephemeral=True)
            view = build_matches_panel(t, matches, ent_map)
            return await interaction.response.send_message(view=view, ephemeral=True)

        if sub == "sel":
            values = data.get("values") or []
            if not values:
                return
            view = await _detail(int(values[0]))
            if view is None:
                return await interaction.response.send_message("Матч не найден.", ephemeral=True)
            return await interaction.response.send_message(view=view, ephemeral=True)

    async def _act_vote(self, interaction, match_db_id: int, side: str) -> None:
        await interaction.response.defer(ephemeral=True)
        outcome = await self.service.cast_vote(match_db_id, interaction.user, side)
        if outcome.kind == "error":
            return await interaction.followup.send(outcome.message, ephemeral=True)
        if outcome.kind == "disputed":
            await interaction.followup.send(outcome.message, ephemeral=True)
            await self._post_match_update(interaction, outcome)
            return
        if outcome.kind == "completed":
            await interaction.followup.send(outcome.message, ephemeral=True)
            await self._post_match_update(interaction, outcome)
            return
        await interaction.followup.send(outcome.message, ephemeral=True)

    async def _post_match_update(self, interaction, outcome) -> None:
        """Обновить карточки после фиксации/спора."""
        t = outcome.tournament
        if t is None:
            m = await ultimatum_store.get_match(outcome.match.id) if outcome.match else None
            if m:
                t = await ultimatum_store.get_tournament(m.tournament_id)
        if t is None:
            return
        try:
            await post_or_update_bracket(self.bot, t)
            await refresh_all_ult_views(self.bot, t)
        except Exception as e:
            logger.warning("post_match_update failed: %s", e)
        # Уведомление организатора о споре/финале
        try:
            if outcome.kind == "disputed":
                org = self.bot.get_user(t.created_by)
                if org:
                    await org.send(
                        f"Спор в матче **{outcome.match.key if outcome.match else '?'}** "
                        f"турнира {t.name}. Используйте /tournament ультиматум исправить."
                    )
            t2 = await ultimatum_store.get_tournament(t.id)
            if t2 and t2.status == "COMPLETED":
                await _announce_final(self.bot, t2)
        except Exception:
            pass

    async def _act_room(self, interaction, match_db_id: int) -> None:
        # Проверка прав до модалки
        m = await ultimatum_store.get_match(match_db_id)
        if not m:
            return await interaction.response.send_message("Матч не найден.", ephemeral=True)
        from utils.permissions import is_org, is_org_check
        is_org = is_org_check(interaction.user, interaction.guild)
        is_participant = False
        for eid in (m.entrant_a, m.entrant_b):
            e = await ultimatum_store.get_entrant(eid) if eid else None
            if e and interaction.user.id in e.member_ids:
                is_participant = True
        if not (is_org or is_participant):
            return await interaction.response.send_message(
                "Данные комнаты видят только участники матча и организаторы.",
                ephemeral=True,
            )
        await interaction.response.send_modal(UltimatumRoomModal(match_db_id))

    # ---------------- исправление ----------------

    async def _act_fixpick(self, interaction, tid: str, data: dict) -> None:
        """Выбор матча в панели исправления → кнопки победителя."""
        values = data.get("values") or []
        if not values:
            return
        match_db_id = int(values[0])
        m = await ultimatum_store.get_match(match_db_id)
        if not m:
            return await interaction.response.send_message("Матч не найден.", ephemeral=True)
        ent_map = {e.id: e for e in await ultimatum_store.get_entrants(m.tournament_id)}
        ea = ent_map.get(m.entrant_a or -1)
        eb = ent_map.get(m.entrant_b or -1)
        view = DesignView(
            title=f"Матч {m.key} — выбор победителя",
            accent=colors.DANGER, timeout=120,
        )
        view.text(
            f"**A:** {ea.label() if ea else '?'}\n"
            f"**B:** {eb.label() if eb else '?'}\n"
            f"**Статус:** {m.status}"
        )
        view.sep()
        view.row(
            btn(f"Победа A: {(ea.label() if ea else '?')[:40]}", style="success",
                custom_id=f"{CID}:fixwin:{match_db_id}:a"),
            btn(f"Победа B: {(eb.label() if eb else '?')[:40]}", style="success",
                custom_id=f"{CID}:fixwin:{match_db_id}:b"),
        )
        if m.status == "DISPUTED":
            view.row(
                btn("Переоткрыть для подтверждения", style="secondary",
                    custom_id=f"{CID}:reopen:{match_db_id}"),
            )
        await interaction.response.send_message(view=view, ephemeral=True)

    async def _act_fixwin(self, interaction, match_db_id: int, side: str) -> None:
        from utils.permissions import is_org, is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            return await interaction.response.send_message(
                "Только организаторы.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        m = await ultimatum_store.get_match(match_db_id)
        if not m:
            return await interaction.followup.send("Матч не найден.", ephemeral=True)
        winner_eid = m.entrant_a if side == "a" else m.entrant_b
        ok, msg = await self.service.admin_set_result(match_db_id, winner_eid, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)
        if ok:
            t = await ultimatum_store.get_tournament(m.tournament_id)
            await post_or_update_bracket(self.bot, t)
            await refresh_all_ult_views(self.bot, t)
            if t and t.status == "COMPLETED":
                await _announce_final(self.bot, t)

    async def _act_reopen(self, interaction, match_db_id: int) -> None:
        from utils.permissions import is_org, is_org_check
        if not is_org_check(interaction.user, interaction.guild):
            return await interaction.response.send_message(
                "Только организаторы.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        ok, msg = await self.service.admin_reopen_match(match_db_id, interaction.user)
        await interaction.followup.send(msg, ephemeral=True)


async def _announce_final(bot: commands.Bot, t: UltTournament) -> None:
    """Итоговая карточка турнира: чемпион + места."""
    try:
        entrants = await ultimatum_store.get_entrants(t.id)
        ent_map = {e.id: e for e in entrants}
        champ = ent_map.get(t.champion_entrant_id or -1)
        view = DesignView(
            title=f"{t.name} — итоги", accent=colors.TOURNAMENT, timeout=None,
        )
        view.text(
            f"**Чемпион:** {champ.label() if champ else '?'}\n"
            f"**Формат:** {'Соло' if t.format is Format.SOLO else 'Дуо'}"
        )
        view.sep()
        # Места 1–4 точно, дальше группы
        ordered = sorted(
            entrants,
            key=lambda e: (e.final_place or 99, e.place_group or ""),
        )
        for e in ordered:
            place_txt = (
                f"{e.final_place} место" if e.final_place
                else f"{e.place_group} места" if e.place_group else "—"
            )
            view.text(f"• {place_txt}: {e.label()} (`{e.wins}:{e.losses}`)")
        # В канал турнира
        if t.channel_id:
            channel = bot.get_channel(t.channel_id)
            if channel is None:
                guild = bot.get_guild(t.guild_id)
                if guild:
                    channel = await guild.fetch_channel(t.channel_id)
            if channel:
                await channel.send(view=view)
        # DM организатору
        try:
            org = bot.get_user(t.created_by) or await bot.fetch_user(t.created_by)
            await org.send(view=view)
        except Exception:
            pass
    except Exception as e:
        logger.warning("announce_final failed: %s", e)


def _invite_view(
    t: UltTournament, invite_id: int, captain_id: int,
) -> DesignView:
    """View приглашения в дуэт (DM или канал — кнопки одинаковые)."""
    iview = DesignView(
        title="Приглашение в дуэт — Ультиматум",
        accent=colors.TOURNAMENT, timeout=None,
    )
    iview.text(
        f"<@{captain_id}> приглашает вас в дуэт "
        f"на турнир **{t.name}**."
    )
    iview.row(
        btn("Принять", style="success", custom_id=f"{CID}:duo:accept:{invite_id}"),
        btn("Отклонить", style="danger", custom_id=f"{CID}:duo:decline:{invite_id}"),
    )
    return iview


async def _post_invite_to_channel(
    interaction: discord.Interaction, t: UltTournament,
    invite_id: int, captain_id: int, partner_id: int,
) -> None:
    """Fallback: опубликовать инвайт в канале турнира.

    Кнопки работают из любого сообщения — диспетчер идёт по
    invite_id, а duo_respond проверяет, что ответил именно
    приглашённый игрок.
    """
    ch = interaction.channel
    if ch is None and t.channel_id:
        try:
            ch = await interaction.client.fetch_channel(t.channel_id)
        except Exception:
            ch = None
    if ch is None:
        raise RuntimeError("no channel")
    msg = await ch.send(
        content=f"<@{partner_id}>",
        view=_invite_view(t, invite_id, captain_id),
        allowed_mentions=discord.AllowedMentions(users=[discord.Object(partner_id)]),
    )
    await _set_invite_message(invite_id, msg.id)


async def _set_invite_message(invite_id: int, message_id: int) -> None:
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE ult2_duo_invites SET message_id=$2 WHERE id=$1",
            invite_id, message_id,
        )


async def _update_invite_dm(client, invite: DuoInvite) -> None:
    """Пометить сообщение-приглашение как обработанное.

    message_id может указывать на DM (обычный случай) или на
    сообщение в канале турнира (fallback при закрытых DM) —
    пробуем оба варианта.
    """
    if not invite.message_id or not invite.partner_id:
        return
    msg = None
    try:
        user = await client.fetch_user(invite.partner_id)
        msg = await user.fetch_message(invite.message_id)
    except Exception:
        msg = None
    if msg is None:
        try:
            t = await ultimatum_store.get_tournament(invite.tournament_id)
            ch = client.get_channel(t.channel_id) if t else None
            if ch is None and t and t.channel_id:
                ch = await client.fetch_channel(t.channel_id)
            if ch is not None:
                msg = await ch.fetch_message(invite.message_id)
        except Exception:
            msg = None
    if msg is None:
        return
    try:
        view = DesignView(
            title="Приглашение в дуэт — Ультиматум",
            accent=colors.TOURNAMENT, timeout=None,
        )
        status_txt = {
            "accepted": "Принято",
            "declined": "Отклонено",
            "cancelled": "Отменено",
            "expired": "⌛ Истекло",
        }.get(invite.status, invite.status)
        view.text(f"Приглашение: **{status_txt}**")
        await msg.edit(view=view)
    except Exception as e:
        logger.warning("invite message update failed: %s", e)


async def _open_fix_panel(
    interaction: discord.Interaction, t: UltTournament,
    followup: bool = False,
) -> None:
    """Панель исправления: выбор матча → выбор победителя."""
    matches = await ultimatum_store.get_matches(t.id)
    candidates = [
        m for m in matches
        if m.status in ("DISPUTED", "READY", "IN_PROGRESS",
                        "AWAITING_CONFIRMATION")
    ]
    if not candidates:
        # Также показываем завершённые без сыгранных потомков —
        # но безопаснее предложить только незавершённые/спорные.
        text = "Нет матчей, доступных для исправления."
        view = DesignView(title="Исправление", accent=colors.DANGER, timeout=120)
        view.text(text)
        if followup:
            await interaction.followup.send(view=view, ephemeral=True)
        else:
            await interaction.response.send_message(view=view, ephemeral=True)
        return

    ent_map = {e.id: e for e in await ultimatum_store.get_entrants(t.id)}
    view = DesignView(
        title="Исправление результата", accent=colors.DANGER, timeout=180,
    )
    view.text("Выберите матч для исправления:")
    view.sep()
    for m in sorted(candidates, key=lambda x: x.key)[:20]:
        ea, eb = ent_map.get(m.entrant_a or -1), ent_map.get(m.entrant_b or -1)
        view.text(
            f"• **{m.key}** ({m.status}): "
            f"{ea.label() if ea else '?'} — {eb.label() if eb else '?'}"
        )
    # Select для выбора матча
    options = [
        discord.SelectOption(
            label=f"{m.key}: {(ent_map.get(m.entrant_a or -1).label() if ent_map.get(m.entrant_a or -1) else '?')} vs {(ent_map.get(m.entrant_b or -1).label() if ent_map.get(m.entrant_b or -1) else '?')}",
            value=str(m.id),
        )[:80]
        for m in sorted(candidates, key=lambda x: x.key)[:25]
    ]
    sel = discord.ui.Select(
        placeholder="Матч для исправления",
        custom_id=f"{CID}:fixpick:{t.id}",
        options=options,
    )
    view.row(sel)
    if followup:
        await interaction.followup.send(view=view, ephemeral=True)
    else:
        await interaction.response.send_message(view=view, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(UltimatumCog(bot))
    logger.info("Cog 'ultimatum' (v2) loaded")
