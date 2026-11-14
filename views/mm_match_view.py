"""CV2-карточка MM-матча (Squad 4×4): запуск, K/D капитанов, подтверждение.

Один постоянный канал из пула — одно сообщение матча. Никаких PNG,
Ready Check и голосований: матч стартует автоматически при наборе
8 игроков, капитаны заполняют K/D ``K/D``, организатор подтверждает
результат и счёт (Kazakhstan, до 5 побед).

Состояние — в ``tournament.match_channels[channel_id]`` (meta) и
``tournament.temp_match_stats[match_id]``. Кнопки — persistent
DynamicItem по custom_id ``mmatch:*``; контекст резолвится через
json_store, регистрировать view на сообщение не нужно.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord

from config import replace_emojis
from models.tournament import Tournament, TournamentPhase
from storage.json_store import store
from utils import kd_stat
from utils.design.layouts import DesignView
from utils.permissions import is_org_check

logger = logging.getLogger(__name__)

MATCH_TYPE = "final"
MATCH_INDEX = 0

# custom_id шаблоны
T_KD = re.compile(r"^mmatch:kd:(?P<gid>\d+):(?P<cid>\d+):(?P<tid>\d+)$")
T_WIN = re.compile(r"^mmatch:win:(?P<side>[ab]):(?P<gid>\d+):(?P<cid>\d+):(?P<tid>\d+)$")
T_WSEL = re.compile(r"^mmatch:winsel:(?P<gid>\d+):(?P<cid>\d+):(?P<tid>\d+)$")
T_CONFIRM = re.compile(
    r"^mmatch:cf:(?P<side>[ab]):(?P<gid>\d+):(?P<cid>\d+):(?P<tid>\d+)"
    r":(?P<w>\d+):(?P<l>\d+)$"
)
T_EDIT = re.compile(r"^mmatch:edit:(?P<gid>\d+):(?P<cid>\d+):(?P<tid>\d+)$")
T_CANCEL = re.compile(r"^mmatch:cancel:(?P<gid>\d+):(?P<cid>\d+):(?P<tid>\d+)$")
T_RULES = re.compile(r"^mmatch:rules$")

RULES_TEXT = (
    "**Правила матча (Kazakhstan)**\n\n"
    "- Формат: Squad 4×4, до **5 победных раундов**.\n"
    "- Допустимые счета: **5:0, 5:1, 5:2, 5:3, 5:4** (или зеркально).\n"
    "- Статистика игроков заполняется в формате **K/D** "
    "(например `7/4`) — по одному набору на каждого из 4 игроков "
    "команды, заполняет капитан своей команды.\n"
    "- Заполнение статистики обеих команд **не завершает** матч — "
    "результат подтверждает организатор.\n"
    "- Подтверждение доступно организатору матча или администратору."
)


def match_id_of(tournament: Tournament) -> str:
    return f"{tournament.id}_{MATCH_TYPE}_{MATCH_INDEX}"


def meta_of(tournament: Tournament) -> dict | None:
    """Meta текущего MM-матча (по channel_id турнира)."""
    mcs = getattr(tournament, "match_channels", None) or {}
    return mcs.get(str(tournament.channel_id)) or mcs.get(
        str(int(tournament.channel_id))
    )


def _team_key(team_index: int) -> str:
    return "a" if team_index == 0 else "b"


def _team_players(tournament: Tournament, team_index: int) -> list[str]:
    if team_index >= len(tournament.teams):
        return []
    data = tournament.teams[team_index] or {}
    players = data.get("players") or []
    if players:
        return [p for p in players if p]
    # Fallback для circle-формата
    return [
        data.get(f"circle{c}") for c in range(1, 5)
        if data.get(f"circle{c}")
    ]


def _team_name(tournament: Tournament, team_index: int) -> str:
    data = tournament.teams[team_index] if team_index < len(tournament.teams) else {}
    return tournament.team_names.get(
        team_index, data.get("captain") or f"Команда {_team_key(team_index).upper()}"
    )


def _roster_mentions(tournament: Tournament, team_index: int) -> str:
    lines = []
    for name in _team_players(tournament, team_index):
        uid = tournament.player_user_ids.get(name, 0)
        lines.append(f"<@{uid}>" if uid else name)
    return "\n".join(lines) if lines else "—"


def _kd_status(tournament: Tournament, team_index: int) -> tuple[int, int, bool]:
    """(заполнено, всего, команда_заполнила_форму)."""
    players = _team_players(tournament, team_index)
    stats = tournament.temp_match_stats.get(match_id_of(tournament), {})
    filled = sum(1 for p in players if p in stats)
    return filled, len(players), bool(players) and filled == len(players)


def _is_captain(tournament: Tournament, user_id: int, team_index: int) -> bool:
    players = _team_players(tournament, team_index)
    if not players:
        return False
    captain = players[0]
    return tournament.player_user_ids.get(captain, 0) == user_id


def _can_fill(tournament: Tournament, user: discord.Member, guild) -> bool:
    """Капитан любой команды матча или организатор/админ."""
    if is_org_check(user, guild):
        return True
    for ti in (0, 1):
        if _is_captain(tournament, user.id, ti):
            return True
    return False


def _can_confirm(user: discord.Member, guild) -> bool:
    return is_org_check(user, guild)


def _ctx(interaction: discord.Interaction, gid: int, cid: int, tid: int):
    """Загрузить турнир по custom_id. None — не найден/неактуален."""
    if interaction.guild_id != gid:
        return None
    tournament = store.get(gid, cid)
    if tournament is None or int(tournament.id) != tid:
        return None
    return tournament


async def _refresh_card(
    interaction: discord.Interaction,
    tournament: Tournament,
    *,
    ephemeral_note: str | None = None,
) -> None:
    """Пересобрать и отредактировать сообщение матча в канале."""
    try:
        channel = interaction.guild.get_channel(tournament.channel_id)
        if channel is None:
            channel = await interaction.guild.fetch_channel(
                tournament.channel_id
            )
        meta = meta_of(tournament)
        mid = (meta or {}).get("card_message_id")
        if not mid:
            return
        msg = await channel.fetch_message(int(mid))
        num = (meta or {}).get("match_num", 0)
        await msg.edit(
            view=build_mm_match_view(tournament, num),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as exc:
        logger.warning("MM match card refresh failed: %s", exc)


async def refresh_mm_cards(bot) -> None:
    """Обновить карточки всех MM-матчей при старте.

    После смены layout (убраны блок «ИНФОРМАЦИЯ» и кнопка
    «Правила») редактирует сообщения карточек — и активных, и
    завершённых (содержимое результата не меняется, только
    оформление). Идемпотентно, ошибки по одной карточке не
    прерывают остальные.
    """
    from storage.json_store import store as _store

    for tournament in _store.all():
        if not getattr(tournament, "mm_search_channel_id", 0):
            continue
        meta = meta_of(tournament)
        mid = (meta or {}).get("card_message_id")
        if not mid:
            continue
        try:
            guild = bot.get_guild(tournament.guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(tournament.channel_id)
            if channel is None:
                channel = await guild.fetch_channel(tournament.channel_id)
            msg = await channel.fetch_message(int(mid))
            await msg.edit(
                view=build_mm_match_view(
                    tournament, (meta or {}).get("match_num", 0)
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.NotFound:
            continue  # сообщение удалено — не ретраим при старте
        except Exception as exc:
            logger.warning(
                "MM card startup refresh failed (tournament %s): %s",
                tournament.id, exc,
            )


# ---------------------------------------------------------------------------
# Сборка CV2-карточки
# ---------------------------------------------------------------------------


def build_mm_match_view(tournament: Tournament, match_num: int = 0) -> DesignView:
    """CV2-сообщение матча: «МАТЧ #N — НАЧАЛСЯ / ПРОВЕРКА / ЗАВЕРШЁН»."""
    gid, cid, tid = tournament.guild_id, tournament.channel_id, int(tournament.id)
    meta = meta_of(tournament) or {}
    state = meta.get("state", "active")
    name_a = _team_name(tournament, 0)
    name_b = _team_name(tournament, 1)

    title = f"МАТЧ #{match_num} — НАЧАЛСЯ" if state != "done" else (
        f"МАТЧ #{match_num} — ЗАВЕРШЁН"
    )
    view = DesignView(title=title, timeout=None)

    if state == "done":
        winner_key = meta.get("winner", "")
        winner_name = name_a if winner_key == "a" else name_b
        view.text(f"**Победитель:** {winner_name}")
        view.text(f"**Счёт:** {meta.get('score', '—')}")
        view.text("**Статус:** подтверждено организатором")
        view.sep()
        view.text("**СТАТИСТИКА (K/D)**")
        view.text(_kd_block(tournament))
        elo_lines = meta.get("elo_lines") or []
        if elo_lines:
            view.sep()
            view.text("**ИЗМЕНЕНИЕ ELO**\n" + "\n".join(elo_lines))
        return view

    view.text(f"**КОМАНДА A — {name_a}**\n{_roster_mentions(tournament, 0)}")
    view.text(f"**КОМАНДА B — {name_b}**\n{_roster_mentions(tournament, 1)}")
    view.sep()

    if state == "check":
        view.text("**МАТЧ #{} — ПРОВЕРКА РЕЗУЛЬТАТА**".format(match_num))
        view.text(_kd_summary(tournament))
        view.text(
            "Статистика восьми игроков заполнена (K/D). "
            "Организатор выбирает победителя и счёт."
        )
        view.row(_winner_select(gid, cid, tid, name_a, name_b))
        view.row(
            _edit_button(gid, cid, tid),
            _cancel_button(gid, cid, tid),
        )
    else:
        view.text(
            "**РЕЗУЛЬТАТ**\n"
            "Ожидается. Капитаны команд заполняют статистику K/D "
            "своих игроков кнопкой ниже."
        )
        view.row(_kd_button(gid, cid, tid))
    return view


