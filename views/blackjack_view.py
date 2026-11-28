"""BLACKJACK 2.0 — CV2 views: меню ставки, раздача, результат.

Диспетчеризация кнопок — через discord.ui.DynamicItem с шаблоном
``bj2:<action>:<round_id>``: работает после рестарта без
перерегистрации view на каждую раздачу. Всё состояние — в PG
(blackjack_store), view только рендерит.
"""

from __future__ import annotations

import io
import logging
import uuid
from datetime import UTC, datetime

import discord

from games.blackjack_engine import (
    BET_PRESETS,
    RULES_VERSION,
    gross_payout,
    hand_total,
    is_natural,
)
from storage.blackjack_store import (
    RoundNotActive,
    StaleVersion,
    blackjack_store,
)
from utils.blackjack_render import render_round
from utils.design import DesignView

logger = logging.getLogger(__name__)

_ACTION_RE = (
    r"bj2:(?P<action>hit|stand|again|change|close)"
    r":(?P<rid>[0-9a-f]{32}):v(?P<ver>\d+)"
)

RULES_TEXT = (
    "**Правила (v" + RULES_VERSION + ")**\n"
    "• 52 карты, одна колода на раздачу\n"
    "• Дилер берёт до hard 17 и на soft 17, стоит на 18+ и hard 17\n"
    "• Равные очки (не блэкджек) — победа дилера\n"
    "• Блэкджек у обоих — возврат ставки; блэкджек игрока — выигрыш; "
    "блэкджек дилера — проигрыш\n"
    "• Победа: выплата 2× ставки (нетто +ставка). Проигрыш: 0. "
    "Оба блэкджека: возврат ставки\n"
    "• Только Hit/Stand. Без split, double, insurance\n"
    "• Нет хода 90 сек — автоматический Stand\n"
    "• Ставки — только виртуальная валюта, без денежной стоимости"
)


def _cid(action: str, round_id: str, version: int) -> str:
    return f"bj2:{action}:{round_id}:v{version}"


# ------------------------------------------------------------------
# Рендер состояния → DesignView + PNG
# ------------------------------------------------------------------

def _fmt_hand(cards) -> str:
    if not cards:
        return "—"
    return " ".join(f"**{r}{s}**" for r, s in cards)


def _dealer_display(stored) -> tuple[str, str]:
    game = stored.game
    if game.phase == "done":
        return _fmt_hand(game.dealer), f"**{hand_total(game.dealer)}**"
    shown = game.dealer[:1]
    hidden = len(game.dealer) - 1
    tail = " " + "?" * hidden if hidden else ""
    # показывает первую карту + скрытые рубашки
    shown_txt = " ".join(f"**{r}{s}**" for r, s in shown) + tail
    return shown_txt, "**?**"


def build_game_view(stored) -> tuple[DesignView, discord.File | None]:
    """DesignView + опциональный PNG-файл для сообщения раздачи."""
    game = stored.game
    done = game.phase == "done"
    reveal = done

    png = render_round(
        [tuple(c) for c in game.player],
        [tuple(c) for c in game.dealer],
        reveal_dealer=reveal,
        stake=stored.stake,
        outcome=game.outcome if done else None,
    )
    file = None
    if png:
        file = discord.File(
            io.BytesIO(png), filename=f"bj_{stored.round_id[:12]}.png"
        )

    view = DesignView(title="BLACKJACK", timeout=None)

    dealer_txt, dealer_total = _dealer_display(stored)
    p_total = hand_total(game.player) if game.player else 0
    p_natural = is_natural(game.player)

    if done and game.reason:
        from games.blackjack_engine import REASON_TEXT
        gross = gross_payout(game.outcome, stored.stake)
        net = gross - stored.stake
        status = f"**{REASON_TEXT.get(game.reason, game.reason)}**"
        if stored.stake:
            money = (
                f"Ставка: **{stored.stake:,}**\n"
                f"Выплата (gross): **{gross:,}**\n"
                f"Изменение (net): **{net:+,}**"
            )
        else:
            money = "Бесплатная игра — без ставки и выплаты"
    else:
        status = "Ваш ход — «Взять карту» или «Стоп»"
        money = (
            f"Ставка: **{stored.stake:,}**\n"
            f"Возможная выплата: **{2 * stored.stake:,}**"
            if stored.stake else "Бесплатная игра (без ставки)"
        )

    lines = [
        f"**Дилер** {dealer_txt} · {dealer_total}",
        f"**Игрок** {_fmt_hand(game.player)} · **{p_total}**"
        + (" · БЛЭКДЖЕК" if p_natural else ""),
        "",
        status,
        "",
        money,
        f"-# правила v{game.rules_version or RULES_VERSION}",
        # маркер доставки: recovery/reconcile находит сообщение
        # по этой строке в history канала (Components V2 content)
        f"-# ref {stored.round_id}",
    ]
    if not done and stored.deadline:
        now = datetime.now(UTC)
        left = max(0, int((stored.deadline - now).total_seconds()))
        lines.insert(4, f"-# авто-Stand через {left} сек")
    view.lines(*lines)
    view.sep()

    if file is not None:
        view.component(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(media=f"attachment://{file.filename}")
            )
        )

    rid = stored.round_id
    ver = stored.version
    if stored.card_closed:
        # «Закрыть»: результат сохранён, кнопки сняты навсегда
        # (card_closed в БД — повторный sync их не вернёт).
        pass
    elif done:
        view.row(
            _btn("Сыграть ещё", "success", _cid("again", rid, ver)),
            _btn("Изменить ставку", "secondary", _cid("change", rid, ver)),
            _btn("Закрыть", "danger", _cid("close", rid, ver)),
        )
    else:
        view.row(
            _btn("Взять карту", "primary", _cid("hit", rid, ver)),
            _btn("Стоп", "secondary", _cid("stand", rid, ver)),
        )
    return view, file


