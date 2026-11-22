"""Чистый рендер публичных публикаций CASH CUP.

Не зависит от Discord-состояния: на вход — snapshot state из
cashcup_tournaments, на выход — список Embed + хэш содержимого.
Используется cog'ом публикаций; вся логика проверяется unit-тестами.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import discord

from services.cashcup_bracket import ready_matches
from utils.ff_profile_formatter import safe_text

EMBED_DESC_LIMIT = 3800  # запас до жёстких 4096 Discord
MAX_EMBEDS = 10          # лимит embeds на одно сообщение

_STAGE_NAMES = {"W": "WINNERS BRACKET", "L": "LOSERS BRACKET",
                "GF": "GRAND FINAL"}
_STAGE_ORDER = {"W": 0, "L": 1, "GF": 2}

_STATUS_RU = {"registration": "регистрация", "running": "идёт",
              "completed": "завершён", "cancelled": "отменён"}

_EVIDENCE_PREFIXES = ("https://discord.com/channels/",
                      "https://cdn.discordapp.com/",
                      "https://media.discordapp.net/")


def _team(state: dict, team_id: str | None) -> dict | None:
    if not team_id:
        return None
    return next((t for t in state.get("teams", []) if t["id"] == team_id),
                None)


def team_display_name(state: dict, team_id: str | None) -> str:
    t = _team(state, team_id)
    return safe_text(t["name"], limit=80) if t else "TBD"


def _match_line(state: dict, m: dict) -> str:
    """Одна строка сетки: `W1-1 · A vs B · BO1` / завершённый вариант."""
    mid = m["id"]
    a = team_display_name(state, m.get("a"))
    b = team_display_name(state, m.get("b"))
    bo = f"BO{m.get('bo', 1)}"
    status = m.get("status")
    if status == "completed":
        sa, sb = (m.get("score") or [0, 0])[:2]
        winner = m.get("winner")
        wa = winner == m.get("a")
        wname = f"**{a}**" if wa else f"**{b}**"
        lname = b if wa else a
        return f"`{mid}` · {wname} {sa}:{sb} {lname} · ✅"
    if status == "void":
        return f"`{mid}` · {a} vs {b} · {bo} · ⛔ технический результат"
    flags = []
    if status == "disputed":
        flags.append("⚠️ СПОР")
    if m["bracket"] == "GF" or m.get("games") \
            or (m.get("score") or [0, 0]) != [0, 0]:
        sa, sb = (m.get("score") or [0, 0])[:2]
        flags.append(f"счёт {sa}:{sb}")
    suffix = (" · " + " · ".join(flags)) if flags else ""
    return f"`{mid}` · {a} vs {b} · {bo}{suffix}"


def _bracket_stats(state: dict) -> dict[str, int]:
    b = state.get("bracket") or {}
    matches = b.get("matches") or []
    return {
        "total": len(matches),
        "completed": sum(1 for m in matches if m.get("status") == "completed"),
        "active": len(ready_matches(b)) if b else 0,
        "disputed": sum(1 for m in matches if m.get("status") == "disputed"),
    }


def _header_lines(state: dict) -> list[str]:
    stats = _bracket_stats(state)
    prize = safe_text(state.get("prize") or "Приз определяет организатор",
                      limit=90)
    lines = [
        f"**Турнир:** {prize}",
        f"**Команд:** {len(state.get('teams') or [])}",
        f"**Статус:** {_STATUS_RU.get(state.get('status'), state.get('status'))}",
        f"**Серий завершено:** {stats['completed']}/{stats['total']}",
        f"**Активных пар:** {stats['active']}"
        + (f" · **Споров:** {stats['disputed']}" if stats["disputed"] else ""),
    ]
    b = state.get("bracket") or {}
    if b.get("champion"):
        lines.append(f"\n**🏆 Чемпион: "
                     f"{team_display_name(state, b['champion'])}**")
    return lines


def bracket_blocks(state: dict) -> list[tuple[str, list[str]]]:
    """(title, lines) блоки для упаковки в embeds.

    Порядок: заголовок+статистика, затем WINNERS / LOSERS / GRAND FINAL.
    Матчи внутри блока — в порядке создания (история раундов)."""
    b = state.get("bracket") or {}
    matches = b.get("matches") or []
    blocks: list[tuple[str, list[str]]] = [
        ("r!z3 CASH CUP · TOURNAMENT BRACKET", _header_lines(state))]
    for key in ("W", "L", "GF"):
        group = [m for m in matches if m.get("bracket") == key]
        if not group:
            continue
        lines = [_match_line(state, m) for m in group]
        blocks.append((_STAGE_NAMES[key], lines))
    if not matches:
        blocks.append(("Сетка", ["Матчи ещё не сформированы."]))
    return blocks


def bracket_embeds(state: dict) -> list[discord.Embed]:
    """Up to MAX_EMBEDS embeds; каждая — отдельный блок/чанк строк.

    Никогда не обрезает строки молча: при переполнении открывается
    новый embed с продолжением заголовка."""
    embeds: list[discord.Embed] = []
    color = 0x26262A
    if (state.get("bracket") or {}).get("champion"):
        color = 0x2ECC71
    elif state.get("status") == "running":
        color = 0x5865F2
    first = True
    overflow = False
    for title, lines in bracket_blocks(state):
        chunk: list[str] = []
        size = 0
        chunks: list[list[str]] = []
        for ln in lines:
            add = len(ln) + 1
            if chunk and size + add > EMBED_DESC_LIMIT:
                chunks.append(chunk)
                chunk, size = [], 0
            chunk.append(ln)
            size += add
        if chunk:
            chunks.append(chunk)
        for ci, part in enumerate(chunks):
            if len(embeds) >= MAX_EMBEDS:
                # Лимит Discord — 10 embeds. Не добавляем 11-й:
                # фиксируем overflow и показываем явную пометку
                # в пределах лимита (не молчаливое обрезание).
                overflow = True
                break
            t = title if ci == 0 else f"{title} ({ci + 1}/{len(chunks)})"
            e = discord.Embed(title=t,
                              description="\n".join(part)[:4096],
                              color=color)
            if first:
                e.set_footer(text=f"r!z3 CASH CUP · {state.get('tournament_id', '')}:bracket")
                first = False
            embeds.append(e)
        if overflow:
            break
    if overflow:
        if len(embeds) < MAX_EMBEDS:
            embeds.append(discord.Embed(
                title="r!z3 CASH CUP · TOURNAMENT BRACKET",
                description="Часть сетки не поместилась в лимит "
                            "сообщения — все матчи доступны в канале "
                            "карточек матчей.",
                color=color))
        else:
            # Все 10 слотов заняты — дописываем пометку в последний
            # embed, если она влезает в лимит описания.
            last = embeds[-1]
            note = "\n… further matches omitted — see match channel"
            desc = last.description or ""
            if len(desc) + len(note) <= 4096:
                last.description = desc + note
    return embeds or [discord.Embed(
        title="r!z3 CASH CUP · TOURNAMENT BRACKET",
        description="Сетка отсутствует.", color=color)]


def bracket_hash(state: dict) -> str:
    """Стабильный хэш видимого содержимого сетки — для dedup edit."""
    payload = {
        "status": state.get("status"),
        "champion": (state.get("bracket") or {}).get("champion"),
        "matches": [
            {k: m.get(k) for k in
             ("id", "a", "b", "bo", "score", "status", "winner", "bracket",
              "checkins", "room_id", "games", "dispute")}
            for m in (state.get("bracket") or {}).get("matches", [])],
    }
    # room_id влияет на отображение «комната выдана» в строках? Нет —
    # в строках сетки комната не показывается, но хэш дешевле держать
    # полным по видимым полям; room_id исключаем, т.к. не рендерится.
    for m in payload["matches"]:
        m.pop("room_id", None)
    return hashlib.sha1(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


# ---------- карточка матча ----------

def _lineup_nicks(state: dict, team: dict | None) -> list[str]:
    """FF-ники основного состава (team.lineup) в порядке основы."""
    if not team:
        return []
    members = {p.get("discord_id"): p for p in team.get("members", [])}
    out = []
    for uid in team.get("lineup") or []:
        p = members.get(uid)
        nick = safe_text((p or {}).get("nick") or "", limit=40)
        out.append(nick or "FF-ник не указан")
    if not out:
        # Основа не задана — показываем состав как есть, честно помечая.
        for p in team.get("members", [])[:5]:
            nick = safe_text(p.get("nick") or "", limit=40)
            out.append(nick or "FF-ник не указан")
    return out[:5]


def _team_block(state: dict, team_id: str | None, label: str) -> str:
    t = _team(state, team_id)
    if not t:
        return f"**{label} — TBD**\nОжидание соперника."
    lines = [f"**{label} — {safe_text(t['name'], limit=80)}**",
             f"Капитан: <@{t.get('captain') or 0}>",
             "",
             "Основной состав:"]
    nicks = _lineup_nicks(state, t)
    if nicks:
        lines += [f"{i}. `{n}`" for i, n in enumerate(nicks, 1)]
    else:
        lines.append("Состав не указан.")
    return "\n".join(lines)


def _match_status_lines(state: dict, m: dict) -> list[str]:
    lines = ["**СТАТУС МАТЧА**",
             f"Готовность: {len(m.get('checkins') or [])}/2 капитана"]
    if m.get("room_id"):
        lines.append("Комната: выдана")
    else:
        lines.append("Комната: ожидается")
    status = m.get("status")
    if status == "completed":
        sa, sb = (m.get("score") or [0, 0])[:2]
        winner = team_display_name(state, m.get("winner"))
        loser = team_display_name(
            state, m.get("b") if m.get("winner") == m.get("a")
            else m.get("a"))
        lines += ["",
                  "✅ **MATCH COMPLETED**",
                  f"**{winner}** {sa}:{sb} {loser}",
                  f"Победитель: **{winner}**",
                  f"Проигравший: **{loser}**"]
        nxt = {"W": "Winners Bracket", "L": "Losers Bracket",
               "GF": "Grand Final"}.get(m.get("bracket"), "")
        if nxt:
            lines.append(f"Следующий этап: {nxt}")
        ev = m.get("evidence") or ""
        if ev.startswith(_EVIDENCE_PREFIXES):
            lines.append(f"[Доказательство]({ev})")
    elif status == "disputed":
        lines += ["", "⚠️ **DISPUTED — РЕЗУЛЬТАТ ОЖИДАЕТ РЕШЕНИЯ**"]
        reason = safe_text((m.get("dispute") or {}).get("reason") or "",
                           limit=150)
        if reason:
            lines.append(f"Причина: {reason}")
    elif status == "void":
        lines += ["", "⛔ Матч завершён технически (дисквалификации)."]
    else:
        lines.append("Результат: не определён")
        if m.get("bracket") == "GF" or m.get("games"):
            sa, sb = (m.get("score") or [0, 0])[:2]
            lines.append(f"Счёт серии: {sa}:{sb}")
    return lines


def match_card(state: dict, m: dict) -> dict[str, Any]:
    """Полное содержимое карточки матча.

    Возвращает {content, embed, hash, ping_ids}:
    - content — пинг капитанов ТОЛЬКО при первом создании;
    - embed — embed карточки;
    - hash — для dedup обновлений;
    - ping_ids — discord_id капитанов для allowed_mentions.
    """
    teams = {t["id"]: t for t in state.get("teams", [])}
    ta, tb = teams.get(m.get("a")), teams.get(m.get("b"))
    cap_a = (ta or {}).get("captain")
    cap_b = (tb or {}).get("captain")
    ping_ids = [int(x) for x in (cap_a, cap_b) if x]

    stage = _STAGE_NAMES.get(m.get("bracket"), m.get("bracket", ""))
    embed = discord.Embed(
        title=f"r!z3 CASH CUP · MATCH {m['id']}",
        description=(f"**{stage} · BO{m.get('bo', 1)}**\n\n"
                     + _team_block(state, m.get("a"), "КОМАНДА A") + "\n\n"
                     + _team_block(state, m.get("b"), "КОМАНДА B")),
        color=0x5865F2)
    embed.add_field(name="\u200b",
                    value="\n".join(_match_status_lines(state, m)),
                    inline=False)
    embed.set_footer(text=f"r!z3 CASH CUP · {state.get('tournament_id', '')}:match:{m['id']}")

    status = m.get("status")
    color_status = {"completed": "завершён", "disputed": "СПОР",
                    "void": "технический", "ready": "ожидание"}.get(
                        status, status or "?")
    payload = {
        "mid": m["id"], "bracket": m.get("bracket"), "bo": m.get("bo"),
        "a": m.get("a"), "b": m.get("b"),
        "score": m.get("score"), "status": status, "winner": m.get("winner"),
        "checkins": m.get("checkins"), "room": bool(m.get("room_id")),
        "games": m.get("games"), "dispute": bool(m.get("dispute")),
        "evidence": m.get("evidence"),
        "lineups": {tid: _lineup_nicks(state, t) for tid, t in
                    ((m.get("a"), ta), (m.get("b"), tb)) if t},
    }
    h = hashlib.sha1(json.dumps(payload, sort_keys=True,
                                default=str).encode()).hexdigest()
    content = None
    if ping_ids:
        content = (f"<@{cap_a}> <@{cap_b}>\n"
                   "⚔️ **CASH CUP — ВАШ МАТЧ ГОТОВ**\n"
                   "Ваш матч сформирован. Проверьте соперников и составы, "
                   "затем подтвердите готовность.")
    return {"content": content, "embed": embed, "hash": h,
            "ping_ids": ping_ids, "status_ru": color_status}