def _kd_block(tournament: Tournament) -> str:
    lines = []
    stats = tournament.temp_match_stats.get(match_id_of(tournament), {})
    for ti in (0, 1):
        lines.append(f"**{_team_name(tournament, ti)}**")
        for name in _team_players(tournament, ti):
            kd = stats.get(name)
            if kd:
                lines.append(
                    f"{name} — {kd.get('kills', 0)}/{kd.get('deaths', 0)}"
                )
            else:
                lines.append(f"{name} — —")
    return "\n".join(lines)


def _kd_summary(tournament: Tournament) -> str:
    parts = []
    for ti, key in ((0, "Команда A"), (1, "Команда B")):
        filled, total, done = _kd_status(tournament, ti)
        parts.append(f"{key} — {filled}/{total} заполнили.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Кнопки (persistent DynamicItem)
# ---------------------------------------------------------------------------


def _kd_button(gid: int, cid: int, tid: int) -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.primary,
        label="Заполнить K/D",
        custom_id=f"mmatch:kd:{gid}:{cid}:{tid}",
    )


def _win_button(side: str, gid: int, cid: int, tid: int, label: str) -> discord.ui.Button:
    """Legacy-кнопка победителя — только для старых карточек."""
    return discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label=f"Победитель: {label[:20]}",
        custom_id=f"mmatch:win:{side}:{gid}:{cid}:{tid}",
    )


