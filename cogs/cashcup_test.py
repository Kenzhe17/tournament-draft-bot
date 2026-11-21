"""r!z3 CASH CUP · TEST — изолированный прогон настоящего Cash Cup.

Команда `/тест` (только ADMIN) открывает панель управления тестовой
сессией. Сессии хранятся в cashcup_test_sessions — настоящая таблица
cashcup_tournaments не затрагивается вообще. Все действия помечаются
TEST: виртуальные check-in, фиктивные комнаты, evidence "TEST:...".

Импорт снимка команд из production выполняется заранее одной командой:
    python tools/cashcup_snapshot.py export --out snapshot.json   # prod, RO
    python tools/cashcup_snapshot.py import --file snapshot.json --guild <staging>
"""
from __future__ import annotations

import logging
from copy import deepcopy
from datetime import UTC

import discord
from discord import app_commands
from discord.ext import commands

from services import moderation_service
from services.cashcup_bracket import (
    BracketError,
    disqualify,
    ready_matches,
    record_final_game,
    record_result,
)
from services.cashcup_sim import simulate_to_final
from services.cashcup_test_store import (
    CashCupTestError,
    cashcup_test_store,
)

logger = logging.getLogger(__name__)

_TEST_PREFIX = "cctest"
_MAX_BUTTONS_PER_ROW = 5


def _is_admin(user) -> bool:
    return isinstance(user, discord.Member) and moderation_service.is_admin(user)


def _cid(sid: str, action: str) -> str:
    return f"{_TEST_PREFIX}:{sid}:{action}"[:100]


def _parse_cid(custom_id: str) -> tuple[str, str] | None:
    parts = str(custom_id).split(":")
    if len(parts) != 3 or parts[0] != _TEST_PREFIX:
        return None
    return parts[1], parts[2]


def _esc(text) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(text)))


def _uid(p: dict) -> str:
    return f"`{p.get('ff_uid') or '—'}`"


def _member_line(p: dict) -> str:
    flags = []
    if p.get("ff_ok") or p.get("ff_uid"):
        flags.append("FF✓")
    if p.get("verified"):
        flags.append("ADMIN✓")
    flag_s = (" " + " ".join(flags)) if flags else ""
    return (f"- {_esc(p.get('nick') or '—')} · FF {_uid(p)} · "
            f"{p.get('region') or '—'}/{p.get('device') or '—'}"
            f"{flag_s} · did `{p.get('discord_id')}`")


def _team_detail(state: dict, team: dict) -> str:
    members = team.get("members") or []
    lineup = set(team.get("lineup") or [])
    lines = [f"**{_esc(team.get('name'))}** (id `{team.get('id')}`)",
             (f"Капитан: `{team.get('captain')}` · "
              f"приз/участие: {_esc(team.get('prize_note') or '—')}")]
    for p in members:
        tag = " **[основа]**" if p.get("discord_id") in lineup else " [запас]"
        lines.append(_member_line(p) + tag)
    for p in team.get("pending") or []:
        lines.append(_member_line(p) + " [pending]")
    return "\n".join(lines)[:1900]


def _match_label(state: dict, m: dict) -> str:
    def name(tid):
        return next((t.get("name") for t in state.get("teams", [])
                     if t.get("id") == tid), tid or "—")
    status = {"ready": "⏳", "disputed": "⚠️", "completed": "✅",
              "void": "🚫"}.get(m.get("status"), "?")
    score = ""
    if m.get("status") == "completed":
        score = f" {m.get('score')}"
    return (f"`{m['id']}` {status} {_esc(name(m.get('a')))} vs "
            f"{_esc(name(m.get('b')))} · BO{m['bo']}{score}")


def _panel_embed(state: dict) -> discord.Embed:
    sid = state.get("session_id")
    status = state.get("status")
    teams = state.get("teams") or []
    bracket = state.get("bracket") or {}
    matches = bracket.get("matches") or []
    done = sum(1 for m in matches if m.get("status") == "completed")
    emb = discord.Embed(
        title="r!z3 CASH CUP — TEST PANEL",
        description=("Изолированная тестовая сессия. Настоящий Cash Cup "
                     "не затрагивается. Все действия помечаются TEST."),
        color=0xE67E22)
    emb.add_field(name="Сессия", value=f"`{sid}`", inline=True)
    emb.add_field(name="Статус", value=f"`{status}`", inline=True)
    emb.add_field(name="Команд", value=str(len(teams)), inline=True)
    emb.add_field(name="Матчи", value=f"{done}/{len(matches)}", inline=True)
    seed = state.get("seed") or (bracket or {}).get("seed")
    if seed is not None:
        emb.add_field(name="Seed", value=f"`{seed}`", inline=True)
    if bracket.get("champion"):
        emb.add_field(name="Чемпион TEST", value=f"`{bracket['champion']}`",
                      inline=True)
    return emb


