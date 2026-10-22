"""Централизованный каталог питомцев."""

from typing import Any, Dict, Optional

# Кастомные эмодзи проекта (используем существующие из config)
EMOJI_STAR = "a_star"
EMOJI_SPARKLE = "a_sparkle"
EMOJI_ARROW = "white_arrow"
EMOJI_DOT = "white_dot"
EMOJI_MONEY = "money"

# Централизованный словарь каталога
PETS_CATALOG: Dict[str, Dict[str, Any]] = {
    "golden_fox": {
        "id": "golden_fox",
        "name": "Золотой лис",
        "title": "МАГАЗИН | ЗОЛОТОЙ ЛИС",
        "rarity": "Обычный",
        "rarity_code": "common",
        "price": 3000,
        "color": 0x2ECC71,
        "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560909867575672842/Eight_Fantasy_Creature_Portraits-removebg-preview.png?ex=6ad46e18&is=6ad31c98&hm=abe44e294e68eb90b03f096fe3cbe3a933912797b24850d7fd5a79cdf09fdf5d&=&format=webp&quality=lossless",
        "description": (
            "Удачливый спутник для тех, кто часто открывает кейсы и переводит монеты. "
            "Помогает увеличивать доход и сокращать расходы на переводы."
        ),
        "abilities": {
            1: {
                "name": "Золотая лапа",
                "max_val": 15,
                "unit": "%",
                "desc_template": "Увеличивает количество монет, получаемых из кейсов, на {val}%."
            },
            2: {
                "name": "Щедрый перевод",
                "max_val": 25,
                "unit": "%",
                "desc_template": "Уменьшает комиссию /pay на {val}% от её исходного размера."
            }
        }
    },
    "scout_fox": {
        "id": "scout_fox",
        "name": "Лис-разведчик",
        "title": "МАГАЗИН | ЛИС-РАЗВЕДЧИК",
        "rarity": "Обычный",
        "rarity_code": "common",
        "price": 3000,
        "color": 0x2ECC71,
        "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560909867063976097/Eight_Fantasy_Creature_Portraits_1_-removebg-preview.png?ex=6ad46e18&is=6ad31c98&hm=4828b42d01726d0aba8f212c8054a0562986702fff117e3817e76e4a448ec121&=&format=webp&quality=lossless",
        "description": (
            "Быстрый и хитрый разведчик. Повышает вероятность успешного "
            "ограбления и помогает быстрее вернуться в игру после заключения."
        ),
        "abilities": {
            1: {
                "name": "Ловкие лапы",
                "max_val": 10,
                "unit": "проц. п.",
                "desc_template": "Повышает базовый шанс успешного /rob на {val} процентных пунктов."
            },
            2: {
                "name": "Побег из тюрьмы",
                "max_val": 25,
                "unit": "%",
                "desc_template": "Сокращает оставшееся время заключения на {val}%."
            }
        }
    },
    "treasury_owl": {
        "id": "treasury_owl",
        "name": "Сова-казначей",
        "title": "МАГАЗИН | СОВА-КАЗНАЧЕЙ",
        "rarity": "Обычный",
        "rarity_code": "common",
        "price": 3000,
        "color": 0x2ECC71,
        "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560909866044891267/Eight_Fantasy_Creature_Portraits_2_-removebg-preview.png?ex=6ad46e18&is=6ad31c98&hm=6106d249b9eaf7fa7e581c9ca8dd436d711a5055b034ccbaa5072dd68e74c1ce&=&format=webp&quality=lossless",
        "description": (
            "Бдительный хранитель капитала. Помогает сохранить часть монет "
            "при ограблении и защищает ценные предметы от чужих рук."
        ),
        "abilities": {
            1: {
                "name": "Сохранность капитала",
                "max_val": 25,
                "unit": "%",
                "desc_template": "Уменьшает потери наличных монет при успешном ограблении на {val}%."
            },
            2: {
                "name": "Цепкие когти",
                "max_val": 15,
                "unit": "%",
                "desc_template": "При успешной краже предмета, даёт {val}% шанс оставить предмет у жертвы. Грабитель ничего не получает."
            }
        }
    },
    "raccoon_robber": {
        "id": "raccoon_robber",
        "name": "Енот-грабитель",
        "title": "МАГАЗИН | ЕНОТ-ГРАБИТЕЛЬ",
        "rarity": "Необычный",
        "rarity_code": "uncommon",
        "price": 8000,
        "color": 0x3498DB,
        "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560909865298296964/Eight_Fantasy_Creature_Portraits_3_-removebg-preview.png?ex=6ad46e18&is=6ad31c98&hm=399f9c5d2ff2405a7df7ab202ceaa260587fa751bd5dc4cfef0b0c542ccd643d&=&format=webp&quality=lossless",
        "description": (
            "Настоящий мастер наживы. Увеличивает прибыль от успешных ограблений "
            "и помогает выгоднее продавать добытые предметы."
        ),
        "abilities": {
            1: {
                "name": "Чужой кошелёк",
                "max_val": 25,
                "unit": "%",
                "desc_template": "Увеличивает добычу при успешном ограблении на {val}%."
            },
            2: {
                "name": "Чёрный рынок",
                "max_val": 35,
                "unit": "%",
                "desc_template": "Уменьшает комиссию продажи предметов на Чёрном рынке на {val}%."
            }
        }
    },
    "raven_smuggler": {
        "id": "raven_smuggler",
        "name": "Ворон-контрабандист",
        "title": "МАГАЗИН | ВОРОН-КОНТРАБАНДИСТ",
        "rarity": "Необычный",
        "rarity_code": "uncommon",
        "price": 8000,
        "color": 0x3498DB,
        "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560909864853577758/Eight_Fantasy_Creature_Portraits_4_-removebg-preview.png?ex=6ad46e18&is=6ad31c98&hm=812e7a25fc8c6545edef5a5869c68cb6e1d8b3afb5ec0ef41eea1f00416af8a9&=&format=webp&quality=lossless",
        "description": (
            "Хладнокровный стратег, который умеет извлекать пользу даже из "
            "рискованных вылазок. Снижает последствия неудач и повышает шанс заполучить чужой предмет."
        ),
        "abilities": {
            1: {
                "name": "План отхода",
                "max_val": 40,
                "unit": "%",
                "desc_template": "Уменьшает штраф за неудачное ограбление на {val}%."
            },
            2: {
                "name": "Охотник за вещами",
                "max_val": 12,
                "unit": "проц. п.",
                "desc_template": "Повышает шанс кражи предмета на {val} процентных пунктов."
            }
        }
    },
    "guardian_turtle": {
        "id": "guardian_turtle",
        "name": "Черепаха-хранитель",
        "title": "МАГАЗИН | ЧЕРЕПАХА-ХРАНИТЕЛЬ",
        "rarity": "Эпический",
        "rarity_code": "epic",
        "price": 15000,
        "color": 0x9B59B6,
        "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560909863893340271/Eight_Fantasy_Creature_Portraits_6_-removebg-preview.png?ex=6ad46e17&is=6ad31c97&hm=c9c4324602ccb360fac4b328be597d053beb9e7eebddf4758398ac9871cf58ca&=&format=webp&quality=lossless",
        "description": (
            "Надёжная крепость для твоих сокровищ. Снижает вероятность успешного "
            "ограбления и позволяет защитить один особенно ценный предмет."
        ),
        "abilities": {
            1: {
                "name": "Крепкий панцирь",
                "max_val": 20,
                "unit": "проц. п.",
                "desc_template": "Уменьшает шанс успешного ограбления владельца на {val} процентных пунктов."
            },
            2: {
                "name": "Тайный сейф",
                "max_val": 1,
                "unit": "слот",
                "desc_template": (
                    "Позволяет выбрать один предмет, который получает абсолютную защиту "
                    "от кражи, пока способность активна и предмет находится в инвентаре."
                )
            }
        }
    },
    "mimic_collector": {
        "id": "mimic_collector",
        "name": "Мимик-собиратель",
        "title": "МАГАЗИН | МИМИК-СОБИРАТЕЛЬ",
        "rarity": "Легендарный",
        "rarity_code": "legendary",
        "price": 30000,
        "color": 0xF1C40F,
        "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560909864367169546/Eight_Fantasy_Creature_Portraits_5_-removebg-preview.png?ex=6ad46e17&is=6ad31c97&hm=63b8677ae72debb9f2d75e564502306c1ed0c9ab0b76506ab03d9a3dfbccb7ee&=&format=webp&quality=lossless",
        "description": (
            "Магический собиратель сокровищ. Иногда находит дополнительную награду "
            "в кейсах и помогает сокращать торговые комиссии."
        ),
        "abilities": {
            1: {
                "name": "Секретный тайник",
                "max_val": 20,
                "unit": "%",
                "desc_template": (
                    "Даёт {val}% шанс получить дополнительную награду монет при открытии кейса. "
                    "Размер дополнительной награды составляет 25–50% от базовой награды. Работает только на монеты."
                )
            },
            2: {
                "name": "Торговый нюх",
                "max_val": 45,
                "unit": "%",
                "desc_template": "Уменьшает поддерживаемые комиссии обмена и продажи предметов на {val}%."
            }
        }
    }
}

