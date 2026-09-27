"""Централизованный конфиг для мини-игр."""

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class GameConfig:
    """Конфигурация игры."""
    id: str  # ID игры (используется в базе данных)
    name: str  # Название
    emoji: str  # Эмодзи
    command: str  # Команда для запуска (например, "coin_flip")
    status: str  # "dev" (в разработке) или "available" (доступна)
    short_description: str  # Краткое описание для списка
    description: str  # Подробное описание
    how_to_play: str  # Инструкция "Как играть"
    min_bet: int  # Минимальная ставка
    max_bet: int  # Максимальная ставка
    category: str  # Категория
    difficulty: str  # Сложность (easy, medium, hard)
    multiplier: float  # Множитель выигрыша
    is_pvp: bool  # PvP игра
    is_pve: bool  # PvE игра


# Категории игр
CATEGORIES = {
    "luck": {
        "emoji": "🎲",
        "name": "Игры на удачу",
        "description": "Игры на удачу с механикой ставок",
        "color": 0x00008B  # dark_blue
    },
    "quiz": {
        "emoji": "🧠",
        "name": "Викторины и интеллект",
        "description": "Викторины и головоломки",
        "color": 0x4B0082  # dark_purple
    },
}

# Игры на удачу (8 игр)
LUCK_GAMES = [
    GameConfig(
        id="coin_flip",
        name="Орёл или решка",
        emoji="🪙",
        command="coin_flip",
        status="available",
        short_description="Классическая игра на удачу",
        description="Выберите орёл или решку и сделайте ставку! Если угадаете - получите двойной выигрыш.",
        how_to_play="1. Сделайте ставку на орёл или решку\n2. Монета подбрасывается\n3. Если угадали - получаете 2x ставку",
        min_bet=10,
        max_bet=10000,
        category="luck",
        difficulty="easy",
        multiplier=2.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="dice_roll",
        name="Бросок кубика",
        emoji="🎲",
        command="dice_roll",
        status="available",
        short_description="Угадайте число на кубике",
        description="Кубик подбрасывается и выпадает число от 1 до 6. Угадайте число и получите 6x ставку!",
        how_to_play="1. Сделайте ставку и выберите число (1-6)\n2. Кубик подбрасывается\n3. Если совпало - получаете 6x ставку",
        min_bet=10,
        max_bet=5000,
        category="luck",
        difficulty="easy",
        multiplier=6.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="guess_number",
        name="Угадай число",
        emoji="🔢",
        command="guess_number",
        status="available",
        short_description="Угадайте число от 1 до 100",
        description="Бот загадывает число от 1 до 100. Попробуйте угадать его за минимальное количество попыток!",
        how_to_play="1. Сделайте ставку\n2. Введите число от 1 до 100\n3. Бот скажет больше или меньше\n4. Угадайте за 7 попыток",
        min_bet=20,
        max_bet=1000,
        category="luck",
        difficulty="medium",
        multiplier=10.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="wheel",
        name="Колесо фортуны",
        emoji="🎡",
        command="wheel",
        status="dev",
        short_description="Вращайте колесо фортуны",
        description="Крутите колесо и выиграйте призы! Разные сектора дают разные множители.",
        how_to_play="1. Сделайте ставку\n2. Вращайте колесо\n3. Получите выигрыш в зависимости от сектора",
        min_bet=50,
        max_bet=5000,
        category="luck",
        difficulty="medium",
        multiplier=5.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="highlow",
        name="Больше-Меньше",
        emoji="📊",
        command="highlow",
        status="dev",
        short_description="Угадайте следующее число больше или меньше",
        description="Бот показывает число. Угадайте, будет следующее число больше или меньше текущего.",
        how_to_play="1. Сделайте ставку\n2. Бот показывает число\n3. Выберите больше или меньше\n4. Следующее число генерируется",
        min_bet=20,
        max_bet=2000,
        category="luck",
        difficulty="medium",
        multiplier=2.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="slots",
        name="Слоты",
        emoji="🎰",
        command="slots",
        status="dev",
        short_description="Классические игровые автоматы",
        description="Крутите барабаны и собирайте выигрышные комбинации!",
        how_to_play="1. Сделайте ставку\n2. Крутите барабаны\n3. Соберите комбинацию для выигрыша",
        min_bet=10,
        max_bet=1000,
        category="luck",
        difficulty="easy",
        multiplier=3.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="scratch",
        name="Лотерейные билеты",
        emoji="🎫",
        command="scratch",
        status="dev",
        short_description="Выиграйте со скретч-карточек",
        description="Купите лотерейный билет и проверьте, выиграли ли вы!",
        how_to_play="1. Сделайте ставку (цена билета)\n2. Сотрите карточку\n3. Узнайте выигрыш",
        min_bet=50,
        max_bet=500,
        category="luck",
        difficulty="easy",
        multiplier=10.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="lucky_box",
        name="Счастливый ящик",
        emoji="📦",
        command="lucky_box",
        status="dev",
        short_description="Откройте ящик с призами",
        description="Выберите один из трёх ящиков. Один содержит приз, другие пустые!",
        how_to_play="1. Сделайте ставку\n2. Выберите ящик\n3. Если приз внутри - заберите его",
        min_bet=100,
        max_bet=2000,
        category="luck",
        difficulty="easy",
        multiplier=3.0,
        is_pvp=False,
        is_pve=True
    ),
]

