"""Публикация анонсов матчей текущей фазы турнира (r!z3 CLASSIC).

Обычные турниры: одно CV2-сообщение на матч в канале турнира
(``tournament.match_cards[match_key]`` хранит message_id). K/D и
подтверждение результата живут в основном сообщении турнира
(views/matches_view.py) — здесь только анонс и кнопка «Правила».

MM-матчи (/mm squad) анонсируются отдельно — views/mm_match_view.py
публикует карточку в выделенный канал пула; здесь они пропускаются.

Никаких PNG, OCR-карточек, голосований за формат и служебных
сообщений.
"""

from __future__ import annotations

import logging
import time

import discord

from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from storage.match_number_store import match_number_store
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_MATCH_LABELS = {
    "qualifier": "Отборочный матч",
    "semifinal": "Полуфинал",
    "final": "Финал",
}

# Правила матча — Kazakhstan, до 5 победных раундов
MATCH_RULES_TEXT = (
    "**Формат:** 4×4, до **5 победных раундов**.\n"
    "**Победа:** команда, первой набравшая 5 раундов "
    "(допустимые счета: 5:0, 5:1, 5:2, 5:3, 5:4).\n\n"
    "**Статистика:** капитаны команд заполняют K/D каждого своего "
    "игрока в формате `K/D` (например `7/4`) через кнопку "
    "«Заполнить статистику» в сообщении турнира.\n\n"
    "**Результат:** после заполнения статистики организатор "
    "выбирает победителя и счёт. Автоматического подтверждения нет."
)


class MatchRulesButton(discord.ui.Button):
    """Кнопка «📜 Правила» — ephemeral CV2-карточка с правилами."""

    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="📜 Правила",
            custom_id="match_rules",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = DesignView(title="📜 Правила матча", timeout=60)
        view.text(MATCH_RULES_TEXT)
        await interaction.response.send_message(
            view=view, ephemeral=True,
        )


