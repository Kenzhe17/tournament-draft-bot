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
    status: str  # "dev" (в разработке) или "ready" (доступна) - Note: "available" is also accepted for backward compatibility
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
        "description": "Быстрые игры на риск: подбрасывайте монеты, бросайте кубики, играйте в бутылочку, КНБ и испытывайте фортуну.",
        "color": 0x00008B  # dark_blue
    },
    "quiz": {
        "emoji": "🧠",
        "name": "Викторины и головоломки",
        "description": "Проверьте эрудицию и скорость мысли: решайте математические примеры и стройте словесные цепочки.",
        "color": 0x4B0082  # dark_purple
    },
    "casino": {
        "emoji": "🎰",
        "name": "Казино и ставки",
        "description": "Азартные игры с коэффициентами: крутите рулетку, слоты, играйте в баккару, лотерею, High-Low и Тройной шанс.",
        "color": 0x006400  # dark_green
    },
    "economy": {
        "emoji": "💰",
        "name": "Экономика",
        "description": "Управление финансами: ограбления, банковский сейф, система коинов и команд взаимодействия.",
        "color": 0xDAA520  # goldenrod
    },
}

# Игры на удачу (8 игр)
LUCK_GAMES = [
    GameConfig(
        id="rps",
        name="Камень-Ножницы-Бумага",
        emoji="✂️",
        command="rps",
        status="dev",
        short_description="Классическая дуэль против бота или игрока.",
        description="Классическая игра на выбывание против бота или другого игрока.",
        how_to_play="Камень бьёт ножницы, ножницы бьют бумагу, бумага кроет камень. Победитель забирает банк (2x).",
        min_bet=10,
        max_bet=1000,
        category="luck",
        difficulty="easy",
        multiplier=2.0,
        is_pvp=True,
        is_pve=True
    ),
    GameConfig(
        id="guess_number",
        name="Угадай число",
        emoji="🔢",
        command="guess_number",
        status="dev",
        short_description="Число от 1 до 100 за 7 попыток (5x).",
        description="Угадайте число от 1 до 100 за 7 попыток с подсказками бота.",
        how_to_play="Бот подсказывает «больше» или «меньше». При победе выигрыш 5x от ставки.",
        min_bet=20,
        max_bet=500,
        category="luck",
        difficulty="medium",
        multiplier=5.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="coin_flip",
        name="Монетка",
        emoji="🪙",
        command="coin_flip",
        status="dev",
        short_description="Подбросьте монету и угадайте сторону (2x).",
        description="Подбросьте монету, выберите Орла или Решку и удвойте ставку.",
        how_to_play="Классическая игра 50/50. Выигрыш выплачивается 1:1 (2x от ставки).",
        min_bet=10,
        max_bet=10000,
        category="luck",
        difficulty="easy",
        multiplier=2.0,
        is_pvp=True,
        is_pve=True
    ),
    GameConfig(
        id="dice_roll",
        name="Кубик",
        emoji="🎲",
        command="dice_roll",
        status="dev",
        short_description="Бросок костей на большее количество очков.",
        description="Бросьте кубик и наберите больше очков, чем соперник.",
        how_to_play="У кого выпадает большее число, тот забирает банк. При ничьей — возврат ставок.",
        min_bet=10,
        max_bet=5000,
        category="luck",
        difficulty="easy",
        multiplier=2.0,
        is_pvp=True,
        is_pve=True
    ),
    GameConfig(
        id="guess_emoji",
        name="Угадай эмодзи",
        emoji="🎯",
        command="guess_emoji",
        status="dev",
        short_description="Найдите загаданный эмодзи по подсказкам.",
        description="Найдите загаданный эмодзи по 3 подсказкам категорий.",
        how_to_play="Используйте подсказки (животное, еда, предмет) и угадайте эмодзи. Выигрыш 3x.",
        min_bet=20,
        max_bet=500,
        category="luck",
        difficulty="medium",
        multiplier=3.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="wheel",
        name="Колесо фортуны",
        emoji="🎡",
        command="wheel",
        status="dev",
        short_description="Крутите колесо с множителями от 0.5x до 10x.",
        description="Крутите колесо с множителями от 0.5x до 10x.",
        how_to_play="Сектора дают множители ставки (0.5x, 1x, 2x, 3x, 5x, 10x). Сектор «Банкрот» = потеря ставки.",
        min_bet=50,
        max_bet=5000,
        category="luck",
        difficulty="medium",
        multiplier=5.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="tictactoe",
        name="Крестики-нолики",
        emoji="❌",
        command="tictactoe",
        status="dev",
        short_description="Классическая игра 3x3 на поле.",
        description="Классическая игра 3x3 на поле против бота или игрока.",
        how_to_play="Соберите 3 символа в ряд (по горизонтали, вертикали или диагонали). Победитель получает банк.",
        min_bet=20,
        max_bet=1000,
        category="luck",
        difficulty="medium",
        multiplier=2.0,
        is_pvp=True,
        is_pve=True
    ),
    GameConfig(
        id="spin_bottle",
        name="Бутылочка",
        emoji="🍾",
        command="spin_bottle",
        status="dev",
        short_description="Крутите бутылочку и бросайте вызов друзьям.",
        description="Крутите бутылочку и бросайте вызов друзьям.",
        how_to_play="Бутылочка выбирает случайного игрока. Кто отказывается от вызова — теряет ставку.",
        min_bet=50,
        max_bet=2000,
        category="luck",
        difficulty="easy",
        multiplier=2.0,
        is_pvp=True,
        is_pve=False
    ),
]

