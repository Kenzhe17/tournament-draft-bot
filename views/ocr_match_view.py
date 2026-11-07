"""OCR-флоу результатов матчей: скриншот → ручное сопоставление игроков → ELO.

- MatchScreenshotButton — кнопка «📸 Скриншот» на каждый матч (вместо
  ручного ввода K/D капитанами).
- Капитан отправляет скриншот в канал результатов OCR_STATS_CHANNEL_ID
  (cogs/ocr_upload.py ловит аттачмент), бот публикует там сообщение
  с embed'ом (скриншот внутри), пингом обоих капитанов и кнопками.
- Каждый капитан сопоставляет ТОЛЬКО свою команду со своей панелью OCR
  своей кнопкой: выбирает панель (левая/правая), затем для каждого
  игрового ника — Discord-игрока из своего состава. Ник и статистику
  можно править.
- После сопоставления обеих команд результат подтверждается
  АВТОМАТИЧЕСКИ (победитель — по OCR-подсветке; если не определён,
  капитанам/организатору предлагается выбрать вручную).
- «Вызвать организатора» — пинг организатора, если статистика неверна.
- «Неправильный скриншот» — повторная загрузка: embed обновляется.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import discord
from utils.design import compose, smart_view, text_card
from discord.ui import Button, Modal, Select, TextInput, View

try:
    # FileUpload в модалках — discord.py >= 2.7 (новый UI Kit)
    from discord.ui import FileUpload
except ImportError:  # pragma: no cover
    FileUpload = None

from config import replace_emojis
from models.tournament import Tournament
from storage.json_store import store
from utils.cosmetics import clean_nickname
from utils.permissions import is_org_check

logger = logging.getLogger(__name__)

PENDING_UPLOAD_TTL = 300  # секунд на отправку скриншота после нажатия кнопки
NONE_VALUE = "__none__"

# Канал, куда приходят скриншоты и публикуются результаты OCR
OCR_STATS_CHANNEL_ID = 1568608461250367569

# Доп. статистика детального экрана отряда ( squad-режим )
EXTRA_STAT_KEYS = (
    "fact_dmg", "knockdowns", "healing", "revives", "revive_x",
    "headshots",
)

# Ожидающие загрузки скриншотов: (guild_id, channel_id, user_id) -> meta
PENDING_UPLOADS: dict[tuple[int, int, int], dict[str, Any]] = {}


def register_pending_upload(
    guild_id: int,
    channel_id: int,
    user_id: int,
    match_type: str,
    match_index: int,
    tchannel_id: int = 0,
    reupload: bool = False,
    squad_team: int | None = None,
    squad_uid: str | None = None,
) -> None:
    PENDING_UPLOADS[(guild_id, channel_id, user_id)] = {
        "match_type": match_type,
        "match_index": match_index,
        "tchannel": tchannel_id or channel_id,
        "reupload": reupload,
        "squad_team": squad_team,
        "squad_uid": squad_uid,
        "expires": time.time() + PENDING_UPLOAD_TTL,
    }


def pop_pending_upload(
    guild_id: int, channel_id: int, user_id: int
) -> dict[str, Any] | None:
    """Точное совпадение (канал + пользователь)."""
    key = (guild_id, channel_id, user_id)
    meta = PENDING_UPLOADS.pop(key, None)
    if meta and meta.get("expires", 0) < time.time():
        return None
    return meta


def pop_pending_upload_any(
    guild_id: int, user_id: int
) -> dict[str, Any] | None:
    """Найти pending-загрузку пользователя в любом канале сервера.

    Нужно, чтобы скриншот можно было прислать и в канал турнира,
    и в канал результатов — важен сам пользователь, а не канал.
    """
    now = time.time()
    for key in list(PENDING_UPLOADS.keys()):
        g, _c, u = key
        if g != guild_id or u != user_id:
            continue
        meta = PENDING_UPLOADS.pop(key)
        if meta.get("expires", 0) < now:
            continue
        return meta
    return None


def _session_key(match_type: str, match_index: int) -> str:
    return f"{match_type}_{match_index}"


def _match_tuple(tournament: Tournament, match_type: str, match_index: int) -> tuple[int, int]:
    if match_type == "qualifier":
        return tournament.qualifier_matches[match_index]
    if match_type == "semifinal":
        return tournament.semifinal_matches[match_index]
    return (tournament.final_teams[0], tournament.final_teams[1])


def team_roster(tournament: Tournament, team_index: int) -> list[str]:
    """Состав команды: players[] (random-auto/draft) или circle1-4."""
    team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    players = [p for p in team.get("players", []) if p]
    if players:
        return players
    return [
        team.get(f"circle{c}") for c in range(1, 5) if team.get(f"circle{c}")
    ]


def _team_display_name(tournament: Tournament, team_index: int) -> str:
    team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    captain = team.get("captain", f"Team {team_index}")
    return tournament.team_names.get(team_index, captain)


def _is_match_captain(user: discord.Member, tournament: Tournament, team_index: int) -> bool:
    team = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    return team.get("captain") == clean_nickname(user.display_name)


def _can_use_ocr(user: discord.Member, tournament: Tournament, team_indexes: list[int]) -> bool:
    """Организатор или капитан любой из команд матча."""
    if is_org_check(user, user.guild):
        return True
    return any(_is_match_captain(user, tournament, i) for i in team_indexes)


def _match_label(match_type: str, match_index: int) -> str:
    if match_type == "final":
        return "Финал"
    if match_type == "semifinal":
        return f"Полуфинал #{match_index + 1}"
    return f"Отборочный #{match_index + 1}"


def _captain_mentions(tournament: Tournament, team_indexes: list[int]) -> str:
    """Пинг капитанов команд матча (или имена, если user_id неизвестен)."""
    parts = []
    for idx in team_indexes:
        team = tournament.teams[idx] if idx < len(tournament.teams) else {}
        captain = team.get("captain", "")
        uid = tournament.player_user_ids.get(captain)
        name = _team_display_name(tournament, idx)
        parts.append(f"<@{uid}> (**{name}**)" if uid else f"**{name}**")
    return "  ".join(parts)


def _ocr_winning_team_index(session: dict[str, Any]) -> int | None:
    """Команда-победитель: по подсветке панели (2-панельный экран)
    или по баннеру отряда (детальный экран одного отряда)."""
    if session.get("mode") == "squad":
        winners = [
            int(t)
            for t, s in session.get("squads", {}).items()
            if s.get("banner") == "win"
        ]
        return winners[0] if len(winners) == 1 else None
    side = session.get("winner_side")
    if side not in ("left", "right"):
        return None
    for key, mapping in session.get("maps", {}).items():
        if mapping.get("panel") == side:
            try:
                return int(key)
            except (TypeError, ValueError):
                return None
    return None


def _stats_complete(tournament: Tournament, match_type: str, match_index: int) -> bool:
    """Все игроки обеих команд уже имеют статистику в temp_match_stats."""
    match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"
    stats = tournament.temp_match_stats.get(match_id, {})
    if not stats:
        return False
    try:
        t1, t2 = _match_tuple(tournament, match_type, match_index)
    except Exception:
        return False
    for idx in (t1, t2):
        if any(name not in stats for name in team_roster(tournament, idx)):
            return False
    return True


def match_is_finished(tournament: Tournament, match_type: str, match_index: int) -> bool:
    """Матч полностью обработан (ELO начислено)."""
    match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"
    return match_id in tournament.processed_elo_matches


# ---------------------------------------------------------------------------
# Кнопка «Отправить скриншот» на матч
# ---------------------------------------------------------------------------

class MatchScreenshotButton(Button):
    """Кнопка загрузки скриншота результата матча (капитан/организатор).

    Если задан team_index — это кнопка конкретного капитана: скриншот
    детального экрана отряда автоматически назначается этой команде.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        label: str,
        team_index: int | None = None,
    ):
        suffix = f":{team_index}" if team_index is not None else ""
        super().__init__(
            label=label,
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"ocr_shot:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}{suffix}"
            ),
        )
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

    async def callback(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return

        # Матч должен быть в активной фазе
        phase = tournament.phase.value
        active = (
            (self.match_type == "qualifier" and phase == "qualifiers")
            or (self.match_type == "semifinal" and phase == "semifinals")
            or (self.match_type == "final" and phase == "final")
        )
        if not active:
            await interaction.response.send_message(
                replace_emojis("❌ Матч ещё не активен."), ephemeral=True
            )
            return

        if match_is_finished(tournament, self.match_type, self.match_index):
            await interaction.response.send_message(
                replace_emojis("❌ Результат этого матча уже зафиксирован."),
                ephemeral=True,
            )
            return

        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        if self.team_index is not None:
            # Кнопка конкретного капитана — только он или организатор
            allowed = is_org_check(interaction.user, interaction.guild) or (
                self.team_index in (t1, t2)
                and _is_match_captain(
                    interaction.user, tournament, self.team_index
                )
            )
        else:
            allowed = _can_use_ocr(interaction.user, tournament, [t1, t2])
        if not allowed:
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Скриншот может отправить капитан команды "
                    "или организатор."
                ),
                ephemeral=True,
            )
            return

        # Уже есть активная OCR-сессия?
        # В squad-режиме (скриншот одного отряда) второй капитан тоже
        # должен иметь возможность загрузить свой отряд — пропускаем.
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        squad_active = bool(
            session
            and session.get("mode") == "squad"
            and session.get("status") not in ("completed", "cancelled")
        )
        if (
            session
            and session.get("status") not in ("completed", "cancelled")
            and not squad_active
        ):
            await interaction.response.send_message(
                replace_emojis(
                    "⚠️ Для этого матча уже загружен скриншот — "
                    "завершите сопоставление или попросите организатора отменить его."
                ),
                ephemeral=True,
            )
            return

        # Канал для скриншотов — канал результатов; если его нет,
        # просим прислать сюда
        stats_channel = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
        if stats_channel is None:
            try:
                stats_channel = await interaction.guild.fetch_channel(
                    OCR_STATS_CHANNEL_ID
                )
            except Exception:
                stats_channel = None
        upload_channel_id = (
            stats_channel.id if stats_channel else self.channel_id
        )

        if FileUpload is not None:
            # Основной путь: модалка с загрузкой файла
            modal = ScreenshotUploadModal(
                self.guild_id, self.channel_id,
                self.match_type, self.match_index,
                squad_team=self.team_index,
            )
            await interaction.response.send_modal(modal)
            return

        register_pending_upload(
            self.guild_id, upload_channel_id, interaction.user.id,
            self.match_type, self.match_index,
            tchannel_id=self.channel_id,
            squad_team=self.team_index,
        )
        where = (
            f"<#{upload_channel_id}>" if stats_channel else "этот канал"
        )
        hint = (
            "Скриншот экрана статистики вашего отряда "
            "(K/D/A, урон, нокдауны, поднятия)"
            if self.team_index is not None
            else "скриншот экрана результатов"
        )
        await interaction.response.send_message(
            replace_emojis(
                f"📸 **{_match_label(self.match_type, self.match_index)}**\n"
                f"Отправьте **{hint}** в {where} "
                f"(в течение {PENDING_UPLOAD_TTL // 60} мин).\n"
                "Там появятся результаты — капитаны сопоставят игроков кнопками."
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Публичное сообщение с результатами OCR + кнопки сопоставления
# ---------------------------------------------------------------------------

async def _send_org_ping(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Пинг организатора (или ролей org) в канал результатов."""
    mentions: list[str] = []
    if tournament.organizer_id:
        mentions.append(f"<@{tournament.organizer_id}>")
    else:
        from config import ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID
        for rid in (ORG_1_ROLE_ID, ORG_2_ROLE_ID, ORG_3_ROLE_ID):
            if rid:
                mentions.append(f"<@&{rid}>")
    ping = " ".join(mentions) if mentions else "организатор"

    label = _match_label(match_type, match_index)
    content = (
        f"{ping}\n"
        f"📢 {interaction.user.mention} вызывает организатора по "
        f"матчу **{label}** — возможно, статистика распознана неверно."
    )
    channel = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(
                OCR_STATS_CHANNEL_ID
            )
        except Exception:
            channel = interaction.channel
    try:
        await channel.send( view=text_card(content))
    except Exception as e:
        logger.warning("OCR call-org ping failed: %s", e)


async def _register_reupload(
    interaction: discord.Interaction,
    match_type: str,
    match_index: int,
    channel_id: int,
    squad_team: int | None = None,
    squad_uid: str | None = None,
) -> str:
    """Зарегистрировать повторную загрузку скриншота.

    Возвращает строку «куда слать скриншот» для ответа пользователю.
    """
    stats_channel = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
    if stats_channel is None:
        try:
            stats_channel = await interaction.guild.fetch_channel(
                OCR_STATS_CHANNEL_ID
            )
        except Exception:
            stats_channel = None
    if stats_channel is not None:
        register_pending_upload(
            interaction.guild.id, stats_channel.id, interaction.user.id,
            match_type, match_index,
            tchannel_id=channel_id, reupload=True,
            squad_team=squad_team, squad_uid=squad_uid,
        )
        return f"<#{stats_channel.id}>"
    register_pending_upload(
        interaction.guild.id, channel_id, interaction.user.id,
        match_type, match_index,
        tchannel_id=channel_id, reupload=True,
        squad_team=squad_team, squad_uid=squad_uid,
    )
    return "этот канал"


# ---------------------------------------------------------------------------
# Модалка загрузки скриншота (FileUpload)
# ---------------------------------------------------------------------------

class ScreenshotUploadModal(Modal):
    """Окно загрузки скриншота результата матча.

    После выбора файла pending-загрузка регистрируется и файл
    прогоняется через тот же on_message-пайплайн cogs/ocr_upload.py
    (шим Message), поэтому squad- и классический режимы работают
    без дублирования логики.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
        reupload: bool = False,
        squad_team: int | None = None,
        squad_uid: str | None = None,
    ):
        title = (
            "Неправильный скриншот" if reupload else "Скриншот результатов"
        )
        super().__init__(title=title[:45])
        self.guild_id = guild_id
        self.channel_id = channel_id  # канал турнира (не канал результатов)
        self.match_type = match_type
        self.match_index = match_index
        self.reupload = reupload
        self.squad_team = squad_team
        self.squad_uid = squad_uid

        # FileUpload — компонент нового UI Kit (discord.py >= 2.7),
        # label задаётся через ui.Label-обёртку
        self.file_label = discord.ui.Label(
            text="Скриншот экрана результатов (PNG/JPG)",
            component=FileUpload(required=True, max_values=1),
        )
        self.add_item(self.file_label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        values = getattr(self.file_label.component, "values", None) or []
        attachment = values[0] if values else None
        if attachment is None:
            await interaction.followup.send(
                replace_emojis("❌ Прикрепите скриншот."), ephemeral=True
            )
            return

        channel = interaction.channel
        if channel is None:
            await interaction.followup.send(
                replace_emojis("❌ Канал недоступен."), ephemeral=True
            )
            return
        register_pending_upload(
            interaction.guild.id, channel.id, interaction.user.id,
            self.match_type, self.match_index,
            tchannel_id=self.channel_id,
            reupload=self.reupload,
            squad_team=self.squad_team,
            squad_uid=self.squad_uid,
        )

        async def reply(text: str) -> None:
            await interaction.followup.send(text, ephemeral=True)

        try:
            from cogs.ocr_upload import ModalUploadShim

            cog = interaction.client.get_cog("OCRUploadCog")
            if cog is None:
                await interaction.followup.send(
                    replace_emojis("❌ OCR-модуль недоступен."),
                    ephemeral=True,
                )
                return
            shim = ModalUploadShim(
                interaction.guild, channel, interaction.user,
                attachment, reply,
            )
            await cog.on_message(shim)
        except Exception:
            logger.exception("OCR modal upload failed")
            await interaction.followup.send(
                replace_emojis("❌ Ошибка при обработке скриншота."),
                ephemeral=True,
            )


class OCRPublicView(View):
    """Кнопки на публичном сообщении с распознанными результатами."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        maps = session.get("maps", {})
        t1, t2 = _match_tuple(tournament, match_type, match_index)
        active = session.get("status") not in ("completed", "cancelled")

        for team_index in (t1, t2):
            name = _team_display_name(tournament, team_index)
            mapped = str(team_index) in maps
            btn = Button(
                label=f"🧩 {name}"[:80],
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"ocr_map:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}"
                ),
                disabled=not active or mapped,
            )
            btn.callback = self._map_callback(team_index)
            self.add_item(btn)

        if active:
            wrong = Button(
                label="❌ Неправильный скриншот",
                style=discord.ButtonStyle.danger,
                custom_id=(
                    f"ocr_wrong:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}"
                ),
            )
            wrong.callback = self._wrong_shot_callback()
            self.add_item(wrong)

            call_org = Button(
                label="📢 Вызвать организатора",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"ocr_callorg:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}"
                ),
            )
            call_org.callback = self._call_org_callback()
            self.add_item(call_org)

    def _map_callback(self, team_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            # Кнопка команды — только её капитан (или организатор)
            if not (
                is_org_check(interaction.user, interaction.guild)
                or _is_match_captain(interaction.user, tournament, team_index)
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Сопоставить свою команду может только её капитан "
                        "или организатор."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            if str(team_index) in session.get("maps", {}):
                await interaction.response.send_message(
                    replace_emojis("✅ Эта команда уже сопоставлена."),
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title="🧩 Сопоставление команды",
                description=(
                    f"Команда **{_team_display_name(tournament, team_index)}**\n"
                    "Выберите, какая панель результатов принадлежит вашей команде:"
                ),
                color=discord.Color.blurple(),
            )
            view = PanelPickView(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index, team_index,
            )
            await interaction.response.send_message( view=compose(embed, view), ephemeral=True
            )
        return callback

    def _wrong_shot_callback(self):
        """Кнопка «Неправильный скриншот» — запросить повторную загрузку."""
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно капитанам команд и организатору."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return

            if FileUpload is not None:
                modal = ScreenshotUploadModal(
                    self.guild_id, self.channel_id,
                    self.match_type, self.match_index,
                    reupload=True,
                )
                await interaction.response.send_modal(modal)
                return

            where = await _register_reupload(
                interaction, self.match_type, self.match_index,
                self.channel_id,
            )
            await interaction.response.send_message(
                replace_emojis(
                    f"📸 Пришлите **правильный скриншот** в {where} "
                    f"(в течение {PENDING_UPLOAD_TTL // 60} мин) — "
                    "это сообщение с результатами обновится."
                ),
                ephemeral=True,
            )
        return callback

    def _call_org_callback(self):
        """Кнопка «Вызвать организатора» — пинг организатора в канал результатов."""
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно капитанам команд и организатору."
                    ),
                    ephemeral=True,
                )
                return

            await _send_org_ping(
                interaction, tournament, self.match_type, self.match_index
            )
            await interaction.response.send_message(
                replace_emojis("📢 Организатор вызван."), ephemeral=True
            )
        return callback


# ---------------------------------------------------------------------------
# Squad-режим: скриншот детального экрана ОДНОГО отряда
# (K/D/A, DMG, фактический урон, нокдауны, исцеление, поднятия, хэдшоты)
# ---------------------------------------------------------------------------


def _squad_panel_key(team_index: int) -> str:
    return f"squad_{team_index}"


class SquadClaimView(View):
    """Кнопки на сообщении с OCR одного отряда до назначения команде.

    Две кнопки — по одной на команду матча: капитан соответствующей
    команды (или организатор) подтверждает, что этот отряд — его.
    """

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        uid: str,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.uid = uid

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        claimed = {int(k) for k in session.get("squads", {})}
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception:
            t1, t2 = session.get("teams", [0, 1])[:2]

        for team_index in (t1, t2):
            name = _team_display_name(tournament, team_index)
            btn = Button(
                label=f"🧩 Это {name}"[:80],
                style=discord.ButtonStyle.primary,
                custom_id=(
                    f"ocr_squad_claim:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}:{uid}"
                ),
                disabled=team_index in claimed,
            )
            btn.callback = self._claim_callback(team_index)
            self.add_item(btn)

        t1v, t2v = t1, t2

        wrong = Button(
            label="❌ Неправильный скриншот",
            style=discord.ButtonStyle.danger,
            custom_id=(
                f"ocr_squad_wrong:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{uid}"
            ),
        )
        wrong.callback = self._wrong_callback(uid, t1v, t2v)
        self.add_item(wrong)

        call_org = Button(
            label="📢 Вызвать организатора",
            style=discord.ButtonStyle.secondary,
            custom_id=(
                f"ocr_squad_org:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{uid}"
            ),
        )
        call_org.callback = self._org_callback(t1v, t2v)
        self.add_item(call_org)

    def _claim_callback(self, team_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            if not (
                is_org_check(interaction.user, interaction.guild)
                or _is_match_captain(interaction.user, tournament, team_index)
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Назначить отряд может только капитан "
                        "этой команды или организатор."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            session.setdefault("squads", {})
            session.setdefault("squad_pool", [])
            session.setdefault("panels", {})
            pool = session.get("squad_pool", [])
            entry = next((e for e in pool if e.get("uid") == self.uid), None)
            if entry is None:
                await interaction.response.send_message(
                    replace_emojis("❌ Этот скриншот уже назначен команде."),
                    ephemeral=True,
                )
                return
            if str(team_index) in session.get("squads", {}):
                await interaction.response.send_message(
                    replace_emojis("❌ У этой команды уже есть свой отряд."),
                    ephemeral=True,
                )
                return

            session["squads"][str(team_index)] = {
                k: v for k, v in entry.items() if k != "uid"
            }
            session["squad_pool"] = [
                e for e in pool if e.get("uid") != self.uid
            ]
            session.setdefault("panels", {})[
                _squad_panel_key(team_index)
            ] = {
                "name": _team_display_name(tournament, team_index),
                "rows": entry.get("rows", []),
            }
            if len(session["squads"]) >= 2:
                session["status"] = "mapping"
            store.set(tournament)

            name = _team_display_name(tournament, team_index)
            try:
                await interaction.message.edit(
                    view=smart_view(interaction.message, SquadAssignedView(
                        self.guild_id, self.channel_id, tournament,
                        self.match_type, self.match_index, team_index,
                    ))
                )
            except Exception as e:
                logger.warning("Squad claim view swap failed: %s", e)
            await interaction.response.send_message(
                replace_emojis(
                    f"✅ Отряд назначен команде **{name}**.\n"
                    "Нажмите **🧩 Сопоставить** ниже, чтобы привязать "
                    "игровые ники к участникам Discord."
                ),
                ephemeral=True,
            )
        return callback

    def _wrong_callback(self, uid: str, t1: int, t2: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно капитанам команд и организатору."
                    ),
                    ephemeral=True,
                )
                return
            if FileUpload is not None:
                modal = ScreenshotUploadModal(
                    self.guild_id, self.channel_id,
                    self.match_type, self.match_index,
                    reupload=True, squad_uid=uid,
                )
                await interaction.response.send_modal(modal)
                return
            where = await _register_reupload(
                interaction, self.match_type, self.match_index,
                self.channel_id, squad_uid=uid,
            )
            await interaction.response.send_message(
                replace_emojis(
                    f"📸 Пришлите **правильный скриншот отряда** в {where} "
                    f"(в течение {PENDING_UPLOAD_TTL // 60} мин) — "
                    "это сообщение обновится."
                ),
                ephemeral=True,
            )
        return callback

    def _org_callback(self, t1: int, t2: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно капитанам команд и организатору."
                    ),
                    ephemeral=True,
                )
                return
            await _send_org_ping(
                interaction, tournament, self.match_type, self.match_index
            )
            await interaction.response.send_message(
                replace_emojis("📢 Организатор вызван."), ephemeral=True
            )
        return callback


class SquadAssignedView(View):
    """Кнопки на сообщении с OCR отряда ПОСЛЕ назначения команде."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        team_index: int,
    ):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        active = session.get("status") not in ("completed", "cancelled")
        mapped = str(team_index) in session.get("maps", {})
        name = _team_display_name(tournament, team_index)

        map_btn = Button(
            label=f"🧩 Сопоставить {name}"[:80],
            style=discord.ButtonStyle.primary,
            custom_id=(
                f"ocr_squad_map:{guild_id}:{channel_id}:{match_type}:"
                f"{match_index}:{team_index}"
            ),
            disabled=not active or mapped,
        )
        map_btn.callback = self._map_callback()
        self.add_item(map_btn)

        if active:
            wrong = Button(
                label="❌ Неправильный скриншот",
                style=discord.ButtonStyle.danger,
                custom_id=(
                    f"ocr_squad_wrong2:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}"
                ),
            )
            wrong.callback = self._wrong_callback()
            self.add_item(wrong)

            call_org = Button(
                label="📢 Вызвать организатора",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"ocr_squad_org2:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}"
                ),
            )
            call_org.callback = self._org_callback()
            self.add_item(call_org)

    def _map_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            if not (
                is_org_check(interaction.user, interaction.guild)
                or _is_match_captain(
                    interaction.user, tournament, self.team_index
                )
            ):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Сопоставить свою команду может только её "
                        "капитан или организатор."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            if str(self.team_index) in session.get("maps", {}):
                await interaction.response.send_message(
                    replace_emojis("✅ Эта команда уже сопоставлена."),
                    ephemeral=True,
                )
                return

            name = _team_display_name(tournament, self.team_index)
            panel = _squad_panel_key(self.team_index)
            rows = (
                session.get("panels", {}).get(panel, {}).get("rows", [])
            )
            embed = discord.Embed(
                title=f"🧩 {name} — сопоставление игроков",
                description=(
                    "Для каждого игрового ника выберите Discord-игрока "
                    "из вашего состава.\n"
                    "Кнопки ✏️ — ник и K/D/A/DMG, кнопки 📊 — "
                    "фактический урон, нокдауны, исцеление, поднятия, "
                    "хэдшоты."
                ),
                color=discord.Color.blurple(),
            )
            for i, row in enumerate(rows):
                embed.add_field(
                    name=f"{i + 1}. {row.get('name', '?')}",
                    value=(
                        f"K/D/A: {row.get('kills', 0)}/"
                        f"{row.get('deaths', 0)}/{row.get('assists', 0)}"
                        f" • DMG {row.get('dmg')}"
                    ),
                    inline=False,
                )
            view = PanelMappingView(
                self.guild_id, self.channel_id, tournament,
                self.match_type, self.match_index, self.team_index, panel,
            )
            await interaction.response.send_message( view=compose(embed, view), ephemeral=True
            )
        return callback

    def _wrong_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            try:
                t1, t2 = _match_tuple(
                    tournament, self.match_type, self.match_index
                )
            except Exception:
                t1, t2 = self.team_index, self.team_index
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно капитанам команд и организатору."
                    ),
                    ephemeral=True,
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            if not session or session.get("status") == "completed":
                await interaction.response.send_message(
                    replace_emojis("❌ OCR-сессия не найдена или завершена."),
                    ephemeral=True,
                )
                return
            if FileUpload is not None:
                modal = ScreenshotUploadModal(
                    self.guild_id, self.channel_id,
                    self.match_type, self.match_index,
                    reupload=True, squad_team=self.team_index,
                )
                await interaction.response.send_modal(modal)
                return
            where = await _register_reupload(
                interaction, self.match_type, self.match_index,
                self.channel_id, squad_team=self.team_index,
            )
            await interaction.response.send_message(
                replace_emojis(
                    f"📸 Пришлите **новый скриншот отряда** в {where} "
                    f"(в течение {PENDING_UPLOAD_TTL // 60} мин) — "
                    "это сообщение обновится, сопоставление сбросится."
                ),
                ephemeral=True,
            )
        return callback

    def _org_callback(self):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            try:
                t1, t2 = _match_tuple(
                    tournament, self.match_type, self.match_index
                )
            except Exception:
                t1, t2 = self.team_index, self.team_index
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Доступно капитанам команд и организатору."
                    ),
                    ephemeral=True,
                )
                return
            await _send_org_ping(
                interaction, tournament, self.match_type, self.match_index
            )
            await interaction.response.send_message(
                replace_emojis("📢 Организатор вызван."), ephemeral=True
            )
        return callback


