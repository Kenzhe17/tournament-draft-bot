"""Нормализация и форматирование данных профиля Free Fire для /ff info.

Чистые функции без discord.py-логики: безопасный текст, числа, даты,
резолв ранговых ID/очков в названия тиров, каталог предметов.

Ранги: API отдаёт внутренние числовые ID (rank/csRank/maxRank/csMaxRank),
которые меняются от сезона к сезону и не имеют публичной таблицы.
Название тира вычисляется из рейтинговых очков (BR) / звёзд (CS)
по таблице порогов; сам ID показывается как есть.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

from services import ff_catalog

# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------

_MD_ESCAPE_RE = re.compile(r"([\\`*_~|>])")
_ZWSP_MENTION_RE = re.compile(r"<(?=[#@!:])")


def safe_text(value: Any, *, limit: int | None = None) -> str:
    """Экранировать markdown и нейтрализовать упоминания/пинги.

    Используется для никнеймов, подписей и любых строк из API.
    """
    text = "" if value is None else str(value)
    text = _MD_ESCAPE_RE.sub(r"\\\1", text)
    # Ломаем mention/emoji-синтаксис zero-width пробелом после '<'.
    text = _ZWSP_MENTION_RE.sub("<\u200b", text)
    text = text.replace("@everyone", "@\u200beveryone").replace(
        "@here", "@\u200bhere"
    )
    if limit is not None and len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def fmt_num(value: Any) -> str:
    """Число с разделителем тысяч; нечисловое — '—'."""
    if value is None or value == "":
        return "—"
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        try:
            return f"{float(value):,.1f}".replace(",", " ")
        except (TypeError, ValueError):
            return str(value)


def fmt_ts(ts: Any) -> str:
    """Unix-timestamp -> Discord-локализованная дата <t:...:F>."""
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    return f"<t:{ts}:F>"


# ---------------------------------------------------------------------------
# Ранги
# ---------------------------------------------------------------------------

# Пороги BR-ранга по рейтинговым очкам и CS-ранга по звёздам.
# Источник: публичный маппер сообщества (DarkForceFREEFIRE/FreeFire_Info_Web,
# js/mapper.js) — таблица старых сезонов;Garena меняет пороги между сезонами,
# поэтому название считается «по очкам», а rank ID показывается отдельно.
_BR_TIERS: list[tuple[int, str, str]] = [
    # (min_points, tier_name_ru, badge_file) — границы как в mapper.js
    (0, "Бронза I–III", "B (1).png"),
    (1300, "Серебро I", "S (1).png"),
    (1400, "Серебро II", "S (2).png"),
    (1500, "Серебро III", "S (3).png"),
    (1600, "Золото I", "G (1).png"),
    (1725, "Золото II", "G (2).png"),
    (1850, "Золото III", "G (3).png"),
    (1975, "Золото IV", "G (4).png"),
    (2100, "Платина I", "P (1).png"),
    (2225, "Платина II", "P (2).png"),
    (2350, "Платина III", "P (3).png"),
    (2474, "Платина IV", "P (4).png"),
    (2599, "Платина V", "P (5).png"),
    (2749, "Алмаз I", "D (1).png"),
    (2899, "Алмаз II", "D (2).png"),
    (3049, "Алмаз III", "D (3).png"),
    (3199, "Алмаз IV", "D (4).png"),
    (3349, "Алмаз V", "D (5).png"),
    (3499, "Героик", "H (1).png"),
    (3799, "Героик", "H (2).png"),
    (4299, "Элитный Героик", "H (3).png"),
    (4899, "Элитный Героик", "H (4).png"),
    (5499, "Элитный Героик", "H (5).png"),
    (6299, "Мастер", "M (1).png"),
    (7099, "Мастер", "M (2).png"),
    (7999, "Элитный Мастер", "M (3).png"),
    (8999, "Элитный Мастер", "M (4).png"),
    (9999, "Грандмастер", "GM (1).png"),
]

# (min_stars, tier_name_ru, badge_file); GM VI — всё, что > 786 звёзд.
_CS_TIERS: list[tuple[int, str, str]] = [
    (0, "Бронза I", "B1.png"),
    (4, "Бронза II", "B2.png"),
    (7, "Бронза III", "B3.png"),
    (10, "Серебро I", "S1.png"),
    (14, "Серебро II", "S2.png"),
    (18, "Серебро III", "S3.png"),
    (22, "Золото I", "G1.png"),
    (26, "Золото II", "G2.png"),
    (31, "Золото III", "G3.png"),
    (34, "Золото IV", "G4.png"),
    (38, "Платина I", "P1.png"),
    (43, "Платина II", "P2.png"),
    (48, "Платина III", "P3.png"),
    (53, "Платина IV", "P4.png"),
    (58, "Платина V", "P5.png"),
    (63, "Алмаз I", "D1.png"),
    (68, "Алмаз II", "D2.png"),
    (73, "Алмаз III", "D3.png"),
    (78, "Алмаз IV", "D4.png"),
    (83, "Алмаз V", "D5.png"),
    (88, "Героик", "CS-H1.png"),
    (113, "Элитный Героик", "CS-H2.png"),
    (137, "Мастер", "M.png"),
    (187, "Элитный Мастер", "EM.png"),
    (287, "Грандмастер I", "CSGM1.png"),
    (387, "Грандмастер II", "CSGM2.png"),
    (487, "Грандмастер III", "CSGM3.png"),
    (587, "Грандмастер IV", "CSGM4.png"),
    (687, "Грандмастер V", "CSGM5.png"),
]

_RANK_BADGE_BASE = (
    "https://raw.githubusercontent.com/DarkForceFREEFIRE/"
    "FreeFire_Info_Web/main/Free%20Fire%20Rank"
)


def _tier_name(tiers: list[tuple[int, str, str]], value: float) -> tuple[str, str]:
    """(название, badge_file) для значения очков/звёзд."""
    name, badge = "—", ""
    for min_v, tier_name, file_name in tiers:
        if value >= min_v:
            name, badge = tier_name, file_name
    return name, badge


def br_rank(points: Any) -> dict[str, Any]:
    """BR-ранг по рейтинговым очкам."""
    try:
        pts = float(points)
    except (TypeError, ValueError):
        return {"name": None, "badge": None}
    name, badge = _tier_name(_BR_TIERS, pts)
    return {
        "name": name,
        "badge": f"{_RANK_BADGE_BASE}/BR/{quote(badge)}" if badge else None,
        "points": int(pts),
    }


def cs_rank(stars: Any) -> dict[str, Any]:
    """CS-ранг по звёздам."""
    try:
        s = float(stars)
    except (TypeError, ValueError):
        return {"name": None, "badge": None}
    if s >= 787:
        name, badge = "Грандмастер VI", "CSGM6.png"
    else:
        name, badge = _tier_name(_CS_TIERS, s)
    return {
        "name": name,
        "badge": f"{_RANK_BADGE_BASE}/CS/{quote(badge)}" if badge else None,
        "stars": int(s),
    }


def rank_label(rank_id: Any, resolved: dict[str, Any] | None) -> str:
    """Строка ранга: название (по очкам) + исходный ID при наличии."""
    parts = []
    if resolved and resolved.get("name"):
        parts.append(str(resolved["name"]))
    try:
        rid = int(rank_id)
    except (TypeError, ValueError):
        rid = None
    if rid is not None:
        parts.append(f"ID {rid}")
    if not parts:
        return "—"
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Нормализация ответа API
# ---------------------------------------------------------------------------

def get(d: dict[str, Any], *keys: str) -> Any:
    cur: Any = d
    for key in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def normalize_profile(result: dict[str, Any]) -> dict[str, Any]:
    """Разложить сырой ответ /freefireinfo/bhau по блокам."""
    basic = result.get("basicInfo") or {}
    return {
        "basic": basic,
        "social": result.get("socialInfo") or {},
        "profile": result.get("profileInfo") or {},
        "pet": result.get("petInfo") or {},
        "clan": result.get("clanBasicInfo") or result.get("clanInfo") or {},
        "guild": result.get("guildBasicInfo") or result.get("guildInfo") or {},
        "credit": result.get("creditScoreInfo") or {},
        "diamond": result.get("diamondCostRes") or {},
        "captain": result.get("captainBasicInfo") or {},
    }


def basic_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Часто используемые поля basicInfo с безопасными значениями."""
    basic = data.get("basic") or {}
    return {
        "nickname": safe_text(basic.get("nickname") or "Unknown", limit=80),
        "uid": basic.get("accountId") or "",
        "level": basic.get("level"),
        "exp": basic.get("exp"),
        "liked": basic.get("liked"),
        "badge_cnt": basic.get("badgeCnt"),
        "region": basic.get("region"),
        "season": basic.get("seasonId"),
        "version": basic.get("releaseVersion"),
        "create_at": basic.get("createAt"),
        "last_login": basic.get("lastLoginAt"),
        "rank_id": basic.get("rank"),
        "max_rank_id": basic.get("maxRank"),
        "rank_points": basic.get("rankingPoints"),
        "cs_rank_id": basic.get("csRank"),
        "cs_max_rank_id": basic.get("csMaxRank"),
        "cs_points": basic.get("csRankingPoints"),
    }


