"""Ephemeral CV2-панель журнала аудита турнира.

Открывается через ``/tournament log`` и пункт «Журнал»
организаторского меню. Доступ проверяется при каждом
взаимодействии: ``can_manage_tournament`` для активного турнира;
для удалённого/архивного — админы/владелец/глобальные org.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from config import BOT_OWNER_ID, replace_emojis
from storage import tournament_audit_store as audit_store
from storage.json_store import store
from services import tournament_audit_service as audit
from utils.permissions import (
    can_manage_tournament,
    is_admin_check,
    is_org_check,
)

logger = logging.getLogger(__name__)

PAGE_SIZE = 8

# Фильтры Select → параметры запроса.
FILTERS: dict[str, tuple[str, str | None]] = {
    "all": ("Все события", None),
    "players": ("Участники", "tournament"),
    "matches": ("Матчи", "match"),
    "rooms": ("Комнаты", "room"),
    "organizers": ("Организаторы", "organizer"),
    "system": ("Системные", "system"),
}

# Для фильтра "players" сужаем до действий с участниками,
# для "matches" — до match_id/source=mm.
_PLAYER_ACTIONS = {
    audit.A.PLAYER_JOIN, audit.A.PLAYER_ADD, audit.A.PLAYER_REMOVE,
    audit.A.PLAYER_MOVE, audit.A.PLAYER_SWAP, audit.A.PLAYER_EXIT,
}
_MATCH_SOURCES = {"tournament", "mm"}


def _allowed_sources(filter_key: str) -> set[str] | None:
    """None — без фильтра по source; иначе множество source."""
    if filter_key == "all":
        return None
    if filter_key in ("rooms", "organizers", "system"):
        return {filter_key}
    return None  # players/matches фильтруются по action/match_id


def _event_matches(ev: dict[str, Any], filter_key: str) -> bool:
    if filter_key == "all":
        return True
    if filter_key == "players":
        return ev.get("action") in _PLAYER_ACTIONS or (
            ev.get("target_user_id") and ev.get("source") == "tournament"
            and "player" in str(ev.get("action")))
    if filter_key == "matches":
        return bool(ev.get("match_id")) or str(ev.get("action", "")).startswith(
            ("match.", "mm.", "final.", "earnings."))
    if filter_key == "rooms":
        return ev.get("source") == "room" or str(
            ev.get("action", "")).startswith("room.")
    if filter_key == "organizers":
        return ev.get("source") == "organizer" or str(
            ev.get("action", "")).startswith("organizer.")
    if filter_key == "system":
        return ev.get("source") == "system"
    return True


async def _can_view(
    interaction: discord.Interaction,
    tournament_id: str | None,
) -> bool:
    """Проверка доступа к журналу при каждом взаимодействии."""
    user, guild = interaction.user, interaction.guild
    if guild is None:
        return False
    tournament = store.get(interaction.guild_id, interaction.channel_id)
    if tournament is not None and (
        tournament_id is None or str(tournament.id) == str(tournament_id)
    ):
        try:
            if can_manage_tournament(user, guild, tournament):
                return True
        except Exception:
            pass
        return False
    # Турнир удалён/архивирован или это чужой турнир канала.
    # Доступ: админы, владелец, глобальные org — а также создатель
    # именно этого турнира по снапшоту архива (записи organizer_store
    # относятся к активному турниру и для архивного не действуют).
    try:
        if user.id == BOT_OWNER_ID:
            return True
        if is_admin_check(user, guild):
            return True
        if is_org_check(user, guild):
            return True
        if tournament_id:
            from storage.tournament_archive import tournament_archive
            entry = tournament_archive.get(str(tournament_id))
            if entry is not None:
                data = entry.get("data") or {}
                try:
                    if int(data.get("organizer_id") or 0) == user.id:
                        return True
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return False


def _format_event(ev: dict[str, Any]) -> str:
    """Одна строка журнала: время · инициатор · действие · матч/цель."""
    time_s = audit._fmt_dt(ev.get("created_at"))
    actor = (
        f"<@{ev['actor_user_id']}>" if ev.get("actor_user_id") else "система"
    )
    title = audit._ACTION_TITLES.get(ev.get("action"), ev.get("action", "?"))
    parts = [f"`{time_s}` **{title}** — {actor}"]
    if ev.get("match_id"):
        parts.append(f" матч `{ev['match_id']}`")
    tgt = ev.get("target_user_id")
    if tgt:
        parts.append(f" → <@{tgt}>")
    # Короткое изменение до/после, если есть
    before, after = ev.get("before_data"), ev.get("after_data")
    if isinstance(before, dict) and isinstance(after, dict):
        diffs = [
            f"{k}: `{before.get(k)}`→`{after[k]}`"
            for k in after
            if k != "…" and before.get(k) != after.get(k)
        ][:2]
        if diffs:
            parts.append(" (" + "; ".join(diffs) + ")")
    return "".join(parts)


def _build_text(
    events: list[dict[str, Any]],
    tournament_label: str,
    filter_key: str,
    page: int,
    total_pages: int,
) -> str:
    filt = FILTERS.get(filter_key, FILTERS["all"])[0]
    lines = [
        f"### ЖУРНАЛ ТУРНИРА\n{tournament_label}",
        f"-# Фильтр: {filt} · Страница {page + 1}/{max(total_pages, 1)}",
    ]
    if not events:
        lines.append("\nСобытий пока нет.")
    else:
        lines.append("\n" + "\n".join(_format_event(e) for e in events))
    return "\n".join(lines)


class TournamentAuditView(discord.ui.View):
    """Ephemeral-панель журнала: фильтр + пагинация.

    Обычный View (не LayoutView) — Discord не позволяет Select
    внутри LayoutView-контейнера в некоторых клиентах; текст
    карточки собирается через compose/TextDisplay-совместимый
    вид. Для простоты и совместимости используем View +
    ephemeral-сообщение с markdown-блоком.
    """

    def __init__(
        self,
        *,
        guild_id: int,
        channel_id: int,
        tournament_id: str | None,
        tournament_label: str,
        filter_key: str = "all",
        page: int = 0,
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tournament_id = tournament_id
        self.tournament_label = tournament_label
        self.filter_key = filter_key
        self.page = page
        self._events: list[dict[str, Any]] = []
        self._total = 0
        self._rendered = False

    # -- данные --------------------------------------------------------

    async def load(self) -> None:
        sources = _allowed_sources(self.filter_key)
        # Для players/matches фильтрация по action/match_id —
        # догружаем с запасом и фильтруем в памяти.
        memory_filter = self.filter_key in ("players", "matches")
        fetch_limit = PAGE_SIZE * 8 if memory_filter else PAGE_SIZE
        try:
            if memory_filter:
                events = await audit_store.list_events(
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    tournament_id=self.tournament_id,
                    source=None,
                    limit=fetch_limit,
                    offset=0,
                )
                events = [e for e in events if _event_matches(e, self.filter_key)]
                self._total = len(events)
                self._events = events[
                    self.page * PAGE_SIZE:(self.page + 1) * PAGE_SIZE
                ]
            else:
                source = next(iter(sources)) if sources else None
                self._total = await audit_store.count_events(
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    tournament_id=self.tournament_id,
                    source=source,
                )
                self._events = await audit_store.list_events(
                    guild_id=self.guild_id,
                    channel_id=self.channel_id,
                    tournament_id=self.tournament_id,
                    source=source,
                    limit=PAGE_SIZE,
                    offset=self.page * PAGE_SIZE,
                )
        except Exception as exc:
            logger.error("audit view: не удалось загрузить журнал: %s", exc)
            self._events = []
            self._total = 0

    # -- доступ ---------------------------------------------------------

    async def _check(self, interaction: discord.Interaction) -> bool:
        ok = await _can_view(interaction, self.tournament_id)
        if not ok:
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(
                        replace_emojis(
                            "У вас нет прав на просмотр журнала."),
                        ephemeral=True)
                else:
                    await interaction.response.send_message(
                        replace_emojis(
                            "У вас нет прав на просмотр журнала."),
                        ephemeral=True)
            except Exception:
                pass
        return ok

    # -- элементы -------------------------------------------------------

    def _rebuild(self) -> None:
        self.clear_items()
        total_pages = max(1, (self._total + PAGE_SIZE - 1) // PAGE_SIZE)

        # Фильтр
        filt = discord.ui.Select(
            placeholder="Фильтр журнала",
            options=[
                discord.SelectOption(
                    label=label, value=key,
                    default=(key == self.filter_key))
                for key, (label, _s) in FILTERS.items()
            ],
            custom_id=f"taudit:filter:{self.guild_id}:{self.channel_id}",
        )
        filt.callback = self._on_filter
        self.add_item(filt)

        # Пагинация
        prev_b = discord.ui.Button(
            label="Назад", style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
            custom_id=f"taudit:prev:{self.guild_id}:{self.channel_id}",
        )
        prev_b.callback = self._on_prev
        self.add_item(prev_b)

        next_b = discord.ui.Button(
            label="Далее", style=discord.ButtonStyle.secondary,
            disabled=self.page + 1 >= total_pages,
            custom_id=f"taudit:next:{self.guild_id}:{self.channel_id}",
        )
        next_b.callback = self._on_next
        self.add_item(next_b)

        # Обновить
        ref_b = discord.ui.Button(
            label="Обновить", style=discord.ButtonStyle.secondary,
            custom_id=f"taudit:refresh:{self.guild_id}:{self.channel_id}",
        )
        ref_b.callback = self._on_refresh
        self.add_item(ref_b)

    async def _respond(self, interaction: discord.Interaction) -> None:
        total_pages = max(1, (self._total + PAGE_SIZE - 1) // PAGE_SIZE)
        text = _build_text(
            self._events, self.tournament_label,
            self.filter_key, self.page, total_pages)
        embed = discord.Embed(
            description=text[:4000],
            color=discord.Color.dark_grey(),
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.edit_message(
                    message_id=interaction.message.id,
                    embed=embed, view=self)
            else:
                await interaction.response.edit_message(
                    embed=embed, view=self)
        except (discord.NotFound, discord.InteractionResponded):
            pass

    # -- callbacks ------------------------------------------------------

    async def _on_filter(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        try:
            self.filter_key = interaction.data["values"][0]
        except Exception:
            return
        self.page = 0
        await self.load()
        self._rebuild()
        await self._respond(interaction)

    async def _on_prev(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        self.page = max(0, self.page - 1)
        await self.load()
        self._rebuild()
        await self._respond(interaction)

    async def _on_next(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        self.page += 1
        await self.load()
        self._rebuild()
        await self._respond(interaction)

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        if not await self._check(interaction):
            return
        await self.load()
        self._rebuild()
        await self._respond(interaction)


async def open_audit_panel(
    interaction: discord.Interaction,
    *,
    tournament_id: str | None = None,
    tournament_label: str | None = None,
) -> None:
    """Открыть ephemeral-панель журнала (из команды или меню)."""
    gid = interaction.guild_id or 0
    cid = interaction.channel_id or 0

    if tournament_id is None:
        tournament = store.get(gid, cid)
        if tournament is not None:
            tournament_id = str(tournament.id)
        else:
            # Удалённый/архивный турнир — последний известный id.
            try:
                tournament_id = await audit_store.latest_tournament_id(
                    gid, cid)
            except Exception:
                tournament_id = None
    if tournament_label is None:
        tournament_label = (
            f"`{tournament_id}`" if tournament_id else "канал"
        )

    view = TournamentAuditView(
        guild_id=gid, channel_id=cid,
        tournament_id=tournament_id,
        tournament_label=tournament_label,
    )
    await view.load()
    view._rebuild()

    embed = discord.Embed(
        description=_build_text(
            view._events, tournament_label, "all", 0,
            max(1, (view._total + PAGE_SIZE - 1) // PAGE_SIZE))[:4000],
        color=discord.Color.dark_grey(),
    )
    if interaction.response.is_done():
        await interaction.followup.send(
            embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True)