def _confirm_embed(
    tournament: Tournament, session: dict, match_type: str, match_index: int
) -> discord.Embed:
    """Сводка сопоставления для финального подтверждения."""
    t1, t2 = _match_tuple(tournament, match_type, match_index)
    embed = discord.Embed(
        title=replace_emojis(f"📊 {_match_label(match_type, match_index)} — подтверждение"),
        color=discord.Color.gold(),
    )
    for team_index in (t1, t2):
        mapping = session.get("maps", {}).get(str(team_index))
        name = _team_display_name(tournament, team_index)
        if not mapping:
            embed.add_field(name=name, value="*не сопоставлена*", inline=False)
            continue
        lines = []
        for row in mapping["rows"]:
            lines.append(
                f"**{row['discord_name']}** — {row['kills']}/{row['deaths']}/"
                f"{row['assists']} • DMG {row['dmg']}  *(OCR: {row['ocr_name']})*"
            )
        embed.add_field(name=name, value="\n".join(lines) or "—", inline=False)
    side = session.get("winner_side")
    if side:
        panel_team = next(
            (int(k) for k, m in session.get("maps", {}).items()
             if m.get("panel") == side),
            None,
        )
        if panel_team is not None:
            wname = _team_display_name(tournament, panel_team)
            embed.description = f"🏆 По OCR победитель: **{wname}**\nВыберите победителя ниже."
    return embed