# Викторины и головоломки (2 игры)
QUIZ_GAMES = [
    GameConfig(
        id="math_quiz",
        name="Математическая викторина",
        emoji="🧮",
        command="math_quiz",
        status="dev",
        short_description="Кто быстрее решит 10 математических примеров.",
        description="Кто быстрее решит 10 математических примеров.",
        how_to_play="Решайте примеры на скорость. Игрок с наибольшим количеством правильных ответов забирает банк.",
        min_bet=20,
        max_bet=1000,
        category="quiz",
        difficulty="medium",
        multiplier=2.0,
        is_pvp=True,
        is_pve=True
    ),
    GameConfig(
        id="word_chain",
        name="Словесные цепочки",
        emoji="🔤",
        command="word_chain",
        status="dev",
        short_description="Слова на последнюю букву за 7 секунд.",
        description="Называйте слова на последнюю букву предыдущего слова за 7 секунд.",
        how_to_play="Кто не успевает назвать слово за отведённое время — проигрывает ставку.",
        min_bet=30,
        max_bet=500,
        category="quiz",
        difficulty="medium",
        multiplier=2.0,
        is_pvp=True,
        is_pve=False
    ),
]

# Казино и ставки (9 игр)
CASINO_GAMES = [
    GameConfig(
        id="roulette",
        name="Рулетка",
        emoji="�",
        command="roulette",
        status="dev",
        short_description="Классическая рулетка с выигрышами от 2x до 35x.",
        description="Классическая рулетка с выигрышами от 2x до 35x.",
        how_to_play="Ставьте на цвет (красный/чёрный = 2x), чёт/нечёт (= 2x) или точное число (= 35x).",
        min_bet=50,
        max_bet=5000,
        category="casino",
        difficulty="medium",
        multiplier=35.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="slots",
        name="Слоты",
        emoji="🎰",
        command="slots",
        status="dev",
        short_description="Однорукий бандит с 3 барабанами и выигрышем до 10x.",
        description="Однорукий бандит с 3 барабанами и выигрышем до 10x.",
        how_to_play="3 одинаковых символа = 10x, 2 одинаковых = 3x, все разные = потеря ставки.",
        min_bet=10,
        max_bet=1000,
        category="casino",
        difficulty="easy",
        multiplier=10.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="craps",
        name="Крэпс",
        emoji="🎲",
        command="craps",
        status="dev",
        short_description="Игра в кости на линии Pass / Don't Pass.",
        description="Игра в кости на линии Pass / Don't Pass.",
        how_to_play="Бросайте 2 кубика. Ставьте на сумму 7/11 (победа) или 2/3/12. Выигрыш 1x–2x.",
        min_bet=50,
        max_bet=2000,
        category="casino",
        difficulty="hard",
        multiplier=2.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="baccarat",
        name="Баккара",
        emoji="�",
        command="baccarat",
        status="dev",
        short_description="Ставьте на Игрока или Банкира и наберите ближе к 9 очкам.",
        description="Ставьте на Игрока или Банкира и наберите ближе к 9 очкам.",
        how_to_play="Простая карточная игра. При правильном прогнозе выигрыш 2x от ставки.",
        min_bet=100,
        max_bet=5000,
        category="casino",
        difficulty="medium",
        multiplier=2.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="lottery",
        name="Лотерея",
        emoji="�️",
        command="lottery",
        status="dev",
        short_description="Покупайте билет и выигрывайте джекпот до 100x.",
        description="Покупайте билет и выигрывайте джекпот до 100x.",
        how_to_play="Угадайте 5 случайных чисел: 2 совпадения = 2x, 3 = 10x, 4 = 50x, 5 = 100x (Джекпот).",
        min_bet=50,
        max_bet=500,
        category="casino",
        difficulty="medium",
        multiplier=100.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="snap",
        name="Снэп",
        emoji="⚡",
        command="snap",
        status="dev",
        short_description="Карточная игра на удвоение или фолд против дилера.",
        description="Карточная игра на удвоение или фолд против дилера.",
        how_to_play="Сравнивайте очки с дилером. Если ваши очки выше — выигрыш 2x.",
        min_bet=50,
        max_bet=2000,
        category="casino",
        difficulty="medium",
        multiplier=2.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="dice_bet",
        name="Дайс",
        emoji="🎲",
        command="dice_bet",
        status="dev",
        short_description="Ставка на точную сумму двух кубиков (2–12).",
        description="Ставка на точную сумму двух кубиков (2–12).",
        how_to_play="Если выпадающая сумма кубиков совпадает со ставкой — выигрыш 6x.",
        min_bet=20,
        max_bet=1000,
        category="casino",
        difficulty="medium",
        multiplier=6.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="high_low",
        name="High-Low",
        emoji="�",
        command="highlow",
        status="dev",
        short_description="Угадайте, будет ли следующая карта выше или ниже текущей.",
        description="Угадайте, будет ли следующая карта выше или ниже текущей.",
        how_to_play="Верный прогноз приносит 2x от ставки.",
        min_bet=20,
        max_bet=2000,
        category="casino",
        difficulty="medium",
        multiplier=2.0,
        is_pvp=False,
        is_pve=True
    ),
    GameConfig(
        id="triple_chance",
        name="Тройной шанс",
        emoji="☘️",
        command="triple_chance",
        status="dev",
        short_description="Угадайте загаданное ботом число от 1 до 3 (шанс 33.3%).",
        description="Угадайте загаданное ботом число от 1 до 3 (шанс 33.3%).",
        how_to_play="Если число угадано с первой попытки — выигрыш 3x от ставки.",
        min_bet=10,
        max_bet=1000,
        category="casino",
        difficulty="easy",
        multiplier=3.0,
        is_pvp=False,
        is_pve=True
    ),
]

