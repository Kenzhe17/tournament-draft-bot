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
        "description": "Быстрые игры на риск: подбрасывайте монеты, бросайте кубики и угадывайте числа.",
        "color": 0x00008B  # dark_blue
    },
    "quiz": {
        "emoji": "🧠",
        "name": "Викторины и головоломки",
        "description": "Проверьте эрудицию и скорость мысли: решайте математические примеры.",
        "color": 0x4B0082  # dark_purple
    },
    "casino": {
        "emoji": "🎰",
        "name": "Казино и ставки",
        "description": "Азартные игры с коэффициентами: рулетка и High-Low.",
        "color": 0x006400  # dark_green
    },
}

# Игры на удачу (3 игры)
LUCK_GAMES = [
    GameConfig(
        id="guess_number",
        name="Угадай число",
        emoji="🔢",
        command="guess_number",
        status="ready",
        short_description="Угадайте число от 1 до 100 за 7 попыток (1.25x).",
        description="Угадайте число от 1 до 100 за 7 попыток с подсказками бота.",
        how_to_play="Бот подсказывает «больше» или «меньше». При победе выигрыш 1.25x от ставки.",
        min_bet=10,
        max_bet=10000,
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
        status="ready",
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
        status="ready",
        short_description="Выберите число от 1 до 6 (6x множитель).",
        description="Выберите число от 1 до 6. Если выпадет ваше число, вы выиграете 6x ставку.",
        how_to_play="Классическая игра 1/6. Выберите число и бросьте кубик. При совпадении выигрыш 6x от ставки.",
        min_bet=10,
        max_bet=10000,
        category="luck",
        difficulty="easy",
        multiplier=6.0,
        is_pvp=False,
        is_pve=True
    ),
]

# Викторины и головоломки (1 игра)
QUIZ_GAMES = [
    GameConfig(
        id="math_quiz",
        name="Математическая викторина",
        emoji="🧮",
        command="mathquiz",
        status="ready",
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
]

# Казино и ставки (2 игры)
CASINO_GAMES = [
    GameConfig(
        id="roulette",
        name="Рулетка",
        emoji="🎰",
        command="roulette",
        status="ready",
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
        id="high_low",
        name="High-Low",
        emoji="⬆️⬇️",
        command="high_low",
        status="ready",
        short_description="Угадайте, будет ли следующее число выше или ниже (до 7.6x).",
        description="Угадайте, будет ли следующее число выше или ниже текущего. Каждая правильная догадка увеличивает множитель на 1.5x.",
        how_to_play="Угадайте выше или ниже. 5 раундов. Правильная догадка = множитель увеличивается. Все 5 правильных = 7.6x от ставки.",
        min_bet=10,
        max_bet=10000,
        category="casino",
        difficulty="medium",
        multiplier=7.6,
        is_pvp=False,
        is_pve=True
    ),
]

# Все игры
ALL_GAMES = LUCK_GAMES + QUIZ_GAMES + CASINO_GAMES


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
