"""View для драфта — Select Menu выбора игрока."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import discord

from config import replace_emojis
from models.tournament import Tournament, TournamentPhase
from storage.json_store import store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

# Сериализация выборов внутри одного турнира: два быстрых select-callback
# (двойной клик, два окна) не должны пройти валидацию до того, как первый
# зафиксировал pick. Таймер драфта мутирует состояние синхронно — гонка
# с ним отсекается повторной валидацией внутри lock.
_PICK_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _pick_lock(guild_id: int, channel_id: int) -> asyncio.Lock:
    key = (guild_id, channel_id)
    lock = _PICK_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PICK_LOCKS[key] = lock
    return lock


class PlayerSelect(discord.ui.Select):
    """Select Menu с доступными игроками текущего круга."""

    def __init__(self, guild_id: int, channel_id: int, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Выберите игрока...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"draft_select:{guild_id}:{channel_id}",
        )
        self.guild_id = guild_id
        self.channel_id = channel_id

    @staticmethod
    def _captain_name(tournament: Tournament) -> str | None:
        """Имя капитана, который сейчас должен выбирать."""
        picker_pos = tournament.current_picker_position()
        if picker_pos is None:
            return None
        return tournament.captains[tournament.captain_order[picker_pos]]

    @staticmethod
    def _turn_owner_ok(tournament: Tournament, user_name: str) -> bool:
        """Проверка «сейчас выбирает этот пользователь» (по нику)."""
        expected = PlayerSelect._captain_name(tournament)
        if expected is None:
            return False
        if tournament.is_test:
            return True
        from utils.cosmetics import clean_nickname

        return clean_nickname(user_name) == clean_nickname(expected)

    async def _ephemeral(self, interaction: discord.Interaction,
                         text: str) -> None:
        """Ответ-ошибка: до defer — response.send_message, после — followup."""
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    replace_emojis(text), ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    replace_emojis(text), ephemeral=True
                )
        except discord.NotFound:
            pass

    async def callback(self, interaction: discord.Interaction) -> None:
        from utils import perf_metrics

        t0 = time.perf_counter()
        marks: dict[str, float] = {}

        def mark(name: str) -> None:
            marks[name] = (time.perf_counter() - t0) * 1000.0

        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await self._ephemeral(interaction, "❌ Турнир не найден.")
            return

        if tournament.phase != TournamentPhase.DRAFT:
            await self._ephemeral(interaction, "❌ Драфт не активен.")
            return

        picker_pos = tournament.current_picker_position()
        if picker_pos is None:
            await self._ephemeral(interaction, "❌ Сейчас не ваш ход.")
            return

        expected_captain_name = tournament.captains[
            tournament.captain_order[picker_pos]
        ]
        user_name = interaction.user.display_name
        from utils.cosmetics import clean_nickname

        cleaned_name = clean_nickname(user_name)
        cleaned_captain_name = clean_nickname(expected_captain_name)

        # In test mode, allow anyone to pick
        if not tournament.is_test and cleaned_name != cleaned_captain_name:
            await self._ephemeral(
                interaction, f"❌ Сейчас выбирает {expected_captain_name}"
            )
            return

        player = self.values[0]
        key = str(tournament.current_circle)
        if player not in tournament.available.get(key, []):
            await self._ephemeral(interaction, "❌ Этот игрок уже выбран.")
            return

        mark("validation")

        # Быстрый ack: DEFERRED_MESSAGE_UPDATE (type 6) — Discord получает
        # подтверждение до любой Discord API работы (edit nick, правка
        # сообщения, отправка пинга). Эфемерных сообщений при успехе нет.
        try:
            await interaction.response.defer()
        except discord.NotFound:
            return

        lock = _pick_lock(self.guild_id, self.channel_id)
        async with lock:
            lock_wait_ms = (time.perf_counter() - t0) * 1000.0 - marks["validation"]

            # Повторная валидация: за время defer/ожидания lock таймер
            # мог сделать случайный выбор или ход мог перейти дальше.
            tournament = store.get(self.guild_id, self.channel_id)
            stale = None
            if tournament is None or tournament.phase != TournamentPhase.DRAFT:
                stale = "Драфт уже завершён."
            else:
                pos = tournament.current_picker_position()
                if pos is None:
                    stale = "Сейчас не ваш ход."
                else:
                    expected = tournament.captains[
                        tournament.captain_order[pos]
                    ]
                    if not tournament.is_test and (
                        clean_nickname(interaction.user.display_name)
                        != clean_nickname(expected)
                    ):
                        stale = f"Сейчас выбирает {expected}"
                    elif player not in tournament.available.get(
                        str(tournament.current_circle), []
                    ):
                        stale = "Этот игрок уже выбран."
            if stale is not None:
                await self._ephemeral(interaction, f"❌ {stale}")
                return
            picker_pos = tournament.current_picker_position()

            # Автоочистка ника от эмодзи — косметика, после ack и вне
            # критического пути (ошибка не должна ломать выбор).
            if cleaned_name != user_name:
                try:
                    await interaction.user.edit(nick=cleaned_name)
                except Exception as exc:  # noqa: BLE001 - косметика
                    logger.debug("Nick cleanup failed: %s", exc)

            tournament.pick_player(picker_pos, player)
            draft_complete = tournament.advance_after_pick()
            mark("state_update")

            # Критическое сохранение выбора — до Discord-обновлений.
            store.set(tournament)
            mark("database_save")

            bot: TournamentBot = interaction.client  # type: ignore[assignment]
            await bot.update_tournament_message(interaction.guild, tournament)
            mark("message_update")

            # Housekeeping пинг-сообщения капитана: прямое удаление по
            # известному ID через HTTP API — один DELETE-запрос без
            # fetch и без ограничений bulk-delete (возраст/пины).
            try:
                if tournament.draft_message_id > 0:
                    try:
                        await bot.http.delete_message(
                            interaction.channel.id,
                            tournament.draft_message_id,
                        )
                    except Exception as exc:  # noqa: BLE001
                        # Сообщение могло быть уже удалено — не критично
                        logger.debug(
                            "Old draft ping delete failed: %s", exc
                        )

                if not draft_complete:
                    next_picker_pos = tournament.current_picker_position()
                    if next_picker_pos is not None:
                        next_captain_name = tournament.captains[
                            tournament.captain_order[next_picker_pos]
                        ]
                        next_captain_id = tournament.player_user_ids.get(
                            next_captain_name, 0
                        )
                        if next_captain_id > 0:
                            new_message = await interaction.channel.send(
                                f" <@{next_captain_id}> - ваша очередь выбирать!"
                            )
                        else:
                            new_message = await interaction.channel.send(
                                f" {next_captain_name} - ваша очередь выбирать!"
                            )
                        tournament.draft_message_id = new_message.id
                        store.set(tournament)
            except Exception:
                logger.warning(
                    "Draft ping housekeeping failed guild=%s channel=%s",
                    self.guild_id, self.channel_id, exc_info=True,
                )
            mark("ping_housekeeping")

        total_ms = (time.perf_counter() - t0) * 1000.0
        stage_ms = {k: round(v, 1) for k, v in marks.items()}
        logger.info(
            "[DRAFT PERF] guild=%s channel=%s validation=%.1fms "
            "lock_wait=%.1fms state=%.1fms save=%.1fms "
            "message=%.1fms ping=%.1fms total=%.1fms complete=%s stages=%s",
            self.guild_id, self.channel_id,
            marks.get("validation", 0.0), lock_wait_ms,
            marks.get("state_update", 0.0) - marks.get("validation", 0.0),
            marks.get("database_save", 0.0) - marks.get("state_update", 0.0),
            marks.get("message_update", 0.0) - marks.get("database_save", 0.0),
            marks.get("ping_housekeeping", 0.0)
            - marks.get("message_update", 0.0),
            total_ms, draft_complete, stage_ms,
        )
        for name, value in (
            ("draft_pick_validation_ms", marks.get("validation", 0.0)),
            ("draft_pick_save_ms",
             marks.get("database_save", 0.0) - marks.get("state_update", 0.0)),
            ("draft_pick_message_ms",
             marks.get("message_update", 0.0) - marks.get("database_save", 0.0)),
            ("draft_pick_total_ms", total_ms),
        ):
            perf_metrics.record(name, value)

        if draft_complete:
            await self._ephemeral(interaction, "✅ Драфт завершён!")


class DraftView(discord.ui.View):
    """Persistent View с Select Menu для драфта."""

    def __init__(self, guild_id: int, channel_id: int, available_players: list[str]):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        options = [
            discord.SelectOption(label=p, value=p) for p in available_players[:25]
        ]
        if options:
            self.add_item(PlayerSelect(guild_id, channel_id, options))
        # Add warning if more than 25 players available
        if len(available_players) > 25:
            self._warning = replace_emojis(f"⚠️ Показано 25 из {len(available_players)} игроков")


def build_draft_view(tournament: Tournament) -> DraftView | None:
    """Создать View для текущего состояния драфта."""
    if tournament.phase != TournamentPhase.DRAFT:
        return None
    key = str(tournament.current_circle)
    available = tournament.available.get(key, [])
    if not available:
        return None
    # Если сейчас автовыбор — Select не нужен
    picker_pos = tournament.current_picker_position()
    if picker_pos is None:
        return None
    return DraftView(tournament.guild_id, tournament.channel_id, available)
