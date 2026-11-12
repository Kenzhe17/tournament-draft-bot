"""View для драфта — Select Menu выбора игрока."""

from __future__ import annotations

import asyncio
import time

from config import replace_emojis

import logging
from typing import TYPE_CHECKING

import discord

from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from utils.embeds import build_embed_for_phase
from utils import perf_metrics

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


async def _save_tournament(tournament: Tournament) -> None:
    """store.set — синхронная запись на диск; выносим из event loop."""
    await asyncio.to_thread(store.set, tournament)


async def _clean_member_nick(member, cleaned: str) -> None:
    """Косметическая правка никнейма — фоново, не блокирует pick."""
    try:
        await member.edit(nick=cleaned)
    except Exception:
        pass


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

    async def callback(self, interaction: discord.Interaction) -> None:
        t_start = time.perf_counter()
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."),
                ephemeral=True,
            )
            return

        if tournament.phase != TournamentPhase.DRAFT:
            await interaction.response.send_message(
                replace_emojis("❌ Драфт не активен."),
                ephemeral=True,
            )
            return

        picker_pos = tournament.current_picker_position()
        if picker_pos is None:
            await interaction.response.send_message(
                replace_emojis("❌ Сейчас не ваш ход."),
                ephemeral=True,
            )
            return

        # Check if it's the captain's turn (by nickname)
        expected_captain_name = tournament.captains[tournament.captain_order[picker_pos]]
        user_name = interaction.user.display_name
        from utils.cosmetics import clean_nickname
        cleaned_name = clean_nickname(user_name)
        cleaned_captain_name = clean_nickname(expected_captain_name)

        # In test mode, allow anyone to pick
        if not tournament.is_test and cleaned_name != cleaned_captain_name:
            await interaction.response.send_message(
                replace_emojis(f"❌ Сейчас выбирает {expected_captain_name}"),
                ephemeral=True,
            )
            return

        player = self.values[0]
        key = str(tournament.current_circle)
        if player not in tournament.available.get(key, []):
            await interaction.response.send_message(
                replace_emojis("❌ Этот игрок уже выбран."),
                ephemeral=True,
            )
            return

        # Атомарная секция без await: проверка + pick + advance.
        # Параллельный interaction не может вклиниться — двойной
        # выбор/пропуск очереди исключены.
        tournament.pick_player(picker_pos, player)
        draft_complete = tournament.advance_after_pick()
        perf_metrics.record(
            "draft_pick_validate_ms",
            (time.perf_counter() - t_start) * 1000,
        )

        # Discord ack сразу — до любых I/O. Успешный выбор не
        # создаёт ephemeral-сообщений; ошибки ниже — через followup.
        try:
            await interaction.response.defer()
        except discord.NotFound:
            pass

        # Правка никнейма (убрать эмодзи) — косметика, в фоне.
        if cleaned_name != user_name:
            asyncio.create_task(
                _clean_member_nick(interaction.user, cleaned_name)
            )

        t_save = time.perf_counter()
        # Критичное сохранение выбора — надёжно, но вне event loop
        # (store.set делает синхронный файловый I/O).
        await _save_tournament(tournament)
        perf_metrics.record(
            "draft_pick_save_ms",
            (time.perf_counter() - t_save) * 1000,
        )

        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        t_ui = time.perf_counter()
        # Главное embed + ping следующего капитана + удаление
        # старого сообщения очереди — независимые Discord-вызовы,
        # выполняем параллельно.
        old_message_id = tournament.draft_message_id

        async def _delete_old() -> None:
            if old_message_id <= 0:
                return
            try:
                old_message = await interaction.channel.fetch_message(
                    old_message_id
                )
                await old_message.delete()
            except Exception:
                pass  # Message might not exist or already deleted

        async def _send_ping() -> discord.Message | None:
            if draft_complete:
                return None
            next_picker_pos = tournament.current_picker_position()
            if next_picker_pos is None:
                return None
            next_captain_name = tournament.captains[
                tournament.captain_order[next_picker_pos]
            ]
            next_captain_id = tournament.player_user_ids.get(
                next_captain_name, 0
            )
            if next_captain_id > 0:
                return await interaction.channel.send(
                    f" <@{next_captain_id}> - ваша очередь выбирать!"
                )
            return await interaction.channel.send(
                f" {next_captain_name} - ваша очередь выбирать!"
            )

        send_task = asyncio.ensure_future(_send_ping())
        results = await asyncio.gather(
            bot.update_tournament_message(interaction.guild, tournament),
            _delete_old(),
            send_task,
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning("draft pick ui step failed: %s", r)
        try:
            new_message = send_task.result()
        except Exception:
            new_message = None
        perf_metrics.record(
            "draft_pick_ui_ms",
            (time.perf_counter() - t_ui) * 1000,
        )
        perf_metrics.record(
            "draft_pick_total_ms",
            (time.perf_counter() - t_start) * 1000,
        )

        if draft_complete:
            try:
                await interaction.followup.send(
                    replace_emojis("✅ Драфт завершён!"),
                    ephemeral=True,
                )
            except discord.NotFound:
                pass
            return

        # Сохраняем id нового сообщения очереди — только если оно
        # реально отправлено и id изменился (второй store.set
        # не нужен при совпадении/ошибке отправки).
        if new_message is not None and new_message.id != old_message_id:
            tournament.draft_message_id = new_message.id
            await _save_tournament(tournament)


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