def _btn(label: str, style: str, custom_id: str) -> discord.ui.Button:
    from utils.design.components import _STYLE_MAP
    return discord.ui.Button(
        label=label, style=_STYLE_MAP.get(style, discord.ButtonStyle.secondary),
        custom_id=custom_id,
    )


# ------------------------------------------------------------------
# Динамические кнопки (persistent dispatch по custom_id)
# ------------------------------------------------------------------

class BlackjackActionButton(
    discord.ui.DynamicItem[discord.ui.Button], template=_ACTION_RE
):
    """Единый динамический item для всех кнопок раздачи."""

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        action = match.group("action")
        rid = match.group("rid")
        ver = int(match.group("ver"))
        labels = {
            "hit": ("Взять карту", discord.ButtonStyle.primary),
            "stand": ("Стоп", discord.ButtonStyle.secondary),
            "again": ("Сыграть ещё", discord.ButtonStyle.success),
            "change": ("Изменить ставку", discord.ButtonStyle.secondary),
            "close": ("Закрыть", discord.ButtonStyle.danger),
        }
        label, style = labels.get(action, ("?", discord.ButtonStyle.secondary))
        button = discord.ui.Button(
            label=label, style=style, custom_id=item.custom_id
        )

        async def callback(inter: discord.Interaction) -> None:
            await handle_round_action(inter, action, rid, ver)

        button.callback = callback
        return cls(button)


async def _ephemeral(interaction: discord.Interaction, text: str) -> None:
    """Ephemeral-ответ: response если ещё не отвечали, иначе followup."""
    kwargs = {"ephemeral": True,
              "allowed_mentions": discord.AllowedMentions.none()}
    if interaction.response.is_done():
        await interaction.followup.send(text, **kwargs)
    else:
        await interaction.response.send_message(text, **kwargs)


async def _owned_round(interaction: discord.Interaction, rid: str):
    stored = await blackjack_store.get_round(rid)
    if (
        stored is None
        or stored.user_id != interaction.user.id
        or stored.guild_id != interaction.guild_id
    ):
        await _ephemeral(interaction, "Это не ваша раздача или она устарела.")
        return None
    return stored


async def handle_round_action(
    interaction: discord.Interaction, action: str, rid: str, ver: int
) -> None:
    # PG + PNG + Discord edit могут превысить 3с — defer сразу,
    # публичная карточка редактируется через edit_original_response
    # (стандартный deferred-update для component interaction).
    if not interaction.response.is_done():
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
    stored = await _owned_round(interaction, rid)
    if stored is None:
        return  # _owned_round уже ответил ephemeral

    if action in ("hit", "stand"):
        await _handle_play(interaction, stored, action, ver)
    elif action == "close":
        await _handle_close(interaction, stored, ver)
    elif action == "again":
        await _handle_again(interaction, stored, same_stake=True)
    elif action == "change":
        await _handle_again(interaction, stored, same_stake=False)