def _winner_select(
    gid: int, cid: int, tid: int, name_a: str, name_b: str
) -> discord.ui.Select:
    """Select победителя — выбор ещё не подтверждение результата."""
    return discord.ui.Select(
        placeholder="Выберите победителя",
        custom_id=f"mmatch:winsel:{gid}:{cid}:{tid}",
        options=[
            discord.SelectOption(
                label=f"Победитель: {name_a[:90]}",
                value="a",
                description="Команда A"),
            discord.SelectOption(
                label=f"Победитель: {name_b[:90]}",
                value="b",
                description="Команда B"),
        ],
    )


def _edit_button(gid: int, cid: int, tid: int) -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.secondary,
        label="Изменить статистику",
        custom_id=f"mmatch:edit:{gid}:{cid}:{tid}",
    )


def _cancel_button(gid: int, cid: int, tid: int) -> discord.ui.Button:
    return discord.ui.Button(
        style=discord.ButtonStyle.danger,
        label="Отмена",
        custom_id=f"mmatch:cancel:{gid}:{cid}:{tid}",
    )


class MmKDBtn(discord.ui.DynamicItem[discord.ui.Button], template=T_KD):
    """Persistent-кнопка «Заполнить K/D» — dispatch после рестарта."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.primary,
            label="Заполнить K/D",
            custom_id=custom_id,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id") or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_kd_click(interaction)


class MmWinBtn(discord.ui.DynamicItem[discord.ui.Button], template=T_WIN):
    """Persistent-кнопка подтверждения победителя."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Подтвердить победу",
            custom_id=custom_id,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id") or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_win_click(interaction, "")


class MmEditBtn(discord.ui.DynamicItem[discord.ui.Button], template=T_EDIT):
    """Persistent-кнопка «Изменить статистику» (организатор)."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Изменить статистику",
            custom_id=custom_id,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id") or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_edit_click(interaction)


class MmCancelBtn(discord.ui.DynamicItem[discord.ui.Button], template=T_CANCEL):
    """Persistent-кнопка отмены проверки результата."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.danger,
            label="Отмена",
            custom_id=custom_id,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id") or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_cancel_click(interaction)


class MmRulesBtn(discord.ui.DynamicItem[discord.ui.Button], template=T_RULES):
    """Persistent-кнопка «Правила» на карточке матча."""

    def __init__(self) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="Правила",
            custom_id="mmatch:rules",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            view=DesignView(title="Правила матча", timeout=60).text(RULES_TEXT),
            ephemeral=True,
        )


class MmWinSel(discord.ui.DynamicItem[discord.ui.Select], template=T_WSEL):
    """Persistent Select «Выберите победителя» (шаг 1 CHECK-флоу)."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Select(
            placeholder="Выберите победителя",
            custom_id=custom_id,
            options=[
                discord.SelectOption(label="Команда A", value="a"),
                discord.SelectOption(label="Команда B", value="b"),
            ],
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id") or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_wsel_click(interaction)


class MmConfirmBtn(discord.ui.DynamicItem[discord.ui.Button], template=T_CONFIRM):
    """Persistent-кнопка «Подтвердить результат» (шаг 4 CHECK-флоу)."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Подтвердить результат",
            custom_id=custom_id,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id") or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        await handle_confirm_click(interaction)


