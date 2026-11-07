"""Конфигурация системы ELO турниров (FACEIT-style, ТЗ r!z3).

Все балансировочные значения новой системы ELO — только здесь.
"""

ELO_CONFIG = {
    # Масштаб разницы Skill Rating для вероятности победы
    "skill_rating_scale": 2.0,

    # Жёсткие границы изменения ELO за матч
    "min_match_elo": 20,
    "max_match_elo": 30,

    # Веса индивидуальной статистики (Performance Score)
    "kill_weight": 1.0,
    "assist_weight": 0.5,
    "death_weight": 0.75,
    "damage_divisor": 2000,

    # Skill Rating: средний Performance Score за игру → шкала 1.0-10.0
    "skill_rating_min_score": -5.0,
    "skill_rating_max_score": 10.0,

    # Поправки за индивидуальный вклад (Performance Score) относительно
    # всех игроков матча: лучший / выше среднего / средний / ниже среднего /
    # худший. Одинаковый score → одинаковая поправка.
    "performance_modifiers": {
        "best": 5,
        "above_average": 3,
        "average": 0,
        "below_average": -3,
        "worst": -5,
    },

    # Защита игроков вне TOP-N ELO-лидерборда
    "rank_protection": {
        "enabled": True,
        "top_limit": 10,
        "bonus": 3,
    },
}