# Экономика (3 игры)
ECONOMY_GAMES = [
    GameConfig(
        id="rob",
        name="Ограбление",
        emoji="🔫",
        command="rob",
        status="ready",
        short_description="Ограбить другого игрока и украсть монеты или предмет.",
        description="Ограбление с 50% шансом успеха. При успехе заберёте монеты или предмет (авто-продажа за 30%). При провале выплатите штраф до 70% от баланса.",
        how_to_play="Используйте /rob @пользователь для одиночного ограбления или /robgroup @пользователь для группового. Наличные уязвимы, деньги в сейфе защищены.",
        min_bet=0,
        max_bet=0,
        category="economy",
        difficulty="medium",
        multiplier=0.0,
        is_pvp=True,
        is_pve=False
    ),
    GameConfig(
        id="robgroup",
        name="Групповое ограбление",
        emoji="👥",
        command="robgroup",
        status="ready",
        short_description="Собрать банду и ограбить жертву вместе.",
        description="Групповое ограбление от 2 до 6 игроков. Шанс успеха 50-80% в зависимости от количества участников. Куш делится поровну.",
        how_to_play="Используйте /robgroup @пользователь для создания лобби. Пригласите других участников и начните штурм. Дефицит штрафа списывается со случайного платежеспособного участника.",
        min_bet=0,
        max_bet=0,
        category="economy",
        difficulty="hard",
        multiplier=0.0,
        is_pvp=True,
        is_pve=False
    ),
    GameConfig(
        id="bank",
        name="Банковский сейф",
        emoji="🏦",
        command="bank",
        status="ready",
        short_description="Безопасное хранение монет с защитой от ограбления.",
        description="Банковский сейф защищает ваши монеты от /rob и /robgroup. Депозит с комиссией 5%, снятие бесплатно. Лимит зависит от уровня.",
        how_to_play="Используйте /bank status для просмотра, /bank deposit [сумма/all] для пополнения, /bank withdraw [сумма/all] для снятия. Лимит: 3000 + ((уровень-1)//10)*2000.",
        min_bet=0,
        max_bet=0,
        category="economy",
        difficulty="easy",
        multiplier=0.0,
        is_pvp=False,
        is_pve=False
    ),
]