# ---------------------------------------------------------------------------
# Выбор панели команды (шаг 1 маппинга)
# ---------------------------------------------------------------------------

class PanelPickView(View):
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        team_index: int,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        panels = session.get("panels", {})
        options = []
        for side, label in (("left", "Левая панель"), ("right", "Правая панель")):
            pname = (panels.get(side) or {}).get("name")
            options.append(
                discord.SelectOption(
                    label=f"{label}: {pname}"[:100] if pname else label,
                    value=side,
                    description=(pname or "")[:100],
                )
            )
        self.select = Select(
            placeholder="Ваша команда — на какой панели?",
            options=options,
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_pick
        self.add_item(self.select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        values = getattr(self.select, "values", None) or interaction.data.get(
            "values", []
        )
        if not values:
            await interaction.response.defer()
            return
        panel = values[0]
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        if not session:
            await interaction.response.send_message(
                replace_emojis("❌ OCR-сессия не найдена."), ephemeral=True
            )
            return
        # Панель не должна быть занята другой командой
        for other_key, other_map in session.get("maps", {}).items():
            if other_key != str(self.team_index) and other_map.get("panel") == panel:
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Эта панель уже закреплена за другой командой."
                    ),
                    ephemeral=True,
                )
                return

        name = _team_display_name(tournament, self.team_index)
        rows = (session.get("panels", {}).get(panel) or {}).get("rows", [])
        embed = discord.Embed(
            title=f"🧩 {name} — сопоставление игроков",
            description=(
                "Для каждого игрового ника выберите Discord-игрока "
                "из вашего состава.\n"
                "Кнопки ✏️ позволяют исправить ник и статистику OCR."
            ),
            color=discord.Color.blurple(),
        )
        for i, row in enumerate(rows):
            embed.add_field(
                name=f"{i + 1}. {row.get('name', '?')}",
                value=(
                    f"K/D/A: {row.get('kills', 0)}/{row.get('deaths', 0)}/"
                    f"{row.get('assists', 0)} • DMG {row.get('dmg')}"
                ),
                inline=False,
            )
        if not rows:
            embed.description += "\n\n⚠️ На этой панели не найдено игроков — попробуйте другую."

        view = PanelMappingView(
            self.guild_id, self.channel_id, tournament,
            self.match_type, self.match_index, self.team_index, panel,
        )
        await interaction.response.send_message( view=compose(embed, view), ephemeral=True
        )


