"""Battle Royale tournament commands."""

import logging
from typing import TYPE_CHECKING

import discord
from utils.design import compose

from discord import app_commands
from discord.ext import commands

from config import BOT_OWNER_ID, BR_ROLE_ID, BR_ROOM_CHANNEL_ID
from models.br_tournament import (BRFormat, BRTournamentFormation,
                                  BRTournamentPhase, br_format_rules)
from storage.br_store import br_store
from utils.embeds import replace_emojis
from views.br_view import BRCardView, public_embed

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)

br_group = app_commands.Group(name="br", description="Battle Royale турниры")

_FORMAT_LABELS = {
    "solo": "Solo (1 игрок)",
    "duo": "Duo (2 игрока)",
    "squad": "Squad (4 игрока)",
}


class BRCog(commands.Cog):
    """Battle Royale tournament commands."""

    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        """Вложить /tournament br в живую группу tournament + биндинг."""
        from cogs.groups import bind_commands

        tourney = self.bot.tree.get_command("tournament")
        if isinstance(tourney, app_commands.Group) and br_group.parent is None:
            try:
                tourney.add_command(br_group)
            except Exception as e:
                logger.warning("Не удалось добавить br_group в tournament: %s", e)
        bind_commands(
            br_group,
            self,
            ["create", "delete", "test_fill", "test_teams", "test_stats"],
        )

    @br_group.command(name="create", description="Создать Battle Royale турнир")
    @app_commands.describe(
        format="Формат: solo/duo/squad",
        formation="Способ формирования: team (готовые составы) / random (жеребьёвка)",
        maps="Количество карт (1-10)",
    )
    @app_commands.choices(format=[
        app_commands.Choice(name="Solo — 1 игрок", value="solo"),
        app_commands.Choice(name="Duo — 2 игрока", value="duo"),
        app_commands.Choice(name="Squad — 4 игрока", value="squad"),
    ])
    @app_commands.choices(formation=[
        app_commands.Choice(name="team — готовые составы", value="team"),
        app_commands.Choice(name="random — жеребьёвка", value="random"),
    ])
    async def br_create(
        self,
        interaction: discord.Interaction,
        format: str | None = None,
        formation: str | None = None,
        maps: int = 3,
    ) -> None:
        """Создать BR турнир."""
        # Проверка на владельца бота
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "Эта команда доступна только владельцу бота.",
                ephemeral=True
            )
            return

        fmt_key = (format or "squad").lower()
        form_key = (formation or "").lower() or None
        if maps < 1 or maps > 10:
            await interaction.response.send_message(
                "Количество карт должно быть от 1 до 10.", ephemeral=True)
            return
        if fmt_key not in ("solo", "duo", "squad"):
            await interaction.response.send_message(
                "Формат должен быть solo, duo или squad.", ephemeral=True)
            return
        if form_key is not None and form_key not in ("team", "random"):
            await interaction.response.send_message(
                "Способ формирования: team или random.", ephemeral=True)
            return
        if fmt_key == "solo" and form_key == "team":
            await interaction.response.send_message(
                "Для Solo доступна только индивидуальная регистрация.", ephemeral=True)
            return
        # Для duo/squad без formation — жеребьёвка; legacy-вызовы с
        # formation=team/random и без format работают как Squad.
        form_key = form_key or "random"

        if BR_ROLE_ID == 0:
            await interaction.response.send_message(
                "BR_ROLE_ID не настроен в конфигурации.", ephemeral=True)
            return
        if BR_ROOM_CHANNEL_ID == 0:
            await interaction.response.send_message(
                "BR_ROOM_CHANNEL_ID не настроен в конфигурации.", ephemeral=True)
            return

        # Один активный BR на канал: повторное создание → ссылка на карточку
        existing = br_store.get(interaction.guild_id, interaction.channel_id)
        if existing:
            link = ""
            if existing.message_id:
                link = f"\nКарточка: https://discord.com/channels/{existing.guild_id}/{existing.channel_id}/{existing.message_id}"
            await interaction.response.send_message(
                f"В этом канале уже идёт BR турнир ({br_format_rules(existing.format)['label']}, "
                f"фаза: {existing.phase.value}).{link}", ephemeral=True)
            return

        from models.br_tournament import BRTournament
        tournament = BRTournament(
            id="",
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            formation=BRTournamentFormation(form_key),
            phase=BRTournamentPhase.REGISTRATION,
            format=BRFormat(fmt_key),
            role_id=BR_ROLE_ID,
            room_channel_id=BR_ROOM_CHANNEL_ID,
            organizer_id=interaction.user.id,
            map_count=maps,
        )
        br_store.set(tournament)

        view = BRCardView(tournament.guild_id, tournament.channel_id, tournament)
        embed = public_embed(tournament)

        await interaction.response.send_message(
            replace_emojis("BR турнир создан"), ephemeral=True)
        message = await interaction.channel.send(view=compose(embed, view))
        tournament.message_id = message.id
        br_store.set(tournament)

        logger.info("BR турнир создан в канале %s сервера %s (%s/%s)",
                    interaction.channel_id, interaction.guild_id, fmt_key, form_key)

    @br_group.command(name="delete", description="Отменить BR турнир (с причиной)")
    async def br_delete(self, interaction: discord.Interaction,
                        reason: str = "Отменено владельцем") -> None:
        """Отменить BR турнир: подтверждение + причина, турнир уходит в историю."""
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "Эта команда доступна только владельцу бота.", ephemeral=True)
            return

        tournament = br_store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "В этом канале нет активного BR турнира.", ephemeral=True)
            return

        from views.br_view import CancelConfirmView
        if tournament.is_active:
            # подтверждение через кнопку + причина уже получена аргументом
            view = CancelConfirmView(
                interaction.guild_id, interaction.channel_id, reason[:200])
            await interaction.response.send_message(
                f"Отменить BR турнир?\nПричина: {reason[:200]}",
                view=view, ephemeral=True)
            return
        # неактивный — просто архивирован
        await interaction.response.send_message("Турнир не активен.", ephemeral=True)

    @br_group.command(name="test_fill", description="Заполнить BR ботами (только тестовый BR)")
    async def br_test(self, interaction: discord.Interaction) -> None:
        """Заполнить BR турнир ботами для тестирования."""
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "Эта команда доступна только владельцу бота.", ephemeral=True)
            return

        tournament = br_store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "В этом канале нет BR турнира.", ephemeral=True)
            return
        if tournament.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message(
                "BR турнир не в фазе регистрации.", ephemeral=True)
            return
        if tournament.mode == "team":
            await interaction.response.send_message(
                "Для team-формата используйте /tournament br test_teams.", ephemeral=True)
            return

        base_user_id = -((interaction.guild_id % 10000) * 10000 + (interaction.channel_id % 100) * 10)
        tournament.clear_players()
        tournament.teams = []
        tournament.is_test = True

        need = tournament.max_players if not tournament.is_solo else tournament.max_slots
        for i in range(1, need + 1):
            tournament.register_player(f"Bot {i}", base_user_id - i)

        br_store.set(tournament)
        await interaction.response.send_message(
            replace_emojis(f"Добавлено {need} ботов!"), ephemeral=True)
        await _refresh_card(interaction, tournament)

    @br_group.command(name="test_teams", description="Заполнить BR ботами-составами (только тестовый BR)")
    async def br_test_team(self, interaction: discord.Interaction) -> None:
        """Заполнить BR турнир ботами-командами для тестирования."""
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "Эта команда доступна только владельцу бота.", ephemeral=True)
            return

        tournament = br_store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "В этом канале нет BR турнира.", ephemeral=True)
            return
        if tournament.phase != BRTournamentPhase.REGISTRATION:
            await interaction.response.send_message(
                "BR турнир не в фазе регистрации.", ephemeral=True)
            return
        if tournament.mode != "team":
            await interaction.response.send_message(
                "test_teams только для team-формата.", ephemeral=True)
            return

        base_user_id = -((interaction.guild_id % 10000) * 10000 + (interaction.channel_id % 100) * 10)
        tournament.is_test = True

        if tournament.format == BRFormat.SQUAD:
            for n in range(1, tournament.max_slots + 1):
                ids = [base_user_id - n * 100 - k for k in range(tournament.slot_size)]
                names = {uid: f"BotTeam{n}_{k}" for k, uid in enumerate(ids)}
                ok, err = tournament.register_team_slot(
                    f"test_team_{n}", f"Team {n}", ids[0], ids, names)
                if not ok:
                    logger.warning("test_teams squad slot %s failed: %s", n, err)
        elif tournament.format == BRFormat.DUO:
            for n in range(1, tournament.max_slots + 1):
                cap = base_user_id - n * 100
                mate = cap - 1
                tournament.create_duo_invite(cap, f"BotCap{n}", mate)
                # подтверждаем сразу
                idx = len(tournament.teams) - 1
                tournament.respond_duo_invite(idx, mate, f"BotMate{n}", True)
        else:
            await interaction.response.send_message(
                "test_teams не нужен для solo — используйте test_fill.", ephemeral=True)
            return

        br_store.set(tournament)
        await interaction.response.send_message(
            replace_emojis("Тестовые составы добавлены!"), ephemeral=True)
        await _refresh_card(interaction, tournament)

    @br_group.command(name="test_stats", description="Заполнить случайную статистику BR (тестовый)")
    async def br_stats_test(self, interaction: discord.Interaction) -> None:
        """Заполнить случайную статистику для всех слотов BR турнира."""
        if interaction.user.id != BOT_OWNER_ID:
            await interaction.response.send_message(
                "Эта команда доступна только владельцу бота.", ephemeral=True)
            return

        tournament = br_store.get(interaction.guild_id, interaction.channel_id)
        if not tournament:
            await interaction.response.send_message(
                "В этом канале нет BR турнира.", ephemeral=True)
            return
        if not tournament.is_test:
            await interaction.response.send_message(
                "Команда доступна только для тестового BR (test_fill/test_teams).",
                ephemeral=True)
            return
        if tournament.phase not in (BRTournamentPhase.COLLECT,):
            await interaction.response.send_message(
                "Сначала завершите все карты («Карта сыграна»).", ephemeral=True)
            return
        if not tournament.teams:
            await interaction.response.send_message("В турнире нет слотов.", ephemeral=True)
            return

        import random
        n_slots = len(tournament.teams)
        for m in range(tournament.map_count):
            places = random.sample(range(1, tournament.place_cap + 1), n_slots)
            for si, slot in enumerate(tournament.teams):
                tournament.set_result_entry(m, si, place=places[si],
                                            kills=random.randint(0, 50),
                                            by=interaction.user.id)
        br_store.set(tournament)
        await interaction.response.send_message(
            replace_emojis("Случайная статистика заполнена!"), ephemeral=True)


async def _refresh_card(interaction: discord.Interaction, tournament) -> None:
    """Обновить публичную карточку после тестовых команд."""
    try:
        if not tournament.message_id:
            return
        channel = interaction.guild.get_channel(tournament.channel_id)
        if not channel:
            return
        message = await channel.fetch_message(tournament.message_id)
        embed = public_embed(tournament)
        await message.edit(view=compose(embed, BRCardView(tournament.guild_id, tournament.channel_id, tournament)))
    except Exception as exc:
        logger.warning("BR refresh card failed: %s", exc)