# Все игры
ALL_GAMES = LUCK_GAMES + QUIZ_GAMES + CASINO_GAMES + ECONOMY_GAMES


def get_game_by_id(game_id: str) -> Optional[GameConfig]:
    """Получить игру по ID."""
    for game in ALL_GAMES:
        if game.id == game_id:
            return game
    return None


def get_game_by_command(command: str) -> Optional[GameConfig]:
    """Получить игру по команде."""
    for game in ALL_GAMES:
        if game.command == command:
            return game
    return None


def get_games_by_category(category: str) -> List[GameConfig]:
    """Получить игры по категории."""
    return [game for game in ALL_GAMES if game.category == category]


def get_all_games() -> List[GameConfig]:
    """Получить все игры."""
    return ALL_GAMES


async def sync_games_to_db(guild_id: int) -> int:
    """Синхронизировать игры из конфига в базу данных.
    
    Args:
        guild_id: ID сервера (для логов)
    
    Returns:
        Количество синхронизированных игр
    """
    from storage.db import get_pool
    
    synced_count = 0
    
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            for game in ALL_GAMES:
                # Проверить существует ли игра
                existing = await conn.fetchrow(
                    "SELECT id FROM minigames WHERE id = $1",
                    game.id
                )
                
                if existing:
                    # Обновить существующую игру
                    await conn.execute(
                        """
                        UPDATE minigames
                        SET name = $2, description = $3, category = $4, difficulty = $5,
                            min_bet = $6, max_bet = $7, multiplier = $8,
                            is_pvp = $9, is_pve = $10, is_active = $11,
                            command_name = $12
                        WHERE id = $1
                        """,
                        game.id, game.name, game.description, game.category,
                        game.difficulty, game.min_bet, game.max_bet,
                        game.multiplier, game.is_pvp, game.is_pve,
                        game.status in ("ready", "available"), game.command
                    )
                else:
                    # Создать новую игру
                    await conn.execute(
                        """
                        INSERT INTO minigames
                        (id, name, description, category, difficulty, min_bet, max_bet, multiplier,
                         is_pvp, is_pve, is_active, command_name)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        """,
                        game.id, game.name, game.description, game.category,
                        game.difficulty, game.min_bet, game.max_bet,
                        game.multiplier, game.is_pvp, game.is_pve,
                        game.status in ("ready", "available"), game.command
                    )
                
                synced_count += 1
    except Exception as e:
        print(f"Error syncing games to database: {e}")
    
    return synced_count