async def _refresh_public(interaction, stored) -> None:
    """Актуализировать публичную карточку после stale/ошибки —
    через единый claim-CAS рендер (games.blackjack.sync_round_card)."""
    from games.blackjack import sync_round_card
    try:
        await sync_round_card(interaction.client, stored.round_id)
    except Exception:
        logger.info("blackjack stale-refresh sync failed",
                    exc_info=True)


async def _handle_play(interaction, stored, action: str, ver: int) -> None:
    if not stored.awaiting_player:
        await _ephemeral(interaction, "Эта раздача уже завершена.")
        return
    try:
        updated = await blackjack_store.apply_action(
            stored.round_id, action, expected_version=ver)
    except (RoundNotActive, StaleVersion):
        await _ephemeral(
            interaction, "Кто-то уже сходил — состояние обновлено.")
        await _refresh_public(interaction, stored)
        return
    # Ownership guard: если карточка уже не принадлежит этой
    # раздаче (message_id изменился), НЕ редактируем её.
    imid = getattr(getattr(interaction, "message", None), "id", None)
    if imid is not None and updated.message_id != imid:
        logger.info("blackjack play edit skipped: card transferred %s",
                    stored.round_id)
        await _ephemeral(interaction, "Раздача завершена — карта обновлена.")
        return
    # Единая сериализованная точка редактирования (claim-CAS):
    # устаревший параллельный sync не перезапишет более новый вид.
    from games.blackjack import render_round_card
    await render_round_card(interaction, updated)


async def _handle_close(interaction, stored, ver: int) -> None:
    # Re-check ownership ПЕРЕД edit: между defer и edit карточка могла
    # быть передана новой раздачей (again/change → detach_message).
    # Без проверки «Закрыть» стёр бы публичную карту новой игры.
    try:
        fresh = await blackjack_store.get_round(stored.round_id)
    except Exception:  # noqa: BLE001 — сбой PG → не доверяем edit
        fresh = None
    msg_id = getattr(getattr(interaction, "message", None), "id", None)
    if (
        fresh is None
        or fresh.message_id is None
        or (msg_id is not None and fresh.message_id != msg_id)
    ):
        await _ephemeral(
            interaction, "Эта карточка уже используется другой раздачей.")
        return
    if fresh.status != "settled":
        await _ephemeral(
            interaction, "Закрыть можно только завершённую раздачу.")
        return
    # card_closed в БД — кнопки не вернутся ни при одном sync;
    # результат раздачи остаётся видимым. Bump version → sync
    # отрендерит карточку без кнопок. Идемпотентно.
    await blackjack_store.mark_card_closed(stored.round_id)
    fresh = await blackjack_store.get_round(stored.round_id)
    if fresh is not None:
        from games.blackjack import render_round_card
        await render_round_card(interaction, fresh)


async def _handle_again(interaction, stored, *, same_stake: bool) -> None:
    """'again'/'change' → ephemeral подтверждение новой ставки."""
    from games.blackjack import show_bet_menu

    if stored.status != "settled":
        await _ephemeral(interaction, "Новая раздача доступна после завершения текущей.")
        return
    if stored.card_closed:
        # Закрытая карточка — устаревшие Again/Change не создают
        # новую раздачу (проверка на сервере, не только в UI).
        await _ephemeral(interaction, "Эта карточка закрыта.")
        return
    # Одна раздача = одна карточка: повторная игра ВСЕГДА создаёт
    # новое публичное сообщение — message_id не переиспользуется,
    # прежняя карточка остаётся историей результата.
    # interaction уже deferred → ephemeral через followup
    kwargs = {"ephemeral": True,
              "allowed_mentions": discord.AllowedMentions.none()}
    if same_stake:
        stake = stored.stake
        view = ConfirmStakeView(
            interaction.guild_id, interaction.user.id, stored.channel_id,
            stake,
        )
        await interaction.followup.send(view=view, **kwargs)
    else:
        await show_bet_menu(
            interaction, guild_id=interaction.guild_id,
            user_id=interaction.user.id, channel_id=stored.channel_id,
        )


# ------------------------------------------------------------------
# Ephemeral меню ставки / подтверждение
# ------------------------------------------------------------------

def _balance_text(balance: int) -> str:
    return f"Ваш баланс: **{balance:,}**"


