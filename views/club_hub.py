"""Persistent public navigation with private, owner-bound pages."""
from __future__ import annotations

import logging

import discord

from config import guild_allowed
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)
PAGES = [('play', 'Играть'), ('profile', 'Мой профиль'), ('economy', 'Экономика'),
         ('ratings', 'Рейтинги'), ('help', 'Помощь')]


class HubButton(discord.ui.Button):
    def __init__(self, key, label, *, public=False, owner_id=0):
        super().__init__(label=label, custom_id=f'club:hub:{key}', style=discord.ButtonStyle.secondary)
        self.key, self.public, self.owner_id = key, public, owner_id

    async def callback(self, interaction):
        if (interaction.guild is None or not guild_allowed(interaction.guild.id)
                or (self.owner_id and self.owner_id != interaction.user.id)):
            await interaction.response.send_message('Этот раздел недоступен.', ephemeral=True)
            return
        if self.key in ('shop', 'inventory'):
            command = interaction.client.tree.get_command(self.key)
            if command is None or not await command._check_can_run(interaction):
                await interaction.response.send_message('Действие недоступно.', ephemeral=True)
                return
            await command.callback(command.binding, interaction)
            return
        await interaction.response.defer(ephemeral=True, thinking=self.public)
        try:
            page = await build_page(interaction, self.key)
            # Public Hub never becomes somebody's personal profile.
            await interaction.edit_original_response(view=page, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            logger.exception('Hub page %s failed', self.key)
            await interaction.edit_original_response(view=DesignView(title='r!z3').text(
                'Не удалось загрузить раздел. Попробуйте ещё раз через /hub.'))


def navigation(view, owner_id):
    buttons = [HubButton(key, label, owner_id=owner_id) for key, label in PAGES]
    if isinstance(view, DesignView):
        view.row(*buttons)
    else:
        view.add_item(discord.ui.ActionRow(*buttons))
    return view


class HubView(DesignView):
    def __init__(self):
        super().__init__(timeout=None, title='r!z3 · ИГРОВОЙ КЛУБ')
        self.text('CS 4×4 · Battle Royale · Ultimatum\nТурниры организаторов, достижения и сообщество.')
        self.row(*(HubButton(key, label, public=True) for key, label in PAGES))


async def build_page(interaction, key):
    uid, gid = interaction.user.id, interaction.guild.id
    view = DesignView(title=dict(PAGES).get(key, 'r!z3'))
    if key == 'play':
        from services.club_hub import active_events
        events, unavailable = await active_events(interaction.guild, interaction.user)
        if not events:
            view.text('Сейчас нет доступных активных турниров. Следите за объявлениями организаторов.')
        for event in events[:15]:
            url = f'https://discord.com/channels/{gid}/{event.channel_id}'
            if event.message_id:
                url += f'/{event.message_id}'
            detail = f'**{event.format}** · {event.state}\n[Открыть турнир]({url})'
            if event.organizer_id:
                detail += f' · Организатор: <@{event.organizer_id}>'
            if event.players is not None:
                detail += f' · Участников: {event.players}'
            view.text(detail)
        if len(events) > 15:
            view.text('Остальные турниры доступны в турнирных каналах.')
        if unavailable:
            view.text('Список Ultimatum временно недоступен. Проверьте канал турнира.')
        view.text('Регистрация и расписание — в карточке выбранного турнира.')
    elif key == 'profile':
        from views.profile_view import build_profile_layout
        view = await build_profile_layout(interaction.guild, interaction.user)
        view.row(HubButton('history', 'История матчей', owner_id=uid))
    elif key == 'history':
        from storage.match_history_store import match_history_store
        from utils.design import compose
        from views.history_view import create_history_embed
        matches = (await match_history_store.get(gid, uid))[:10]
        stats = await match_history_store.get_recent_stats(gid, uid, limit=10)
        # Existing renderer; keeps the same history and rating interpretation.
        view = compose(create_history_embed(gid, uid, matches, stats, page=0,
                       user_name=interaction.user.display_name, limit=10))
    elif key == 'economy':
        from storage.user_balance_store import user_balance_store
        balance = await user_balance_store.get_balance(gid, uid)
        view.text(f'Монеты: **{balance:,}**\n/daily — ежедневная награда')
        view.row(HubButton('shop', 'Магазин', owner_id=uid), HubButton('inventory', 'Инвентарь', owner_id=uid))
    elif key == 'ratings':
        from utils.design import compose
        from utils.embeds import build_leaderboard_embed
        from views.leaderboard_view import UniversalLeaderboardView
        leaderboard = UniversalLeaderboardView(gid, uid)
        await leaderboard.initialize()
        leaderboard.add_item(HubButton("play", "Турнирный центр", owner_id=uid))
        embed = await build_leaderboard_embed(gid, user_id=uid)
        # The established leaderboard owns its pagination and member checks.
        return compose(embed, leaderboard)
    else:
        view.text('**Как играть**\nОткройте «Играть», выберите турнир и зарегистрируйтесь в его карточке.')
        view.text('**Данные комнаты**\nКнопка в карточке комнаты доступна участникам матча и ответственным организаторам.')
        view.text('**Поддержка**\nПо вопросам матча обращайтесь к его организатору. По нарушениям — к модераторам сервера.')
        view.text('/help — полная справка\n/booyah — зал славы')
    return navigation(view, uid)