class _ReviewCancelButton(discord.ui.Button):
    """Вторичная кнопка 'Отмена' на карточке проверки результата."""

    def __init__(self) -> None:
        super().__init__(
            style=discord.ButtonStyle.secondary,
            label="Отмена",
            custom_id="mmatch:cf_cancel",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = DesignView(title="Отменено", timeout=60)
        view.text("Ввод результата отменён — матч не завершён.")
        try:
            await interaction.response.edit_message(view=view)
        except discord.NotFound:
            pass


class MmMatchTemplateView(discord.ui.View):
    """Регистрируется один раз в setup_hook — dispatch по шаблонам."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(MmKDBtn("mmatch:kd:0:0:0"))
        self.add_item(MmWinBtn("mmatch:win:a:0:0:0"))
        self.add_item(MmWinSel("mmatch:winsel:0:0:0"))
        self.add_item(MmConfirmBtn("mmatch:cf:a:0:0:0:5:0"))
        self.add_item(MmEditBtn("mmatch:edit:0:0:0"))
        self.add_item(MmCancelBtn("mmatch:cancel:0:0:0"))
        self.add_item(MmRulesBtn())


# ---------------------------------------------------------------------------
# Модалки
# ---------------------------------------------------------------------------


class MmKDModal(discord.ui.Modal):
    """Одна форма — четыре поля K/D для команды."""

    def __init__(self, gid: int, cid: int, tid: int, team_index: int,
                 team_label: str, players: list[str]):
        super().__init__(title=f"K/D — {team_label}"[:45])
        self.gid, self.cid, self.tid = gid, cid, tid
        self.team_index = team_index
        self.players = players
        self._cid_to_player: dict[str, str] = {}
        for idx, name in enumerate(players):
            cid_key = f"kd_{idx}"
            self._cid_to_player[cid_key] = name
            self.add_item(discord.ui.TextInput(
                label=name[:45],
                placeholder="K/D, например 7/4",
                required=True,
                max_length=12,
                custom_id=cid_key,
            ))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = _ctx(interaction, self.gid, self.cid, self.tid)
        if tournament is None:
            await interaction.response.send_message(
                "Матч не найден или уже завершён.", ephemeral=True
            )
            return
        meta = meta_of(tournament) or {}
        if meta.get("state") == "done" or tournament.phase == TournamentPhase.COMPLETE:
            await interaction.response.send_message(
                "Матч уже завершён.", ephemeral=True
            )
            return
        if not _can_fill(tournament, interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Заполнять K/D может только капитан команды "
                "или организатор.", ephemeral=True
            )
            return
        # Капитан — только своя команда; организатор — любая
        if not is_org_check(interaction.user, interaction.guild):
            if not _is_captain(tournament, interaction.user.id, self.team_index):
                await interaction.response.send_message(
                    "Вы не капитан этой команды.", ephemeral=True
                )
                return

        # Актуальный состав — игроки не должны были измениться
        current = _team_players(tournament, self.team_index)
        if set(current) != set(self.players):
            await interaction.response.send_message(
                "Состав команды изменился — форма устарела.",
                ephemeral=True,
            )
            return

        stats: dict[str, dict[str, int]] = {}
        for item in self.children:
            if not isinstance(item, discord.ui.TextInput):
                continue
            player = self._cid_to_player.get(item.custom_id)
            if player is None:
                continue
            parsed = kd_stat.parse_kd(item.value)
            if parsed is None:
                await interaction.response.send_message(
                    f"Неверный формат K/D для **{player}**. "
                    "Введите два числа через `/`, например `7/4`.",
                    ephemeral=True,
                )
                return
            stats[player] = {"kills": parsed[0], "deaths": parsed[1]}

        mid = match_id_of(tournament)
        bucket = tournament.temp_match_stats.setdefault(mid, {})
        # Заменяем статистику только своей команды
        team_set = set(current)
        prev_kd = {
            p: bucket.get(p) for p in current if p in bucket
        }
        for p in list(bucket.keys()):
            if p in team_set:
                del bucket[p]
        bucket.update(stats)

        kd = meta.setdefault("kd", {})
        kd[_team_key(self.team_index)] = {
            "done": True,
            "by": interaction.user.id,
        }
        # Обе команды заполнены → состояние проверки результата
        ka = kd.get("a", {}).get("done")
        kb = kd.get("b", {}).get("done")
        if ka and kb:
            meta["state"] = "check"
            meta["status_text"] = "статистика заполнена — ожидание организатора"
        else:
            meta["status_text"] = "ожидается"
        store.set(tournament)

        try:
            from services import tournament_audit_service as audit_svc
            _kd = lambda d: (
                f"{d.get('kills', 0)}/{d.get('deaths', 0)}"
                if isinstance(d, dict) else None
            )
            _uids = getattr(tournament, "player_user_ids", {}) or {}

            def _pk(name: str) -> str:
                uid = _uids.get(name)
                return str(uid) if uid else str(name)

            await audit_svc.record_interaction(
                interaction,
                tournament_id=str(tournament.id),
                match_id=mid,
                action=audit_svc.A.MM_KD_SUBMIT,
                source="mm",
                before={
                    _pk(p): _kd(v) for p, v in prev_kd.items() if v
                },
                after={_pk(p): _kd(stats[p]) for p in stats},
            )
        except Exception:
            pass

        await interaction.response.send_message(
            f"Статистика команды сохранена ({len(stats)}/4).",
            ephemeral=True,
        )
        await _refresh_card(interaction, tournament)


class MmScoreModal(discord.ui.Modal, title="Счёт матча"):
    def __init__(self, gid: int, cid: int, tid: int, side: str):
        super().__init__()
        self.gid, self.cid, self.tid, self.side = gid, cid, tid, side
        self.score_input = discord.ui.TextInput(
            label="Счёт победителя (например 5:3)",
            placeholder="5:3",
            required=True,
            max_length=6,
        )
        self.add_item(self.score_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tournament = _ctx(interaction, self.gid, self.cid, self.tid)
        if tournament is None:
            await interaction.response.send_message(
                "Матч не найден или уже завершён.", ephemeral=True
            )
            return
        if not _can_confirm(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Подтверждать результат может только организатор "
                "или администратор.", ephemeral=True
            )
            return
        meta = meta_of(tournament) or {}
        if meta.get("state") != "check":
            await interaction.response.send_message(
                "Статистика заполнена не полностью — подтверждение "
                "результата пока недоступно.", ephemeral=True
            )
            return
        score = kd_stat.parse_match_score(self.score_input.value)
        if score is None:
            await interaction.response.send_message(
                "Некорректный счёт. Kazakhstan — до 5 побед: "
                "5:0, 5:1, 5:2, 5:3 или 5:4.", ephemeral=True
            )
            return

        # Шаг 3 — проверка результата перед финальным подтверждением.
        # ELO/статистика применяются только по кнопке «Подтвердить
        # результат» на этой карточке.
        w, l = score
        winner_idx = 0 if self.side == "a" else 1
        winner_name = _team_name(tournament, winner_idx)
        loser_name = _team_name(tournament, 1 - winner_idx)
        score_display = (
            f"{w}:{l}" if self.side == "a" else f"{l}:{w}"
        )
        gid, cid, tid = self.gid, self.cid, self.tid
        confirm_id = (
            f"mmatch:cf:{self.side}:{gid}:{cid}:{tid}:{w}:{l}"
        )
        review = DesignView(title="Проверьте результат", timeout=300)
        review.text(
            f"**Победитель:** {winner_name}\n"
            f"**Счёт:** {score_display}\n"
            f"**Проигравший:** {loser_name}\n"
            "-# ELO и статистика будут начислены только после "
            "подтверждения."
        )
        review.row(discord.ui.Button(
            style=discord.ButtonStyle.success,
            label="Подтвердить результат",
            custom_id=confirm_id,
        ))
        review.row(_ReviewCancelButton())
        await interaction.response.send_message(
            view=review, ephemeral=True
        )


# ---------------------------------------------------------------------------
# Handlers кнопок
# ---------------------------------------------------------------------------


def _parse_ctx(custom_id: str):
    m = T_KD.match(custom_id) or T_EDIT.match(custom_id) or T_CANCEL.match(custom_id)
    if m:
        return int(m.group("gid")), int(m.group("cid")), int(m.group("tid"))
    m = T_WIN.match(custom_id)
    if m:
        return (m.group("side"), int(m.group("gid")),
                int(m.group("cid")), int(m.group("tid")))
    return None


async def handle_kd_click(interaction: discord.Interaction) -> None:
    parsed = _parse_ctx(str(interaction.data.get("custom_id", "")))
    if not parsed:
        return
    gid, cid, tid = parsed
    tournament = _ctx(interaction, gid, cid, tid)
    if tournament is None:
        await interaction.response.send_message(
            "Матч не найден или уже завершён.", ephemeral=True
        )
        return
    meta = meta_of(tournament) or {}
    if meta.get("state") == "done":
        await interaction.response.send_message(
            "Матч уже завершён.", ephemeral=True
        )
        return
    if not _can_fill(tournament, interaction.user, interaction.guild):
        await interaction.response.send_message(
            "Заполнять K/D может только капитан команды "
            "или организатор.", ephemeral=True
        )
        return

    # Капитан → сразу модалка своей команды; организатор → выбор команды
    own_team = None
    for ti in (0, 1):
        if _is_captain(tournament, interaction.user.id, ti):
            own_team = ti
            break
    is_org = is_org_check(interaction.user, interaction.guild)
    if own_team is None and not is_org:
        await interaction.response.send_message(
            "Вы не капитан ни одной команды этого матча.",
            ephemeral=True,
        )
        return
    if own_team is not None and not is_org:
        team_index = own_team
    else:
        team_index = None  # организатор выбирает ниже

    if team_index is None:
        # Организатор: ephemeral-выбор команды
        view = discord.ui.View(timeout=120)

        async def _pick(ix: discord.Interaction, ti: int) -> None:
            players = _team_players(tournament, ti)
            modal = MmKDModal(
                gid, cid, tid, ti,
                _team_name(tournament, ti), players,
            )
            await ix.response.send_modal(modal)

        for ti in (0, 1):
            label = _team_name(tournament, ti)[:25]
            btn = discord.ui.Button(
                label=f"{label}", style=discord.ButtonStyle.primary,
            )
            btn.callback = lambda ix, t=ti: _pick(ix, t)
            view.add_item(btn)
        await interaction.response.send_message(
            "Выберите команду для заполнения K/D:",
            view=view, ephemeral=True,
        )
        return

    players = _team_players(tournament, team_index)
    modal = MmKDModal(
        gid, cid, tid, team_index,
        _team_name(tournament, team_index), players,
    )
    await interaction.response.send_modal(modal)


async def handle_win_click(interaction: discord.Interaction, side: str) -> None:
    custom_id = str(interaction.data.get("custom_id", ""))
    m = T_WIN.match(custom_id)
    if not m:
        return
    side = m.group("side")
    gid, cid, tid = int(m.group("gid")), int(m.group("cid")), int(m.group("tid"))
    tournament = _ctx(interaction, gid, cid, tid)
    if tournament is None:
        await interaction.response.send_message(
            "Матч не найден или уже завершён.", ephemeral=True
        )
        return
    if not _can_confirm(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "Подтверждать результат может только организатор "
            "или администратор.", ephemeral=True
        )
        return
    meta = meta_of(tournament) or {}
    if meta.get("state") != "check":
        await interaction.response.send_message(
            "Статистика заполнена не полностью — подтверждение "
            "результата пока недоступно.", ephemeral=True
        )
        return
    await interaction.response.send_modal(MmScoreModal(gid, cid, tid, side))


async def handle_wsel_click(interaction: discord.Interaction) -> None:
    """Select победителя → модалка счёта (только организатор)."""
    m = T_WSEL.match(str(interaction.data.get("custom_id", "")))
    if not m:
        return
    gid, cid, tid = int(m.group("gid")), int(m.group("cid")), int(m.group("tid"))
    tournament = _ctx(interaction, gid, cid, tid)
    if tournament is None:
        await interaction.response.send_message(
            "Матч не найден или уже завершён.", ephemeral=True
        )
        return
    if not _can_confirm(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "Подтверждать результат может только организатор "
            "или администратор.", ephemeral=True
        )
        return
    meta = meta_of(tournament) or {}
    if meta.get("state") != "check":
        await interaction.response.send_message(
            "Статистика заполнена не полностью — подтверждение "
            "результата пока недоступно.", ephemeral=True
        )
        return
    values = interaction.data.get("values") or []
    side = values[0] if values else ""
    if side not in ("a", "b"):
        await interaction.response.send_message(
            "Не выбран победитель.", ephemeral=True
        )
        return
    await interaction.response.send_modal(
        MmScoreModal(gid, cid, tid, side)
    )


async def handle_confirm_click(interaction: discord.Interaction) -> None:
    """Финальное «Подтвердить результат» на карточке проверки."""
    m = T_CONFIRM.match(str(interaction.data.get("custom_id", "")))
    if not m:
        return
    side = m.group("side")
    gid, cid, tid = int(m.group("gid")), int(m.group("cid")), int(m.group("tid"))
    w, l = int(m.group("w")), int(m.group("l"))
    tournament = _ctx(interaction, gid, cid, tid)
    if tournament is None:
        await interaction.response.send_message(
            "Матч не найден или уже завершён.", ephemeral=True
        )
        return
    if not _can_confirm(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "Подтверждать результат может только организатор "
            "или администратор.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    ok, err = await finalize_mm_match(
        interaction, tournament, side, (w, l)
    )
    if not ok:
        await interaction.followup.send(f"{err}", ephemeral=True)
        return
    await interaction.followup.send(
        "Результат матча подтверждён.", ephemeral=True
    )


async def handle_edit_click(interaction: discord.Interaction) -> None:
    # Организатор может исправить K/D до финализации — то же, что
    # «Заполнить K/D», но доступно и вне state=check.
    await handle_kd_click(interaction)


async def handle_cancel_click(interaction: discord.Interaction) -> None:
    parsed = _parse_ctx(str(interaction.data.get("custom_id", "")))
    if not parsed:
        return
    gid, cid, tid = parsed
    tournament = _ctx(interaction, gid, cid, tid)
    if tournament is None:
        await interaction.response.send_message(
            "Матч не найден или уже завершён.", ephemeral=True
        )
        return
    if not _can_confirm(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "Недостаточно прав.", ephemeral=True
        )
        return
    meta = meta_of(tournament)
    if meta and meta.get("state") == "check":
        meta["state"] = "active"
        meta["status_text"] = "ожидается"
        store.set(tournament)
        try:
            from services import tournament_audit_service as audit_svc
            await audit_svc.record_interaction(
                interaction,
                tournament_id=str(tournament.id),
                match_id=match_id_of(tournament),
                action=audit_svc.A.RESULT_RESET,
                source="mm",
                after={"reason": "cancel_check"},
            )
        except Exception:
            pass
        await interaction.response.send_message(
            "Проверка отменена — статистику можно исправить.",
            ephemeral=True,
        )
        await _refresh_card(interaction, tournament)
    else:
        await interaction.response.send_message(
            "Нечего отменять.", ephemeral=True
        )


# ---------------------------------------------------------------------------
# Финализация
# ---------------------------------------------------------------------------


# Per-match asyncio-lock финализации: два одновременных нажатия
# «Подтвердить» сериализуются, второй видит уже завершённое состояние.
_FINALIZE_LOCKS: dict[str, asyncio.Lock] = {}


async def finalize_mm_match(
    interaction: discord.Interaction,
    tournament: Tournament,
    winner_side: str,
    score: tuple[int, int],
) -> tuple[bool, str]:
    """Финализация MM-матча под per-match lock (защита от double-click)."""
    mid = match_id_of(tournament)
    lock = _FINALIZE_LOCKS.setdefault(mid, asyncio.Lock())
    async with lock:
        try:
            return await _finalize_mm_match_locked(
                interaction, tournament, winner_side, score
            )
        except Exception as exc:
            # Ошибка БД/хранилища: матч остаётся в check, статистика
            # не применилась (флаг откачен), повторная попытка возможна.
            logger.exception("MM finalize failed for %s", mid)
            return False, f"Ошибка подтверждения: {exc}"


async def _finalize_mm_match_locked(
    interaction: discord.Interaction,
    tournament: Tournament,
    winner_side: str,
    score: tuple[int, int],
) -> tuple[bool, str]:
    """Единая безопасная операция подтверждения результата MM-матча.

    Проверки → process_match_result (ELO один раз) → история →
    confirm_final_winner → обновление карточки → освобождение канала.
    При ошибке БД матч НЕ считается завершённым.
    """
    from views.kd_input_view import process_match_result

    meta = meta_of(tournament)
    meta_state = meta.get("state") if meta else None
    if meta_state == "done":
        # Все этапы завершены, но mark_done мог упасть (БД была
        # недоступна). Дозаписываем маркер — ELO/монеты/история
        # НЕ повторяются: они уже подтверждены stats_done/done.
        from storage import finalize_store

        mid0 = match_id_of(tournament)
        try:
            if await finalize_store.get_status(
                tournament.guild_id, mid0
            ) == "stats_done":
                await finalize_store.mark_done(tournament.guild_id, mid0)
                meta["finalize_marker_done"] = True
                store.set(tournament)
                return True, ""
        except Exception as exc:
            logger.warning("MM finalize marker retry failed: %s", exc)
        return False, "Матч уже завершён."
    if meta is None or meta_state != "check":
        return False, "Матч не в состоянии проверки результата."

    mid = match_id_of(tournament)

    # DB-authoritative статус ЧИТАЕМ ДО in-memory флага:
    # processed_elo_matches может быть выставлен при уже применённом
    # ELO, а история/payouts — упасть. Статус 'done' → полностью
    # завершено; 'stats_done' → ELO не трогаем, доводим остальные
    # этапы; отсутствие записи → статистика ещё не применялась.
    # Ошибка чтения БД пробрасывается — 'нет записи' и 'БД недоступна'
    # нельзя смешивать (иначе повторное начисление).
    from storage import finalize_store

    fstatus = await finalize_store.get_status(tournament.guild_id, mid)
    if fstatus == "done":
        return False, "Результат уже подтверждён."
    stats_applied = fstatus == "stats_done"

    # COMPLETE без DB-маркёра done — либо финализация в процессе
    # (stats_done → resume), либо рассинхрон: не трогаем ELO.
    if tournament.phase == TournamentPhase.COMPLETE and not stats_applied:
        return False, "Матч уже завершён."

    team1_index, team2_index = 0, 1
    winning_team_index = 0 if winner_side == "a" else 1

    # Все 8 K/D должны быть заполнены
    temp_stats = tournament.temp_match_stats.get(mid, {})
    roster = []
    for ti in (0, 1):
        roster.extend(_team_players(tournament, ti))
    missing = [p for p in roster if p not in temp_stats]
    if missing:
        return False, f"Не заполнена статистика: {', '.join(missing)}"
    # Состав не должен был измениться после заполнения K/D
    snapshot = meta.get("roster") or []
    if snapshot and sorted(snapshot) != sorted(roster):
        return False, "Состав игроков изменился после заполнения K/D."

    score_w, score_l = score
    # Счёт в порядке «A:B» для отображения
    score_str = (
        f"{score_l}:{score_w}" if winner_side == "b"
        else f"{score_w}:{score_l}"
    )

    if not stats_applied:
        if mid in tournament.processed_elo_matches:
            # In-memory флаг без DB-маркёра stats_done — расхождение
            # (например, set_many без finalize_match_id). Повторное
            # начисление недопустимо — требуем восстановления маркёра.
            return False, (
                "Статистика помечена обработанной, но DB-маркер "
                "отсутствует. Повторите подтверждение позже."
            )
        # 1. ELO/статистика/монеты — транзакция set_many +
        #    идемпотентные match_coin_grants
        applied = await process_match_result(
            tournament.guild_id,
            tournament,
            {
                "match_type": MATCH_TYPE,
                "match_index": MATCH_INDEX,
                "winning_team_index": winning_team_index,
                "team1_index": team1_index,
                "team2_index": team2_index,
                "temp_kd_data": temp_stats,
                "finalize_match_id": mid,
            },
            interaction,
        )
        if not applied:
            return False, "Результат уже обработан."

    # 2. История матчей для всех 8 игроков
    await _save_match_histories(
        interaction, tournament, winning_team_index, temp_stats
    )

    # 3. Победитель + завершение турнира
    store.set(tournament)
    await tournament.confirm_final_winner(winning_team_index)

    # 4. Зафиксировать состояние матча
    meta["state"] = "done"
    meta["winner"] = winner_side
    meta["score"] = score_str
    meta["status_text"] = "подтверждено"
    meta["elo_lines"] = await _elo_lines(tournament, roster)
    store.set(tournament)

    # 5. Обновить карточку «ЗАВЕРШЁН», затем освободить канал.
    #    Освобождение ДО mark_done: сбой записи маркёра не должен
    #    оставлять канал занятым.
    try:
        await _refresh_card(interaction, tournament)
    except Exception as exc:
        logger.warning("MM done card update failed: %s", exc)
    await _release_match_channel(interaction.guild, tournament)

    # 6. DB-маркер 'done' — последним шагом. Если он упадёт, матч уже
    #    полностью завершён (ELO/история/подтверждение/канал), а статус
    #    остаётся 'stats_done' — повторный проход безопасен: все этапы
    #    идемпотентны, mark_done просто дозапишется.
    await finalize_store.mark_done(tournament.guild_id, mid)
    meta["finalize_marker_done"] = True
    store.set(tournament)

    # Аудит финального подтверждения MM-матча (дедуп по match_id —
    # повторное нажатие/рестарт не дублирует событие).
    try:
        from services import tournament_audit_service as audit_svc
        await audit_svc.record_interaction(
            interaction,
            tournament_id=str(tournament.id),
            match_id=mid,
            action=audit_svc.A.MM_RESULT_CONFIRM,
            source="mm",
            after={
                "winner_side": winner_side,
                "score": score_str,
                "phase": tournament.phase.value,
                "elo": meta.get("elo_lines", []),
            },
            dedup_key=f"mmconfirm:{mid}",
        )
    except Exception as _exc:
        logger.warning("audit mm confirm failed: %s", _exc)
    return True, ""


async def _elo_lines(tournament: Tournament, roster: list[str]) -> list[str]:
    from storage.player_stats_store import player_stats_store

    lines = []
    for name in roster:
        uid = tournament.player_user_ids.get(name, 0)
        stats = await player_stats_store.get(tournament.guild_id, uid)
        delta = getattr(stats, "last_elo_change", 0) if stats else 0
        sign = "+" if delta >= 0 else ""
        lines.append(f"{name}: ELO {sign}{delta}")
    return lines


async def _save_match_histories(
    interaction: discord.Interaction,
    tournament: Tournament,
    winning_team_index: int,
    temp_stats: dict,
) -> None:
    """MatchHistory для каждого игрока матча (как в ConfirmFinalWinnerButton)."""
    from datetime import datetime

    from models.match_history import MatchHistory
    from storage.match_history_store import match_history_store
    from storage.player_stats_store import player_stats_store

    match = (tournament.final_teams[0], tournament.final_teams[1])
    team_a_idx, team_b_idx = match
    team_a = tournament.teams[team_a_idx] if team_a_idx < len(tournament.teams) else {}
    team_b = tournament.teams[team_b_idx] if team_b_idx < len(tournament.teams) else {}
    team_a_name = tournament.team_names.get(team_a_idx, team_a.get("captain", "A"))
    team_b_name = tournament.team_names.get(team_b_idx, team_b.get("captain", "B"))

    user_ids = dict(tournament.player_user_ids)
    try:
        for stat in await player_stats_store.get_all(tournament.guild_id):
            user_ids[stat.name] = stat.user_id
    except Exception:
        pass

    team_players = {
        0: _team_players(tournament, 0),
        1: _team_players(tournament, 1),
    }
    team_names = {0: team_a_name, 1: team_b_name}
    team_idx_of = {0: team_a_idx, 1: team_b_idx}

    for side, names in team_players.items():
        my_idx = team_idx_of[side]
        opp_side = 1 - side
        opp_idx = team_idx_of[opp_side]
        for name in names:
            user_id = user_ids.get(name)
            if not user_id:
                continue
            kd = temp_stats.get(name, {})
            teammates = [
                {"name": n,
                 "kills": temp_stats.get(n, {}).get("kills", 0),
                 "deaths": temp_stats.get(n, {}).get("deaths", 0),
                 "user_id": user_ids.get(n)}
                for n in names
            ]
            opponents = [
                {"name": n,
                 "kills": temp_stats.get(n, {}).get("kills", 0),
                 "deaths": temp_stats.get(n, {}).get("deaths", 0),
                 "user_id": user_ids.get(n)}
                for n in team_players[opp_side]
            ]
            result = "win" if my_idx == winning_team_index else "loss"
            stats = await player_stats_store.get(tournament.guild_id, user_id)
            elo_change = getattr(stats, "last_elo_change", 0) if stats else 0
            await match_history_store.add(MatchHistory(
                guild_id=tournament.guild_id,
                user_id=user_id,
                match_id=match_id_of(tournament),
                tournament_id=tournament.id,
                date=datetime.now(),
                match_type=MATCH_TYPE,
                match_index=MATCH_INDEX,
                team_index=my_idx,
                team_name=team_names[side],
                teammates=teammates,
                opponent_team_index=opp_idx,
                opponent_team_name=team_names[opp_side],
                opponents=opponents,
                kills=kd.get("kills", 0),
                deaths=kd.get("deaths", 0),
                result=result,
                elo_change=elo_change,
            ))


async def _release_match_channel(guild: discord.Guild, tournament: Tournament) -> None:
    """Вернуть слот пула в FREE после финализации.

    Безопасность:
    - слот проверяется по фактическому state в БД, а не по коду возврата;
    - owner обязан совпадать с поколением этого матча — слот, уже
      выданный другому матчу, НЕ освобождается;
    - при неуспехе meta['channel_released'] не выставляется — sweeper
      повторит освобождение автоматически.
    """
    from services import match_channel_pool

    cid = tournament.channel_id
    meta = meta_of(tournament) or {}
    match_num = int(meta.get("match_num", 0) or 0)
    # owner слота при acquire: mm:{gid}:{search_channel_id}:{match_num}
    our_owner = (
        f"mm:{guild.id}:{tournament.mm_search_channel_id}:{match_num}"
    )
    # Legacy-формат (до введения match_num) — тот же поиск, матч один.
    legacy_owner = (
        f"mm:{guild.id}:{tournament.mm_search_channel_id}"
    )

    try:
        info = await match_channel_pool.slot_info(cid)
    except Exception as exc:
        logger.warning("MM slot_info failed for %s: %s", cid, exc)
        return
    if info is None:
        meta["channel_released"] = True  # не пул-канал — нечего ждать
        store.set(tournament)
        return
    if info.get("state") == "free":
        meta["channel_released"] = True
        store.set(tournament)
        return
    owner = info.get("owner") or ""
    if owner not in (our_owner, legacy_owner):
        # Слот уже принадлежит другому матчу/владельцу — не трогаем.
        # Для нас канал «ушёл»: считаем освобождённым, чтобы sweeper
        # не пытался снимать чужой слот повторно.
        logger.warning(
            "MM release skipped: slot %s owner=%s (ours=%s) state=%s",
            cid, owner, our_owner, info.get("state"),
        )
        meta["channel_released"] = True
        store.set(tournament)
        return

    channel = guild.get_channel(cid)
    if channel is None:
        try:
            channel = await guild.fetch_channel(cid)
        except Exception:
            channel = None
    released = False
    if isinstance(channel, discord.TextChannel):
        try:
            await match_channel_pool.release_channel(channel)
        except Exception as exc:
            logger.error("MM channel release failed: %s", exc)
    # Проверка ФАКТИЧЕСКОГО состояния слота после попытки
    try:
        info = await match_channel_pool.slot_info(cid)
        released = bool(info) and info.get("state") == "free"
    except Exception as exc:
        logger.warning("MM slot verify failed for %s: %s", cid, exc)
    if released:
        meta["channel_released"] = True
        store.set(tournament)
    else:
        logger.warning(
            "MM channel %s not freed (state=%s) — sweeper will retry",
            cid, (info or {}).get("state", "?"),
        )
