"""r!z3 Voice Control — команда /voice и CV2-панель управления
голосовыми каналами.

Модель доступа: /voice публикует постоянную публичную карточку
Voice Control Center (одна на сервер, привязка к сообщению — в
PostgreSQL, кнопка работает после рестарта через persistent view).
Открыть персональную ephemeral-панель могут BOT_OWNER_ID и
Discord Administrator; права проверяются при каждом взаимодействии.

Ephemeral-панель персональная (состояние в памяти бота, ~14 минут;
по истечении — снова кнопка публичной карточки). Режимы и
ограничения живут в PostgreSQL и НЕ зависят от жизни панели.

Целостность сессии: каждая панель имеет panel_id, message_id и
revision. Кнопки старой панели (до повторного /voice) не имеют
доступа к состоянию новой — callback сверяет message_id.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from config import VOICE_CONTROL_ENABLED
from services import voice_control as vc
from utils.design.layouts import DesignView

log = logging.getLogger(__name__)

PANEL_TTL = 14 * 60          # ephemeral interaction token живёт ~15 мин
VOICE_DEBOUNCE = 1.0         # debounce voice_state_update
PANEL_REFRESH_DEBOUNCE = 3.0
CHANNELS_PER_PAGE = 25
MEMBERS_PER_PAGE = 15
# Статический custom_id кнопки публичной карточки — persistent view
# после перезапуска бота сопоставляет callback по этому id.
PUBLIC_PANEL_CUSTOM_ID = "vc_open_panel"

# Действия, изменяющие состояние Discord/БД. При выключенном
# VOICE_CONTROL_ENABLED они запрещены; навигация по страницам — нет.
MUTATING_ACTIONS = {
    "mute_all", "unmute_all",
    "member_mute", "member_unmute", "member_deafen", "member_undeafen",
    "member_disconnect", "member_move_do", "member_speaker_toggle",
    "member_protect",
    "speaker_add_pick", "speaker_remove_pick", "speaker_start",
    "speaker_stop", "close_do", "open",
    "move_do", "move_confirm", "disconnect_all_do",
    "extra_auto_mute", "extra_undo", "protect_remove",
    "extra_rename", "extra_limit", "protect_add_user", "protect_add_role",
    "emergency_execute",
}

# Длительные операции: ранний defer, чтобы не получить
# Interaction Failed на массовых действиях (100+ участников).
LONG_ACTIONS = {
    "mute_all", "unmute_all",
    "member_mute", "member_unmute", "member_deafen", "member_undeafen",
    "member_disconnect", "member_move_do", "member_speaker_toggle",
    "speaker_add_pick", "speaker_remove_pick", "speaker_start",
    "speaker_stop", "close_do", "open",
    "move_confirm", "disconnect_all_do", "extra_undo",
    "emergency_execute",
}

# Действия, открывающие модальные окна — defer недопустим
# (send_modal должен быть первым ответом).
MODAL_ACTIONS = {
    "extra_rename", "extra_limit", "protect_add_user", "protect_add_role",
}


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _participants_word(n: int) -> str:
    return _plural(n, "участник", "участника", "участников")


class PanelState:
    """Состояние одной ephemeral-панели администратора.

    panel_id/message_id/revision связывают callback с конкретным
    сообщением: кнопки старой панели после повторного /voice
    отвергаются."""

    __slots__ = (
        "chan_page", "channel_id", "confirm_kind", "confirm_snapshot",
        "duration", "guild_id", "interaction", "last_touch", "lock",
        "member_page", "message_id", "move_pick", "move_target", "page",
        "panel_id", "revision", "selected_member", "source_interaction",
        "user_id", "valid",
    )

    def __init__(self, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.user_id = user_id
        self.panel_id = uuid.uuid4().hex
        self.revision = 0
        self.valid = True
        self.channel_id: int | None = None
        self.page = "main"
        self.chan_page = 0
        self.member_page = 0
        self.duration: timedelta | None = None  # None = до ручного снятия
        self.selected_member: int | None = None
        self.move_pick: set[int] = set()
        self.move_target: int | None = None
        self.confirm_kind: str | None = None
        self.confirm_snapshot: dict[str, Any] | None = None
        self.message_id: int | None = None
        self.last_touch = time.monotonic()
        self.interaction: discord.Interaction | None = None
        self.source_interaction: discord.Interaction | None = None
        self.lock = asyncio.Lock()

    def touch(self) -> None:
        self.last_touch = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.last_touch > PANEL_TTL

    def invalidate(self) -> None:
        self.valid = False


class VoiceControlCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.panels: dict[tuple[int, int], PanelState] = {}
        self._voice_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._voice_pending: set[tuple[int, int]] = set()
        self._refresh_tasks: dict[tuple[int, int], asyncio.Task] = {}
        self._sweep_task: asyncio.Task | None = None
        self._startup_done = False
        vc.set_bot(bot)

    async def cog_load(self) -> None:
        self._sweep_task = asyncio.create_task(self._sweep_loop())
        # Persistent view публичной карточки: после перезапуска
        # кнопка «Открыть панель управления» продолжает работать —
        # discord.py диспетчеризует по статическому custom_id.
        try:
            self.bot.add_view(self._public_card_view())
        except Exception:
            log.exception("voice control: persistent view not registered")

    async def cog_unload(self) -> None:
        if self._sweep_task:
            self._sweep_task.cancel()
        for task in list(self._voice_tasks.values()) + list(self._refresh_tasks.values()):
            task.cancel()

    # ------------------------------------------------------------------
    # Фоновые задачи
    # ------------------------------------------------------------------
    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(20)
                await vc.sweep_timers(self.bot)
                self._cleanup_panels()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("voice control sweep failed")

    def _cleanup_panels(self) -> None:
        """Удалить просроченные и невалидные панели."""
        now = time.monotonic()
        for key, panel in list(self.panels.items()):
            if not panel.valid or now - panel.last_touch > PANEL_TTL * 2:
                panel.invalidate()
                self.panels.pop(key, None)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._startup_done:
            return
        self._startup_done = True
        try:
            await vc.startup_reconcile(self.bot)
        except Exception:
            log.exception("voice control startup reconcile failed")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member,
                                    before: discord.VoiceState,
                                    after: discord.VoiceState) -> None:
        if member.bot or not member.guild:
            return
        key = (member.guild.id, member.id)
        task = self._voice_tasks.get(key)
        if task and not task.done():
            # Не отменяем уже начатую изменяющую операцию —
            # помечаем повторную сверку после завершения.
            self._voice_pending.add(key)
        else:
            self._voice_tasks[key] = asyncio.create_task(
                self._debounced_reconcile(member))
        channel_ids = {
            c.id for c in (before.channel, after.channel) if c is not None}
        for cid in channel_ids or {0}:
            rkey = (member.guild.id, cid)
            rtask = self._refresh_tasks.get(rkey)
            if rtask and not rtask.done():
                rtask.cancel()
            self._refresh_tasks[rkey] = asyncio.create_task(
                self._debounced_refresh(member.guild.id))

    async def _debounced_reconcile(self, member: discord.Member) -> None:
        key = (member.guild.id, member.id)
        try:
            await asyncio.sleep(VOICE_DEBOUNCE)
            await vc.reconcile_member(member)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("voice reconcile failed user=%s", member.id)
        finally:
            if key in self._voice_pending:
                self._voice_pending.discard(key)
                # Повторная сверка актуального состояния после
                # завершения предыдущей (member — свежий объект кэша).
                self._voice_tasks[key] = asyncio.create_task(
                    self._debounced_reconcile(member))

    async def _debounced_refresh(self, guild_id: int) -> None:
        try:
            await asyncio.sleep(PANEL_REFRESH_DEBOUNCE)
            # Обновляем ВСЕ панели гильдии: список каналов глобален,
            # страница «Обзор» показывает любой канал.
            for (gid, _uid), panel in list(self.panels.items()):
                if gid != guild_id or not panel.valid:
                    continue
                await self._refresh_panel(panel)
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("voice panel refresh failed")

    async def _refresh_panel(self, panel: PanelState) -> None:
        async with panel.lock:
            try:
                view = await self._render(panel)
                panel.revision += 1
                await self._edit_panel_message(panel, view)
            except Exception:
                log.debug("voice panel refresh failed", exc_info=True)

    async def _edit_panel_message(self, panel: PanelState,
                                  view: DesignView) -> None:
        """Отредактировать сообщение панели. Источник истины —
        source_interaction (/voice): его токен живёт PANEL_TTL и
        привязан именно к этому ephemeral-сообщению."""
        src = panel.source_interaction
        if src is not None:
            try:
                await src.edit_original_response(view=view)
                return
            except Exception as exc:
                log.debug("panel edit via source failed: %s", exc)
        inter = panel.interaction
        if inter is not None:
            try:
                if inter.response.is_done():
                    if panel.message_id:
                        await inter.followup.edit_message(
                            panel.message_id, view=view)
                    else:
                        await inter.followup.send(view=view, ephemeral=True)
                else:
                    await inter.response.edit_message(view=view)
                return
            except Exception as exc:
                log.debug("panel edit via interaction failed: %s", exc)

    # ------------------------------------------------------------------
    # Команда /voice
    # ------------------------------------------------------------------
    @app_commands.command(name="voice",
                          description="Панель управления голосовыми каналами")
    async def voice(self, interaction: discord.Interaction) -> None:
        """Публикация/обновление постоянной публичной карточки
        Voice Control Center. Доступна исключительно BOT_OWNER_ID."""
        if not VOICE_CONTROL_ENABLED:
            await interaction.response.send_message(
                "Voice Control отключён (VOICE_CONTROL_ENABLED=0).",
                ephemeral=True)
            return
        if not interaction.guild or interaction.channel is None:
            await interaction.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True)
            return
        if interaction.user.id != vc.BOT_OWNER_ID:
            await interaction.response.send_message(
                "Команда доступна только владельцу бота.", ephemeral=True)
            return
        # Ранний defer: публикация может включать fetch/edit сообщения.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        try:
            await self._publish_public_panel(interaction)
        except Exception:
            log.exception("voice public panel publish failed")
            await interaction.followup.send(
                "Не удалось обновить публичную панель — "
                "см. логи бота.", ephemeral=True)
            return
        await interaction.followup.send(
            "Панель Voice Control Center опубликована/обновлена.",
            ephemeral=True)

    # ------------------------------------------------------------------
    # Постоянная публичная карточка + ephemeral-панель
    # ------------------------------------------------------------------
    def _public_card_view(self) -> DesignView:
        """Публичная CV2-карточка: заголовок, описание, одна кнопка.
        timeout=None + статический custom_id → persistent view."""
        view = DesignView(
            title="r!z3 | Voice Control Center", timeout=None)
        view.text("Управление голосовыми каналами сервера.")

        async def _open(interaction: discord.Interaction) -> None:
            await self._open_ephemeral(interaction)

        view.row(_CallbackButton(
            _open, label="Открыть панель управления",
            style=discord.ButtonStyle.primary,
            custom_id=PUBLIC_PANEL_CUSTOM_ID))
        return view

    async def _find_panel_message(
            self, guild: discord.Guild,
            stored: dict[str, Any] | None) -> discord.Message | None:
        """Найти опубликованное сообщение карточки по записи в PG.
        None — записи нет, канал/сообщение удалены."""
        if not stored:
            return None
        channel = guild.get_channel(stored["channel_id"])
        if channel is None:
            try:
                channel = await guild.fetch_channel(stored["channel_id"])
            except Exception:
                return None
        try:
            return await channel.fetch_message(stored["message_id"])
        except Exception:
            return None

    async def _publish_public_panel(
            self, interaction: discord.Interaction) -> None:
        """Одна карточка на сервер: обновить существующую или
        опубликовать новую в текущем текстовом канале. Повторный
        вызов не создаёт дубликатов; удалённое сообщение
        восстанавливается."""
        guild = interaction.guild
        card = self._public_card_view()
        stored = await vc.db.panel(guild.id)
        msg = await self._find_panel_message(guild, stored)
        if msg is not None:
            try:
                await msg.edit(view=card)
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await interaction.channel.send(view=card)
        if stored is None or stored.get("channel_id") != msg.channel.id \
                or stored.get("message_id") != msg.id:
            await vc.db.put_panel(guild.id, msg.channel.id, msg.id)

    async def _open_ephemeral(
            self, interaction: discord.Interaction) -> None:
        """Открыть персональную ephemeral-панель (кнопка публичной
        карточки). Доступ: BOT_OWNER_ID или Discord Administrator —
        проверяется здесь и при каждом следующем действии."""
        if not interaction.guild:
            await self._safe_text(interaction, "Только на сервере.")
            return
        try:
            await vc.require_access(interaction)
        except vc.VoiceControlError as exc:
            await self._safe_text(interaction, str(exc))
            return
        # Повторное открытие инвалидирует предыдущую панель этого
        # администратора: её кнопки больше не имеют доступа.
        old_key = (interaction.guild.id, interaction.user.id)
        old = self.panels.pop(old_key, None)
        if old is not None:
            old.invalidate()
        panel = PanelState(interaction.guild.id, interaction.user.id)
        panel.source_interaction = interaction
        voice_state = getattr(interaction.user, "voice", None)
        if voice_state and voice_state.channel and \
                vc._is_voice_channel(voice_state.channel):
            conf = await vc.db.settings(interaction.guild.id)
            if not vc.is_protected_channel(
                    interaction.guild.id, voice_state.channel.id, conf):
                panel.channel_id = voice_state.channel.id
        self.panels[old_key] = panel
        view = await self._render(panel)
        panel.interaction = interaction
        await interaction.response.send_message(view=view, ephemeral=True)
        try:
            msg = await interaction.original_response()
            panel.message_id = getattr(msg, "id", None)
        except Exception:
            panel.message_id = None

    # ------------------------------------------------------------------
    # Диспетчер callback'ов
    # ------------------------------------------------------------------
    def _panel_for(self, interaction: discord.Interaction) -> PanelState | None:
        if not interaction.guild:
            return None
        panel = self.panels.get((interaction.guild.id, interaction.user.id))
        if panel is None or not panel.valid:
            return None
        # Привязка callback к конкретному сообщению панели: кнопки
        # старой панели после повторного /voice отвергаются.
        msg = getattr(interaction, "message", None)
        if panel.message_id is not None and msg is not None \
                and getattr(msg, "id", None) != panel.message_id:
            return None
        return panel

    async def _guard(self, interaction: discord.Interaction,
                     panel: PanelState, *, mutating: bool) -> bool:
        """Проверка прав при КАЖДОМ взаимодействии + TTL + feature flag.
        expired проверяется ДО touch — иначе бесконечные нажатия
        продлевали бы жизнь панели."""
        if not panel.valid or panel.expired():
            if panel.valid:
                self.panels.pop((panel.guild_id, panel.user_id), None)
                panel.invalidate()
            await self._safe_text(
                interaction, "Панель устарела — откройте панель заново.")
            return False
        panel.touch()
        try:
            await vc.require_access(interaction)
        except vc.VoiceControlError as exc:
            await self._safe_text(interaction, str(exc))
            return False
        if mutating and not VOICE_CONTROL_ENABLED:
            await self._safe_text(
                interaction,
                "Voice Control отключён (VOICE_CONTROL_ENABLED=0).")
            return False
        return panel.guild_id == interaction.guild.id

    async def _respond(self, interaction: discord.Interaction,
                       panel: PanelState) -> None:
        """Обновить СУЩЕСТВУЮЩУЮ ephemeral-панель (не создавать новую)."""
        panel.touch()
        try:
            view = await self._render(panel)
            panel.revision += 1
        except Exception:
            log.exception("voice panel render failed")
            return
        panel.interaction = interaction
        # Modal submit и прочие уже отвеченные взаимодействия:
        # правим сообщение через токен исходной /voice-команды.
        if interaction.response.is_done() or \
                interaction.type == discord.InteractionType.modal_submit:
            src = panel.source_interaction
            if src is not None:
                try:
                    await src.edit_original_response(view=view)
                    return
                except Exception as exc:
                    log.debug("panel edit via source failed: %s", exc)
            try:
                await interaction.followup.edit_message(
                    panel.message_id, view=view) if panel.message_id else \
                    await interaction.followup.send(view=view, ephemeral=True)
            except Exception as exc:
                log.debug("voice panel followup edit failed: %s", exc)
            return
        try:
            await interaction.response.edit_message(view=view)
        except discord.HTTPException:
            # Fallback: сообщение могло быть отправлено другим
            # interaction — правим через исходный токен.
            src = panel.source_interaction
            if src is not None:
                try:
                    await src.edit_original_response(view=view)
                    return
                except Exception as exc:
                    log.debug("panel fallback edit failed: %s", exc)
            try:
                await interaction.response.send_message(view=view,
                                                        ephemeral=True)
            except Exception as exc:
                log.debug("voice panel re-render failed: %s", exc)

    async def handle(self, interaction: discord.Interaction, action: str,
                     **params: Any) -> None:
        panel = self._panel_for(interaction)
        if panel is None:
            await self._safe_text(
                interaction, "Панель не найдена — откройте панель заново.")
            return
        mutating = action in MUTATING_ACTIONS
        async with panel.lock:
            if not await self._guard(interaction, panel, mutating=mutating):
                return
            # Ранний defer для длительных операций — иначе Discord
            # оборвёт interaction через ~3 секунды.
            if action in LONG_ACTIONS and not interaction.response.is_done():
                try:
                    await interaction.response.defer(ephemeral=True)
                except discord.HTTPException:
                    pass
            try:
                await self._dispatch(interaction, panel, action, **params)
            except vc.StaleTargetError:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Участник покинул канал — список обновлён.")
            except vc.VoiceControlError as exc:
                await self._respond(interaction, panel)
                await self._safe_text(interaction, str(exc))
            except discord.Forbidden:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Недостаточно прав Discord у бота.")
            except discord.HTTPException as exc:
                log.warning("voice panel action %s failed: %s", action, exc)
                await self._safe_text(
                    interaction,
                    "Discord временно недоступен — попробуйте позже.")
            except Exception:
                log.exception("voice panel action %s crashed", action)
                await self._safe_text(
                    interaction, "Внутренняя ошибка — действие не подтверждено.")

    async def _safe_text(self, interaction: discord.Interaction,
                         text: str) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception as exc:
            log.debug("voice panel notice failed: %s", exc)

    async def _flash(self, interaction: discord.Interaction,
                     panel: PanelState, text: str) -> None:
        """Обновить панель и показать компактное уведомление."""
        await self._respond(interaction, panel)
        await self._safe_text(interaction, text)

    def _begin_confirm(self, panel: PanelState, kind: str,
                       snapshot: dict[str, Any]) -> None:
        """Фиксирует неизменяемый snapshot для подтверждения:
        повторное нажатие или смена состояния инвалидирует его."""
        panel.confirm_kind = kind
        panel.confirm_snapshot = snapshot
        panel.page = "confirm"

    def _consume_confirm(self, panel: PanelState, kind: str) -> dict | None:
        """Снимает подтверждение (защита от повторного нажатия) и
        возвращает snapshot, если он актуален."""
        if panel.confirm_kind != kind or panel.confirm_snapshot is None:
            return None
        snap = panel.confirm_snapshot
        panel.confirm_kind = None
        panel.confirm_snapshot = None
        return snap

    # ------------------------------------------------------------------
    # Диспетчер действий
    # ------------------------------------------------------------------
    async def _dispatch(self, interaction: discord.Interaction,
                        panel: PanelState, action: str, **params: Any) -> None:
        guild = interaction.guild
        member = interaction.user
        channel = self._panel_channel(panel)

        if action == "refresh":
            await self._respond(interaction, panel)
            return
        if action == "goto":
            panel.page = params["page"]
            panel.member_page = 0
            panel.confirm_kind = None
            panel.confirm_snapshot = None
            await self._respond(interaction, panel)
            return
        if action == "back":
            panel.page = "main"
            panel.member_page = 0
            panel.confirm_kind = None
            panel.confirm_snapshot = None
            await self._respond(interaction, panel)
            return
        if action == "chan_page":
            panel.chan_page = params["page"]
            await self._respond(interaction, panel)
            return
        if action == "select_channel":
            panel.channel_id = params["channel_id"]
            panel.page = "main"
            panel.selected_member = None
            panel.confirm_kind = None
            panel.confirm_snapshot = None
            await self._respond(interaction, panel)
            return
        if action == "select_member":
            panel.selected_member = params["user_id"]
            panel.page = "member"
            await self._respond(interaction, panel)
            return
        if action == "member_page":
            panel.member_page = params["page"]
            panel.page = "members"
            panel.selected_member = None
            await self._respond(interaction, panel)
            return
        if action == "set_duration":
            panel.duration = params["duration"]
            await self._respond(interaction, panel)
            return

        # --- Действия, требующие выбранного канала ---
        if channel is None:
            await self._respond(interaction, panel)
            await self._safe_text(interaction, "Сначала выберите голосовой канал.")
            return

        if action == "mute_all":
            result = await vc.bulk_mute(channel, member, duration=panel.duration)
            if result.get("error") == "disabled":
                await self._safe_text(
                    interaction, "Voice Control отключён.")
                return
            await self._flash(
                interaction, panel,
                f"Микрофоны отключены: {result['done']}\n"
                f"Пропущено: {result['skipped']}"
                + (f"\nОшибок: {result['errors']}" if result["errors"] else ""))
            return
        if action == "unmute_all":
            result = await vc.bulk_unmute(channel, member)
            if result.get("error") == "speaker_mode_active":
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction,
                    "Сначала завершите режим ведущего — «Разрешить всем» "
                    "снимает только ограничения Voice Control.")
            elif result.get("error") == "disabled":
                await self._safe_text(interaction, "Voice Control отключён.")
            else:
                await self._flash(
                    interaction, panel,
                    f"Микрофоны включены: {result['done']}\n"
                    f"Пропущено: {result['skipped']}\n"
                    f"Ошибок: {result['errors']}")
            return
        if action == "member_mute":
            target = await self._target_member(guild, panel.selected_member)
            if target is None:
                await self._safe_text(interaction, "Участник не найден.")
                await self._respond(interaction, panel)
                return
            exceptions = await vc.db.exceptions(guild.id, channel.id)
            result = await vc.apply_restriction(
                target, channel, "server_mute", vc.ORIGIN_MANUAL,
                actor=member, reason="Voice Control: ручной мут",
                expires_at=self._expiry(panel.duration),
                exceptions=exceptions)
            if result.get("skipped") == "foreign_mute":
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction,
                    "Серверный мут уже стоит и не принадлежит Voice Control — "
                    "не трогаем.")
            elif result.get("skipped") == "already_restricted":
                await self._respond(interaction, panel)
                await self._safe_text(interaction,
                                      "Участник уже ограничен Voice Control.")
            else:
                rec = result.get("restriction") or {}
                op_id = f"mute:{guild.id}:{channel.id}:{target.id}:{int(time.time())}"
                await vc.db.op_create(
                    op_id, guild.id, channel.id, "mute_member", member.id,
                    {"user_id": target.id, "restriction_id": rec.get("id")})
                await vc.db.op_finish(op_id, "done")
                await self._flash(interaction, panel,
                                  "Микрофон участника выключен.")
            return
        if action == "member_unmute":
            uid = panel.selected_member
            restrictions = await vc.db.restrictions(
                guild.id, user_id=uid, active_only=True)
            released_ids = []
            for r in restrictions:
                if r["kind"] == "server_mute":
                    await vc.release_restriction(r, actor=member)
                    released_ids.append(r["id"])
            op_id = f"unmute:{guild.id}:{channel.id}:{uid}:{int(time.time())}"
            await vc.db.op_create(
                op_id, guild.id, channel.id, "unmute_member", member.id,
                {"user_id": uid, "restriction_ids": released_ids})
            await vc.db.op_finish(op_id, "done")
            await self._flash(interaction, panel,
                              f"Снято ограничений Voice Control: "
                              f"{len(released_ids)}")
            return
        if action == "member_deafen":
            target = await self._target_member(guild, panel.selected_member)
            if target is None:
                await self._safe_text(interaction, "Участник не найден.")
                await self._respond(interaction, panel)
                return
            exceptions = await vc.db.exceptions(guild.id, channel.id)
            result = await vc.apply_restriction(
                target, channel, "server_deafen", vc.ORIGIN_MANUAL,
                actor=member,
                reason="Voice Control: серверное отключение звука",
                expires_at=self._expiry(panel.duration),
                exceptions=exceptions)
            if result.get("skipped") == "foreign_mute":
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Ограничение уже стоит и не принадлежит "
                                 "Voice Control — не трогаем.")
            elif result.get("skipped") == "already_restricted":
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Ограничение уже активно.")
            else:
                await self._flash(interaction, panel,
                                  "Звук участника отключён.")
            return
        if action == "member_undeafen":
            uid = panel.selected_member
            restrictions = await vc.db.restrictions(
                guild.id, user_id=uid, active_only=True)
            released = 0
            for r in restrictions:
                if r["kind"] == "server_deafen":
                    await vc.release_restriction(r, actor=member)
                    released += 1
            await self._flash(interaction, panel,
                              f"Снято ограничений звука: {released}")
            return
        if action == "member_disconnect":
            target = await self._target_member(guild, panel.selected_member)
            if target is None:
                await self._safe_text(interaction, "Участник не найден.")
                await self._respond(interaction, panel)
                return
            await vc.disconnect_member(target, member,
                                       expected_channel_id=channel.id)
            await self._flash(interaction, panel,
                              "Участник отключён от войса.")
            return
        if action == "goto_move_member":
            panel.page = "move_member"
            await self._respond(interaction, panel)
            return
        if action == "move_member_set_target":
            panel.move_target = params["channel_id"]
            panel.page = "move_member"
            await self._respond(interaction, panel)
            return
        if action == "member_move_do":
            target = await self._target_member(guild, panel.selected_member)
            dest = guild.get_channel(panel.move_target) \
                if panel.move_target else None
            if target is None or not vc._is_voice_channel(dest):
                await self._respond(interaction, panel)
                await self._safe_text(interaction,
                                      "Сначала выберите канал назначения.")
                return
            await vc.move_member(target, dest, member,
                                 expected_channel_id=channel.id)
            await self._flash(interaction, panel, "Участник перемещён.")
            return
        if action == "member_speaker_toggle":
            target = await self._target_member(guild, panel.selected_member)
            if target is None:
                await self._safe_text(interaction, "Участник не найден.")
                await self._respond(interaction, panel)
                return
            mode = await vc.db.mode(guild.id, channel.id)
            speakers = await vc.db.speakers(guild.id, channel.id)
            if mode and mode.get("speaker_mode") and target.id in speakers:
                await vc.speaker_remove(channel, target, member)
                await self._flash(interaction, panel,
                                  "Ведущий снят — участник замучен.")
            elif mode and mode.get("speaker_mode"):
                await vc.speaker_add(channel, target, member)
                await self._flash(interaction, panel,
                                  "Участник назначен ведущим.")
            else:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction,
                    "Режим ведущего не активен — откройте «Режим ведущего».")
            return
        if action == "member_protect":
            target = await self._target_member(guild, panel.selected_member)
            if target is None:
                await self._safe_text(interaction, "Участник не найден.")
                await self._respond(interaction, panel)
                return
            conf = await vc.db.settings(guild.id)
            protected = set(conf.get("protected_user_ids") or [])
            if target.id in protected:
                protected.discard(target.id)
                await vc.db.put_settings(
                    guild.id, protected_user_ids=sorted(protected))
                await self._flash(interaction, panel, "Защита снята.")
            else:
                protected.add(target.id)
                await vc.db.put_settings(
                    guild.id, protected_user_ids=sorted(protected))
                await self._flash(interaction, panel,
                                  "Участник добавлен в исключения.")
            return

        if action == "goto_speakers":
            panel.page = "speakers"
            await self._respond(interaction, panel)
            return
        if action == "speaker_add_pick":
            target = await self._target_member(guild, params["user_id"])
            if target is not None:
                await vc.speaker_add(channel, target, member)
                await self._flash(interaction, panel, "Ведущий добавлен.")
            return
        if action == "speaker_remove_pick":
            target = await self._target_member(guild, params["user_id"])
            if target is not None:
                await vc.speaker_remove(channel, target, member)
                await self._flash(interaction, panel, "Ведущий убран.")
            return
        if action == "speaker_start":
            result = await vc.start_speaker_mode(channel, member)
            if result.get("error") == "disabled":
                await self._safe_text(interaction, "Voice Control отключён.")
                return
            await self._flash(
                interaction, panel,
                f"Режим ведущего активирован.\n"
                f"Замучено слушателей: {result['muted']}")
            return
        if action == "speaker_stop_confirm":
            self._begin_confirm(panel, "speaker_stop",
                                {"channel_id": channel.id})
            await self._respond(interaction, panel)
            return
        if action == "speaker_stop":
            snap = self._consume_confirm(panel, "speaker_stop")
            if snap is None or snap.get("channel_id") != channel.id:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Подтверждение устарело — повторите.")
                return
            result = await vc.stop_speaker_mode(channel, member)
            await self._flash(
                interaction, panel,
                f"Режим завершён.\n"
                f"Снято ограничений: {result['released']}")
            return

        if action == "close_confirm":
            mode = await vc.db.mode(guild.id, channel.id)
            if mode and mode.get("closed"):
                await self._respond(interaction, panel)
                await self._safe_text(interaction, "Канал уже закрыт.")
                return
            self._begin_confirm(panel, "close", {"channel_id": channel.id})
            await self._respond(interaction, panel)
            return
        if action == "close_do":
            snap = self._consume_confirm(panel, "close")
            if snap is None or snap.get("channel_id") != channel.id:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Подтверждение устарело — повторите.")
                return
            await vc.close_voice(channel, member)
            await self._flash(interaction, panel,
                              "Войс закрыт для новых участников.")
            return
        if action == "open":
            result = await vc.open_voice(channel, member)
            msg = "Войс открыт."
            if result.get("conflicts"):
                msg += (f"\nКонфликтов перезаписей: {result['conflicts']} "
                        "(сторонние изменения не тронуты).")
            await self._flash(interaction, panel, msg)
            return

        if action == "goto_move":
            panel.page = "move"
            panel.move_pick = set()
            panel.move_target = None
            await self._respond(interaction, panel)
            return
        if action == "move_pick_set":
            panel.move_pick = {int(u) for u in params.get("user_ids", [])}
            await self._respond(interaction, panel)
            return
        if action == "move_pick_toggle":
            uid = params["user_id"]
            if uid in panel.move_pick:
                panel.move_pick.discard(uid)
            else:
                panel.move_pick.add(uid)
            await self._respond(interaction, panel)
            return
        if action == "move_pick_all":
            panel.move_pick = {m.id for m in vc.humans_in(channel)
                               if m.id != member.id}
            await self._respond(interaction, panel)
            return
        if action == "move_pick_clear":
            panel.move_pick = set()
            await self._respond(interaction, panel)
            return
        if action == "move_set_target":
            panel.move_target = params["channel_id"]
            await self._respond(interaction, panel)
            return
        if action == "move_do":
            if not panel.move_pick or panel.move_target is None:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction,
                    "Выберите участников и канал назначения.")
                return
            # Неизменяемый snapshot: исходный канал, цель, участники.
            self._begin_confirm(panel, "move", {
                "channel_id": channel.id,
                "target_id": panel.move_target,
                "user_ids": sorted(panel.move_pick),
            })
            await self._respond(interaction, panel)
            return
        if action == "move_confirm":
            snap = self._consume_confirm(panel, "move")
            if snap is None or snap.get("channel_id") != channel.id:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Подтверждение устарело — повторите.")
                return
            dest = guild.get_channel(snap.get("target_id"))
            if not vc._is_voice_channel(dest):
                await self._respond(interaction, panel)
                await self._safe_text(interaction,
                                      "Канал назначения недоступен.")
                return
            conf = await vc.db.settings(guild.id)
            exceptions = await vc.db.exceptions(guild.id, channel.id)
            moved = skipped = errors = 0
            for uid in snap.get("user_ids", []):
                target = guild.get_member(uid)
                if target is None:
                    continue
                if vc._member_is_protected(target, conf, exceptions) \
                        or uid == member.id:
                    skipped += 1
                    continue
                try:
                    await vc.move_member(target, dest, member,
                                         expected_channel_id=channel.id)
                    moved += 1
                except vc.StaleTargetError:
                    skipped += 1
                except (vc.VoiceControlError, discord.HTTPException):
                    errors += 1
            await vc.log_event(guild.id, member.id, "voice.move_selected",
                               channel_id=channel.id,
                               detail={"target_channel": dest.id,
                                       "moved": moved, "skipped": skipped,
                                       "errors": errors})
            await self._flash(
                interaction, panel,
                f"Перемещено: {moved}\nПропущено: {skipped}"
                + (f"\nОшибок: {errors}" if errors else ""))
            return
        if action == "disconnect_all_confirm":
            self._begin_confirm(panel, "disconnect_all",
                                {"channel_id": channel.id})
            await self._respond(interaction, panel)
            return
        if action == "disconnect_all_do":
            snap = self._consume_confirm(panel, "disconnect_all")
            if snap is None or snap.get("channel_id") != channel.id:
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, "Подтверждение устарело — повторите.")
                return
            result = await vc.disconnect_all(channel, member)
            await self._flash(
                interaction, panel,
                f"Отключено: {result['done']}\n"
                f"Пропущено: {result['skipped']}"
                + (f"\nОшибок: {result['errors']}"
                   if result["errors"] else ""))
            return
        if action == "confirm_no":
            panel.page = "main"
            panel.confirm_kind = None
            panel.confirm_snapshot = None
            await self._respond(interaction, panel)
            return

        if action == "goto_extras":
            panel.page = "extras"
            await self._respond(interaction, panel)
            return
        if action == "extra_auto_mute":
            if not VOICE_CONTROL_ENABLED:
                await self._safe_text(interaction,
                                      "Voice Control отключён.")
                return
            mode = await vc.db.mode(guild.id, channel.id)
            new_value = not (mode and mode.get("auto_mute"))
            result = await vc.set_auto_mute(channel, member, new_value)
            await self._flash(interaction, panel,
                              "Автомут новых участников "
                              + ("включён." if new_value else "выключен.")
                              + (f" Снято ограничений: {result['released']}."
                                 if result.get("released") else ""))
            return
        if action == "extra_rename":
            await interaction.response.send_modal(RenameModal(panel, self))
            return
        if action == "extra_limit":
            await interaction.response.send_modal(LimitModal(panel, self))
            return
        if action == "extra_history":
            panel.page = "history"
            await self._respond(interaction, panel)
            return
        if action == "extra_undo":
            result = await vc.undo_last(channel, member)
            errors_map = {
                "nothing_to_undo": "Нет действия для отмены.",
                "not_reversible": "Действие не обратимо.",
                "target_left": "Участник уже не в канале.",
            }
            if result.get("error"):
                await self._respond(interaction, panel)
                await self._safe_text(
                    interaction, errors_map.get(result["error"],
                                                "Отмена невозможна."))
            else:
                await self._flash(interaction, panel,
                                  f"Отменено: {result['undone']}")
            return
        if action == "goto_protect":
            panel.page = "protect"
            panel.member_page = 0
            await self._respond(interaction, panel)
            return
        if action == "protect_add_user":
            await interaction.response.send_modal(ProtectModal(panel, self, "user"))
            return
        if action == "protect_add_role":
            await interaction.response.send_modal(ProtectModal(panel, self, "role"))
            return
        if action == "protect_remove":
            ptype, tid = params["target_type"], params["target_id"]
            conf = await vc.db.settings(guild.id)
            key = ("protected_user_ids" if ptype == "user"
                   else "protected_role_ids")
            ids = set(conf.get(key) or [])
            ids.discard(tid)
            await vc.db.put_settings(guild.id, **{key: sorted(ids)})
            await vc.log_event(guild.id, member.id,
                               f"voice.protect_{ptype}_remove",
                               detail={"target_id": tid})
            await self._flash(interaction, panel, "Исключение удалено.")
            return

        if action == "goto_overview":
            panel.page = "overview"
            await self._respond(interaction, panel)
            return

        await self._respond(interaction, panel)

    # ------------------------------------------------------------------
    # Вспомогательные
    # ------------------------------------------------------------------
    def _panel_channel(self, panel: PanelState) -> discord.VoiceChannel | None:
        if panel.channel_id is None:
            return None
        channel = self.bot.get_channel(panel.channel_id)
        if vc._is_voice_channel(channel) and \
                channel.guild.id == panel.guild_id:
            return channel
        return None

    async def _target_member(self, guild: discord.Guild,
                             user_id: int | None):
        if user_id is None:
            return None
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                return None
        return member

    def _expiry(self, duration: timedelta | None):
        if duration is None:
            return None
        return datetime.now(UTC) + duration

    # ------------------------------------------------------------------
    # Рендер страниц
    # ------------------------------------------------------------------
    async def _render(self, panel: PanelState) -> DesignView:
        guild = self.bot.get_guild(panel.guild_id)
        view = DesignView(title="r!z3 | Voice Control", timeout=PANEL_TTL)
        if guild is None:
            view.text("Сервер недоступен.")
            return view
        conf = await vc.db.settings(guild.id)
        chan_list = await vc.active_channels(guild, conf)
        channel = self._panel_channel(panel)
        # Валидация выбранного канала: удалённый/пустой без режима —
        # сбрасываем выбор; пустой С режимом остаётся доступным.
        if panel.channel_id is not None and channel is None:
            panel.channel_id = None
        builders = {
            "main": self._page_main,
            "members": self._page_members,
            "member": self._page_member,
            "move_member": self._page_move_member,
            "speakers": self._page_speakers,
            "move": self._page_move,
            "extras": self._page_extras,
            "protect": self._page_protect,
            "history": self._page_history,
            "overview": self._page_overview,
            "confirm": self._page_confirm,
        }
        await builders.get(panel.page, self._page_main)(
            view, panel, guild, conf, chan_list, channel)
        return view

    # ------------------------------------------------------------------
    # Страницы
    # ------------------------------------------------------------------
    async def _page_main(self, view, panel, guild, conf, chan_list, channel):
        humans = vc.humans_in(channel) if channel else []
        server_muted = sum(1 for m in humans
                           if getattr(m.voice, "mute", False)) \
            if channel else 0
        mode = await vc.db.mode(guild.id, channel.id) if channel else None

        # Один TextDisplay: CV2 Container ограничен 10 компонентами —
        # сводим всю текстовую часть в один блок.
        lines: list[str] = []
        if channel is None:
            lines.append("Выберите голосовой канал.")
            if not chan_list:
                lines.append("Активных голосовых каналов нет.")
        else:
            lines += [
                f"**Голосовой канал:** {channel.mention}",
                f"**Участников:** {len(humans)}",
                f"**Без серверного мута:** {len(humans) - server_muted}",
                f"**С серверным мутом:** {server_muted}",
            ]
            if mode and mode.get("speaker_mode"):
                speakers = await vc.db.speakers(guild.id, channel.id)
                sp = [m for m in (guild.get_member(s) for s in speakers)
                      if m]
                listeners = len([m for m in humans
                                 if m.id not in speakers])
                lines += [
                    "**Режим ведущего активен**",
                    "**Ведущие:** "
                    + (", ".join(f"<@{m.id}>" for m in sp) or "—"),
                    f"**Слушателей:** {listeners}",
                ]
            else:
                flags = []
                if mode and mode.get("closed"):
                    flags.append("закрыт")
                if mode and mode.get("auto_mute"):
                    flags.append("автомут")
                if mode and mode.get("name_override"):
                    flags.append(f"название: {mode['name_override']}")
                if mode and mode.get("user_limit"):
                    flags.append(f"лимит: {mode['user_limit']}")
                lines.append("**Режим:** "
                             + (", ".join(flags) if flags else "Обычный"))
        if lines:
            view.text("\n".join(lines))
        else:
            view.text("*Нет активных войсов с участниками.*")

        # Выбор канала с пагинацией (>25 активных).
        if chan_list:
            total_pages = (len(chan_list) + CHANNELS_PER_PAGE - 1) \
                // CHANNELS_PER_PAGE
            page = max(0, min(panel.chan_page, total_pages - 1))
            chunk = chan_list[page * CHANNELS_PER_PAGE:
                              (page + 1) * CHANNELS_PER_PAGE]
            options = [
                discord.SelectOption(
                    label=(f"{item['channel'].name} — {item['count']} "
                           f"{_participants_word(item['count'])}")[:100],
                    value=str(item["channel"].id),
                    description="режим активен" if item["mode_active"] else None,
                    default=(item["channel"].id == panel.channel_id))
                for item in chunk
            ]
            view.row(self._chan_select(options))
            if total_pages > 1:
                view.row(
                    self._btn("◀", "chan_page", page=max(0, page - 1)),
                    self._btn("▶", "chan_page",
                              page=min(total_pages - 1, page + 1)),
                )
        # Длительность мута.
        dur_opts = [
            discord.SelectOption(
                label="До ручного отключения", value="0",
                default=panel.duration is None),
            discord.SelectOption(
                label="30 секунд", value="30",
                default=panel.duration == timedelta(seconds=30)),
            discord.SelectOption(
                label="1 минута", value="60",
                default=panel.duration == timedelta(minutes=1)),
            discord.SelectOption(
                label="3 минуты", value="180",
                default=panel.duration == timedelta(minutes=3)),
            discord.SelectOption(
                label="5 минут", value="300",
                default=panel.duration == timedelta(minutes=5)),
            discord.SelectOption(
                label="10 минут", value="600",
                default=panel.duration == timedelta(minutes=10)),
        ]

        async def _dur_cb(interaction: discord.Interaction) -> None:
            values = interaction.data.get("values") or ["0"]
            secs = int(values[0])
            await self.handle(
                interaction, "set_duration",
                duration=timedelta(seconds=secs) if secs else None)

        view.row(_CallbackSelect(
            _dur_cb, placeholder="Длительность мута…", options=dur_opts))

        if channel is not None:
            closed = bool(mode and mode.get("closed"))
            view.row(
                self._btn("Выключить всем", "mute_all",
                          discord.ButtonStyle.danger),
                self._btn("Разрешить всем", "unmute_all",
                          discord.ButtonStyle.success),
                self._btn("Участники", "goto", page="members"),
                self._btn("Режим ведущего", "goto_speakers"),
                self._btn("Открыть войс" if closed else "Закрыть войс",
                          "open" if closed else "close_confirm",
                          discord.ButtonStyle.success if closed
                          else discord.ButtonStyle.secondary),
            )
            view.row(
                self._btn("Перемещение", "goto_move"),
                self._btn("Дополнительно", "goto_extras"),
                self._btn("Обновить", "refresh"),
                self._btn("Обзор войсов", "goto_overview"),
            )
            if mode and mode.get("speaker_mode"):
                view.row(
                    self._btn("Добавить ведущего", "goto_speakers"),
                    self._btn("Убрать ведущего", "goto_speakers"),
                    self._btn("Завершить режим", "speaker_stop_confirm",
                              discord.ButtonStyle.danger),
                )

    async def _page_members(self, view, panel, guild, conf, chan_list, channel):
        view.text("**Участники**")
        if channel is None:
            view.text("Канал не выбран.")
            view.row(self._btn("Назад", "back"))
            return
        humans = vc.humans_in(channel)
        total_pages = max(1, (len(humans) + MEMBERS_PER_PAGE - 1)
                          // MEMBERS_PER_PAGE)
        page = max(0, min(panel.member_page, total_pages - 1))
        chunk = humans[page * MEMBERS_PER_PAGE:(page + 1) * MEMBERS_PER_PAGE]
        mode = await vc.db.mode(guild.id, channel.id)
        speakers = await vc.db.speakers(guild.id, channel.id)
        exceptions = await vc.db.exceptions(guild.id, channel.id)
        protected_users = set(conf.get("protected_user_ids") or [])
        protected_roles = set(conf.get("protected_role_ids") or [])
        exc_users = {e["target_id"] for e in exceptions
                     if e["target_type"] == "user"}
        exc_roles = {e["target_id"] for e in exceptions
                     if e["target_type"] == "role"}

        lines = []
        for m in chunk:
            flags = []
            v = getattr(m, "voice", None)
            if v and getattr(v, "mute", False):
                flags.append("🔇мут")
            if v and getattr(v, "self_mute", False):
                flags.append("selfmute")
            if v and getattr(v, "deaf", False):
                flags.append("🔈глух")
            if v and getattr(v, "self_deaf", False):
                flags.append("selfdeaf")
            if mode and mode.get("speaker_mode"):
                flags.append("🎤ведущий" if m.id in speakers else "слушатель")
            if m.id in protected_users or m.id in exc_users or any(
                    r.id in protected_roles or r.id in exc_roles
                    for r in m.roles):
                flags.append("🛡защита")
            lines.append(f"<@{m.id}> — {', '.join(flags) if flags else '—'}")
        view.text("\n".join(lines) if lines else "В канале нет участников.")

        view.row(self._member_select(humans, "select_member"))
        nav = []
        if total_pages > 1:
            nav.append(self._btn("◀", "member_page", page=max(0, page - 1)))
            nav.append(self._btn("▶", "member_page",
                                 page=min(total_pages - 1, page + 1)))
        nav.append(self._btn("Назад", "back"))
        view.row(*nav)

    async def _page_member(self, view, panel, guild, conf, chan_list, channel):
        view.text("**Участник**")
        target = await self._target_member(guild, panel.selected_member)
        if target is None or channel is None:
            view.text("Участник или канал недоступен.")
            view.row(self._btn("Назад", "goto", page="members"))
            return
        v = getattr(target, "voice", None)
        flags = []
        if v:
            if getattr(v, "mute", False):
                flags.append("серверный мут")
            if getattr(v, "self_mute", False):
                flags.append("само-мут")
            if getattr(v, "deaf", False):
                flags.append("серверная глухота")
            if getattr(v, "self_deaf", False):
                flags.append("само-глухота")
        protected = target.id in set(conf.get("protected_user_ids") or [])
        view.text(f"{target.mention} — {target.display_name}\n"
                  + (", ".join(flags) if flags else "без ограничений")
                  + ("\n🛡 В исключениях" if protected else ""))
        view.row(
            self._btn("Выключить микрофон", "member_mute",
                      discord.ButtonStyle.danger),
            self._btn("Снять мут панели", "member_unmute",
                      discord.ButtonStyle.success),
        )
        view.row(
            self._btn("Выключить звук", "member_deafen",
                      discord.ButtonStyle.danger),
            self._btn("Включить звук", "member_undeafen",
                      discord.ButtonStyle.success),
        )
        view.row(
            self._btn("Переместить", "goto_move_member"),
            self._btn("Отключить от войса", "member_disconnect",
                      discord.ButtonStyle.danger),
        )
        view.row(
            self._btn("Ведущий ±", "member_speaker_toggle"),
            self._btn("В исключения" if not protected
                      else "Убрать из исключений", "member_protect"),
        )
        view.row(self._btn("Назад к участникам", "goto", page="members"))

    async def _page_move_member(self, view, panel, guild, conf, chan_list,
                                channel):
        view.text("**Переместить участника**")
        dests = [c for c in guild.channels
                 if vc._is_voice_channel(c)
                 and not vc.is_protected_channel(guild.id, c.id, conf)
                 and (channel is None or c.id != channel.id)]
        options = [discord.SelectOption(
            label=f"{c.name} — {len(vc.humans_in(c))}", value=str(c.id),
            default=(c.id == panel.move_target)) for c in dests[:25]]
        if options:
            view.row(self._chan_select(options, action="move_member_set_target"))
        dest = guild.get_channel(panel.move_target) \
            if panel.move_target else None
        if vc._is_voice_channel(dest):
            view.text(f"Канал назначения: {dest.mention}")
        view.row(
            self._btn("Переместить", "member_move_do",
                      discord.ButtonStyle.success),
            self._btn("Назад", "goto", page="member"),
        )

    async def _page_speakers(self, view, panel, guild, conf, chan_list,
                             channel):
        view.text("**Режим ведущего**")
        if channel is None:
            view.text("Канал не выбран.")
            view.row(self._btn("Назад", "back"))
            return
        mode = await vc.db.mode(guild.id, channel.id)
        speakers = await vc.db.speakers(guild.id, channel.id)
        humans = vc.humans_in(channel)
        if mode and mode.get("speaker_mode"):
            sp = [guild.get_member(s) for s in speakers]
            sp = [m for m in sp if m]
            view.text("**Активен**")
            view.text("Ведущие: "
                      + (", ".join(f"<@{m.id}>" for m in sp) or "—"))
            view.text("Слушателей: "
                      + str(len([m for m in humans if m.id not in speakers])))
            non_sp = [m for m in humans if m.id not in speakers]
            sp_here = [m for m in sp
                       if m.voice and m.voice.channel
                       and m.voice.channel.id == channel.id]
            view.row(self._member_select(non_sp, "speaker_add_pick"))
            view.row(self._member_select(sp_here, "speaker_remove_pick"))
            view.row(self._btn("Завершить режим", "speaker_stop_confirm",
                               discord.ButtonStyle.danger))
        else:
            view.text(
                "Режим не активен. Ведущий и выбранные участники смогут "
                "говорить, остальные получат серверный мут (включая новых "
                "участников).")
            view.row(self._btn("Активировать режим", "speaker_start",
                               discord.ButtonStyle.success))
        view.row(self._btn("Назад", "back"))

    async def _page_move(self, view, panel, guild, conf, chan_list, channel):
        view.text("**Перемещение**")
        if channel is None:
            view.text("Канал не выбран.")
            view.row(self._btn("Назад", "back"))
            return
        humans = vc.humans_in(channel)
        view.text(f"Канал: {channel.mention} — участников: {len(humans)}")
        view.text(f"Выбрано для перемещения: {len(panel.move_pick)}")

        options = [
            discord.SelectOption(
                label=(m.display_name or m.name)[:100], value=str(m.id),
                default=(m.id in panel.move_pick))
            for m in humans[:25]
        ] or [discord.SelectOption(label="— нет участников —", value="0")]

        async def _multi_cb(interaction: discord.Interaction) -> None:
            values = [int(v) for v in (interaction.data.get("values") or [])
                      if int(v)]
            await self.handle(interaction, "move_pick_set", user_ids=values)

        view.row(_CallbackSelect(
            _multi_cb, placeholder="Участники (можно несколько)…",
            options=options[:25], min_values=1,
            max_values=min(25, max(1, len(humans)))))
        view.row(
            self._btn("Выбрать всех", "move_pick_all"),
            self._btn("Сбросить", "move_pick_clear"),
        )
        dests = [c for c in guild.channels
                 if vc._is_voice_channel(c)
                 and not vc.is_protected_channel(guild.id, c.id, conf)
                 and c.id != channel.id]
        dest_options = [discord.SelectOption(
            label=f"{c.name} — {len(vc.humans_in(c))}", value=str(c.id),
            default=(c.id == panel.move_target)) for c in dests[:25]]
        if dest_options:
            view.row(self._chan_select(dest_options,
                                       action="move_set_target"))
        dest = guild.get_channel(panel.move_target) \
            if panel.move_target else None
        if vc._is_voice_channel(dest):
            view.text(f"Цель: {dest.mention}")
        view.row(
            self._btn("Переместить выбранных", "move_do",
                      discord.ButtonStyle.success),
            self._btn("Отключить всех", "disconnect_all_confirm",
                      discord.ButtonStyle.danger),
        )
        view.row(self._btn("Назад", "back"))

    async def _page_extras(self, view, panel, guild, conf, chan_list, channel):
        view.text("**Дополнительно**")
        if channel is None:
            view.text("Канал не выбран.")
            view.row(self._btn("Назад", "back"))
            return
        mode = await vc.db.mode(guild.id, channel.id)
        auto = bool(mode and mode.get("auto_mute"))
        view.row(
            self._btn(f"Автомут новых: {'вкл' if auto else 'выкл'}",
                      "extra_auto_mute"),
            self._btn("Переименовать", "extra_rename"),
            self._btn("Лимит участников", "extra_limit"),
        )
        view.row(
            self._btn("Защищённые участники/роли", "goto_protect"),
            self._btn("История действий", "extra_history"),
            self._btn("Отменить последнее", "extra_undo"),
        )
        view.row(self._btn("Назад", "back"))

    async def _page_protect(self, view, panel, guild, conf, chan_list,
                            channel):
        view.text("**Защищённые участники и роли** (уровень сервера)")
        users = conf.get("protected_user_ids") or []
        roles = conf.get("protected_role_ids") or []
        lines = []
        for uid in users[:10]:
            m = guild.get_member(uid)
            lines.append(f"👤 <@{uid}>" if m else f"👤 {uid}")
        for rid in roles[:10]:
            r = guild.get_role(rid)
            lines.append(f"🎭 {r.mention}" if r else f"🎭 {rid}")
        view.text("\n".join(lines) if lines else "Исключений нет.")
        view.row(
            self._btn("Добавить участника (ID)", "protect_add_user"),
            self._btn("Добавить роль (ID)", "protect_add_role"),
        )
        opts = []
        for uid in users[:20]:
            m = guild.get_member(uid)
            opts.append(discord.SelectOption(
                label=f"Убрать: {(m.display_name if m else str(uid))[:80]}",
                value=f"user:{uid}"))
        for rid in roles[:5]:
            r = guild.get_role(rid)
            opts.append(discord.SelectOption(
                label=f"Убрать роль: {(r.name if r else str(rid))[:80]}",
                value=f"role:{rid}"))
        if opts:
            async def _prot_cb(interaction: discord.Interaction) -> None:
                v = (interaction.data.get("values") or [""])[0]
                ptype, _, tid = v.partition(":")
                await self.handle(interaction, "protect_remove",
                                  target_type=ptype, target_id=int(tid))
            view.row(_CallbackSelect(
                _prot_cb, placeholder="Убрать исключение…",
                options=opts[:25]))
        view.row(self._btn("Назад", "back"))

    async def _page_history(self, view, panel, guild, conf, chan_list,
                            channel):
        view.text("**История действий Voice Control**")
        events = await vc.db.events(guild.id, limit=15)
        if not events:
            view.text("Записей нет.")
        else:
            lines = []
            for e in events:
                actor = f"<@{e['actor_id']}>" if e["actor_id"] else "система"
                when = (f"<t:{int(e['created_at'].timestamp())}:R>"
                        if e.get("created_at") else "")
                detail = vc._jdetail(e.get("detail"))
                extra = ""
                for k in ("done", "muted", "released", "moved", "errors"):
                    if k in detail:
                        extra += f" {k}={detail[k]}"
                lines.append(f"`{e['action']}` — {actor} {when}{extra}")
            view.text("\n".join(lines[:15]))
        view.row(self._btn("Назад", "back"))

    async def _page_overview(self, view, panel, guild, conf, chan_list,
                             channel):
        view.text("**Voice Control | Обзор сервера**")
        total_members = sum(i["count"] for i in chan_list)
        muted_total = 0
        modes = await vc.db.modes(guild.id)
        speaker_modes = sum(1 for m in modes if m.get("speaker_mode"))
        for item in chan_list:
            muted_total += sum(
                1 for m in item["humans"]
                if getattr(m.voice, "mute", False))
        view.text(
            f"Активных войсов: {len(chan_list)}\n"
            f"Всего участников: {total_members}\n"
            f"С серверным мутом: {muted_total}\n"
            f"Активных режимов ведущего: {speaker_modes}")
        view.sep()
        lines = [
            f"{i['channel'].name} — {i['count']} "
            f"{_participants_word(i['count'])}"
            + (" · режим" if i["mode_active"] else "")
            for i in chan_list[:25]
        ]
        view.text("\n".join(lines) if lines else "Нет активных войсов.")
        if chan_list:
            options = [discord.SelectOption(
                label=f"{i['channel'].name} — {i['count']}",
                value=str(i["channel"].id)) for i in chan_list[:25]]
            view.row(self._chan_select(options))
        view.row(self._btn("Обновить", "refresh"),
                 self._btn("Назад", "back"))

    async def _page_confirm(self, view, panel, guild, conf, chan_list,
                            channel):
        kind = panel.confirm_kind
        if channel is None:
            view.text("Канал не выбран.")
            view.row(self._btn("Назад", "back"))
            return
        texts = {
            "close": (f"Закрыть войс «{channel.name}»? Новые участники не "
                      "смогут войти (кроме тех, у кого уже есть права)."),
            "speaker_stop": ("Завершить режим ведущего? Ограничения режима "
                             "будут сняты."),
            "move": self._confirm_move_text(panel, channel),
            "disconnect_all": (f"Отключить ВСЕХ участников от войса "
                               f"«{channel.name}»? Действие необратимо."),
        }
        view.text("**Подтверждение**")
        view.text(texts.get(kind, "Подтвердите действие."))
        actions = {
            "close": "close_do",
            "speaker_stop": "speaker_stop",
            "move": "move_confirm",
            "disconnect_all": "disconnect_all_do",
        }
        action = actions.get(kind)
        if action:
            view.row(
                self._btn("Подтвердить", action, discord.ButtonStyle.danger),
                self._btn("Отмена", "confirm_no"),
            )
        else:
            view.row(self._btn("Назад", "back"))

    def _confirm_move_text(self, panel: PanelState,
                           channel: discord.VoiceChannel) -> str:
        snap = panel.confirm_snapshot or {}
        target_id = snap.get("target_id") or panel.move_target
        dest = self.bot.get_channel(target_id) if target_id else None
        dest_name = getattr(dest, "name", "канал") if dest else "канал"
        n = len(snap.get("user_ids") or panel.move_pick)
        return (f"Переместить {n} участников из «{channel.name}» "
                f"в «{dest_name}»?")

    # ------------------------------------------------------------------
    # Аварийная очистка (owner-only)
    # ------------------------------------------------------------------
    @app_commands.command(
        name="voice_emergency",
        description="Аварийное завершение режимов Voice Control (только владелец бота)")
    @app_commands.describe(
        confirm="false — dry-run отчёт; true — выполнить очистку")
    async def voice_emergency(self, interaction: discord.Interaction,
                              confirm: bool = False) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Только на сервере.", ephemeral=True)
            return
        member = interaction.user
        # Аварийная очистка — глобальная операция: исключительно
        # BOT_OWNER_ID. Administrator и владелец сервера не могут.
        if member.id != getattr(vc, "BOT_OWNER_ID", 0):
            await interaction.response.send_message(
                "Только для владельца бота.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await vc.emergency_cleanup(
                interaction.guild, member, dry_run=not confirm)
            lines = [
                f"**dry_run:** {report['dry_run']}",
                f"Каналов с режимами: {report['channels_with_modes']}",
                f"Активных ограничений: {report['active_restrictions']}",
                f"Незавершённых: {report['pending_states']}",
                f"Overwrite-записей: {report['active_overwrites']}",
            ]
            if confirm:
                lines.append(f"Снято попыток: {report['released_attempted']}")
                lines.append(f"Осталось pending: {report['pending_remaining']}")
                lines.append(f"Ошибок: {report['errors']}")
            else:
                lines.append("Повторите с confirm=true для выполнения.")
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except Exception as exc:
            log.exception("voice emergency failed")
            await interaction.followup.send(
                f"Ошибка аварийной очистки: {exc}", ephemeral=True)

    # ------------------------------------------------------------------
    # Компоненты
    # ------------------------------------------------------------------
    def _btn(self, label: str, action: str,
             style: discord.ButtonStyle = discord.ButtonStyle.secondary,
             **params) -> discord.ui.Button:
        async def cb(interaction: discord.Interaction) -> None:
            await self.handle(interaction, action, **params)
        return _CallbackButton(cb, label=label[:80], style=style)

    def _chan_select(self, options: list[discord.SelectOption],
                     action: str = "select_channel") -> discord.ui.Select:
        async def cb(interaction: discord.Interaction) -> None:
            values = interaction.data.get("values") or []
            if values:
                await self.handle(interaction, action,
                                  channel_id=int(values[0]))
        return _CallbackSelect(
            cb, placeholder="Выбрать голосовой канал…",
            options=options[:25])

    def _member_select(self, members: list[discord.Member], action: str,
                       max_values: int = 1) -> discord.ui.Select:
        options = [
            discord.SelectOption(
                label=(m.display_name or m.name)[:100], value=str(m.id),
                description="🔇" if getattr(getattr(m, "voice", None),
                                            "mute", False) else None)
            for m in members[:25]
        ] or [discord.SelectOption(label="— нет участников —", value="0")]

        async def cb(interaction: discord.Interaction) -> None:
            values = [int(v) for v in (interaction.data.get("values") or [])
                      if int(v)]
            if values:
                await self.handle(interaction, action, user_id=values[0])
        return _CallbackSelect(
            cb, placeholder="Выбрать участника…", options=options[:25],
            min_values=1, max_values=max_values)


class _CallbackButton(discord.ui.Button):
    """Button с callback, переданным в конструктор — в discord.py
    2.7 callback kwarg у Button/Select удалён."""

    def __init__(self, callback, **kwargs):
        super().__init__(**kwargs)
        self._cb = callback

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cb(interaction)


class _CallbackSelect(discord.ui.Select):
    """Select с callback, переданным в конструктор
    (discord.ui.Select не принимает callback kwarg)."""

    def __init__(self, callback, **kwargs):
        super().__init__(**kwargs)
        self._cb = callback

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._cb(interaction)


# ----------------------------------------------------------------------
# Модальные окна
# ----------------------------------------------------------------------
class _PanelModal(discord.ui.Modal):
    """Базовая модалка: привязка к panel_id/guild/user/каналу.
    Submit от устаревшей модалки (канал сменился, панель инвалидна)
    отвергается."""

    def __init__(self, panel: PanelState, cog: VoiceControlCog,
                 **modal_kwargs):
        super().__init__(**modal_kwargs)
        self.panel = panel
        self.cog = cog
        self.expected_channel_id = panel.channel_id
        self.expected_panel_id = panel.panel_id

    async def _panel_alive(self, interaction: discord.Interaction) -> bool:
        panel = self.panel
        if not panel.valid or panel.expired():
            await interaction.response.send_message(
                "Панель устарела — откройте панель заново.", ephemeral=True)
            return False
        if self.cog.panels.get((panel.guild_id, panel.user_id)) is not panel \
                or panel.panel_id != self.expected_panel_id:
            await interaction.response.send_message(
                "Панель была пересоздана — повторите действие.",
                ephemeral=True)
            return False
        try:
            await vc.require_access(interaction)
        except vc.VoiceControlError:
            await interaction.response.send_message(
                "Недостаточно прав.", ephemeral=True)
            return False
        if not VOICE_CONTROL_ENABLED:
            await interaction.response.send_message(
                "Voice Control отключён.", ephemeral=True)
            return False
        return True

    async def _channel_still_valid(self, interaction: discord.Interaction,
                                   panel: PanelState):
        if panel.channel_id != self.expected_channel_id:
            await interaction.response.send_message(
                "Выбранный канал изменился — повторите действие.",
                ephemeral=True)
            return None
        channel = self.cog._panel_channel(panel)
        if channel is None:
            await interaction.response.send_message(
                "Канал недоступен.", ephemeral=True)
            return None
        return channel

    async def _finish(self, interaction: discord.Interaction,
                      panel: PanelState) -> None:
        """Modal submit: defer → обновление панели через handle."""
        panel.touch()
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass
        await self.cog.handle(interaction, "refresh")


class RenameModal(_PanelModal, title="Переименовать войс"):
    name = discord.ui.TextInput(label="Новое название", max_length=100)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._panel_alive(interaction):
            return
        panel = self.panel
        async with panel.lock:
            channel = await self._channel_still_valid(interaction, panel)
            if channel is None:
                return
            guild = interaction.guild
            old_name = getattr(channel, "name", None)
            mode = await vc.db.mode(guild.id, channel.id)
            await vc.db.put_mode(
                guild.id, channel.id, name_override=self.name.value,
                name_before=(mode or {}).get("name_before") or old_name)
            try:
                await channel.edit(name=self.name.value[:100],
                                   reason="r!z3 Voice Control")
            except discord.HTTPException as exc:
                await vc.db.put_mode(guild.id, channel.id,
                                     name_override=None)
                await interaction.response.send_message(
                    f"Discord отклонил переименование: {exc}",
                    ephemeral=True)
                return
            await vc.log_event(guild.id, interaction.user.id, "voice.rename",
                               channel_id=channel.id,
                               detail={"name": self.name.value})
            await self._finish(interaction, panel)


class LimitModal(_PanelModal, title="Лимит участников"):
    limit = discord.ui.TextInput(label="Лимит (0 — без лимита)", max_length=3)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._panel_alive(interaction):
            return
        panel = self.panel
        async with panel.lock:
            try:
                value = int(self.limit.value.strip())
            except ValueError:
                await interaction.response.send_message(
                    "Введите число.", ephemeral=True)
                return
            if value < 0 or value > 99:
                await interaction.response.send_message(
                    "Лимит 0–99.", ephemeral=True)
                return
            channel = await self._channel_still_valid(interaction, panel)
            if channel is None:
                return
            guild = interaction.guild
            mode = await vc.db.mode(guild.id, channel.id)
            await vc.db.put_mode(
                guild.id, channel.id,
                user_limit=value or None,
                user_limit_before=(mode or {}).get("user_limit_before")
                if (mode or {}).get("user_limit_before") is not None
                else getattr(channel, "user_limit", None))
            try:
                # user_limit=0 в Discord API = снять ограничение.
                await channel.edit(user_limit=value,
                                   reason="r!z3 Voice Control")
            except discord.HTTPException as exc:
                await vc.db.put_mode(guild.id, channel.id, user_limit=None)
                await interaction.response.send_message(
                    f"Discord отклонил изменение лимита: {exc}",
                    ephemeral=True)
                return
            await vc.log_event(guild.id, interaction.user.id, "voice.limit",
                               channel_id=channel.id, detail={"limit": value})
            await self._finish(interaction, panel)


class ProtectModal(_PanelModal):
    def __init__(self, panel: PanelState, cog: VoiceControlCog, kind: str):
        self.kind = kind
        title = ("Защищённый участник" if kind == "user"
                 else "Защищённая роль")
        super().__init__(panel, cog, title=title)
        self.raw_id = discord.ui.TextInput(
            label="Discord ID (число)", max_length=25)
        self.add_item(self.raw_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self._panel_alive(interaction):
            return
        panel = self.panel
        async with panel.lock:
            try:
                target_id = int(self.raw_id.value.strip())
            except ValueError:
                await interaction.response.send_message(
                    "Нужен числовой ID.", ephemeral=True)
                return
            guild = interaction.guild
            # Валидация принадлежности цели текущему серверу.
            if self.kind == "user":
                try:
                    await guild.fetch_member(target_id)
                except discord.HTTPException:
                    await interaction.response.send_message(
                        "Участник не найден на этом сервере.",
                        ephemeral=True)
                    return
            else:
                if guild.get_role(target_id) is None:
                    await interaction.response.send_message(
                        "Роль не найдена на этом сервере.",
                        ephemeral=True)
                    return
            key = ("protected_user_ids" if self.kind == "user"
                   else "protected_role_ids")
            conf = await vc.db.settings(guild.id)
            ids = set(conf.get(key) or [])
            ids.add(target_id)
            await vc.db.put_settings(guild.id, **{key: sorted(ids)})
            await vc.log_event(guild.id, interaction.user.id,
                               f"voice.protect_{self.kind}_add",
                               detail={"target_id": target_id})
            await self._finish(interaction, panel)


async def setup(bot: commands.Bot) -> None:
    cog = VoiceControlCog(bot)
    await bot.add_cog(cog)
