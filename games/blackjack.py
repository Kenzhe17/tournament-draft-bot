"""Блэкджек против дилера в основном Discord-боте."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

import discord

from config import GAMES_CHANNEL_ID, guild_allowed
from utils.design import DesignView

logger = logging.getLogger(__name__)

Card = tuple[str, str]
RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A")
SUITS = ("♠", "♥", "♦", "♣")
VALUES = dict(zip(RANKS, (*range(2, 11), 10, 10, 10, 11)))


def hand_total(cards: tuple[Card, ...]) -> int:
    total = sum(VALUES[rank] for rank, _ in cards)
    for rank, _ in cards:
        if total <= 21:
            break
        if rank == "A":
            total -= 10
    return total


@dataclass(frozen=True)
class BlackjackRound:
    deck: tuple[Card, ...]
    player: tuple[Card, ...]
    dealer: tuple[Card, ...]
    result: str | None = None

    @classmethod
    def deal(cls, deck: tuple[Card, ...]) -> BlackjackRound:
        game = cls(deck[4:], deck[0:4:2], deck[1:4:2])
        player_has_blackjack = hand_total(game.player) == 21
        dealer_has_blackjack = hand_total(game.dealer) == 21
        if player_has_blackjack and dealer_has_blackjack:
            return replace(game, result="Ничья — блэкджек у обоих.")
        if player_has_blackjack:
            return replace(game, result="Блэкджек! Вы выиграли.")
        if dealer_has_blackjack:
            return replace(game, result="У дилера блэкджек. Вы проиграли.")
        return game

    @classmethod
    def new(cls) -> BlackjackRound:
        deck = [(rank, suit) for suit in SUITS for rank in RANKS]
        random.shuffle(deck)
        return cls.deal(tuple(deck))

    def hit(self) -> BlackjackRound:
        game = replace(self, deck=self.deck[1:], player=(*self.player, self.deck[0]))
        total = hand_total(game.player)
        if total > 21:
            return replace(game, result="Перебор. Вы проиграли.")
        if total == 21:
            return game.stand()
        return game

    def stand(self) -> BlackjackRound:
        dealer = self.dealer
        drawn = 0
        while hand_total(dealer) < 17:
            dealer = (*dealer, self.deck[drawn])
            drawn += 1

        player_total = hand_total(self.player)
        dealer_total = hand_total(dealer)
        if dealer_total > 21:
            result = "У дилера перебор. Вы выиграли."
        elif player_total > dealer_total:
            result = "Вы выиграли."
        elif player_total == dealer_total:
            result = "Ничья."
        else:
            result = "Дилер выиграл."
        return replace(self, deck=self.deck[drawn:], dealer=dealer, result=result)


def format_hand(cards: tuple[Card, ...]) -> str:
    return " ".join(f"`{rank}{suit}`" for rank, suit in cards)


class BlackjackView(DesignView):
    def __init__(
        self,
        source: discord.Interaction,
        player_id: int,
        game: BlackjackRound,
        *,
        expired: bool = False,
    ) -> None:
        super().__init__(title="Блэкджек", timeout=None if expired else 90)
        self.source = source
        self.player_id = player_id
        self.game = game
        self.lock = asyncio.Lock()
        self.replaced = False

        dealer_hand = f"{format_hand(game.dealer[:1])} `?`"
        if game.result or expired:
            dealer_hand = f"{format_hand(game.dealer)} · **{hand_total(game.dealer)}**"

        self.lines(
            f"**Игрок** <@{player_id}>",
            f"{format_hand(game.player)} · **{hand_total(game.player)}**",
            "",
            "**Дилер**",
            dealer_hand,
        )
        self.sep()

        if game.result or expired:
            self.text(f"**{game.result or 'Время вышло. Раздача завершена.'}**")
            if not expired:
                self.row(self.button("Сыграть ещё", discord.ButtonStyle.primary, self.replay))
        else:
            self.text("Ваш ход. Дилер берёт карты до 17.")
            self.row(
                self.button("Взять карту", discord.ButtonStyle.primary, self.hit),
                self.button("Стоп", discord.ButtonStyle.secondary, self.stand),
            )

    @staticmethod
    def button(
        label: str,
        style: discord.ButtonStyle,
        callback: Callable[[discord.Interaction], Awaitable[None]],
    ) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style)
        button.callback = callback
        return button

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.player_id:
            return True
        await interaction.response.send_message("Эта раздача принадлежит другому игроку.", ephemeral=True)
        return False

    async def hit(self, interaction: discord.Interaction) -> None:
        await self.apply(interaction, self.game.hit)

    async def stand(self, interaction: discord.Interaction) -> None:
        await self.apply(interaction, self.game.stand)

    async def replay(self, interaction: discord.Interaction) -> None:
        await self.apply(interaction, BlackjackRound.new)

    async def apply(
        self,
        interaction: discord.Interaction,
        next_round: Callable[[], BlackjackRound],
    ) -> None:
        async with self.lock:
            if self.is_finished():
                await interaction.response.send_message("Эта кнопка уже устарела.", ephemeral=True)
                return
            view = BlackjackView(interaction, self.player_id, next_round())
            await interaction.response.edit_message(view=view, allowed_mentions=discord.AllowedMentions.none())
            self.replaced = True
            self.stop()

    async def on_timeout(self) -> None:
        async with self.lock:
            if self.replaced:
                return
            view = BlackjackView(self.source, self.player_id, self.game, expired=True)
            try:
                await self.source.edit_original_response(view=view, allowed_mentions=discord.AllowedMentions.none())
            except discord.NotFound:
                return
            except discord.HTTPException:
                logger.warning("Не удалось обновить карточку блэкджека по тайм-ауту", exc_info=True)


async def start_blackjack(interaction: discord.Interaction) -> None:
    if interaction.guild_id is None or not guild_allowed(interaction.guild_id):
        await interaction.response.send_message("Блэкджек доступен на сервере r!z3.", ephemeral=True)
        return
    if GAMES_CHANNEL_ID and interaction.channel_id != GAMES_CHANNEL_ID:
        await interaction.response.send_message(
            f"Чтобы сыграть в блэкджек, перейдите в <#{GAMES_CHANNEL_ID}>.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    view = BlackjackView(interaction, interaction.user.id, BlackjackRound.new())
    await interaction.response.send_message(view=view, allowed_mentions=discord.AllowedMentions.none())
