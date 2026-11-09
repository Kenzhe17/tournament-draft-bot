"""Вычисление рекордов Зала славы.

Все показатели берутся из реальных источников: player_stats (PG),
match_history (PG), elo_ratings (PG, Ultimatum), user_balance/
user_bank (PG), banner_voice_daily (PG), rep_invites (PG),
br_tournaments.json, player_inventory.json (через сторы).

Эпоха статистики = данные после последнего /admin resetprogress:
reset удаляет match_history, player_stats-прогресс и hof_records,
поэтому старые рекорды не возвращаются.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from services.hall_of_fame import repository

logger = logging.getLogger(__name__)

MIN_GAMES = 10  # минимум матчей для avg/ratio-рекордов

CATEGORIES = [
    ("combat", "Боевые рекорды"),
    ("results", "Результаты матчей"),
    ("rating", "Рейтинг"),
    ("tournaments", "Турниры"),
    ("ultimatum", "Ultimatum"),
    ("economy", "Экономика"),
    ("community", "Сообщество"),
]


@dataclass
class Record:
    title: str
    value: str = ""
    user_id: int | None = None
    user_name: str = ""
    context: str = ""
    available: bool = True  # False — данных для рекорда нет в принципе


@dataclass
class CategoryData:
    key: str
    title: str
    records: list[Record] = field(default_factory=list)


def _f_date(dt) -> str:
    if not dt:
        return ""
    try:
        return dt.strftime("%d.%m.%Y")
    except Exception:
        return ""


def _fmt_hours(seconds: int) -> str:
    h, m = divmod(int(seconds) // 60, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}д {h}ч"
    return f"{h}ч {m}м" if h else f"{m}м"


def _match_ctx(row: dict | None) -> str:
    if not row:
        return ""
    parts = []
    mt = row.get("match_type")
    if mt:
        parts.append({"final": "финал", "semifinal": "полуфинал",
                      "qualifier": "отбор"}.get(mt, mt))
    d = _f_date(row.get("date"))
    if d:
        parts.append(d)
    return " · ".join(parts)


async def _peak_elo(guild_id: int, current_max: int, user_id: int) -> tuple[int, int]:
    """Исторический пик ELO: max(сохранённый пик, текущий максимум)."""
    try:
        from storage.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hof_records (
                    guild_id BIGINT NOT NULL,
                    record_key TEXT NOT NULL,
                    user_id BIGINT NOT NULL,
                    value BIGINT NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    PRIMARY KEY (guild_id, record_key)
                )
                """)
            row = await conn.fetchrow(
                "SELECT user_id, value FROM hof_records "
                "WHERE guild_id=$1 AND record_key='peak_elo'", guild_id)
            stored_val = int(row["value"]) if row else 0
            stored_uid = int(row["user_id"]) if row else 0
            if current_max > stored_val:
                await conn.execute(
                    """
                    INSERT INTO hof_records (guild_id, record_key, user_id, value)
                    VALUES ($1,'peak_elo',$2,$3)
                    ON CONFLICT (guild_id, record_key)
                    DO UPDATE SET user_id=$2, value=$3, updated_at=now()
                    """, guild_id, user_id, current_max)
                return current_max, user_id
            return stored_val, stored_uid
    except Exception:
        logger.exception("hof peak_elo failed")
        return current_max, user_id