# ---------------------------------------------------------------------------
# Предметы / экипировка
# ---------------------------------------------------------------------------

CLOTHES_ORDER = [
    ("Head", "Головной убор"),
    ("Mask", "Маска"),
    ("Facepaint", "Макияж"),
    ("Top", "Верхняя одежда"),
    ("Bottom", "Нижняя одежда"),
    ("Shoe", "Обувь"),
]
_OTHER_CATEGORY = "Прочее"


def item_entry(item_id: Any) -> dict[str, Any]:
    """Описание предмета каталога или fallback для неизвестного ID."""
    item = ff_catalog.get_item(item_id)
    if item:
        return {
            "id": str(item_id),
            "name": safe_text(item.get("name") or f"ID {item_id}", limit=60),
            "type": str(item.get("type") or ""),
            "rarity": str(item.get("rarity") or ""),
            "icon": ff_catalog.icon_url(item.get("itemID", item_id)),
            "known": True,
        }
    return {
        "id": str(item_id),
        "name": f"ID {item_id}",
        "type": "",
        "rarity": "",
        "icon": None,
        "known": False,
    }


def group_clothes(profile_info: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Экипировка, сгруппированная по типу предмета из каталога."""
    clothes = [c for c in (profile_info.get("clothes") or []) if c]
    by_type: dict[str, list[dict[str, Any]]] = {}
    for cid in clothes:
        entry = item_entry(cid)
        by_type.setdefault(entry["type"] or _OTHER_CATEGORY, []).append(entry)

    order = {t: i for i, (t, _) in enumerate(CLOTHES_ORDER)}
    labels = dict(CLOTHES_ORDER)
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for type_name, entries in by_type.items():
        groups.append((labels.get(type_name, type_name or _OTHER_CATEGORY), entries))
    groups.sort(key=lambda g: order.get(next(
        (t for t, _ in CLOTHES_ORDER if labels.get(t) == g[0]), ""), 999))
    return groups


def pet_entry(pet_info: dict[str, Any]) -> dict[str, Any] | None:
    """Питомец: название/иконка из каталога, уровень и EXP."""
    pet_id = pet_info.get("id")
    if not pet_id:
        return None
    entry = item_entry(pet_id)
    return {
        "id": str(pet_id),
        "name": entry["name"],
        "icon": entry["icon"],
        "known": entry["known"],
        "level": pet_info.get("level"),
        "exp": pet_info.get("exp"),
    }


# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------

REGION_NAMES = {
    "RU": "Россия / СНГ",
    "CIS": "Россия / СНГ",
    "SG": "Сингапур / Азия",
    "IND": "Индия",
    "BR": "Бразилия",
    "BD": "Бангладеш",
    "ID": "Индонезия",
    "TH": "Таиланд",
    "TW": "Тайвань",
    "US": "США",
    "NA": "Северная Америка",
    "SAC": "Латинская Америка",
    "ME": "Ближний Восток",
    "PK": "Пакистан",
    "VN": "Вьетнам",
    "EU": "Европа",
}

_MODE_PREFER = {
    "ModePrefer_BR": "Battle Royale",
    "ModePrefer_CS": "Clash Squad",
}


def region_label(code: Any) -> str:
    if not code:
        return "—"
    c = str(code).upper()
    return f"{c} · {REGION_NAMES[c]}" if c in REGION_NAMES else c


def mode_prefer(social: dict[str, Any]) -> str:
    mp = social.get("modePrefer") or ""
    return _MODE_PREFER.get(mp, mp or "—")


def language_label(social: dict[str, Any]) -> str:
    lang = str(social.get("language") or "—")
    lang = lang.removeprefix("Language_")
    return lang.upper() if lang and lang != "—" else "—"


def signature_text(social: dict[str, Any], *, limit: int = 120) -> str:
    sig = social.get("signature")
    if not sig:
        return ""
    return safe_text(str(sig).replace("\n", " ").strip(), limit=limit)
