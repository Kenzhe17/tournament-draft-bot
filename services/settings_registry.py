"""Единый реестр динамических настроек Control Center.

Каждая настройка описывается SettingDef: ключ, тип, значение по
умолчанию, диапазон, область действия (global/guild), требуемое
разрешение панели и runtime_editable.

Правила:
- секреты (DISCORD_TOKEN, DATABASE_URL, OAuth secret, webhook'и)
  в реестр НЕ включаются никогда;
- настройка без подключённого consumer'а помечается
  consumers_pending=True — значение хранится, но помечается
  в API как «ожидает интеграции»;
- настройки, применяемые только к новым сущностям (матч,
  турнир), помечаются applies_to="new_only".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SettingDef:
    key: str
    type: str  # bool | int | float | str | json
    default: Any
    module: str
    description: str = ""
    scope: str = "global"  # global | guild | both
    min: float | None = None
    max: float | None = None
    permission: str = "settings.manage"
    runtime_editable: bool = True
    consumers_pending: bool = False
    applies_to: str = "immediate"  # immediate | new_only
    choices: tuple[Any, ...] | None = None


_REGISTRY: dict[str, SettingDef] = {}


def register(d: SettingDef) -> SettingDef:
    if d.key in _REGISTRY:
        raise ValueError(f"duplicate setting key: {d.key}")
    _REGISTRY[d.key] = d
    return d


def get(key: str) -> SettingDef | None:
    return _REGISTRY.get(key)


def all_defs() -> list[SettingDef]:
    return list(_REGISTRY.values())


def keys() -> list[str]:
    return sorted(_REGISTRY)


def _reg(**kw: Any) -> SettingDef:
    return register(SettingDef(**kw))


# ------------------------------------------------------------------
# Feature flags — централизованное вкл/выкл модулей.
# Политика отключения: не отменяет активные матчи и подтверждённые
# выплаты — блокирует только новые операции (реализуется в
# consumer'ах на этапе интеграции).
# ------------------------------------------------------------------

for _key, _desc in [
    ("feature.tournaments_enabled", "Турниры (создание и участие)"),
    ("feature.matchmaking_enabled", "Ranked Matchmaking (очереди и поиск)"),
    ("feature.minigames_enabled", "Мини-игры"),
    ("feature.shop_enabled", "Магазин и косметика"),
    ("feature.ocr_enabled", "OCR-обработка скриншотов результатов"),
    ("feature.welcome_enabled", "Приветствия новых участников"),
    ("feature.coin_drop_enabled", "Спонтанные ивенты с монетами"),
    ("feature.pet_enabled", "Питомцы"),
    ("feature.betting_enabled", "Ставки на матчи"),
    ("feature.betting_odds_enabled", "Динамические коэффициенты ставок"),
]:
    _reg(
        key=_key, type="bool", default=True, module="feature",
        description=_desc, permission="feature_flags.manage",
    )

# ------------------------------------------------------------------
# ELO / Skill Rating — зеркало utils/elo_config.py::ELO_CONFIG.
# Применяется к новым расчётам; завершённые матчи не пересчитываются.
# ------------------------------------------------------------------

_reg(key="elo.skill_rating_scale", type="float", default=2.0,
     module="elo", min=0.1, max=100.0,
     description="Масштаб разницы Skill Rating для вероятности победы")
_reg(key="elo.min_match_elo", type="int", default=20,
     module="elo", min=0, max=1000,
     description="Минимальное изменение ELO за матч")
_reg(key="elo.max_match_elo", type="int", default=30,
     module="elo", min=0, max=1000,
     description="Максимальное изменение ELO за матч")
_reg(key="elo.kill_weight", type="float", default=1.0,
     module="elo", min=0.0, max=100.0,
     description="Вес убийств в Performance Score")
_reg(key="elo.death_weight", type="float", default=0.75,
     module="elo", min=0.0, max=100.0,
     description="Вес смертей в Performance Score")
for _name, _label in [
    ("best", "лучший"), ("above_average", "выше среднего"),
    ("average", "средний"), ("below_average", "ниже среднего"),
    ("worst", "худший"),
]:
    _default = {"best": 5, "above_average": 3, "average": 0,
                "below_average": -3, "worst": -5}[_name]
    _reg(key=f"elo.performance_{_name}", type="int", default=_default,
         module="elo", min=-100, max=100,
         description=f"Поправка ELO за индивидуальный вклад ({_label})")
_reg(key="elo.rank_protection_enabled", type="bool", default=True,
     module="elo", description="Защита игроков вне TOP-N ELO-лидерборда")
_reg(key="elo.rank_protection_top_limit", type="int", default=10,
     module="elo", min=0, max=1000,
     description="Размер защищаемого TOP-N")
_reg(key="elo.rank_protection_bonus", type="int", default=3,
     module="elo", min=-100, max=100,
     description="Бонус ELO для игроков вне TOP-N")

# ------------------------------------------------------------------
# Matchmaking — веса балансировки и таймауты.
# Применяется только к новым матчам (applies_to=new_only): состав
# уже начатого матча не меняется.
# ------------------------------------------------------------------

_reg(key="matchmaking.balance_weight_sum", type="float", default=0.40,
     module="matchmaking", min=0.0, max=1.0,
     description="Вес суммарного SR команд", applies_to="new_only")
_reg(key="matchmaking.balance_weight_top", type="float", default=0.30,
     module="matchmaking", min=0.0, max=1.0,
     description="Вес топ-игроков команд", applies_to="new_only")
_reg(key="matchmaking.balance_weight_dist", type="float", default=0.20,
     module="matchmaking", min=0.0, max=1.0,
     description="Вес распределения SR", applies_to="new_only")
_reg(key="matchmaking.balance_weight_spread", type="float", default=0.10,
     module="matchmaking", min=0.0, max=1.0,
     description="Вес разброса внутри команд", applies_to="new_only")
_reg(key="matchmaking.balance_sum_tolerance", type="float", default=1.0,
     module="matchmaking", min=0.0, max=100.0,
     description="Допуск минимальной разницы суммарного SR",
     applies_to="new_only")
_reg(key="matchmaking.balance_epsilon", type="float", default=0.001,
     module="matchmaking", min=0.0, max=1.0,
     description="Порог «практически равного» balance_score",
     applies_to="new_only")
_reg(key="matchmaking.ready_timeout_seconds", type="int", default=90,
     module="matchmaking", min=10, max=3600,
     description="Таймаут Ready Check «Найден матч» (сек)")
_reg(key="matchmaking.match_start_retry_cooldown", type="float",
     default=120.0, module="matchmaking", min=0.0, max=86400.0,
     description="Cooldown повторной попытки запуска матча (сек)")

# ------------------------------------------------------------------
# Economy — лимиты и суммы. Денежные операции игроков панелью
# выполняются только через транзакционные сервисы бота.
# ------------------------------------------------------------------

_reg(key="economy.max_balance", type="int", default=100000,
     module="economy", min=1, max=10**12, permission="economy.manage",
     description="Максимальный баланс в монетах")
_reg(key="economy.min_bet", type="int", default=10,
     module="economy", min=0, max=10**9, permission="economy.manage",
     description="Минимальная ставка")
_reg(key="economy.max_bet", type="int", default=1000,
     module="economy", min=1, max=10**12, permission="economy.manage",
     description="Максимальная ставка (обычные игры)")

# ------------------------------------------------------------------
# Reputation 2.0 — суммы и проверки (env REP_* → runtime).
# ------------------------------------------------------------------

_reg(key="reputation.manual_amount", type="int", default=1,
     module="reputation", min=0, max=1000,
     description="REP за ручную выдачу (/rep)")
_reg(key="reputation.manual_cooldown_hours", type="int", default=24,
     module="reputation", min=0, max=8760,
     description="Cooldown ручной выдачи REP (часы)")
_reg(key="reputation.invite_amount", type="int", default=5,
     module="reputation", min=0, max=1000,
     description="REP за приглашение на сервер")
_reg(key="reputation.invite_verification_hours", type="int", default=72,
     module="reputation", min=0, max=87600,
     description="Срок верификации приглашения (часы)")
_reg(key="reputation.boost_amount", type="int", default=10,
     module="reputation", min=0, max=1000,
     description="REP за буст сервера")
_reg(key="reputation.boost_verification_days", type="int", default=7,
     module="reputation", min=0, max=3650,
     description="Срок верификации буста (дни)")
_reg(key="reputation.server_tag_amount", type="int", default=5,
     module="reputation", min=0, max=1000,
     description="REP за серверный тег")
_reg(key="reputation.one_invite_reward_per_invited_user", type="bool",
     default=True, module="reputation",
     description="Одна награда за приглашённого пользователя")
_reg(key="reputation.one_boost_reward_per_user", type="bool",
     default=True, module="reputation",
     description="Одна награда за буст на пользователя")
_reg(key="reputation.one_manual_rep_per_pair", type="bool",
     default=True, module="reputation",
     description="Одна ручная выдача REP на пару")

# ------------------------------------------------------------------
# Турниры — лимиты состава. Применяется к новым регистрациям
# (applies_to=new_only), чтобы не ломать идущие турниры.
# ------------------------------------------------------------------

_reg(key="tournament.max_captains", type="int", default=4,
     module="tournament", min=1, max=100, permission="tournament.manage",
     description="Максимум капитанов в турнире",
     applies_to="new_only")
_reg(key="tournament.max_players_per_circle", type="int", default=4,
     module="tournament", min=1, max=100, permission="tournament.manage",
     description="Максимум игроков в круге",
     applies_to="new_only")
_reg(key="tournament.circles", type="json", default=[2, 3, 4],
     module="tournament", permission="tournament.manage",
     description="Допустимые размеры кругов (JSON-массив)",
     applies_to="new_only")

# ------------------------------------------------------------------
# VAC — блокировки участия в турнирах.
# ------------------------------------------------------------------

_reg(key="vac.max_days", type="int", default=365,
     module="vac", min=1, max=36500, permission="moderation.manage",
     description="Максимальный срок VAC-блокировки (дни)")


# ------------------------------------------------------------------
# Валидация значений по реестру.
# ------------------------------------------------------------------

class SettingValidationError(ValueError):
    pass


def coerce(d: SettingDef, raw: Any) -> Any:
    """Привести и провалидировать значение по типу SettingDef.

    Бросает SettingValidationError при несоответствии.
    JSON-значения из БД приходят уже распарсенными (JSONB→Python).
    """
    if d.type == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            v = raw.strip().lower()
            if v in ("1", "true", "yes", "on"):
                return True
            if v in ("0", "false", "no", "off"):
                return False
        raise SettingValidationError(
            f"{d.key}: expected bool, got {raw!r}"
        )
    if d.type == "int":
        if isinstance(raw, bool):
            raise SettingValidationError(f"{d.key}: expected int")
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, float) and raw.is_integer():
            value = int(raw)
        elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            value = int(raw.strip())
        else:
            raise SettingValidationError(
                f"{d.key}: expected int, got {raw!r}"
            )
    elif d.type == "float":
        if isinstance(raw, bool):
            raise SettingValidationError(f"{d.key}: expected float")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise SettingValidationError(
                f"{d.key}: expected float, got {raw!r}"
            ) from None
    elif d.type == "str":
        if not isinstance(raw, str):
            raise SettingValidationError(f"{d.key}: expected str")
        value = raw
    elif d.type == "json":
        value = raw  # JSONB → произвольный Python-объект
    else:
        raise SettingValidationError(f"{d.key}: unknown type {d.type!r}")

    if d.choices is not None and value not in d.choices:
        raise SettingValidationError(
            f"{d.key}: {value!r} not in {d.choices!r}"
        )
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if d.min is not None and value < d.min:
            raise SettingValidationError(
                f"{d.key}: {value} < min {d.min}"
            )
        if d.max is not None and value > d.max:
            raise SettingValidationError(
                f"{d.key}: {value} > max {d.max}"
            )
    return value


def describe() -> list[dict[str, Any]]:
    """Сериализация реестра для API."""
    return [
        {
            "key": d.key,
            "type": d.type,
            "default": d.default,
            "module": d.module,
            "description": d.description,
            "scope": d.scope,
            "min": d.min,
            "max": d.max,
            "permission": d.permission,
            "runtime_editable": d.runtime_editable,
            "consumers_pending": d.consumers_pending,
            "applies_to": d.applies_to,
            "choices": list(d.choices) if d.choices else None,
        }
        for d in all_defs()
    ]