class _EphemeralView(discord.ui.View):
    """Базовый эфемерный view: admin+guild+session проверки."""

    def __init__(self, cog, sid: str, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.sid = sid

    async def _allowed(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or interaction.guild.id != self.cog._guild_of(self.sid):
            await interaction.response.send_message(
                "TEST: сессия принадлежит другому серверу", ephemeral=True)
            return False
        if not _is_admin(interaction.user):
            await interaction.response.send_message(
                "TEST: только ADMIN", ephemeral=True)
            return False
        return True


class _MatchPickView(_EphemeralView):
    """Выбор матча из активных (ready/disputed) для действия."""

    def __init__(self, cog, sid: str, state: dict, action: str,
                 title: str = "Выберите матч"):
        super().__init__(cog, sid)
        self.action = action
        active = [m for m in (state.get("bracket") or {}).get("matches", [])
                  if m.get("status") in ("ready", "disputed")
                  and m["id"] in ((state.get("bracket") or {})
                                  .get("active_keys") or [])]
        if not active:
            self.add_item(discord.ui.Button(
                label="Нет активных матчей", style=discord.ButtonStyle.secondary,
                custom_id=_cid(sid, "noop"), disabled=True))
            return
        opts = [discord.SelectOption(
            label=f"{m['id']} · {m['bracket']} · BO{m['bo']}",
            value=m["id"], description=(m.get("status") or "")[:100])
            for m in active[:25]]
        sel = discord.ui.Select(
            placeholder=title, options=opts,
            custom_id=_cid(sid, f"pick:{action}"))
        sel.callback = lambda i: self._picked(i)
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        mid = interaction.data["values"][0]
        # Без defer: ветка dispute отвечает send_modal первым ответом.
        await self.cog._run_match_action(interaction, self.sid,
                                         self.action, mid)


class _TeamPickView(_EphemeralView):
    def __init__(self, cog, sid: str, state: dict, action: str,
                 title: str = "Выберите команду"):
        super().__init__(cog, sid)
        teams = state.get("teams") or []
        opts = [discord.SelectOption(
            label=(t.get("name") or "?")[:100], value=str(t.get("id")),
            description=f"капитан {t.get('captain')}")
            for t in teams[:25]]
        sel = discord.ui.Select(placeholder=title, options=opts,
                                custom_id=_cid(sid, f"tpick:{action}"))
        sel.callback = lambda i: self._picked(i)
        self.add_item(sel)

    async def _picked(self, interaction: discord.Interaction) -> None:
        if not await self._allowed(interaction):
            return
        tid = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True)
        await self.cog._run_team_action(interaction, self.sid,
                                        self.action, tid)


class _ScoreView(_EphemeralView):
    """Кнопки допустимых счётов для BO1/BO3 матча."""

    def __init__(self, cog, sid: str, match: dict):
        super().__init__(cog, sid)
        self.mid = match["id"]
        allowed = ([(2, 0), (2, 1), (1, 2), (0, 2)] if match["bo"] == 3
                   else [(1, 0), (0, 1)])
        for sa, sb in allowed:
            btn = discord.ui.Button(
                label=f"{sa}:{sb}", style=discord.ButtonStyle.primary,
                custom_id=_cid(sid, f"score:{self.mid}:{sa}-{sb}"))
            btn.callback = lambda i, a=sa, b=sb: self._score(i, a, b)
            self.add_item(btn)

    async def _score(self, interaction: discord.Interaction,
                     sa: int, sb: int) -> None:
        if not await self._allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog._record_score(interaction, self.sid, self.mid,
                                     sa, sb)


class _SideView(_EphemeralView):
    """Выбор стороны A/B (тех. победа, спор, игра GF)."""

    def __init__(self, cog, sid: str, match: dict, action: str,
                 names: tuple[str, str]):
        super().__init__(cog, sid)
        self.action = action
        self.mid = match["id"]
        for side, label in (("a", names[0]), ("b", names[1])):
            btn = discord.ui.Button(
                label=label[:80], style=discord.ButtonStyle.primary,
                custom_id=_cid(sid, f"side:{action}:{self.mid}:{side}"))
            btn.callback = lambda i, s=side: self._side(i, s)
            self.add_item(btn)

    async def _side(self, interaction: discord.Interaction,
                    side: str) -> None:
        if not await self._allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        await self.cog._run_side_action(interaction, self.sid,
                                        self.action, self.mid, side)


class _ReasonModal(discord.ui.Modal):
    def __init__(self, sid: str, mid: str):
        super().__init__(title=f"TEST · спор {mid}")
        self.sid = sid
        self.mid = mid
        self.reason = discord.ui.TextInput(
            label="Причина спора (TEST)", max_length=200,
            default="Тестовый спор — проверка блокировки результата")
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("CashCupTestCog")
        if cog is None or not _is_admin(interaction.user):
            await interaction.response.send_message(
                "TEST: только ADMIN", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await cog._open_dispute(interaction, self.sid, self.mid,
                                str(self.reason.value))


class TestPanelView(discord.ui.View):
    """Постоянная панель тестовой сессии (custom_id содержит session id —
    кнопки старой сессии не могут изменить новую)."""

    def __init__(self, sid: str):
        super().__init__(timeout=None)
        self.sid = sid
        actions = [
            ("Команды", "teams", discord.ButtonStyle.primary),
            ("Матчи", "matches", discord.ButtonStyle.primary),
            ("Проверка", "check", discord.ButtonStyle.secondary),
            ("Сетка", "bracket", discord.ButtonStyle.secondary),
            ("Старт TEST", "start", discord.ButtonStyle.success),
            ("Check-in", "checkin", discord.ButtonStyle.secondary),
            ("Комната", "room", discord.ButtonStyle.secondary),
            ("Результат", "result", discord.ButtonStyle.success),
            ("Тех. победа", "forfeit", discord.ButtonStyle.danger),
            ("Спор", "dispute", discord.ButtonStyle.secondary),
            ("Дисквалификация", "dq", discord.ButtonStyle.danger),
            ("Игра GF", "gf", discord.ButtonStyle.primary),
            ("Симуляция", "sim", discord.ButtonStyle.success),
            ("Заново", "restart", discord.ButtonStyle.secondary),
            ("Удалить", "delete", discord.ButtonStyle.danger),
            ("Обновить", "refresh", discord.ButtonStyle.secondary),
        ]
        for idx, (label, action, style) in enumerate(actions):
            btn = discord.ui.Button(
                label=label, style=style, custom_id=_cid(sid, action),
                row=min(idx // _MAX_BUTTONS_PER_ROW, 4))
            btn.callback = self._make_cb(action)
            self.add_item(btn)

    def _make_cb(self, action: str):
        async def cb(interaction: discord.Interaction) -> None:
            cog = interaction.client.get_cog("CashCupTestCog")
            if cog is None:
                await interaction.response.send_message(
                    "TEST: cog не загружен", ephemeral=True)
                return
            await cog._panel_action(interaction, self.sid, action)
        return cb


class CashCupTestCog(commands.Cog):
    """Cog имени для get_cog и удобства."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._guilds: dict[str, int] = {}
        self._views_registered: set[str] = set()

    # ---------- инфраструктура ----------

    def _guild_of(self, sid: str) -> int | None:
        return self._guilds.get(sid)

    async def cog_load(self) -> None:
        pass

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        """Регистрирует persistent views всех живых тестовых сессий —
        кнопки панели работают после рестарта бота."""
        try:
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT id, guild_id FROM cashcup_test_sessions
                    WHERE status <> 'archived'
                """)
            for r in rows:
                sid = r["id"]
                self._guilds[sid] = int(r["guild_id"])
                self._register_view(sid)
        except Exception:
            logger.exception("CASH CUP TEST: восстановление views failed")

    def _register_view(self, sid: str) -> None:
        if sid in self._views_registered:
            return
        self._views_registered.add(sid)
        self.bot.add_view(TestPanelView(sid))

    async def _state(self, guild_id: int, sid: str | None = None):
        if sid:
            return await cashcup_test_store.get(guild_id, sid)
        return await cashcup_test_store.latest(guild_id)

    async def _reply(self, interaction: discord.Interaction,
                     text: str, *, view=None) -> None:
        # ВАЖНО: view=None передавать нельзя — discord.py падает с
        # TypeError "expected view parameter to be of type View or
        # LayoutView, not NoneType". kwargs собираем условно.
        kwargs = {"ephemeral": True}
        if view is not None:
            kwargs["view"] = view
        try:
            await interaction.followup.send(text[:2000], **kwargs)
        except discord.HTTPException:
            pass

    async def _guard(self, interaction: discord.Interaction,
                     sid: str) -> dict | None:
        """Проверки admin/guild/существования сессии для panel actions.
        Вызывается ПОСЛЕ defer — ошибки идут через followup."""
        async def deny(text: str) -> None:
            try:
                await interaction.followup.send(text, ephemeral=True)
            except discord.HTTPException:
                pass
        if not interaction.guild:
            await deny("TEST: нужна команда на сервере")
            return None
        if not _is_admin(interaction.user):
            await deny("TEST: только ADMIN")
            return None
        state = await self._state(interaction.guild.id, sid)
        if state is None:
            await deny("TEST: сессия не найдена")
            return None
        if int(state.get("guild_id") or 0) != interaction.guild.id:
            await deny("TEST: сессия принадлежит другому серверу")
            return None
        return state

    # ---------- /тест ----------

    @app_commands.command(
        name="тест",
        description="TEST · изолированный прогон CASH CUP (только ADMIN)")
    async def open_panel(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not _is_admin(interaction.user):
            await interaction.response.send_message(
                "TEST: только ADMIN на сервере", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            state = await cashcup_test_store.latest(interaction.guild.id)
            if state is None:
                await self._reply(
                    interaction,
                    "**CASH CUP TEST**\nНет тестовой сессии. Импортируйте "
                    "снимок production (read-only) командой:\n"
                    "`python tools/cashcup_snapshot.py import "
                    "--file snapshot.json --guild <ID этого сервера>`\n"
                    "Затем снова выполните /тест.")
                return
            await self._post_panel(interaction, state)
            await self._reply(
                interaction,
                "TEST: панель управления сессией открыта/обновлена.")
        except Exception:
            logger.exception("CASH CUP TEST /тест failed")
            await self._reply(interaction, "TEST: внутренняя ошибка — "
                                           "см. логи бота")

    async def _post_panel(self, interaction: discord.Interaction,
                          state: dict) -> None:
        sid = state["session_id"]
        self._guilds[sid] = interaction.guild.id
        self._register_view(sid)
        mid = state.get("message_id")
        channel = None
        if mid:
            channel = self.bot.get_channel(int(state.get("channel_id") or 0))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(
                        int(state.get("channel_id") or 0))
                except discord.HTTPException:
                    channel = None
        view = TestPanelView(sid)
        if channel is not None:
            try:
                msg = await channel.fetch_message(int(mid))
                await msg.edit(embed=_panel_embed(state), view=view)
                return
            except discord.HTTPException:
                pass
        msg = await interaction.channel.send(
            embed=_panel_embed(state), view=view)
        await cashcup_test_store.set_message(
            interaction.guild.id, sid, msg.id,
            channel_id=interaction.channel.id)
        state["message_id"] = msg.id

    # ---------- panel dispatch ----------

    async def _panel_action(self, interaction: discord.Interaction,
                            sid: str, action: str) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.InteractionResponded:
            return
        state = await self._guard(interaction, sid)
        if state is None:
            return  # _guard уже отправил причину отказа
        handler = {
            "teams": self._act_teams,
            "team": self._act_team_pick,
            "matches": self._act_matches,
            "check": self._act_check,
            "bracket": self._act_bracket,
            "start": self._act_start,
            "checkin": self._act_pick("checkin", "Check-in матча"),
            "room": self._act_pick("room", "Комната матча"),
            "result": self._act_pick("result", "Результат матча"),
            "forfeit": self._act_pick("forfeit", "Тех. победа"),
            "dispute": self._act_pick("dispute", "Открыть спор"),
            "resolve": self._act_pick("resolve", "Решить спор"),
            "dq": self._act_team_pick("dq", "Дисквалифицировать"),
            "gf": self._act_gf,
            "sim": self._act_sim,
            "restart": self._act_restart,
            "delete": self._act_delete,
            "refresh": self._act_refresh,
            "history": self._act_history,
        }.get(action)
        if handler is None:
            await self._reply(interaction, f"TEST: неизвестное действие {action}")
            return
        await handler(interaction, state)

    def _act_pick(self, action: str, title: str):
        async def fn(interaction, state):
            view = _MatchPickView(self, state["session_id"], state,
                                  action, title=title)
            await interaction.followup.send(
                f"**TEST · {title}** — выберите матч:",
                view=view, ephemeral=True)
        return fn

    def _act_team_pick(self, action: str, title: str):
        async def fn(interaction, state):
            view = _TeamPickView(self, state["session_id"], state,
                                 action, title=title)
            await interaction.followup.send(
                f"**TEST · {title}** — выберите команду:",
                view=view, ephemeral=True)
        return fn

    # ---------- read-only действия ----------

    async def _act_teams(self, interaction, state):
        teams = state.get("teams") or []
        if not teams:
            await self._reply(interaction, "TEST: в сессии нет команд")
            return
        # Компактный список + кнопки детального просмотра через select.
        lines = [f"**TEST · команд: {len(teams)}**"]
        for t in teams[:20]:
            members = t.get("members") or []
            ff = sum(1 for p in members if p.get("ff_uid"))
            ver = sum(1 for p in members if p.get("verified"))
            lines.append(f"- **{_esc(t.get('name'))}** · "
                         f"{len(members)}/5 · FF {ff} · ADMIN {ver} · "
                         f"cap `{t.get('captain')}`")
        if len(teams) > 20:
            lines.append(f"… ещё {len(teams)-20}")
        await self._reply(interaction, "\n".join(lines)[:1900])
        # Детальный просмотр первой команды + select для остальных.
        view = _TeamPickView(self, state["session_id"], state, "info",
                             title="Детали команды")
        await interaction.followup.send(
            "Детали команды (состав, FF-ники):", view=view, ephemeral=True)

    async def _run_team_action(self, interaction, sid, action, tid):
        state = await self._state(interaction.guild.id, sid)
        team = next((t for t in (state or {}).get("teams", [])
                     if str(t.get("id")) == tid), None)
        if team is None:
            await self._reply(interaction, "TEST: команда не найдена")
            return
        await self._reply(interaction, _team_detail(state, team))
        if action == "dq":
            await self._dq_team(interaction, sid, tid)

    async def _act_matches(self, interaction, state):
        b = state.get("bracket") or {}
        matches = b.get("matches") or []
        if not matches:
            await self._reply(interaction, "TEST: сетка ещё не сформирована")
            return
        active = ready_matches(b)
        disputed = [m for m in matches if m.get("status") == "disputed"]
        lines = [f"**TEST · активных матчей: {len(active)}**"]
        for m in active[:15]:
            lines.append(_match_label(state, m))
        if disputed:
            lines.append(f"\n**Спорные:** {len(disputed)}")
            for m in disputed[:5]:
                lines.append(_match_label(state, m))
        recent = [m for m in matches if m.get("status") == "completed"][-10:]
        if recent:
            lines.append("\n**Последние результаты:**")
            for m in recent:
                lines.append(_match_label(state, m))
        await self._reply(interaction, "\n".join(lines)[:1900])

    async def _act_bracket(self, interaction, state):
        b = state.get("bracket") or {}
        matches = b.get("matches") or []
        if not matches:
            await self._reply(interaction, "TEST: сетка не сформирована — "
                                           "нажмите «Старт TEST»")
            return
        lines = ["**TEST · сетка Double Elimination**"]
        for m in matches[:40]:
            lines.append(_match_label(state, m))
        if len(matches) > 40:
            lines.append(f"… ещё {len(matches)-40} матчей")
        await self._reply(interaction, "\n".join(lines)[:1900])

    async def _act_check(self, interaction, state):
        """Диагностика готовности команд (§7 ТЗ) — read-only."""
        from cogs.cashcup import (
            _STATUS_ERROR,
            _STATUS_NOT_READY,
            _STATUS_READY,
            _STATUS_WAIT_ADMIN,
            _team_diag,
        )
        teams = state.get("teams") or []
        counts = {"ready": 0, "wait_admin": 0, "not_ready": 0, "error": 0}
        key = {_STATUS_READY: "ready", _STATUS_WAIT_ADMIN: "wait_admin",
               _STATUS_NOT_READY: "not_ready", _STATUS_ERROR: "error"}
        blocks = []
        uid_seen: dict[str, str] = {}
        collisions = []
        for t in teams:
            d = _team_diag(state, t)
            counts[key.get(d["status"], "error")] += 1
            if d["status"] != _STATUS_READY:
                blocks.append(
                    f"**{_esc(t.get('name'))}** — {d['status']}\n"
                    + "\n".join(f"- {p}" for p in d["problems"][:8]))
            for p in t.get("members") or []:
                u = str(p.get("ff_uid") or "")
                if u:
                    if u in uid_seen:
                        collisions.append(u)
                    uid_seen.setdefault(u, t.get("name"))
        would_start = counts["ready"] == len(teams) and not collisions
        lines = ["**CASH CUP TEST — Проверка регистрации**", "",
                 f"Всего команд: **{len(teams)}**",
                 f"Полностью готовых: **{counts['ready']}**",
                 f"Ожидающих ADMIN: **{counts['wait_admin']}**",
                 f"Не готовых: **{counts['not_ready']}**",
                 f"С ошибками состава: **{counts['error']}**",
                 f"Конфликтующие FF UID: **{len(collisions)}**",
                 "",
                 f"Оригинальный Cash Cup **{'РАЗРЕШИЛ БЫ' if would_start else 'НЕ разрешил бы'}** запуск.",
                 "",
                 ("Не проверяется автоматически: живой Discord-статус "
                  "игроков, актуальность FF API, ручное решение ADMIN, "
                  "права на тестовом сервере."),
                 ("Тест всё равно позволяет использовать все команды — "
                  "verified-флаги реальных данных не меняются.")]
        if blocks:
            lines += ["", "**Проблемные команды:**"] + blocks[:10]
        await self._reply(interaction, "\n".join(lines)[:3900])

    async def _act_history(self, interaction, state):
        b = state.get("bracket") or {}
        matches = [m for m in b.get("matches", [])
                   if m.get("status") == "completed"]
        if not matches:
            await self._reply(interaction, "TEST: завершённых матчей нет")
            return
        lines = [f"**TEST · история матчей ({len(matches)})**"]
        for m in matches[-30:]:
            lines.append(_match_label(state, m))
        if len(matches) > 30:
            lines.append(f"… ещё {len(matches)-30}")
        await self._reply(interaction, "\n".join(lines)[:3900])

    # ---------- изменяющие действия ----------

    async def _act_start(self, interaction, state):
        sid = state["session_id"]
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") != "snapshot":
                raise CashCupTestError(
                    "Старт возможен только из snapshot-сессии")
            from services.cashcup_bracket import create_bracket
            seed = st.get("seed")
            st["bracket"] = create_bracket(
                [t["id"] for t in st["teams"]], seed=seed)
            st["status"] = "running"
            return len(st["teams"])

        try:
            _, count = await cashcup_test_store.mutate(
                gid, uid, "test_start", change, session_id=sid)
            await self._reply(
                interaction,
                f"**TEST · турнир запущен.** Команд: {count}. "
                "Сетка Double Elimination сформирована (TEST).")
        except (CashCupTestError, BracketError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    def _find_match(self, state: dict, mid: str) -> dict | None:
        b = state.get("bracket") or {}
        active = set(b.get("active_keys") or [])
        return next((m for m in b.get("matches", [])
                     if m.get("id") == mid.upper()
                     and m["id"] in active
                     and m.get("status") in ("ready", "disputed")), None)

    async def _run_match_action(self, interaction, sid, action, mid):
        state = await self._state(interaction.guild.id, sid)
        if state is None:
            await interaction.followup.send(
                "TEST: сессия не найдена", ephemeral=True)
            return
        m = self._find_match(state, mid)
        if m is None:
            await interaction.followup.send(
                "TEST: матч не найден или не активен", ephemeral=True)
            return
        # defer нужен всем веткам, кроме dispute (send_modal — первый ответ).
        if action != "dispute":
            try:
                await interaction.response.defer(ephemeral=True)
            except discord.InteractionResponded:
                pass
        if action == "checkin":
            await self._do_checkin(interaction, sid, mid)
        elif action == "room":
            await self._do_room(interaction, sid, mid)
        elif action == "result":
            if m.get("bracket") == "GF" and m.get("games"):
                await self._reply(
                    interaction,
                    "TEST: Grand Final уже начат — используйте «Игра GF»")
                return
            bo = m["bo"]
            if m.get("status") == "disputed":
                await self._reply(
                    interaction, "TEST: матч в споре — сначала решите спор")
                return
            view = _ScoreView(self, sid, m)
            await interaction.followup.send(
                f"**TEST · результат {mid}** (BO{bo}) — выберите счёт:",
                view=view, ephemeral=True)
        elif action == "forfeit":
            names = self._side_names(state, m)
            view = _SideView(self, sid, m, "forfeit", names)
            await interaction.followup.send(
                f"**TEST · техническая победа {mid}** — кто побеждает?",
                view=view, ephemeral=True)
        elif action == "dispute":
            # Эта ветка без defer: ошибки — response.send_message,
            # успех — send_modal (оба являются первым ответом).
            if m.get("bracket") == "GF":
                await interaction.response.send_message(
                    "TEST: по GF спор не открывается", ephemeral=True)
                return
            if m.get("status") == "disputed":
                await interaction.response.send_message(
                    "TEST: спор уже открыт", ephemeral=True)
                return
            await interaction.response.send_modal(_ReasonModal(sid, mid))
        elif action == "resolve":
            if m.get("status") != "disputed":
                await self._reply(interaction, "TEST: по матчу нет спора")
                return
            names = self._side_names(state, m)
            view = _SideView(self, sid, m, "resolve", names)
            await interaction.followup.send(
                f"**TEST · решение спора {mid}** — победитель?",
                view=view, ephemeral=True)

    def _side_names(self, state, m) -> tuple[str, str]:
        def name(tid):
            return next((t.get("name") or "?" for t in state["teams"]
                         if t.get("id") == tid), "?")
        return (name(m["a"])[:40], name(m["b"])[:40])

    async def _do_checkin(self, interaction, sid, mid):
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") != "running":
                raise CashCupTestError("TEST: турнир не запущен")
            m = self._find_match(st, mid)
            if not m:
                raise CashCupTestError("TEST: матч не найден")
            caps = []
            for tid in (m["a"], m["b"]):
                t = next((x for x in st["teams"] if x["id"] == tid), None)
                if t and t.get("captain") is not None:
                    caps.append(int(t["captain"]))
            have = set(m.get("checkins") or [])
            for c in caps:
                if c not in have:
                    m.setdefault("checkins", []).append(c)
            m["test_checkin"] = True
            return len(m["checkins"])

        try:
            _, count = await cashcup_test_store.mutate(
                gid, uid, "test_checkin", change, session_id=sid)
            await self._reply(
                interaction,
                f"TEST: виртуальный check-in выполнен ({count}/2 капитана).")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _do_room(self, interaction, sid, mid):
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") != "running":
                raise CashCupTestError("TEST: турнир не запущен")
            m = self._find_match(st, mid)
            if not m:
                raise CashCupTestError("TEST: матч не найден")
            if len(m.get("checkins") or []) != 2:
                raise CashCupTestError(
                    "TEST: сначала виртуальный check-in обоих капитанов")
            m["room_id"] = f"TEST-{mid}"
            m["room_password"] = "test-only"
            m["room_set_by"] = uid
            m["test_room"] = True
            return m["room_id"]

        try:
            _, room = await cashcup_test_store.mutate(
                gid, uid, "test_room", change, session_id=sid)
            await self._reply(
                interaction,
                f"TEST: фиктивная комната выдана — `{room}` "
                "(реальные уведомления не отправляются).")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _record_score(self, interaction, sid, mid, sa, sb):
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") != "running":
                raise CashCupTestError("TEST: турнир не запущен")
            b = st["bracket"]
            m = next((x for x in ready_matches(b) if x["id"] == mid), None)
            if not m:
                raise CashCupTestError("TEST: матч не готов к результату")
            if m.get("bracket") == "GF" and m.get("games"):
                raise CashCupTestError(
                    "TEST: GF начат — используйте «Игра GF»")
            if m.get("status") == "disputed":
                raise CashCupTestError("TEST: матч в споре")
            if len(m.get("checkins") or []) != 2:
                raise CashCupTestError(
                    "TEST: нужен виртуальный check-in обоих капитанов")
            if not m.get("room_id"):
                raise CashCupTestError(
                    "TEST: сначала выдайте фиктивную комнату")
            record_result(b, mid, sa, sb, evidence=f"TEST:{uid}:{sa}-{sb}")
            if b["champion"]:
                st["status"] = "completed"
            return m.get("winner")

        try:
            new_state, winner = await cashcup_test_store.mutate(
                gid, uid, "test_result",
                change, {"match": mid, "score": [sa, sb]},
                session_id=sid)
            name = next((t.get("name") for t in new_state["teams"]
                         if t.get("id") == winner), winner)
            done = " TEST-турнир завершён!" if new_state["status"] == "completed" else ""
            await self._reply(
                interaction,
                f"TEST: результат {mid} = {sa}:{sb}. "
                f"Победитель: **{_esc(name)}**.{done}")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _run_side_action(self, interaction, sid, action, mid, side):
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") != "running":
                raise CashCupTestError("TEST: турнир не запущен")
            b = st["bracket"]
            m = next((x for x in b["matches"] if x["id"] == mid), None)
            if not m:
                raise CashCupTestError("TEST: матч не найден")
            if action == "forfeit":
                if m.get("status") != "ready" or m["id"] not in \
                        (b.get("active_keys") or []):
                    raise CashCupTestError("TEST: матч не активен")
                if m["bracket"] == "GF":
                    # Тех. победа в GF — две игры подряд 2:0.
                    record_final_game(b, side, evidence=f"TEST:{uid}:ff1")
                    if b["champion"] is None:
                        record_final_game(b, side,
                                          evidence=f"TEST:{uid}:ff2")
                else:
                    score = (1, 0) if side == "a" else (0, 1)
                    if m["bo"] == 3:
                        score = (2, 0) if side == "a" else (0, 2)
                    record_result(b, mid, *score,
                                  evidence=f"TEST:{uid}:forfeit")
                if b["champion"]:
                    st["status"] = "completed"
                return m.get("winner")
            if action == "resolve":
                if m.get("status") != "disputed":
                    raise CashCupTestError("TEST: спор не открыт")
                m["status"] = "ready"
                score = (1, 0) if side == "a" else (0, 1)
                record_result(b, mid, *score,
                              evidence=f"TEST:{uid}:dispute:{side}")
                if b["champion"]:
                    st["status"] = "completed"
                return m.get("winner")
            raise CashCupTestError("TEST: неизвестное действие")

        try:
            _, winner = await cashcup_test_store.mutate(
                gid, uid, f"test_{action}", change,
                {"match": mid, "side": side}, session_id=sid)
            await self._reply(interaction,
                              f"TEST: действие `{action}` по `{mid}` "
                              f"выполнено (победитель `{winner}`).")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _open_dispute(self, interaction, sid, mid, reason):
        gid = interaction.guild.id
        uid = interaction.user.id
        from datetime import datetime

        def change(st):
            if st.get("status") != "running":
                raise CashCupTestError("TEST: турнир не запущен")
            m = self._find_match(st, mid)
            if not m:
                raise CashCupTestError("TEST: матч не найден")
            if m.get("bracket") == "GF":
                raise CashCupTestError("TEST: по GF спор не открывается")
            m["status"] = "disputed"
            m["dispute"] = {"reason": reason[:300], "by": uid,
                            "at": datetime.now(UTC).isoformat(),
                            "evidence": [], "test": True}
            return mid

        try:
            await cashcup_test_store.mutate(
                gid, uid, "test_dispute", change, session_id=sid)
            await self._reply(
                interaction,
                f"TEST: спор по `{mid}` открыт — продвижение результата "
                "заблокировано. Решите через «Спор» → «Решить спор».")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _dq_team(self, interaction, sid, tid):
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") != "running":
                raise CashCupTestError("TEST: турнир не запущен")
            disqualify(st["bracket"], tid, evidence=f"TEST:{uid}:dq")
            if st["bracket"]["champion"]:
                st["status"] = "completed"
            return tid

        try:
            await cashcup_test_store.mutate(
                gid, uid, "test_dq", change, session_id=sid)
            await self._reply(interaction,
                              f"TEST: команда `{tid}` дисквалифицирована.")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _act_gf(self, interaction, state):
        b = state.get("bracket") or {}
        gf = next((m for m in ready_matches(b) if m.get("bracket") == "GF"),
                  None)
        if gf is None:
            await self._reply(interaction,
                              "TEST: Grand Final сейчас не активен")
            return
        names = self._side_names(state, gf)
        view = _SideView(self, state["session_id"], gf, "gf_game", names)
        # gf_game обрабатывается отдельно: одна игра, не вся серия.
        view.action = "gf_game"
        for item in view.children:
            if isinstance(item, discord.ui.Button):
                item.callback = self._gf_cb(state["session_id"], gf["id"])
        await interaction.followup.send(
            f"**TEST · Grand Final `{gf['id']}`** — кто выиграл игру? "
            f"Счёт серии: {gf.get('score')}",
            view=view, ephemeral=True)

    def _gf_cb(self, sid: str, mid: str):
        async def cb(interaction: discord.Interaction) -> None:
            # custom_id кодирует сторону в последнем сегменте.
            side = str(interaction.data.get("custom_id", "")).rsplit(":", 1)[-1]
            if side not in ("a", "b"):
                return
            cog = interaction.client.get_cog("CashCupTestCog")
            if cog is None:
                return
            # Права проверяются в момент клика: ADMIN мог быть отозван
            # после открытия ephemeral-кнопок.
            if not interaction.guild or not _is_admin(interaction.user):
                await interaction.response.send_message(
                    "TEST: только ADMIN", ephemeral=True)
                return
            guild_of = cog._guild_of(sid)
            if guild_of is not None and guild_of != interaction.guild.id:
                await interaction.response.send_message(
                    "TEST: сессия принадлежит другому серверу",
                    ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            await cog._gf_game(interaction, sid, mid, side)
        return cb

    async def _gf_game(self, interaction, sid, mid, side):
        # Повторная проверка на входе: права могли быть отозваны
        # между кликом и выполнением.
        if (not interaction.guild
                or not _is_admin(interaction.user)):
            await self._reply(interaction, "TEST: только ADMIN")
            return
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") != "running":
                raise CashCupTestError("TEST: турнир не запущен")
            m = record_final_game(st["bracket"], side,
                                  evidence=f"TEST:{uid}:gf:{side}")
            if st["bracket"]["champion"]:
                st["status"] = "completed"
            return list(m.get("score") or [])

        try:
            new_state, score = await cashcup_test_store.mutate(
                gid, uid, "test_gf_game", change, session_id=sid)
            done = (" TEST-турнир завершён!"
                    if new_state.get("status") == "completed" else "")
            await self._reply(
                interaction,
                f"TEST: игра GF записана. Счёт серии: {score}.{done}")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _act_sim(self, interaction, state):
        sid = state["session_id"]
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            if st.get("status") == "completed":
                raise CashCupTestError(
                    "TEST: сессия уже завершена — «Заново» для повторной "
                    "симуляции")
            seed = st.get("seed")
            summary = simulate_to_final(st, seed=seed)
            st["last_sim"] = {
                "seed": summary["seed"],
                "matches_completed": summary["matches_completed"],
                "places": summary["places"],
                "history": summary["history"],
            }
            return summary

        try:
            _, summary = await cashcup_test_store.mutate(
                gid, uid, "test_simulate", change, session_id=sid)
            await self._reply_sim(interaction, state, summary)
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: симуляция прервана — {exc}")

    async def _reply_sim(self, interaction, state, summary):
        names = {t["id"]: t.get("name") for t in state.get("teams", [])}

        def nm(tid):
            return _esc(names.get(tid) or tid or "—")

        p = summary["places"]
        gf_score = p.get("gf_score") or []
        lines = ["**r!z3 CASH CUP — TEST RESULTS**", "",
                 f"Команд: **{len(state.get('teams') or [])}**",
                 (f"Сыграно серий: **{summary['matches_completed']}** "
                  f"(всего в сетке {summary['matches_total']})"),
                 f"Seed: `{summary['seed']}`",
                 "",
                 f"🏆 Чемпион: **{nm(p.get('champion'))}**",
                 f"🥈 Второе место: {nm(p.get('runner_up'))}",
                 f"🥉 Третье место: {nm(p.get('third'))}",
                 f"Winner Bracket: {nm(p.get('wb_winner'))}",
                 f"Losers Bracket: {nm(p.get('lb_winner'))}",
                 f"Grand Final: {gf_score[0]}:{gf_score[1] if len(gf_score)>1 else 0}",
                 "",
                 ("Полная история матчей — кнопка «Матчи»/«Сетка». "
                  "Результаты вычислены из сетки, не назначены случайно.")]
        await self._reply(interaction, "\n".join(lines)[:3900])

    async def _act_restart(self, interaction, state):
        sid = state["session_id"]
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            base = st.get("base")
            if not base:
                raise CashCupTestError("TEST: нет базового снимка")
            fresh = deepcopy(base)
            fresh.update({
                "status": "snapshot", "test": True,
                "guild_id": st.get("guild_id"),
                "bracket": None, "payouts": [],
                "seed": st.get("seed"), "base": deepcopy(base),
            })
            st.clear()
            st.update(fresh)
            return len(st.get("teams") or [])

        try:
            _, count = await cashcup_test_store.mutate(
                gid, uid, "test_restart", change, session_id=sid)
            await self._reply(
                interaction,
                f"TEST: сессия сброшена к снимку команд ({count}). "
                "Сетка и результаты очищены.")
        except (CashCupTestError, BracketError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _act_delete(self, interaction, state):
        sid = state["session_id"]
        gid = interaction.guild.id
        uid = interaction.user.id

        def change(st):
            st["status"] = "archived"
            return sid

        try:
            await cashcup_test_store.mutate(
                gid, uid, "test_delete", change, session_id=sid)
            # Панельное сообщение помечаем как удалённое.
            mid = state.get("message_id")
            ch = self.bot.get_channel(int(state.get("channel_id") or 0))
            if ch and mid:
                try:
                    msg = await ch.fetch_message(int(mid))
                    await msg.edit(
                        content="TEST: сессия удалена. /тест создаст новую "
                                "после импорта снимка.",
                        embed=None, view=None)
                except discord.HTTPException:
                    pass
            await self._reply(interaction,
                              "TEST: сессия архивирована (данные сохранены "
                              "в cashcup_test_sessions, статус archived).")
        except (CashCupTestError, ValueError) as exc:
            await self._reply(interaction, f"TEST: {exc}")

    async def _act_refresh(self, interaction, state):
        sid = state["session_id"]
        mid = state.get("message_id")
        ch = self.bot.get_channel(int(state.get("channel_id") or 0))
        if ch and mid:
            try:
                msg = await ch.fetch_message(int(mid))
                await msg.edit(embed=_panel_embed(state),
                               view=TestPanelView(sid))
            except discord.HTTPException:
                pass
        await self._reply(interaction, "TEST: панель обновлена.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CashCupTestCog(bot))