# ---------------------------------------------------------------------------
# Маппинг игроков одной команды (шаг 2)
# ---------------------------------------------------------------------------

class PanelMappingView(View):
    """4 select'а (OCR-ник → Discord-игрок) + кнопки правки + сохранение."""

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
        team_index: int,
        panel: str,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.team_index = team_index
        self.panel = panel
        # row_idx -> discord_name
        self.assign: dict[int, str] = {}

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        rows = (session.get("panels", {}).get(panel) or {}).get("rows", [])
        roster = team_roster(tournament, team_index)

        for i, row in enumerate(rows[:4]):
            options = [
                discord.SelectOption(
                    label="— не назначен —",
                    value=NONE_VALUE,
                    description="Пропустить этого игрока",
                )
            ] + [
                discord.SelectOption(label=n[:100], value=n[:100], description="Игрок команды")
                for n in roster
            ]
            sel = Select(
                placeholder=f"{row.get('name', '?')[:80]} → игрок",
                options=options[:25],
                min_values=1,
                max_values=1,
            )
            sel.callback = self._make_select_callback(sel, i)
            self.add_item(sel)

        # Кнопки правки по одной на строку + сохранение (макс. 5 в ряду)
        for i, row in enumerate(rows[:4]):
            btn = Button(
                label=f"✏️ {i + 1}",
                style=discord.ButtonStyle.secondary,
                custom_id=(
                    f"ocr_edit:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{panel}:{i}"
                ),
            )
            btn.callback = self._make_edit_callback(i)
            self.add_item(btn)

        # Для squad-строк — вторая кнопка правки доп. статистики
        if panel.startswith("squad_"):
            for i in range(min(len(rows), 4)):
                btn = Button(
                    label=f"📊 {i + 1}",
                    style=discord.ButtonStyle.secondary,
                    custom_id=(
                        f"ocr_editx:{guild_id}:{channel_id}:{match_type}:"
                        f"{match_index}:{panel}:{i}"
                    ),
                )
                btn.callback = self._make_extra_edit_callback(i)
                self.add_item(btn)

        save = Button(
            label="💾 Сохранить сопоставление",
            style=discord.ButtonStyle.success,
        )
        save.callback = self._save
        self.add_item(save)

    def _make_select_callback(self, sel: Select, row_idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            values = getattr(sel, "values", None) or interaction.data.get(
                "values", []
            )
            if not values:
                await interaction.response.defer()
                return
            value = values[0]
            self.assign[row_idx] = None if value == NONE_VALUE else value
            await interaction.response.defer()
        return callback

    def _make_edit_callback(self, row_idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            row = (
                (session or {}).get("panels", {})
                .get(self.panel, {}).get("rows", [])
            )
            if row_idx >= len(row):
                await interaction.response.send_message(
                    replace_emojis("❌ Строка не найдена."), ephemeral=True
                )
                return
            modal = OcrRowEditModal(
                self.guild_id, self.channel_id, self.match_type,
                self.match_index, self.panel, row_idx,
            )
            await interaction.response.send_modal(modal)
        return callback

    def _make_extra_edit_callback(self, row_idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            session = tournament.ocr_sessions.get(
                _session_key(self.match_type, self.match_index)
            )
            row = (
                (session or {}).get("panels", {})
                .get(self.panel, {}).get("rows", [])
            )
            if row_idx >= len(row):
                await interaction.response.send_message(
                    replace_emojis("❌ Строка не найдена."), ephemeral=True
                )
                return
            modal = OcrSquadExtraModal(
                self.guild_id, self.channel_id, self.match_type,
                self.match_index, self.panel, row_idx,
            )
            await interaction.response.send_modal(modal)
        return callback

    async def _save(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        if not session:
            await interaction.response.send_message(
                replace_emojis("❌ OCR-сессия не найдена."), ephemeral=True
            )
            return

        rows = (session.get("panels", {}).get(self.panel) or {}).get("rows", [])
        roster = set(team_roster(tournament, self.team_index))

        # Валидация: все распознанные строки сопоставлены, без дублей,
        # только свой состав
        chosen = [self.assign.get(i) for i in range(len(rows))]
        if not rows or any(not c for c in chosen):
            await interaction.response.send_message(
                replace_emojis(
                    "❌ Сопоставьте всех игроков — каждому игровому "
                    "нику должен соответствовать участник Discord."
                ),
                ephemeral=True,
            )
            return
        if len(set(chosen)) != len(chosen):
            await interaction.response.send_message(
                replace_emojis("❌ Один игрок не может быть назначен дважды."),
                ephemeral=True,
            )
            return
        if any(c not in roster for c in chosen):
            await interaction.response.send_message(
                replace_emojis("❌ Выбранный игрок не входит в вашу команду."),
                ephemeral=True,
            )
            return

        # Панель не должна быть занята другой командой
        for other_key, other_map in session.get("maps", {}).items():
            if other_key != str(self.team_index) and other_map.get("panel") == self.panel:
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Эта панель уже закреплена за другой командой."
                    ),
                    ephemeral=True,
                )
                return

        # Запись маппинга (статистика — актуальная из сессии, после правок)
        mapped_rows = []
        for i, row in enumerate(rows):
            mapped = {
                "discord_name": chosen[i],
                "ocr_name": row.get("name", "?"),
                "kills": int(row.get("kills", 0)),
                "deaths": int(row.get("deaths", 0)),
                "assists": int(row.get("assists", 0)),
                "dmg": int(row.get("dmg", 0) or 0),
            }
            for k in EXTRA_STAT_KEYS:
                if row.get(k) is not None:
                    mapped[k] = row[k]
            mapped_rows.append(mapped)
        session.setdefault("maps", {})[str(self.team_index)] = {
            "panel": self.panel,
            "rows": mapped_rows,
        }
        done = len(session["maps"])
        auto = done >= 2
        session["status"] = "ready" if auto else "mapping"
        store.set(tournament)

        # Обновляем публичное сообщение (кнопки/прогресс)
        await _refresh_public_message(
            interaction, tournament,
            self.match_type, self.match_index,
        )

        name = _team_display_name(tournament, self.team_index)

        if not auto:
            await interaction.response.send_message(
                replace_emojis(
                    f"✅ Команда **{name}** сопоставлена ({done}/2).\n"
                    "Осталась вторая команда — результат зафиксируется "
                    "автоматически."
                ),
                ephemeral=True,
            )
            return

        # Обе команды сопоставлены → автоподтверждение результата
        t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
        winner = _ocr_winning_team_index(session)

        if winner is not None and winner in (t1, t2):
            await interaction.response.defer(ephemeral=True)
            try:
                await finalize_ocr_match(
                    interaction, tournament,
                    self.match_type, self.match_index, winner,
                )
                wname = _team_display_name(tournament, winner)
                await interaction.followup.send(
                    replace_emojis(
                        f"✅ Сопоставление завершено — результат "
                        f"**{_match_label(self.match_type, self.match_index)}** "
                        f"зафиксирован автоматически. Победитель: **{wname}** "
                        "(по OCR)."
                    ),
                    ephemeral=True,
                )
            except Exception:
                logger.exception("OCR auto-finalize failed")
                await interaction.followup.send(
                    replace_emojis(
                        "❌ Ошибка при автосохранении результата — "
                        "вызовите организатора."
                    ),
                    ephemeral=True,
                )
            return

        # Победитель по OCR не определён — предлагаем выбрать вручную
        embed = _confirm_embed(
            tournament, session, self.match_type, self.match_index
        )
        embed.description = (
            (embed.description or "")
            + "\n\n⚠️ OCR не определил победителя — выберите вручную:"
        )
        try:
            channel = interaction.guild.get_channel(OCR_STATS_CHANNEL_ID)
            if channel is None:
                channel = await interaction.guild.fetch_channel(
                    OCR_STATS_CHANNEL_ID
                )
        except Exception:
            channel = None
        if channel is not None:
            try:
                await channel.send( view=compose(embed, OCRWinnerPickView(
                        self.guild_id, self.channel_id, tournament,
                        self.match_type, self.match_index,
                    )),
                )
            except Exception as e:
                logger.warning("OCR winner-pick send failed: %s", e)
        await interaction.response.send_message(
            replace_emojis(
                f"✅ Команда **{name}** сопоставлена — сопоставление "
                "завершено. OCR не определил победителя: выбор отправлен "
                "в канал результатов."
            ),
            ephemeral=True,
        )


async def _results_channel(
    interaction: discord.Interaction, session: dict | None = None
) -> discord.TextChannel | None:
    """Канал, где опубликовано OCR-сообщение (канал результатов)."""
    channel_id = (session or {}).get("results_channel_id") or OCR_STATS_CHANNEL_ID
    channel = interaction.guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await interaction.guild.fetch_channel(channel_id)
        except Exception:
            return None
    return channel


async def _refresh_public_message(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
) -> None:
    """Перерисовать view публичного OCR-сообщения после сохранения маппинга."""
    session = tournament.ocr_sessions.get(_session_key(match_type, match_index))
    if not session:
        return
    channel = await _results_channel(interaction, session)
    if channel is None:
        return

    # Squad-режим: у каждого отряда своё сообщение в канале результатов
    if session.get("mode") == "squad":
        try:
            t1, t2 = _match_tuple(tournament, match_type, match_index)
        except Exception:
            t1, t2 = session.get("teams", [0, 1])[:2]
        for entry in session.get("squad_pool", []):
            mid = entry.get("message_id")
            if not mid:
                continue
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(
                    view=smart_view(msg, SquadClaimView(
                        tournament.guild_id, tournament.channel_id,
                        tournament, match_type, match_index,
                        entry["uid"],
                    ))
                )
            except Exception as e:
                logger.warning("Squad pool view refresh failed: %s", e)
        for tkey, sentry in session.get("squads", {}).items():
            mid = sentry.get("message_id")
            if not mid:
                continue
            try:
                tidx = int(tkey)
            except (TypeError, ValueError):
                continue
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(
                    view=smart_view(msg, SquadAssignedView(
                        tournament.guild_id, tournament.channel_id,
                        tournament, match_type, match_index, tidx,
                    ))
                )
            except Exception as e:
                logger.warning("Squad assigned view refresh failed: %s", e)
        return

    if not session.get("message_id"):
        return
    try:
        msg = await channel.fetch_message(session["message_id"])
        await msg.edit(
            view=smart_view(msg, OCRPublicView(
                tournament.guild_id, tournament.channel_id, tournament,
                match_type, match_index,
            ))
        )
    except Exception as e:
        logger.warning("OCR public message refresh failed: %s", e)


# ---------------------------------------------------------------------------
# Модалка правки строки OCR (ник + K/D/A/DMG)
# ---------------------------------------------------------------------------

class OcrRowEditModal(Modal, title="Исправить строку OCR"):
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
        panel: str,
        row_idx: int,
    ):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.panel = panel
        self.row_idx = row_idx

        tournament = store.get(guild_id, channel_id)
        session = (
            tournament.ocr_sessions.get(_session_key(match_type, match_index))
            if tournament else {}
        ) or {}
        panel_rows = session.get("panels", {}).get(panel, {}).get("rows") or []
        row = panel_rows[row_idx] if row_idx < len(panel_rows) else {}

        self.name_input = TextInput(
            label="Игровой ник (OCR)",
            placeholder="Ник как на экране результатов",
            required=False,
            max_length=64,
            default=str(row.get("name", ""))[:64] or None,
        )
        self.kills_input = TextInput(
            label="Убийства (K)", required=False, max_length=4,
            default=str(row.get("kills", "")) or None,
        )
        self.deaths_input = TextInput(
            label="Смерти (D)", required=False, max_length=4,
            default=str(row.get("deaths", "")) or None,
        )
        self.assists_input = TextInput(
            label="Ассисты (A)", required=False, max_length=4,
            default=str(row.get("assists", "")) or None,
        )
        self.dmg_input = TextInput(
            label="Урон (DMG)", required=False, max_length=8,
            default=str(row.get("dmg", "")) or None,
        )
        for item in (
            self.name_input, self.kills_input, self.deaths_input,
            self.assists_input, self.dmg_input,
        ):
            self.add_item(item)

    @staticmethod
    def _to_int(value: str | None, default: int = 0) -> int | None:
        v = (value or "").strip()
        if not v:
            return default
        try:
            return int(v)
        except ValueError:
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        rows = (session or {}).get("panels", {}).get(self.panel, {}).get("rows", [])
        if self.row_idx >= len(rows):
            await interaction.response.send_message(
                replace_emojis("❌ Строка не найдена."), ephemeral=True
            )
            return

        kills = self._to_int(self.kills_input.value)
        deaths = self._to_int(self.deaths_input.value)
        assists = self._to_int(self.assists_input.value)
        dmg = self._to_int(self.dmg_input.value)
        if None in (kills, deaths, assists, dmg):
            await interaction.response.send_message(
                replace_emojis("❌ Статистика должна быть целыми числами."),
                ephemeral=True,
            )
            return

        row = rows[self.row_idx]
        new_name = (self.name_input.value or "").strip()
        if new_name:
            row["name"] = new_name[:64]
        row["kills"] = kills
        row["deaths"] = deaths
        row["assists"] = assists
        row["dmg"] = dmg
        store.set(tournament)

        await interaction.response.send_message(
            replace_emojis("✅ Строка обновлена. Не забудьте нажать «Сохранить»."),
            ephemeral=True,
        )


class OcrSquadExtraModal(Modal, title="Доп. статистика отряда"):
    """Правка расширенной статистики squad-строки (5 полей)."""

    FIELDS = (
        ("fact_dmg", "Фактический урон", 8),
        ("knockdowns", "Нокдауны", 4),
        ("healing", "Исцеление", 8),
        ("revives", "Поднятия", 4),
        ("headshots", "Хэдшоты %", 6),
    )

    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        match_type: str,
        match_index: int,
        panel: str,
        row_idx: int,
    ):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index
        self.panel = panel
        self.row_idx = row_idx

        tournament = store.get(guild_id, channel_id)
        session = (
            tournament.ocr_sessions.get(_session_key(match_type, match_index))
            if tournament else {}
        ) or {}
        panel_rows = session.get("panels", {}).get(panel, {}).get("rows") or []
        row = panel_rows[row_idx] if row_idx < len(panel_rows) else {}

        self.inputs: dict[str, TextInput] = {}
        for key, label, max_len in self.FIELDS:
            val = row.get(key)
            ti = TextInput(
                label=label,
                required=False,
                max_length=max_len,
                default=(
                    str(val)[:max_len] if val is not None else None
                ),
            )
            self.add_item(ti)
            self.inputs[key] = ti

    @staticmethod
    def _to_num(value: str | None):
        v = (value or "").strip().replace(",", ".").rstrip("%")
        if not v:
            return None
        try:
            f = float(v)
        except ValueError:
            return "ERR"
        return int(f) if f == int(f) else round(f, 1)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = store.get(self.guild_id, self.channel_id)
        if not tournament:
            await interaction.response.send_message(
                replace_emojis("❌ Турнир не найден."), ephemeral=True
            )
            return
        session = tournament.ocr_sessions.get(
            _session_key(self.match_type, self.match_index)
        )
        rows = (session or {}).get("panels", {}).get(self.panel, {}).get(
            "rows", []
        )
        if self.row_idx >= len(rows):
            await interaction.response.send_message(
                replace_emojis("❌ Строка не найдена."), ephemeral=True
            )
            return

        row = rows[self.row_idx]
        for key, _label, _ml in self.FIELDS:
            num = self._to_num(self.inputs[key].value)
            if num == "ERR":
                await interaction.response.send_message(
                    replace_emojis("❌ Значения должны быть числами."),
                    ephemeral=True,
                )
                return
            if num is None:
                row.pop(key, None)
            else:
                row[key] = num
        store.set(tournament)

        await interaction.response.send_message(
            replace_emojis(
                "✅ Доп. статистика обновлена. Не забудьте нажать "
                "«Сохранить»."
            ),
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# Выбор победителя + финализация
# ---------------------------------------------------------------------------

class OCRWinnerPickView(View):
    def __init__(
        self,
        guild_id: int,
        channel_id: int,
        tournament: Tournament,
        match_type: str,
        match_index: int,
    ):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.match_type = match_type
        self.match_index = match_index

        session = tournament.ocr_sessions.get(
            _session_key(match_type, match_index), {}
        )
        t1, t2 = _match_tuple(tournament, match_type, match_index)
        # Помечаем OCR-победителя (работает и для squad-режима)
        ocr_winner = _ocr_winning_team_index(session)

        for team_index in (t1, t2):
            name = _team_display_name(tournament, team_index)
            label = f"🏆 {name}" if team_index == ocr_winner else name
            btn = Button(
                label=label[:80],
                style=discord.ButtonStyle.success,
                custom_id=(
                    f"ocr_win:{guild_id}:{channel_id}:{match_type}:"
                    f"{match_index}:{team_index}"
                ),
            )
            btn.callback = self._make_callback(team_index)
            self.add_item(btn)

    def _make_callback(self, team_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            tournament = store.get(self.guild_id, self.channel_id)
            if not tournament:
                await interaction.response.send_message(
                    replace_emojis("❌ Турнир не найден."), ephemeral=True
                )
                return
            t1, t2 = _match_tuple(tournament, self.match_type, self.match_index)
            if not _can_use_ocr(interaction.user, tournament, [t1, t2]):
                await interaction.response.send_message(
                    replace_emojis(
                        "❌ Подтверждение доступно капитанам команд "
                        "и организатору."
                    ),
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                await finalize_ocr_match(
                    interaction, tournament,
                    self.match_type, self.match_index, team_index,
                )
                await interaction.followup.send(
                    replace_emojis(
                        f"✅ Результат {_match_label(self.match_type, self.match_index)} "
                        "зафиксирован: статистика сохранена, ELO начислено."
                    ),
                    ephemeral=True,
                )
            except Exception:
                logger.exception("OCR finalize failed")
                await interaction.followup.send(
                    replace_emojis("❌ Ошибка при сохранении результата."),
                    ephemeral=True,
                )
        return callback


async def finalize_ocr_match(
    interaction: discord.Interaction,
    tournament: Tournament,
    match_type: str,
    match_index: int,
    winning_team_index: int,
) -> None:
    """Сохранить статистику из OCR-маппинга и провести матч до конца.

    temp_match_stats → process_match_result (ELO/XP/монеты) → история →
    confirm_* победителя → ставки → обновление сообщения турнира.
    """
    guild_id = tournament.guild_id
    channel_id = tournament.channel_id
    key = _session_key(match_type, match_index)
    session = tournament.ocr_sessions.get(key)
    if not session or len(session.get("maps", {})) < 2:
        raise ValueError("OCR-сессия не готова")

    match_id = f"{tournament.id}_{key}"
    t1, t2 = _match_tuple(tournament, match_type, match_index)

    # Статистика: discord_name -> {kills,deaths,assists,damage,ocr_name[,extra]}
    temp_stats: dict[str, dict[str, Any]] = {}
    for mapping in session["maps"].values():
        for i, row in enumerate(mapping["rows"]):
            entry = {
                "kills": row["kills"],
                "deaths": row["deaths"],
                "assists": row["assists"],
                "damage": row["dmg"],
                "circle": i + 1,
                "ocr_name": row["ocr_name"],
            }
            for k in EXTRA_STAT_KEYS:
                if row.get(k) is not None:
                    entry[k] = row[k]
            temp_stats[row["discord_name"]] = entry

    already_processed = match_id in tournament.processed_elo_matches

    if not already_processed:
        # 1. ELO/XP/монеты + запись temp_match_stats (process_match_result)
        from views.kd_input_view import process_match_result

        await process_match_result(
            guild_id,
            tournament,
            {
                "match_type": match_type,
                "match_index": match_index,
                "winning_team_index": winning_team_index,
                "team1_index": t1,
                "team2_index": t2,
                "temp_kd_data": temp_stats,
            },
            interaction,
        )

        # Возвращаем ocr_name в сохранённую статистику (исходный ник с экрана)
        stored = tournament.temp_match_stats.get(match_id, {})
        for name, kd in temp_stats.items():
            if name in stored:
                stored[name]["ocr_name"] = kd["ocr_name"]
        store.set(tournament)

        # 2. История матчей для всех игроков
        await _save_match_history(
            tournament, match_type, match_index, winning_team_index,
            t1, t2, temp_stats,
        )

        # 3. Подтверждение победителя (двигает сетку) — только если
        # матч ещё не подтверждён, чтобы не перегенерировать bracket
        tournament = store.get(guild_id, channel_id) or tournament
        phase = tournament.phase.value
        if match_type == "qualifier" and phase == "qualifiers":
            tournament.confirm_qualifier_winner(match_index, winning_team_index)
        elif (
            match_type == "semifinal"
            and match_index < len(tournament.semifinal_winners)
            and tournament.semifinal_winners[match_index] is None
        ):
            tournament.confirm_semifinal_winner(match_index, winning_team_index)
        elif match_type == "final" and tournament.winner_team_index is None:
            await tournament.confirm_final_winner(winning_team_index)
        store.set(tournament)

        # 4. Выплата ставок
        await _resolve_match_bets(tournament, match_type, match_index, t1, t2, winning_team_index)
    else:
        # Матч уже обработан — только обновляем статистику
        if match_id not in tournament.temp_match_stats:
            tournament.temp_match_stats[match_id] = {}
        tournament.temp_match_stats[match_id].update(temp_stats)
        store.set(tournament)

    # 5. Закрываем OCR-сессию и обновляем сообщение турнира
    session["status"] = "completed"
    session["winning_team_index"] = winning_team_index
    store.set(tournament)

    final_text = (
        f"✅ Результат зафиксирован. Победитель: "
        f"**{_team_display_name(tournament, winning_team_index)}**"
    )
    try:
        channel = await _results_channel(interaction, session)
    except Exception:
        channel = None

    if session.get("mode") == "squad":
        # У каждого отряда своё сообщение — гасим кнопки на всех
        mids = [
            s.get("message_id") for s in session.get("squads", {}).values()
        ] + [
            e.get("message_id") for e in session.get("squad_pool", [])
        ]
        if channel is not None:
            for mid in mids:
                if not mid:
                    continue
                try:
                    msg = await channel.fetch_message(mid)
                    await msg.edit( view=compose(text=replace_emojis(final_text), view=None)
                    )
                except Exception as e:
                    logger.warning("Squad message finalize failed: %s", e)
    elif session.get("message_id") and channel is not None:
        try:
            msg = await channel.fetch_message(session["message_id"])
            await msg.edit( view=compose(text=replace_emojis(final_text), view=None),
            )
        except Exception as e:
            logger.warning("OCR message finalize edit failed: %s", e)

    try:
        bot = interaction.client
        await bot.update_tournament_message(interaction.guild, tournament)
    except Exception as e:
        logger.warning("update_tournament_message failed: %s", e)


async def _save_match_history(
    tournament: Tournament,
    match_type: str,
    match_index: int,
    winning_team_index: int,
    t1: int,
    t2: int,
    temp_stats: dict[str, dict[str, Any]],
) -> None:
    """Сохранить MatchHistory для каждого игрока (как в ConfirmFinalWinnerButton)."""
    from storage.match_history_store import match_history_store
    from models.match_history import MatchHistory
    from storage.player_stats_store import player_stats_store

    guild_id = tournament.guild_id
    team_a, team_b = tournament.teams[t1], tournament.teams[t2]
    team_a_name = _team_display_name(tournament, t1)
    team_b_name = _team_display_name(tournament, t2)

    user_ids: dict[str, int] = dict(tournament.player_user_ids)
    for stat in await player_stats_store.get_all(guild_id):
        user_ids[stat.name] = stat.user_id

    def roster_data(team_index: int) -> list[dict]:
        out = []
        for name in team_roster(tournament, team_index):
            kd = temp_stats.get(name, {})
            out.append(
                {
                    "name": name,
                    "kills": kd.get("kills", 0),
                    "deaths": kd.get("deaths", 0),
                    "user_id": user_ids.get(name),
                }
            )
        return out

    players_a = roster_data(t1)
    players_b = roster_data(t2)

    for player_name, kd_data in temp_stats.items():
        is_a = player_name in [p["name"] for p in players_a]
        is_b = player_name in [p["name"] for p in players_b]
        if not (is_a or is_b):
            continue
        user_id = user_ids.get(player_name)
        if not user_id:
            continue

        if is_a:
            my_idx, my_name, mates, opp_idx, opp_name, opps = (
                t1, team_a_name, players_a, t2, team_b_name, players_b
            )
        else:
            my_idx, my_name, mates, opp_idx, opp_name, opps = (
                t2, team_b_name, players_b, t1, team_a_name, players_a
            )

        player_stats = await player_stats_store.get(guild_id, user_id)
        elo_change = (
            player_stats.last_elo_change
            if player_stats and hasattr(player_stats, "last_elo_change")
            else 0
        )

        await match_history_store.add(
            MatchHistory(
                guild_id=guild_id,
                user_id=user_id,
                match_id=f"{tournament.id}_{match_type}_{match_index}",
                tournament_id=tournament.id,
                date=datetime.now(),
                match_type=match_type,
                match_index=match_index,
                team_index=my_idx,
                team_name=my_name,
                teammates=mates,
                opponent_team_index=opp_idx,
                opponent_team_name=opp_name,
                opponents=opps,
                kills=kd_data.get("kills", 0),
                deaths=kd_data.get("deaths", 0),
                assists=kd_data.get("assists", 0),
                damage=kd_data.get("damage", 0),
                result="win" if my_idx == winning_team_index else "loss",
                elo_change=elo_change,
            )
        )


async def _resolve_match_bets(
    tournament: Tournament,
    match_type: str,
    match_index: int,
    t1: int,
    t2: int,
    winning_team_index: int,
) -> None:
    """Выплата ставок по матчу (как в ConfirmFinalWinnerButton)."""
    from storage.bet_store import bet_store
    from storage.user_balance_store import user_balance_store

    match_id = f"{tournament.id}_{_session_key(match_type, match_index)}"
    name_a = _team_display_name(tournament, t1)
    name_b = _team_display_name(tournament, t2)
    winning_name = name_a if winning_team_index == t1 else name_b
    winning_pos = 0 if winning_team_index == t1 else 1

    try:
        payouts, payouts_by_name = await bet_store.resolve_match_bets(
            tournament.guild_id, tournament.id, match_id,
            winning_name, winning_pos,
        )
        for user_id, payout in payouts.items():
            if payout > 0:
                await user_balance_store.add_balance(
                    tournament.guild_id, user_id, payout
                )
        for user_name, payout in payouts_by_name.items():
            if payout > 0:
                tournament.betting_results[user_name] = (
                    tournament.betting_results.get(user_name, 0) + payout
                )
        store.set(tournament)
    except Exception as e:
        logger.error("OCR bet resolution failed: %s", e, exc_info=True)