async def compute_all(guild_id: int) -> dict[str, CategoryData]:
    """Полный пересчёт всех категорий. Вызывается через кэш (TTL)."""
    from storage.player_stats_store import player_stats_store

    stats = [
        p for p in await player_stats_store.get_all(guild_id)
        if p.user_id > 0
    ]
    by_id = {p.user_id: p for p in stats}

    agg = await repository.match_aggregates(guild_id)
    econ = await repository.economy_leaders(guild_id)
    voice = await repository.voice_leader(guild_id)
    invites = await repository.invite_leader(guild_id)
    ulti = await repository.ultimatum_records()
    br = await repository.br_wins_leader(guild_id)

    # --- helpers ---
    def rec_for(uid: int | None, name: str = "") -> tuple[int | None, str]:
        if uid and uid in by_id:
            return uid, by_id[uid].name or name
        return uid, name

    def stat_rec(p, title, value, ctx="") -> Record:
        return Record(title=title, value=value, user_id=p.user_id,
                      user_name=p.name, context=ctx)

    eligible = [p for p in stats if p.games >= MIN_GAMES]

    cats: dict[str, CategoryData] = {k: CategoryData(k, t) for k, t in CATEGORIES}

    # ======================= 1. БОЕВЫЕ =======================
    c = cats["combat"]

    if agg.get("top_kills"):
        r = agg["top_kills"]
        uid, nm = rec_for(int(r["user_id"]))
        c.records.append(Record(
            "Больше всего убийств за матч",
            f"{int(r['kills'])} KILLS", uid, nm, _match_ctx(r)))
    else:
        c.records.append(Record("Больше всего убийств за матч", available=False))

    if eligible:
        p = max(eligible, key=lambda x: x.avg_kills)
        c.records.append(stat_rec(p, "Наибольший AVG Kills",
                                  f"{p.avg_kills:.2f}", f"{p.games} матчей"))
        p = max(eligible, key=lambda x: x.kd_ratio)
        c.records.append(stat_rec(p, "Лучший K/D",
                                  f"{p.kd_ratio:.2f}", f"{p.games} матчей"))
        p = max(eligible, key=lambda x: x.kda_ratio)
        c.records.append(stat_rec(p, "Лучший KDA",
                                  f"{p.kda_ratio:.2f}", f"{p.games} матчей"))
        p = max(eligible, key=lambda x: x.avg_damage)
        c.records.append(stat_rec(p, "Наибольший AVG Damage",
                                  f"{p.avg_damage:.0f}", f"{p.games} матчей"))
    else:
        for t in ("Наибольший AVG Kills", "Лучший K/D", "Лучший KDA",
                  "Наибольший AVG Damage"):
            c.records.append(Record(t, available=False))

    def match_row_rec(title: str, row_key: str, val_key: str,
                      unit: str) -> None:
        r = agg.get(row_key)
        if r:
            uid, nm = rec_for(int(r["user_id"]))
            c.records.append(Record(
                title, f"{int(r[val_key])} {unit}",
                uid, nm, _match_ctx(r)))
        else:
            c.records.append(Record(title, available=False))

    match_row_rec("Больше всего ассистов за матч", "top_assists",
                  "assists", "ASSISTS")
    match_row_rec("Максимальный урон за матч", "top_damage",
                  "damage", "DMG")

    if stats:
        p = max(stats, key=lambda x: x.total_kills)
        c.records.append(stat_rec(p, "Больше всего убийств за всё время",
                                  f"{p.total_kills} KILLS", f"{p.games} матчей"))
        p = max(stats, key=lambda x: x.total_damage)
        c.records.append(stat_rec(p, "Наибольший суммарный урон",
                                  f"{p.total_damage} DMG", f"{p.games} матчей"))

    # ======================= 2. РЕЗУЛЬТАТЫ =======================
    c = cats["results"]
    if eligible:
        p = max(eligible, key=lambda x: x.win_rate)
        c.records.append(stat_rec(p, "Лучший WinRate",
                                  f"{p.win_rate:.1f}%", f"{p.games} матчей"))
    else:
        c.records.append(Record("Лучший WinRate", available=False))
    if stats:
        p = max(stats, key=lambda x: x.wins)
        c.records.append(stat_rec(p, "Больше всего побед",
                                  f"{p.wins} WINS", f"{p.games} матчей"))
        p = max(stats, key=lambda x: x.games)
        c.records.append(stat_rec(p, "Больше всего матчей",
                                  f"{p.games} MATCHES"))
        p = max(stats, key=lambda x: x.best_win_streak)
        if p.best_win_streak > 0:
            c.records.append(stat_rec(p, "Самая длинная серия побед",
                                      f"{p.best_win_streak} ПОДРЯД"))
        else:
            c.records.append(Record("Самая длинная серия побед", available=False))
        p = max(stats, key=lambda x: x.best_loss_streak)
        if p.best_loss_streak > 0:
            c.records.append(stat_rec(p, "Самая длинная серия поражений",
                                      f"{p.best_loss_streak} ПОДРЯД"))
        else:
            c.records.append(Record("Самая длинная серия поражений",
                                    available=False))

    # ======================= 3. РЕЙТИНГ =======================
    c = cats["rating"]
    if stats:
        p = max(stats, key=lambda x: x.elo)
        peak_val, peak_uid = await _peak_elo(guild_id, p.elo, p.user_id)
        if peak_uid in by_id:
            pp = by_id[peak_uid]
            ctx = "исторический максимум" if peak_val > p.elo else ""
            c.records.append(Record(
                "Максимальный ELO", f"{peak_val:,} ELO",
                peak_uid, pp.name, ctx))
        else:
            c.records.append(Record(
                "Максимальный ELO", f"{peak_val:,} ELO", peak_uid, ""))
        if eligible:
            p = max(eligible, key=lambda x: x.skill_rating)
            c.records.append(stat_rec(
                p, "Наивысший Skill Rating", f"{p.skill_rating:.1f} SR",
                f"{p.games} матчей"))
        else:
            c.records.append(Record("Наивысший Skill Rating", available=False))
        p = max(stats, key=lambda x: x.level)
        c.records.append(stat_rec(p, "Самый высокий уровень",
                                  f"LVL {p.level}", f"{p.xp_to_next_level} XP до след."))
    else:
        for t in ("Максимальный ELO", "Наивысший Skill Rating",
                  "Самый высокий уровень"):
            c.records.append(Record(t, available=False))

    if agg.get("top_elo_gain"):
        r = agg["top_elo_gain"]
        uid, nm = rec_for(int(r["user_id"]))
        c.records.append(Record(
            "Наибольший прирост ELO за матч", f"+{int(r['elo_change'])} ELO",
            uid, nm, _match_ctx(r)))
    else:
        c.records.append(Record("Наибольший прирост ELO за матч",
                                available=False))

    # ======================= 4. ТУРНИРЫ =======================
    c = cats["tournaments"]
    champs = agg.get("championships", {})
    if champs:
        uid = max(champs, key=champs.get)
        u, nm = rec_for(uid)
        c.records.append(Record("Больше всего выигранных турниров",
                                f"{champs[uid]} ТИТУЛОВ", u, nm))
    else:
        c.records.append(Record("Больше всего выигранных турниров",
                                available=False))
    if stats:
        p = max(stats, key=lambda x: x.finals)
        if p.finals > 0:
            c.records.append(stat_rec(p, "Больше всего выходов в финал",
                                      f"{p.finals} ФИНАЛОВ"))
        else:
            c.records.append(Record("Больше всего выходов в финал",
                                    available=False))
        p = max(stats, key=lambda x: x.tournament_participations)
        if p.tournament_participations > 0:
            c.records.append(stat_rec(
                p, "Больше всего участий",
                f"{p.tournament_participations} УЧАСТИЙ"))
        else:
            c.records.append(Record("Больше всего участий", available=False))
    mcounts = agg.get("match_counts", {})
    if mcounts:
        uid = max(mcounts, key=mcounts.get)
        u, nm = rec_for(uid)
        c.records.append(Record("Больше всего турнирных матчей",
                                f"{mcounts[uid]} МАТЧЕЙ", u, nm))
    else:
        c.records.append(Record("Больше всего турнирных матчей",
                                available=False))
    if br:
        uid, cnt = br
        u, nm = rec_for(uid)
        c.records.append(Record("Больше всего побед в Battle Royale",
                                f"{cnt} ПОБЕД", u, nm))
    else:
        c.records.append(Record("Больше всего побед в Battle Royale",
                                available=False))

    # ======================= 5. ULTIMATUM =======================
    c = cats["ultimatum"]
    if ulti.get("top_elo"):
        r = ulti["top_elo"]
        uid = int(r["discord_id"])
        u, nm = rec_for(uid, r.get("username") or "")
        c.records.append(Record("Максимальный Ultimatum ELO",
                                f"{int(r['peak_elo']):,} ELO", u, nm))
    else:
        c.records.append(Record("Максимальный Ultimatum ELO", available=False))
    if ulti.get("top_wins"):
        r = ulti["top_wins"]
        uid = int(r["discord_id"])
        u, nm = rec_for(uid, r.get("username") or "")
        c.records.append(Record("Больше всего побед в дуэлях",
                                f"{int(r['wins'])} ПОБЕД", u, nm))
    else:
        c.records.append(Record("Больше всего побед в дуэлях",
                                available=False))
    if ulti.get("top_winrate"):
        r = ulti["top_winrate"]
        uid = int(r["discord_id"])
        u, nm = rec_for(uid, r.get("username") or "")
        wr = int(r["wins"]) / max(1, int(r["rated_matches"])) * 100
        c.records.append(Record("Лучший WinRate Ultimatum",
                                f"{wr:.1f}%", u, nm,
                                f"{int(r['rated_matches'])} дуэлей"))
    else:
        c.records.append(Record("Лучший WinRate Ultimatum", available=False))
    if ulti.get("top_duels"):
        r = ulti["top_duels"]
        uid = int(r["discord_id"])
        u, nm = rec_for(uid, r.get("username") or "")
        c.records.append(Record("Больше всего сыгранных дуэлей",
                                f"{int(r['rated_matches'])} ДУЭЛЕЙ", u, nm))
    else:
        c.records.append(Record("Больше всего сыгранных дуэлей",
                                available=False))

    # ======================= 6. ЭКОНОМИКА =======================
    c = cats["economy"]
    wealth = econ.get("wealth", {})
    wealth = {u: v for u, v in wealth.items() if u >= repository.MIN_REAL_USER_ID}
    if wealth:
        uid = max(wealth, key=wealth.get)
        u, nm = rec_for(uid)
        c.records.append(Record("Богатейший участник",
                                f"{wealth[uid]:,} МОНЕТ", u, nm,
                                "баланс + банк"))
    else:
        c.records.append(Record("Богатейший участник", available=False))

    inv_counts: dict[int, int] = {}
    try:
        from storage.shop_store import inventory_store
        for key, items in inventory_store._inventory.items():
            try:
                gid_s, uid_s = key.split(":", 1)
                if int(gid_s) == guild_id and int(uid_s) > 0:
                    inv_counts[int(uid_s)] = len(items)
            except Exception:
                continue
    except Exception:
        pass
    if inv_counts:
        uid = max(inv_counts, key=inv_counts.get)
        if inv_counts[uid] > 0:
            u, nm = rec_for(uid)
            c.records.append(Record("Самый большой инвентарь",
                                    f"{inv_counts[uid]} ПРЕДМЕТОВ", u, nm))
        else:
            c.records.append(Record("Самый большой инвентарь",
                                    available=False))
    else:
        c.records.append(Record("Самый большой инвентарь", available=False))

    # ======================= 7. СООБЩЕСТВО =======================
    c = cats["community"]
    if stats:
        p = max(stats, key=lambda x: x.reputation)
        if p.reputation > 0:
            c.records.append(stat_rec(p, "Наибольшая репутация",
                                      f"{p.reputation} REP"))
        else:
            c.records.append(Record("Наибольшая репутация", available=False))
    else:
        c.records.append(Record("Наибольшая репутация", available=False))
    if voice:
        uid, secs = voice
        u, nm = rec_for(uid)
        c.records.append(Record("Больше всего времени в войсе",
                                _fmt_hours(secs), u, nm))
    else:
        c.records.append(Record("Больше всего времени в войсе",
                                available=False))
    if invites:
        uid, cnt = invites
        u, nm = rec_for(uid)
        c.records.append(Record("Больше всего подтверждённых приглашений",
                                f"{cnt} ПРИГЛАШЕНИЙ", u, nm))
    else:
        c.records.append(Record("Больше всего подтверждённых приглашений",
                                available=False))
    return cats