# Викторины и интеллект (9 игр)
QUIZ_GAMES = [
    GameConfig(
        id="guess_emoji",
        name="Угадай эмодзи",
        emoji="😀",
        command="guess_emoji",
        status="available",
        short_description="Угадайте эмодзи по подсказкам",
        description="Бот загадывает эмодзи и даёт подсказки. Попробуйте угадать его за минимальное количество попыток!",
        how_to_play="1. Сделайте ставку\n2. Бот даёт подсказки об эмодзи\n3. Введите эмодзи\n4. Угадайте за 5 попыток",
        min_bet=20,
        max_bet=500,
        category="quiz",
        difficulty="medium",
        multiplier=5.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="math_quiz",
        name="Математическая викторина",
        emoji="➕",
        command="math_quiz",
        status="available",
        short_description="Решите математические примеры",
        description="Бот задаёт математические примеры. Решите их правильно и получите выигрыш!",
        how_to_play="1. Сделайте ставку\n2. Бот даёт пример\n3. Введите ответ\n4. Если правильно - получите 2x ставку",
        min_bet=10,
        max_bet=1000,
        category="quiz",
        difficulty="easy",
        multiplier=2.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="flag_quiz",
        name="Угадай флаг",
        emoji="🏳",
        command="flag_quiz",
        status="available",
        short_description="Угадайте страну по флагу",
        description="Бот показывает флаг страны. Попробуйте угадать, какая это страна!",
        how_to_play="1. Сделайте ставку\n2. Бот показывает флаг\n3. Введите название страны\n4. Угадайте за 3 попытки",
        min_bet=30,
        max_bet=500,
        category="quiz",
        difficulty="medium",
        multiplier=3.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="hangman",
        name="Виселица",
        emoji="🪝",
        command="hangman",
        status="dev",
        short_description="Классическая игра виселица",
        description="Угадайте слово по буквам, пока не остались попытки!",
        how_to_play="1. Сделайте ставку\n2. Бот показывает длину слова\n3. Вводите буквы\n4. Угадайте слово до того как фигура будет повешена",
        min_bet=20,
        max_bet=500,
        category="quiz",
        difficulty="medium",
        multiplier=5.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="millionaire",
        name="Кто хочет стать миллионером",
        emoji="💰",
        command="millionaire",
        status="dev",
        short_description="Ответьте на вопросы за призы",
        description="Отвечайте на вопросы с подсказками. Чем меньше подсказок используете - тем больше выигрыш!",
        how_to_play="1. Сделайте ставку\n2. Бот задаёт вопрос\n3. Выберите ответ\n4. Используйте подсказки если нужно",
        min_bet=50,
        max_bet=2000,
        category="quiz",
        difficulty="hard",
        multiplier=10.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="memory",
        name="Память",
        emoji="🧠",
        command="memory",
        status="dev",
        short_description="Запомните последовательность",
        description="Бот показывает последовательность. Запомните её и повторите!",
        how_to_play="1. Сделайте ставку\n2. Бот показывает последовательность\n3. Повторите её\n4. Чем длиннее - тем больше выигрыш",
        min_bet=20,
        max_bet=1000,
        category="quiz",
        difficulty="medium",
        multiplier=3.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="anagrams",
        name="Анаграммы",
        emoji="🔤",
        command="anagrams",
        status="dev",
        short_description="Составьте слово из букв",
        description="Бот даёт перемешанные буквы. Составьте из них слово!",
        how_to_play="1. Сделайте ставку\n2. Бот даёт буквы\n3. Введите слово\n4. Угадайте за 3 попытки",
        min_bet=30,
        max_bet=500,
        category="quiz",
        difficulty="medium",
        multiplier=4.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="logic_puzzle",
        name="Логические задачи",
        emoji="🧩",
        command="logic_puzzle",
        status="dev",
        short_description="Решите логические загадки",
        description="Бот задаёт логическую загадку. Решите её правильно!",
        how_to_play="1. Сделайте ставку\n2. Бот даёт загадку\n3. Введите ответ\n4. Если правильно - получите 3x ставку",
        min_bet=20,
        max_bet=500,
        category="quiz",
        difficulty="hard",
        multiplier=3.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="sequence",
        name="Найди закономерность",
        emoji="🔢",
        command="sequence",
        status="dev",
        short_description="Найдите следующее число в последовательности",
        description="Бот показывает последовательность чисел. Найдите следующее!",
        how_to_play="1. Сделайте ставку\n2. Бот показывает числа\n3. Введите следующее число\n4. Если правильно - получите 4x ставку",
        min_bet=20,
        max_bet=1000,
        category="quiz",
        difficulty="hard",
        multiplier=4.0,
        is_pvp=False,
        is_pve=True
    ),
]

# Все игры
ALL_GAMES = LUCK_GAMES + QUIZ_GAMES


def get_game_by_id(game_id: str) -> Optional[GameConfig]:
    """Получить игру по ID."""
    for game in ALL_GAMES:
        if game.id == game_id:
            return game
    return None


def get_games_by_category(category: str) -> List[GameConfig]:
    """Получить игры по категории."""
    return [game for game in ALL_GAMES if game.category == category]


def get_all_games() -> List[GameConfig]:
    """Получить все игры."""
    return ALL_GAMES
