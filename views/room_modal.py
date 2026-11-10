from config import replace_emojis
"""Modal form for entering room ID and password."""

import logging
import time
import discord
from utils.design import compose, text_card
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from models.tournament import Tournament


# Cooldown для отправки комнаты (1 секунда)
room_submission_cooldowns = {}  # guild_id -> timestamp


class RoomModal(discord.ui.Modal, title="Комната игры"):
    """Modal for entering room ID and password."""

    room_id = discord.ui.TextInput(
        label="ID комнаты",
        placeholder="123456",
        required=True,
        max_length=20
    )

    room_password = discord.ui.TextInput(
        label="Пароль",
        placeholder="123",
        required=True,
        max_length=20
    )

    def __init__(self, match_type: str, match_index: int, team1_index: int, team2_index: int):
        super().__init__()
        self.match_type = match_type
        self.match_index = match_index
        self.team1_index = team1_index
        self.team2_index = team2_index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle room ID and password submission."""
        from storage.json_store import store

        # Defer immediately to avoid timeout
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            # Interaction expired or server error, continue anyway
            pass

        # Check cooldown (1 second)
        guild_id = interaction.guild_id
        current_time = time.time()
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 1:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {1 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        tournament = store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            try:
                await interaction.followup.send(replace_emojis("❌ Нет активного турнира."), ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Validate inputs
        if not self.room_id.value or not self.room_password.value:
            try:
                await interaction.followup.send(replace_emojis("❌ ID и пароль комнаты не могут быть пустыми."), ephemeral=True)
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Check if room data already exists for this match
        existing_room = None
        if self.match_type == "qualifier":
            existing_room = tournament.qualifier_rooms.get(self.match_index)
        elif self.match_type == "semifinal":
            existing_room = tournament.semifinal_rooms.get(self.match_index)
        elif self.match_type == "final":
            existing_room = tournament.final_room

        if existing_room:
            try:
                await interaction.followup.send(
                    replace_emojis("❌ Данные комнаты уже были отправлены другим пользователем."),
                    ephemeral=True
                )
            except (discord.NotFound, discord.DiscordServerError):
                pass
            return

        # Check cooldown (1 second)
        if guild_id in room_submission_cooldowns:
            elapsed = current_time - room_submission_cooldowns[guild_id]
            if elapsed < 1:
                try:
                    await interaction.followup.send(
                        f"❌ Подождите {1 - elapsed:.1f} секунд перед отправкой комнаты.",
                        ephemeral=True
                    )
                except (discord.NotFound, discord.DiscordServerError):
                    pass
                return

        # Set cooldown BEFORE sending (to prevent simultaneous submissions)
        room_submission_cooldowns[guild_id] = time.time()

        # Store room data based on match type
        room_data = {"id": self.room_id.value, "password": self.room_password.value}

        if self.match_type == "qualifier":
            tournament.qualifier_rooms[self.match_index] = room_data
        elif self.match_type == "semifinal":
            tournament.semifinal_rooms[self.match_index] = room_data
        elif self.match_type == "final":
            tournament.final_room = room_data

        store.set(tournament)

        # Update tournament message
        from bot import TournamentBot
        bot: TournamentBot = interaction.client  # type: ignore[assignment]
        await bot.update_tournament_message(interaction.guild, tournament)

        # Send DM notifications to team members
        await send_room_dm_notifications(
            bot, tournament, self.match_type, self.match_index,
            self.team1_index, self.team2_index,
            self.room_id.value, self.room_password.value,
        )

        # Confirm to user
        try:
            await interaction.followup.send(replace_emojis("✅ Комната создана и данные отправлены."), ephemeral=True)
        except (discord.NotFound, discord.DiscordServerError):
            pass


def _team_roster(tournament: Any, team_data: dict) -> list[str]:
    """Имена состава: players[] или circle1-4."""
    players = [p for p in team_data.get("players", []) if p]
    if players:
        return players
    return [
        team_data.get(f"circle{c}") for c in range(1, 5)
        if team_data.get(f"circle{c}")
    ]


def _team_line(tournament: Any, team_data: dict) -> str:
    """Строка вида '<@p1> <@p2> <@p3> <@p4>'."""
    parts = []
    for name in _team_roster(tournament, team_data):
        uid = tournament.player_user_ids.get(name)
        parts.append(f"<@{uid}>" if uid and uid > 0 else name)
    return " ".join(parts) if parts else "*"


def _room_mentions(tournament: Any, team_data: dict) -> str:
    """Упоминания состава команды (<@id> там, где известен user_id)."""
    return _team_line(tournament, team_data)


async def _resolve_mm_match_channel(
    bot: Any, tournament: Any, match_type: str, match_index: int
):
    """Канал матча (cs-4x4-match-N), где анонсирован данный матч."""
    for ch_id, mc in (getattr(tournament, "match_channels", None) or {}).items():
        bound = mc.get("matches") or []
        direct = (
            mc.get("match_type") == match_type
            and mc.get("match_index") == match_index
        )
        if [match_type, match_index] in bound or direct:
            ch = bot.get_channel(int(ch_id))
            if ch is None:
                try:
                    ch = await bot.fetch_channel(int(ch_id))
                except Exception:
                    ch = None
            if ch is not None:
                return ch
    preset = int(getattr(tournament, "mm_preset_channel_id", 0) or 0)
    if preset:
        ch = bot.get_channel(preset)
        if ch is None:
            try:
                ch = await bot.fetch_channel(preset)
            except Exception:
                ch = None
        if ch is not None:
            return ch
    return None


def _room_card_text(
    tournament: Any,
    team1_data: dict,
    team2_data: dict,
    name1: str,
    name2: str,
) -> str:
    """Текст карточки «КОМНАТА ОТКРЫТА» (V2 — упоминания пингуют)."""
    line1 = f"**{name1}**\n{_room_mentions(tournament, team1_data)}"
    line2 = f"**{name2}**\n{_room_mentions(tournament, team2_data)}"
    return (
        "**КОМНАТА ОТКРЫТА**\n\n"
        f"**Матч:** {name1} vs {name2}\n\n"
        f"{line1}\n\n{line2}\n\n"
        "**Данные:**"
    )


async def _send_room_data(
    channel: Any, room_id: str, room_password: str
) -> None:
    """ID и пароль — двумя отдельными обычными сообщениями."""
    await channel.send(room_id)
    await channel.send(room_password)


async def send_room_dm_notifications(
    bot: Any,
    tournament: Any,
    match_type: str,
    match_index: int,
    team1_index: int,
    team2_index: int,
    room_id: str,
    room_password: str,
) -> None:
    """Уведомление об открытой комнате.

    MM (/mm squad): 1 карточка + ID/пароль отдельными сообщениями —
    всё в канал матча, общий канал не трогаем.
    Обычный турнир (/tournament create): карточка с данными в канал
    результатов + анонс с пингом роли в канал уведомлений.
    """
    team1_data = tournament.teams[team1_index] if team1_index < len(tournament.teams) else {}
    team2_data = tournament.teams[team2_index] if team2_index < len(tournament.teams) else {}

    # Название команды = имя капитана
    name1 = team1_data.get("captain", f"Team {team1_index}")
    name2 = team2_data.get("captain", f"Team {team2_index}")

    line1 = f"**{name1}**\n{_room_mentions(tournament, team1_data)}"
    line2 = f"**{name2}**\n{_room_mentions(tournament, team2_data)}"

    if getattr(tournament, "mm_search_channel_id", 0):
        channel = await _resolve_mm_match_channel(
            bot, tournament, match_type, match_index
        )
        if channel is None:
            logger.warning(
                "MM room notify: match channel not found "
                "(%s/%s, tournament %s)",
                match_type, match_index, getattr(tournament, "id", "?"),
            )
            return
        await channel.send(
            view=text_card(_room_card_text(
                tournament, team1_data, team2_data, name1, name2
            )),
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
        await _send_room_data(channel, room_id, room_password)
        return

    # Обычный турнир: данные — в канал результатов, анонс — в канал
    # уведомлений с пингом роли (mention в content, не внутри embed)
    data_channel = bot.get_channel(1561683314480255057)
    if data_channel is None:
        try:
            data_channel = await bot.fetch_channel(1561683314480255057)
        except Exception:
            data_channel = None
    if data_channel is not None:
        await data_channel.send(
            view=text_card(_room_card_text(
                tournament, team1_data, team2_data, name1, name2
            )),
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
        await _send_room_data(data_channel, room_id, room_password)

    from config import NOTIFICATION_CHANNEL_ID, NOTIFICATION_ROLE_ID
    general_channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if general_channel is None:
        try:
            general_channel = await bot.fetch_channel(NOTIFICATION_CHANNEL_ID)
        except Exception:
            general_channel = None
    if general_channel is not None and data_channel is not None:
        guild_obj = getattr(general_channel, "guild", None)
        role = (
            guild_obj.get_role(NOTIFICATION_ROLE_ID)
            if guild_obj is not None else None
        )
        role_mention = role.mention if role else ""
        await general_channel.send(
            content=role_mention or None,
            view=text_card(
                f"**Комната открыта** — данные в <#{data_channel.id}>"
            ),
            allowed_mentions=discord.AllowedMentions(
                users=False, roles=True, everyone=False
            ),
        )