# Данные для "Загадочного яйца"
MYSTERY_EGG_DATA = {
    "title": "МАГАЗИН | ЗАГАДОЧНОЕ ЯЙЦО",
    "category": "Лотерея компаньонов",
    "price": 500,
    "color": 0xF1C40F,
    "image_url": "https://media.discordapp.net/attachments/1560907251412238396/1560918277125967963/mythic_egg_92177005617456119468743173808181512483.png?ex=6ad475ed&is=6ad3246d&hm=20b7ccdec0639377dacd5758f1e3fb8812070d872676d70f1e7326c2e9676a3d&=&format=webp&quality=lossless",
    "description": (
        "Испытай удачу! Внутри может оказаться питомец любой редкости — от обычного "
        "Золотого лиса до легендарного Волка арены. Но будь осторожен: яйцо может оказаться пустым."
    ),
    # Абсолютные шансы для вычислений в коде:
    "absolute_chances": {
        "none": 0.800,        # 80.0% Пустое яйцо
        "common": 0.120,      # 12.0% Обычный (60% от 20%)
        "uncommon": 0.050,    #  5.0% Необычный (25% от 20%)
        "epic": 0.024,        #  2.4% Эпический (12% от 20%)
        "legendary": 0.006    #  0.6% Легендарный (3% от 20%)
    }
}


def calculate_ability_power(pet_level: int, max_val: float) -> float:
    """Вычисляет текущее значение силы способности - теперь всегда максимальное."""
    # Способности теперь открыты с 1 уровня и всегда имеют максимальное значение
    return max_val