class BetMenuView(DesignView):
    """Ephemeral меню выбора ставки. Правила видны до списания."""

    def __init__(self, guild_id, user_id, channel_id, balance):
        super().__init__(title="BLACKJACK — выбор ставки", timeout=300)
        self.guild_id = guild_id
        self.user_id = user_id
        self.channel_id = channel_id

        self.lines(
            _balance_text(balance),
            "",
            RULES_TEXT,
        )
        self.sep()
        row1 = discord.ui.ActionRow()
        for stake in BET_PRESETS:
            row1.add_item(_bound_button(
                f"{stake:,}", "primary",
                lambda i, s=stake: self._pick(i, s)))
        self.container.add_item(row1)
        row2 = discord.ui.ActionRow()
        row2.add_item(_bound_button("Своя ставка", "secondary", self._custom))
        row2.add_item(_bound_button("Без ставки", "secondary", lambda i: self._pick(i, 0)))
        row2.add_item(_bound_button("Закрыть", "danger", self._close))
        self.container.add_item(row2)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await _ephemeral(interaction, "Это чужое меню.")
            return False
        return True

    async def _close(self, interaction):
        await interaction.response.edit_message(
            view=DesignView.simple(title="BLACKJACK", body="Отменено."),
            allowed_mentions=discord.AllowedMentions.none())

    async def _pick(self, interaction, stake: int):
        await interaction.response.edit_message(
            view=ConfirmStakeView(
                self.guild_id, self.user_id, self.channel_id, stake),
            allowed_mentions=discord.AllowedMentions.none())

    async def _custom(self, interaction):
        await interaction.response.send_modal(
            BetModal(self.guild_id, self.user_id, self.channel_id))


def _bound_button(label, style, callback) -> discord.ui.Button:
    from utils.design.components import _STYLE_MAP
    btn = discord.ui.Button(
        label=label, style=_STYLE_MAP.get(style, discord.ButtonStyle.secondary))
    btn.callback = callback
    return btn


class BetModal(discord.ui.Modal, title="Своя ставка"):
    stake = discord.ui.TextInput(
        label="Ставка (10–10 000)", placeholder="например, 250", max_length=6)

    def __init__(self, guild_id, user_id, channel_id):
        super().__init__()
        self.guild_id = guild_id
        self.user_id = user_id
        self.channel_id = channel_id

    async def on_submit(self, interaction):
        raw = self.stake.value.strip().replace(" ", "").replace(",", "")
        try:
            stake = int(raw)
        except ValueError:
            await interaction.response.send_message(
                "Введите целое число.", ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.edit_message(
            view=ConfirmStakeView(
                self.guild_id, self.user_id, self.channel_id, stake),
            allowed_mentions=discord.AllowedMentions.none())


class ConfirmStakeView(DesignView):
    """Финальное подтверждение: отдельное нажатие + новая проверка баланса."""

    def __init__(self, guild_id, user_id, channel_id, stake,
                 op_id: str | None = None):
        super().__init__(title="Подтверждение ставки", timeout=120)
        self.guild_id = guild_id
        self.user_id = user_id
        self.channel_id = channel_id
        self.stake = stake
        # идемпотентность подтверждения: один op_id на экземпляр
        # подтверждения → двойной клик/повтор не спишет ставку дважды
        self.op_id = op_id or uuid.uuid4().hex
        payout = 2 * stake if stake else 0
        self.lines(
            f"Ставка: **{stake:,}**" if stake else "Режим: **без ставки**",
            f"Возможная выплата: **{payout:,}**" if stake else "",
            RULES_TEXT.split("\n")[0],
            "",
            "Подтвердите — ставка будет списана при создании раздачи.",
        )
        row = discord.ui.ActionRow()
        row.add_item(_bound_button("Подтвердить", "success", self._confirm))
        row.add_item(_bound_button("Назад", "secondary", self._back))
        row.add_item(_bound_button("Отмена", "danger", self._cancel))
        self.container.add_item(row)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await _ephemeral(interaction, "Это чужое подтверждение.")
            return False
        return True

    async def _cancel(self, interaction):
        await interaction.response.edit_message(
            view=DesignView.simple(title="BLACKJACK", body="Отменено."),
            allowed_mentions=discord.AllowedMentions.none())

    async def _back(self, interaction):
        from storage.user_balance_store import user_balance_store
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        await interaction.response.edit_message(
            view=BetMenuView(self.guild_id, self.user_id, self.channel_id,
                             balance),
            allowed_mentions=discord.AllowedMentions.none())

    async def _confirm(self, interaction):
        from games.blackjack import deal_round
        await deal_round(
            interaction, guild_id=self.guild_id, user_id=self.user_id,
            channel_id=self.channel_id, stake=self.stake,
            op_id=self.op_id)
