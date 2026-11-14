"""Views «Ультиматума» 2.0 — карточки регистрации, сетки, матчей.

Все кнопки используют custom_id с префиксом ``ult2:`` и
обрабатываются централизованно в cogs/ultimatum.py::on_interaction —
поэтому интерфейс переживает перезапуск бота без регистрации views.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord

from config import get_emoji
from utils.design import DesignView, btn, colors, em, muted
from ultimatum.bracket import Format, entrant_count
from ultimatum.store import (
    DuoInvite, UltEntrant, UltMatch, UltTournament, ultimatum_store,
)

logger = logging.getLogger(__name__)

CID = "ult2"

STATUS_ICON = {
    "WAITING": "⏳",
    "READY": "🟢",
    "IN_PROGRESS": "🔵",
    "AWAITING_CONFIRMATION": "🟡",
    "DISPUTED": "⚠️",
    "COMPLETED": "✅",
    "CANCELLED": "❌",
}

BRACKET_ORDER = {"W": 0, "L": 1, "GF": 2}


def _key_sort(key: str) -> tuple:
    """Натуральная сортировка ключей: U1-2 < U1-10, GF после LB."""
    m = re.match(r"([A-Z]+)(\d*)-?(\d*)", key)
    if not m:
        return (9, key)
    prefix, bracket_n, sub = m.group(1), m.group(2), m.group(3)
    order = {"U": 0, "L": 1, "GF": 2}.get(prefix, 9)
    return (order, int(bracket_n or 0), int(sub or 0), key)


def match_sort_key(m: UltMatch) -> tuple:
    return _key_sort(m.key)


def entrant_label(e: Optional[UltEntrant]) -> str:
    if e is None:
        return "—"
    rec = f"{e.wins}:{e.losses}"
    place = ""
    if e.final_place:
        place = f" · {e.final_place} место"
    elif e.place_group:
        place = f" · {e.place_group} места"
    return f"{e.label()} ({rec}){place}"


async def load_context(tid: str):
    """Загрузить турнир + entrants + matches + invites."""
    t = await ultimatum_store.get_tournament(tid)
    if not t:
        return None
    entrants = await ultimatum_store.get_entrants(tid)
    matches = await ultimatum_store.get_matches(tid)
    invites = await ultimatum_store.list_invites(tid) if t.format is Format.DUO else []
    return t, {e.id: e for e in entrants}, matches, entrants, invites


# ---------------------------------------------------------------------------
# Регистрация
# ---------------------------------------------------------------------------

def build_registration_view(
    tournament: UltTournament,
    entrants: list[UltEntrant],
    invites: list[DuoInvite],
) -> DesignView:
    """Карточка регистрации 'УЛЬТИМАТУМ — СОЛО/ДУО'."""
    fmt = tournament.format
    need = entrant_count(fmt)
    is_duo = fmt is Format.DUO

    view = DesignView(
        title=f" {em('winner')} УЛЬТИМАТУМ — {'СОЛО' if not is_duo else 'ДУО'}",
        accent=colors.TOURNAMENT,
        timeout=None,
    )
    status_txt = {
        "REGISTRATION": "📋 Регистрация",
        "IN_PROGRESS": "🎮 Турнир идёт",
        "COMPLETED": "🏆 Завершён",
        "CANCELLED": "❌ Отменён",
    }.get(tournament.status, tournament.status)

    lines = [
        f"**Формат:** {'1×1 — каждый за себя' if not is_duo else '2×2 — команды по два игрока'}",
        f"**Участников:** {len(entrants)}/{need}",
        f"**Статус:** {status_txt}",
        "**Правило:** два поражения — выбывание. Чемпион определяется в гранд-финале (при необходимости — два матча).",
    ]
    if not tournament.is_open:
        lines.append(
            f"🔒 Закрытый турнир — нужен тег {get_emoji('guild_tag')}r!z3"
        )
    view.text("\n".join(lines))
    view.sep()

    # Список участников
    if entrants:
        if is_duo:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)

            def _alive(i) -> bool:
                if i.status != "pending" or i.expires_at is None:
                    return i.status == "pending"
                exp = i.expires_at
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                return exp > now

            pending = [i for i in invites if _alive(i)]
            roster = []
            for e in sorted(entrants, key=lambda x: (x.seed or 999)):
                roster.append(f"• #{e.seed or '—'} {e.label()} — {e.wins}:{e.losses}")
            for inv in pending:
                roster.append(
                    f"• ⏳ ожидание: <@{inv.captain_id}> + <@{inv.partner_id}>"
                )
            view.text("**Команды:**\n" + "\n".join(roster))
        else:
            roster = [
                f"• #{e.seed or '—'} {e.label()}" for e in entrants
            ]
            view.text(
                "**Игроки:**\n" + ("\n".join(roster) if roster else "*")
            )
    else:
        view.text(muted("Пока нет участников."))
    view.sep()

    # Кнопки — зависят от формата и статуса
    if tournament.status == "REGISTRATION":
        if is_duo:
            view.row(
                btn("Создать дуэт", style="success", custom_id=f"{CID}:duo:new:{tournament.id}", emoji_key="team"),
                btn("Выйти из дуэта", style="danger", custom_id=f"{CID}:duo:leave:{tournament.id}", emoji_key="cross"),
            )
        else:
            view.row(
                btn("Участвовать", style="success", custom_id=f"{CID}:join:{tournament.id}", emoji_key="check"),
                btn("Покинуть", style="danger", custom_id=f"{CID}:leave:{tournament.id}", emoji_key="cross"),
            )
        view.row(
            btn("⚙️ Организатор", style="secondary", custom_id=f"{CID}:org:{tournament.id}", emoji_key="settings"),
        )
    else:
        view.row(
            btn("🏆 Сетка турнира", style="primary", custom_id=f"{CID}:br:open:{tournament.id}", emoji_key="trophy"),
            btn("🎮 Матчи", style="primary", custom_id=f"{CID}:br:matches:{tournament.id}", emoji_key="sword"),
        )
        view.row(
            btn("⚙️ Организатор", style="secondary", custom_id=f"{CID}:org:{tournament.id}", emoji_key="settings"),
        )
    return view


async def refresh_registration(bot, tournament: UltTournament) -> None:
    """Обновить карточку регистрации в канале."""
    if not tournament.message_id or not tournament.channel_id:
        return
    try:
        channel = bot.get_channel(tournament.channel_id)
        if channel is None:
            guild = bot.get_guild(tournament.guild_id)
            channel = await guild.fetch_channel(tournament.channel_id) if guild else None
        if channel is None:
            return
        msg = await channel.fetch_message(tournament.message_id)
        ctx = await load_context(tournament.id)
        if ctx is None:
            return
        _, _, _, entrants, invites = ctx
        await msg.edit(view=build_registration_view(tournament, entrants, invites))
    except Exception as e:
        logger.warning("refresh_registration %s: %s", tournament.id, e)


# ---------------------------------------------------------------------------
# Сетка
# ---------------------------------------------------------------------------

def _match_line(m: UltMatch, ent_a: Optional[UltEntrant], ent_b: Optional[UltEntrant]) -> str:
    icon = STATUS_ICON.get(m.status, "❓")
    if m.status == "COMPLETED" and m.winner_entrant:
        w = ent_a if m.winner_entrant == m.entrant_a else ent_b
        l = ent_b if m.winner_entrant == m.entrant_a else ent_a
        return f"{icon} **{m.key}** {w.label() if w else '?'} 🏆"
    a_txt = ent_a.label() if ent_a else "?"
    b_txt = ent_b.label() if ent_b else "?"
    rec_a = f"{ent_a.wins}:{ent_a.losses}" if ent_a else ""
    rec_b = f"{ent_b.wins}:{ent_b.losses}" if ent_b else ""
    return (
        f"{icon} **{m.key}** {a_txt} `{rec_a}` — {b_txt} `{rec_b}`"
        f" · {m.status}"
    )


def build_bracket_text(
    tournament: UltTournament,
    matches: list[UltMatch],
    entrants: dict[int, UltEntrant],
) -> str:
    """Текст сетки для Components V2 (chunked снаружи)."""
    matches = sorted(matches, key=match_sort_key)
    sections: dict[str, list[str]] = {}
    for m in matches:
        bracket_name = {
            "W": "Верхняя сетка",
            "L": "Нижняя сетка",
            "GF": "Гранд-финал",
        }.get(m.bracket, m.bracket)
        sections.setdefault(bracket_name, []).append(
            _match_line(m, entrants.get(m.entrant_a or -1), entrants.get(m.entrant_b or -1))
        )

    total = len(matches)
    done = sum(1 for m in matches if m.status == "COMPLETED")
    header = (
        f"**{tournament.name}** · {'Соло' if tournament.format is Format.SOLO else 'Дуо'}\n"
        f"Прогресс: **{done}/{total}** матчей"
    )
    parts = [header]
    for name in ("Верхняя сетка", "Нижняя сетка", "Гранд-финал"):
        if name not in sections:
            continue
        parts.append(f"\n**{name}**")
        parts.extend(sections[name])
    return "\n".join(parts)


def build_bracket_view(
    tournament: UltTournament,
    matches: list[UltMatch],
    entrants: dict[int, UltEntrant],
) -> DesignView:
    text = build_bracket_text(tournament, matches, entrants)
    view = DesignView(
        title=f" {em('trophy')} {tournament.name}",
        accent=colors.TOURNAMENT,
        timeout=None,
    )
    # TextDisplay лимит ~4000 — режем на куски по 3000
    for i in range(0, len(text), 3000):
        view.text(text[i:i + 3000])
        if i + 3000 < len(text):
            view.sep()
    view.row(
        btn("🎮 Матчи", style="primary", custom_id=f"{CID}:br:matches:{tournament.id}", emoji_key="sword"),
        btn("🔄 Обновить", style="secondary", custom_id=f"{CID}:br:refresh:{tournament.id}", emoji_key="refresh"),
        btn("🖼️ PNG", style="secondary", custom_id=f"{CID}:br:png:{tournament.id}", emoji_key="screenshot"),
    )
    view.row(
        btn("⚙️ Организатор", style="secondary", custom_id=f"{CID}:org:{tournament.id}", emoji_key="settings"),
    )
    return view


async def post_or_update_bracket(bot, tournament: UltTournament) -> None:
    """Создать/обновить сообщение сетки в канале турнира."""
    ctx = await load_context(tournament.id)
    if ctx is None:
        return
    _, ent_map, matches, _, _ = ctx
    view = build_bracket_view(tournament, matches, ent_map)

    channel = None
    if tournament.channel_id:
        channel = bot.get_channel(tournament.channel_id)
        if channel is None and tournament.guild_id:
            guild = bot.get_guild(tournament.guild_id)
            if guild:
                try:
                    channel = await guild.fetch_channel(tournament.channel_id)
                except Exception:
                    channel = None
    if channel is None:
        return

    old_bracket_id = tournament.bracket_message_id
    if old_bracket_id:
        try:
            msg = await channel.fetch_message(old_bracket_id)
            await msg.edit(view=view)
            return
        except Exception:
            # Сообщение удалено/недоступно — создаём новое,
            # но только если никто не сделал это параллельно.
            pass
    msg = await channel.send(view=view)
    # CAS: если параллельный resume уже записал свой message_id —
    # удаляем дубликат и остаётся чужое (эквивалентное) сообщение.
    claimed = await ultimatum_store.claim_bracket_message(
        tournament.id, msg.id, old_bracket_id
    )
    if not claimed:
        try:
            await msg.delete()
        except Exception:
            pass
        fresh = await ultimatum_store.get_tournament(tournament.id)
        if fresh:
            tournament.bracket_message_id = fresh.bracket_message_id
        return
    tournament.bracket_message_id = msg.id


async def refresh_all_ult_views(bot, tournament: UltTournament) -> None:
    """Обновить все карточки турнира (регистрация + сетка)."""
    await refresh_registration(bot, tournament)
    await post_or_update_bracket(bot, tournament)


# ---------------------------------------------------------------------------
# Панель матчей (ephemeral)
# ---------------------------------------------------------------------------

def build_matches_panel(
    tournament: UltTournament,
    matches: list[UltMatch],
    ent_map: dict[int, UltEntrant],
) -> DesignView:
    """Список активных матчей с кнопкой выбора."""
    active = [
        m for m in matches
        if m.status in ("READY", "IN_PROGRESS", "AWAITING_CONFIRMATION", "DISPUTED")
    ]
    view = DesignView(
        title=f" 🎮 Активные матчи — {tournament.name}",
        accent=colors.MATCH,
        timeout=180,
    )
    if not active:
        view.text(muted("Нет активных матчей."))
        view.row(btn("🔙 К сетке", style="secondary", custom_id=f"{CID}:br:open:{tournament.id}"))
        return view

    lines = []
    for m in sorted(active, key=match_sort_key):
        icon = STATUS_ICON.get(m.status, "❓")
        ea, eb = ent_map.get(m.entrant_a or -1), ent_map.get(m.entrant_b or -1)
        lines.append(
            f"{icon} **{m.key}** {ea.label() if ea else '?'} — {eb.label() if eb else '?'}"
        )
    view.text("\n".join(lines))
    view.sep()
    view.text("Выберите матч для голосования или данных комнаты:")
    # Select со списком активных матчей (до 25)
    options = []
    for m in sorted(active, key=match_sort_key)[:25]:
        ea, eb = ent_map.get(m.entrant_a or -1), ent_map.get(m.entrant_b or -1)
        label = f"{m.key}: {(ea.label() if ea else '?')[:30]} vs {(eb.label() if eb else '?')[:30]}"
        options.append(discord.SelectOption(label=label[:100], value=str(m.id)))
    sel = discord.ui.Select(
        placeholder="Матч",
        custom_id=f"{CID}:match:sel:{tournament.id}",
        options=options,
    )
    view.row(sel)
    view.row(
        btn("🔙 К сетке", style="secondary", custom_id=f"{CID}:br:open:{tournament.id}"),
    )
    return view


def build_match_detail(
    tournament: UltTournament,
    m: UltMatch,
    ent_map: dict[int, UltEntrant],
    votes: list,
    viewer_id: Optional[int] = None,
    is_org: bool = False,
) -> DesignView:
    """Карточка конкретного матча: стороны, счёт, комната, статус.

    Пароль комнаты и кнопки действий показываются только участникам
    матча и организаторам (viewer_id/is_org).
    """
    ea, eb = ent_map.get(m.entrant_a or -1), ent_map.get(m.entrant_b or -1)
    is_participant = bool(
        viewer_id
        and any(
            e and viewer_id in e.member_ids for e in (ea, eb)
        )
    )
    can_see_room = is_participant or is_org
    can_act = can_see_room and m.status in (
        "READY", "IN_PROGRESS", "AWAITING_CONFIRMATION"
    )
    view = DesignView(
        title=f" ⚔️ Матч {m.key} — {m.round_name}",
        accent=colors.MATCH,
        timeout=180,
    )
    icon = STATUS_ICON.get(m.status, "❓")
    view.text(f"**Статус:** {icon} {m.status}")
    view.sep()

    def side(e: Optional[UltEntrant], tag: str) -> str:
        if e is None:
            return f"**{tag}:** —"
        members = "\n".join(f"  • {n}" for n in e.member_names) or ""
        return (
            f"**{tag}:** {e.label()} — `{e.wins}:{e.losses}`\n{members}"
        )

    view.text(side(ea, "A"))
    view.text("**VS**")
    view.text(side(eb, "B"))
    view.sep()

    if m.room_id:
        if can_see_room:
            room_txt = f"🗝️ Комната: `{m.room_id}`"
            if m.room_password:
                room_txt += f" · пароль: `{m.room_password}`"
            view.text(room_txt)
        else:
            view.text(muted("🗝️ Комната указана — данные видны участникам матча."))
    else:
        view.text(muted("Комната ещё не указана."))

    if votes:
        view.sep()
        view.text("**Голоса:**")
        for v in votes:
            voter_ent = ent_map.get(v.voter_entrant or -1)
            voted_ent = ent_map.get(v.voted_entrant)
            view.text(
                f"• <@{v.user_id}> ({voter_ent.label() if voter_ent else '?'}) "
                f"→ {voted_ent.label() if voted_ent else '?'}"
            )

    view.sep()
    if can_act:
        a_label = (ea.label() if ea else "A")[:60]
        b_label = (eb.label() if eb else "B")[:60]
        view.row(
            btn(f"Победа: {a_label}", style="success",
                custom_id=f"{CID}:vote:{m.id}:a", emoji_key="winner"),
            btn(f"Победа: {b_label}", style="success",
                custom_id=f"{CID}:vote:{m.id}:b", emoji_key="winner"),
        )
        view.row(
            btn("🗝️ Комната", style="secondary", custom_id=f"{CID}:room:{m.id}", emoji_key="room"),
        )
    elif m.status == "DISPUTED":
        view.text("⚠️ Спор — ожидается решение организатора.")
    elif m.status == "COMPLETED":
        w = ent_map.get(m.winner_entrant or -1)
        view.text(f"🏆 Победитель: **{w.label() if w else '?'}**")
    elif not can_see_room:
        view.text(muted("Действия доступны участникам матча."))
    view.row(
        btn("🔙 К матчам", style="secondary", custom_id=f"{CID}:br:matches:{tournament.id}"),
    )
    return view


# ---------------------------------------------------------------------------
# Орг-меню (ephemeral)
# ---------------------------------------------------------------------------

def build_org_menu(tournament: UltTournament) -> DesignView:
    view = DesignView(
        title=" ⚙️ Меню организатора",
        accent=colors.DANGER,
        timeout=180,
    )
    view.text(
        f"**Турнир:** {tournament.name}\n"
        f"**Формат:** {'Соло' if tournament.format is Format.SOLO else 'Дуо'}\n"
        f"**Статус:** {tournament.status}"
    )
    view.sep()
    if tournament.status == "REGISTRATION":
        view.row(
            btn("▶️ Старт", style="success", custom_id=f"{CID}:org:start:{tournament.id}", emoji_key="match"),
            btn("🔓 Открыть/Закрыть", style="secondary", custom_id=f"{CID}:org:open:{tournament.id}", emoji_key="security"),
        )
        view.row(
            btn("🗑️ Удалить турнир", style="danger", custom_id=f"{CID}:org:delete:{tournament.id}", emoji_key="delete"),
        )
    else:
        view.row(
            btn("🔧 Исправить результат", style="secondary",
                custom_id=f"{CID}:org:fix:{tournament.id}", emoji_key="warning"),
            btn("🗑️ Удалить турнир", style="danger", custom_id=f"{CID}:org:delete:{tournament.id}", emoji_key="delete"),
        )
        view.row(
            btn("🔓 Открыть/Закрыть", style="secondary", custom_id=f"{CID}:org:open:{tournament.id}", emoji_key="security"),
        )
    return view
