"""Модели для мини-игр."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class Minigame:
    """Модель мини-игры."""
    id: str
    name: str
    description: str
    category: str  # luck, quiz, casino, pvp, mixed
    difficulty: str  # easy, medium, hard
    min_bet: int  # Минимальная ставка
    max_bet: int  # Максимальная ставка
    multiplier: float  # Множитель выигрыша для PvE
    is_pvp: bool
    is_pve: bool
    is_active: bool = True


@dataclass
class MinigameSession:
    """Модель сессии мини-игры."""
    session_id: str
    game_id: str
    guild_id: int
    player1_id: int
    player1_bet: int
    player2_id: Optional[int]  # None для PvE
    player2_bet: Optional[int]
    status: str  # waiting, active, completed
    created_at: datetime
    completed_at: Optional[datetime]
    winner_id: Optional[int]
    winnings: int  # Сумма выигрыша
    payout_processed: bool = False