class MatchRulesView(discord.ui.View):
    """Persistent view для кнопки «📜 Правила» в карточках матчей."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(MatchRulesButton())


def _phase_matches(tournament: Tournament) -> list[tuple[str, int, int, int]]:
    """Матчи текущей фазы: (match_type, match_index, team_a, team_b)."""
    phase = tournament.phase
    out: list[tuple[str, int, int, int]] = []
    if phase == TournamentPhase.QUALIFIERS:
        for i, (a, b) in enumerate(
            getattr(tournament, "qualifier_matches", None) or []
        ):
            out.append(("qualifier", i, a, b))
    elif phase == TournamentPhase.SEMIFINALS:
        for i, (a, b) in enumerate(
            getattr(tournament, "semifinal_matches", None) or []
        ):
            out.append(("semifinal", i, a, b))
    elif phase == TournamentPhase.FINAL:
        ft = getattr(tournament, "final_teams", None)
        if ft and len(ft) == 2:
            out.append(("final", 0, ft[0], ft[1]))
    return out


def _match_key(tournament: Tournament, match_type: str, match_index: int) -> str:
    """Ключ матча — тот же формат, что match_id в карточке результата."""
    return f"{tournament.id}_{match_type}_{match_index}"


def _match_label(match_type: str, match_index: int) -> str:
    return _MATCH_LABELS.get(match_type, match_type)


def _team_display(tournament: Tournament, team_index: int) -> str:
    """Название команды для анонса (без пингов)."""
    name = tournament.team_names.get(team_index)
    if name:
        return name
    if team_index < len(tournament.teams):
        data = tournament.teams[team_index] or {}
        return data.get("captain") or f"Команда {team_index + 1}"
    return f"Команда {team_index + 1}"


def _roster_names(tournament: Tournament, team_index: int) -> str:
    """Состав команды обычными никами — без <@id>-упоминаний."""
    if team_index >= len(tournament.teams):
        return "—"
    data = tournament.teams[team_index] or {}
    players = data.get("players") or []
    if not players:
        players = [
            data.get(f"circle{c}") for c in range(1, 5)
            if data.get(f"circle{c}")
        ]
    return "\n".join(f"• {p}" for p in players) or "—"


def _match_card_view(
    tournament: Tournament,
    number: int,
    label: str,
    name_a: str,
    name_b: str,
    t1: int,
    t2: int,
) -> DesignView:
    view = DesignView(
        title=f"⚔️ МАТЧ #{number} — {label}",
        timeout=None,
    )
    view.text(f"**{name_a}** vs **{name_b}**")
    view.text(f"**{name_a}**\n{_roster_names(tournament, t1)}")
    view.text(f"**{name_b}**\n{_roster_names(tournament, t2)}")
    return view


async def refresh_announced_cards(bot: "discord.Client") -> None:
    """Обновить уже опубликованные карточки анонсов матчей.

    После смены layout (убраны блок «ИНФОРМАЦИЯ» и кнопка
    «📜 Правила») один раз при старте редактирует сообщения
    активных матчей текущей фазы. Нумерация матчей сохраняется
    (get_or_assign идемпотентен), результаты не затрагиваются —
    карточка содержит только анонс и составы.
    """
    for tournament in store.all():
        if tournament.phase == TournamentPhase.COMPLETE:
            continue
        mcards = tournament.match_cards or {}
        if not mcards:
            continue
        guild = bot.get_guild(tournament.guild_id)
        if guild is None:
            continue
        channel = guild.get_channel(tournament.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(tournament.channel_id)
            except Exception:
                channel = None
        if not isinstance(channel, discord.TextChannel):
            continue

        phase_map = {
            (mt, mi): (mt, mi, t1, t2)
            for mt, mi, t1, t2 in _phase_matches(tournament)
        }
        dirty = False
        for key, entry in mcards.items():
            mid = entry.get("message_id")
            if not mid or entry.get("layout_v2"):
                continue
            match = phase_map.get((entry.get("match_type"), entry.get("match_index")))
            if match is None:
                # Матч не из текущей фазы (завершён/устарел) — не трогаем.
                entry["layout_v2"] = True
                dirty = True
                continue
            try:
                number = await match_number_store.get_or_assign(key)
                mt, mi, t1, t2 = match
                msg = await channel.fetch_message(int(mid))
                await msg.edit(
                    view=_match_card_view(
                        tournament, number, _match_label(mt, mi),
                        _team_display(tournament, t1),
                        _team_display(tournament, t2), t1, t2,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                entry["layout_v2"] = True
                dirty = True
            except Exception as exc:
                logger.warning(
                    "Match card refresh failed (%s): %s", key, exc)
        if dirty:
            store.set(tournament)


async def announce_phase_matches(
    tournament: Tournament,
    guild: discord.Guild,
    bot: "discord.Client | None" = None,
) -> None:
    """Анонсировать все ещё не анонсированные матчи текущей фазы.

    Идемпотентно: факт анонса — match_number_store.is_announced(key).
    """
    matches = _phase_matches(tournament)
    if not matches:
        return
    # MM-матчи публикуют собственную карточку в канале пула —
    # здесь их не дублируем.
    if getattr(tournament, "mm_search_channel_id", 0):
        return

    channel = guild.get_channel(tournament.channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(tournament.channel_id)
        except Exception:
            channel = None
    if not isinstance(channel, discord.TextChannel):
        return

    mcards = tournament.match_cards
    if mcards is None:
        mcards = tournament.match_cards = {}

    for match_type, match_index, t1, t2 in matches:
        key = _match_key(tournament, match_type, match_index)
        entry = mcards.get(key) or {}
        if entry.get("announced") or await match_number_store.is_announced(key):
            continue

        try:
            number = await match_number_store.get_or_assign(key)
            name_a = _team_display(tournament, t1)
            name_b = _team_display(tournament, t2)
            label = _match_label(match_type, match_index)

            msg = await channel.send(
                view=_match_card_view(
                    tournament, number, label, name_a, name_b, t1, t2,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            mcards[key] = {
                "match_type": match_type,
                "match_index": match_index,
                "message_id": msg.id,
                "announced": True,
                "announced_at": time.time(),
            }
            store.set(tournament)
            await match_number_store.mark_announced(key)
            logger.info(
                "Match #%d announced: %s %s vs %s (tournament %s)",
                number, label, name_a, name_b, tournament.id,
            )
        except Exception as exc:
            logger.error("Match announce failed for %s: %s", key, exc)
