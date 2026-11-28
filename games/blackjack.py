"""BLACKJACK 2.0 — точка входа /games blackjack и оркестрация.

Публичный контракт (совместимость с Dockerfile smoke-import и
cogs/tournament.py): ``start_blackjack(interaction)``.

Поток: ephemeral меню ставки (правила видны до списания) →
подтверждение (op_id — идемпотентность) → атомарное списание +
раздача в PG → одно публичное CV2-сообщение с PNG-столом.
Ходы/settlement — в views/blackjack_view.py и storage/blackjack_store.py.

Доставка имеет явный state machine (delivery_state):
    pending → sending → delivered | unknown | failed | disputed
Право на отправку захватывается атомарно (claim_delivery CAS с
уникальным attempt_id) — не более одного отправителя на раздачу.
Refund выполняется ТОЛЬКО при доказанной недоставке:
'pending' (HTTP к Discord не вызывался) и 'failed' (pre-send сбой
до вызова Discord). 'sending'/'unknown'/'delivered'/'disputed'
автоматически НЕ рефандятся: check_delivery возвращает FOUND или
UNKNOWN — отсутствие маркера в истории никогда не доказывает
недоставку (сообщение могло быть удалено/изменено). Неоднозначные
раздачи разрешаются обычным тайм-аутом; после
MAX_RECONCILE_ATTEMPTS помечаются 'disputed' для администратора.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum

import discord

from config import GAMES_CHANNEL_ID, guild_allowed
from games.blackjack_engine import BET_MAX, BET_MIN, Round
from storage.blackjack_store import (
    ActiveGameExists,
    InsufficientFunds,
    PayoutNotGuaranteed,
    blackjack_store,
)
from storage.user_balance_store import user_balance_store
from utils.design import DesignView

logger = logging.getLogger(__name__)

# Сколько ждать после создания раздачи, прежде чем считать
# 'pending'/'sending'/'unknown' кандидатами на reconcile. Даёт
# запас на штатную доставку и медленный Discord API.
DELIVERY_GRACE_SECONDS = 120
# После стольких безрезультатных проходов reconcile раздача
# помечается 'disputed' — спорный случай для администратора.
MAX_RECONCILE_ATTEMPTS = 30


def _bet_limits(guild_id: int) -> tuple[int, int]:
    """Лимиты ставок из Control Center (economy.blackjack_*), fallback 10–10000."""
    try:
        from services.settings_service import settings_service
        lo = int(settings_service.peek(guild_id, "economy.blackjack_min_bet"))
        hi = int(settings_service.peek(guild_id, "economy.blackjack_max_bet"))
        # Абсолютный технический максимум BET_MAX (10000): финансовая
        # гарантия выплаты рассчитана на него. Ставка 0 — это бесплатный
        # режим и лимитами не управляется, поэтому платный минимум не
        # может быть ниже BET_MIN. Некорректная конфигурация
        # (hi > BET_MAX, lo > hi, вне диапазона) → fallback на дефолт.
        if BET_MIN <= lo <= hi <= BET_MAX:
            return lo, hi
    except Exception:
        logger.debug("blackjack bet limits fallback", exc_info=True)
    return BET_MIN, BET_MAX


def _bets_enabled(guild_id: int) -> bool:
    """Fail-CLOSED: при ошибке настроек ставки ЗАПРЕЩЕНЫ
    (бесплатная игра остаётся). Иначе админ-отключение могло бы
    молча не сработать."""
    try:
        from services.settings_service import settings_service
        return bool(settings_service.peek(guild_id, "economy.blackjack_bets_enabled"))
    except Exception:
        logger.warning("blackjack bets_enabled check failed — "
                       "ставки запрещены до восстановления настроек",
                       exc_info=True)
        return False


def _minigames_enabled(guild_id: int) -> bool:
    try:
        from services.settings_service import settings_service
        return bool(settings_service.peek(guild_id, "feature.minigames_enabled"))
    except Exception:
        logger.warning("blackjack minigames_enabled check failed",
                       exc_info=True)
        return False


async def _precheck(interaction: discord.Interaction) -> bool:
    """guild/channel/feature-флаги. False → уже отвечено ephemeral."""
    if interaction.guild_id is None or not guild_allowed(interaction.guild_id):
        await interaction.response.send_message(
            "Блэкджек доступен на сервере r!z3.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return False
    if GAMES_CHANNEL_ID and interaction.channel_id != GAMES_CHANNEL_ID:
        await interaction.response.send_message(
            f"Чтобы сыграть в блэкджек, перейдите в <#{GAMES_CHANNEL_ID}>.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return False
    if not _minigames_enabled(interaction.guild_id):
        await interaction.response.send_message(
            "Этот раздел временно отключён.", ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())
        return False
    return True


async def start_blackjack(interaction: discord.Interaction) -> None:
    """Slash-команда /games blackjack и кнопка «Сыграть» в /games list."""
    if not await _precheck(interaction):
        return
    await show_bet_menu(
        interaction,
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        channel_id=interaction.channel_id,
    )


async def show_bet_menu(
    interaction: discord.Interaction,
    *,
    guild_id: int,
    user_id: int,
    channel_id: int,
) -> None:
    """Ephemeral меню ставки. Правила видны до списания.

    PG-запросы перед ответом → defer(ephemeral) при необходимости,
    затем followup. Работает и из slash, и из deferred component
    interaction (тогда defer пропускается)."""
    from views.blackjack_view import BetMenuView

    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

    async def _note(text: str) -> None:
        await interaction.followup.send(
            text, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none())

    try:
        active = await blackjack_store.get_active(guild_id, user_id)
    except Exception:
        active = None
        logger.info("blackjack get_active failed (non-fatal)", exc_info=True)
    if active is not None:
        await _note("У вас уже есть активная раздача — завершите её.")
        return
    try:
        balance = await user_balance_store.get_balance(guild_id, user_id)
    except Exception:
        logger.exception("blackjack balance read failed")
        await _note("Не удалось прочитать баланс. Попробуйте позже.")
        return
    view = BetMenuView(guild_id, user_id, channel_id, balance)
    await interaction.followup.send(
        view=view, ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none())


async def deal_round(
    interaction: discord.Interaction,
    *,
    guild_id: int,
    user_id: int,
    channel_id: int,
    stake: int,
    op_id: str | None = None,
) -> None:
    """Подтверждённая ставка → атомарное списание + раздача + доставка.

    Одна раздача = одно НОВОЕ публичное сообщение: message_id никогда
    не переиспользуется между round_id — предыдущая карточка остаётся
    историей результата и не может быть перезаписана новой игрой.

    ``op_id`` делает подтверждение идемпотентным: повторный вызов с
    тем же op_id (двойной клик, retry) возвращает существующий round_id
    без повторного списания — включая случай мгновенного natural,
    когда partial unique index уже не защищает от второй раздачи.
    """
    from views.blackjack_view import build_game_view

    # Сразу ack на ephemeral-кнопку — дальше работа может занять >3с.
    try:
        await interaction.response.edit_message(
            view=DesignView.simple(title="BLACKJACK", body="Создаём раздачу…"),
            allowed_mentions=discord.AllowedMentions.none())
    except discord.InteractionResponded:
        pass

    async def _fail(text: str) -> None:
        view = DesignView.simple(title="BLACKJACK", body=text)
        try:
            await interaction.edit_original_response(
                view=view, allowed_mentions=discord.AllowedMentions.none())
        except Exception:  # noqa: BLE001 — любой сбой Discord → пробуем followup
            try:
                await interaction.followup.send(
                    text, ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none())
            except Exception:
                logger.debug("blackjack fail-notify failed", exc_info=True)

    if stake < 0 or stake > BET_MAX:
        await _fail(f"Некорректная ставка (максимум {BET_MAX:,}).")
        return
    lo, hi = _bet_limits(guild_id)
    if stake > 0:
        if not _bets_enabled(guild_id):
            await _fail("Ставки сейчас отключены. "
                        "Доступна бесплатная игра.")
            return
        if stake < lo or stake > hi:
            await _fail(f"Ставка должна быть от {lo:,} до {hi:,}.")
            return

    if not _minigames_enabled(guild_id):
        await _fail("Этот раздел временно отключён.")
        return

    game = Round.deal()
    try:
        round_id, created = await blackjack_store.create_round(
            guild_id, user_id, channel_id, stake, game, op_id=op_id)
    except InsufficientFunds:
        balance = await user_balance_store.get_balance(guild_id, user_id)
        await _fail(f"Недостаточно монет. У вас: **{balance:,}**, "
                    f"ставка: **{stake:,}**.")
        return
    except PayoutNotGuaranteed:
        await _fail("Эта ставка не может быть гарантированно оплачена "
                    "при текущем балансе. Ставка не списана.")
        return
    except ActiveGameExists:
        await _fail("У вас уже есть активная раздача.")
        return
    except Exception:
        logger.exception("blackjack create_round failed")
        await _fail("Не удалось создать раздачу. Попробуйте позже.")
        return

    stored = await blackjack_store.get_round(round_id)
    if stored is None:
        await _fail("Ошибка состояния раздачи.")
        return
    if not created:
        # идемпотентный повтор подтверждения
        if stored.message_id is not None:
            await _fail("Раздача уже создана — смотрите сообщение выше.")
            return
        if stored.status not in ("active", "settled"):
            await _fail("Эта раздача уже завершена.")
            return
        if stored.delivery_state in ("sending", "unknown", "delivered"):
            # первая попытка ещё в полёте или доставка подтверждена
            # без message_id — не создаём второе публичное сообщение
            await _fail("Раздача уже отправляется — смотрите канал.")
            return
        # delivery_state pending/failed → повторная попытка доставки
        logger.info("blackjack op replay: redelivering %s", round_id)

    view, png = build_game_view(stored)

    # ---------- атомарная доставка ----------
    # CAS-захват права на отправку: только один обработчик (в т.ч.
    # в двух процессах) получит 'sending' для этого round_id.
    attempt_id = uuid.uuid4().hex
    try:
        claimed = await blackjack_store.claim_delivery(round_id, attempt_id)
    except Exception:
        logger.exception("blackjack claim_delivery failed %s", round_id)
        await _fail("Не удалось создать раздачу. Попробуйте позже.")
        return
    if not claimed:
        # кто-то уже отправляет или доставил
        cur = await blackjack_store.get_round(round_id)
        if cur is not None and cur.message_id is not None:
            await _fail("Раздача уже создана — смотрите сообщение выше.")
        else:
            await _fail("Раздача уже отправляется — статус проверяется.")
        return

    # Pre-send: всё, что может доказуемо НЕ дойти до Discord.
    # Всегда НОВОЕ публичное сообщение — reuse message_id запрещён.
    try:
        kwargs = {"view": view,
                  "ephemeral": False,
                  "wait": True,
                  "allowed_mentions": discord.AllowedMentions.none()}
        if png is not None:
            kwargs["files"] = [png]
    except Exception:
        # Discord ещё не вызывался → доставка доказуемо не состоялась
        logger.warning("blackjack pre-send failed for %s", round_id,
                       exc_info=True)
        try:
            await blackjack_store.mark_delivery(
                round_id, "failed", attempt_id)
        except Exception:
            logger.exception("blackjack mark_delivery(failed) failed")
        await _refund_and_fail(interaction, round_id, _fail)
        return

    # Граница отправки: с этого момента исход HTTP-вызова неизвестен.
    delivered = False
    sent_id = None
    try:
        msg = await interaction.followup.send(**kwargs)
        sent_id = msg.id if msg is not None else None
        delivered = sent_id is not None
    except Exception:
        logger.warning("blackjack deal delivery failed for %s",
                       round_id, exc_info=True)

    if delivered:
        try:
            # sent_version = версия, из которой построено сообщение —
            # card_rendered фиксирует ФАКТИЧЕСКИ отправленное,
            # а не текущую версию БД (она могла уйти вперёд).
            await blackjack_store.mark_message(
                round_id, sent_id, sent_version=stored.version)
        except Exception:
            # Ответ mark_message потерян, но доставка ДОКАЗАНА sent_id:
            # фиксируем 'delivered' без message_id — refund запрещён,
            # reconcile найдёт карточку по маркеру и дозапишет id.
            logger.exception("blackjack mark_message failed %s", round_id)
            try:
                await blackjack_store.mark_delivery(
                    round_id, "delivered", attempt_id)
            except Exception:
                logger.exception("blackjack mark_delivery(delivered) failed")
    else:
        try:
            await blackjack_store.mark_delivery(
                round_id, "unknown", attempt_id)
        except Exception:
            logger.exception("blackjack mark_delivery(unknown) failed")
        # 'unknown' → НЕ рефандим и НЕ пересылаем: сообщение могло
        # дойти. Reconcile проверит маркер; исход раздачи решит
        # обычный тайм-аут — ставка не зависает.
        settled_note = ""
        try:
            cur = await blackjack_store.get_round(round_id)
            if cur is not None and cur.status == "settled":
                settled_note = " Раздача уже рассчитана."
        except Exception:
            logger.debug("blackjack settled-check failed", exc_info=True)
        await _fail(
            "Не удалось подтвердить доставку раздачи. "
            "Статус проверяется — средства не потеряны." + settled_note)
        return

    # ephemeral-подтверждение → короткий статус
    try:
        await interaction.edit_original_response(
            view=DesignView.simple(
                title="BLACKJACK",
                body="Раздача началась — карта выше. Удачи!"),
            allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        logger.debug("blackjack deal-status edit failed", exc_info=True)


async def _refund_and_fail(interaction, round_id: str, fail) -> None:
    """Доказанная недоставка ('failed'/'pending') → idempotent refund."""
    try:
        refunded = await blackjack_store.refund_undelivered(round_id)
    except Exception:
        refunded = False
        logger.exception("blackjack refund failed for %s", round_id)
    await fail(
        "Не удалось отправить раздачу. "
        + ("Ставка возвращена." if refunded else
           "Статус раздачи проверяется — средства не потеряны."))


# ------------------------------------------------------------------
# Тайм-аут / доставка / recovery
# ------------------------------------------------------------------

_DELIVERY_MARKER_PREFIX = "ref "
_SCAN_LIMIT = 500


class DeliveryCheck(Enum):
    """Типизированный результат проверки доставки.

    FOUND — маркер найден, сообщение существует.
    CONFIRMED_NOT_SENT — отправка доказуемо не состоялась
        (delivery_state='pending': HTTP к Discord не вызывался).
    UNKNOWN — всё остальное: ошибки API, недоступная/неполная
        история, лимит сканирования, ошибки парсинга, отсутствие
        маркера, удалённое/изменённое сообщение. Отсутствие маркера
        НИКОГДА не доказывает недоставку.
    """
    FOUND = "found"
    CONFIRMED_NOT_SENT = "confirmed_not_sent"
    UNKNOWN = "unknown"


def _msg_has_marker(msg: discord.Message, round_id: str) -> bool:
    """CV2-сообщение содержит маркер ``-# ref <round_id>`` в components."""
    try:
        blob = json.dumps(
            [c.to_dict() for c in (msg.components or [])],
            ensure_ascii=False,
        )
    except Exception as exc:
        # Ошибка парсинга компонента → результат неполный → UNKNOWN
        raise _MarkerScanError() from exc
    return f"{_DELIVERY_MARKER_PREFIX}{round_id}" in blob


class _MarkerScanError(Exception):
    pass


async def check_delivery(
    client: discord.Client, stored
) -> tuple[DeliveryCheck, discord.Message | None]:
    """Проверить доставку раздачи. Никогда не «доказывает» отсутствие
    отправки по результатам сканирования — только FOUND или UNKNOWN.

    CONFIRMED_NOT_SENT возвращается только для 'pending' — состояния,
    в котором HTTP-вызов к Discord доказуемо не выполнялся.
    """
    if stored.delivery_state == "pending":
        return DeliveryCheck.CONFIRMED_NOT_SENT, None
    channel = client.get_channel(stored.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(stored.channel_id)
        except Exception:  # noqa: BLE001 — Forbidden/HTTPException/NotFound
            return DeliveryCheck.UNKNOWN, None
    since = (stored.created_at or datetime.now(UTC)) - timedelta(seconds=10)
    scanned = 0
    try:
        async for msg in channel.history(
            limit=_SCAN_LIMIT, after=since, oldest_first=True
        ):
            scanned += 1
            try:
                if _msg_has_marker(msg, stored.round_id):
                    return DeliveryCheck.FOUND, msg
            except _MarkerScanError:
                return DeliveryCheck.UNKNOWN, None
        # История прочитана целиком без маркера — но сообщение могло
        # быть удалено/изменено → это UNKNOWN, а не «не доставлено».
        if scanned >= _SCAN_LIMIT:
            logger.info("blackjack scan hit limit for %s", stored.round_id)
        return DeliveryCheck.UNKNOWN, None
    except Exception:
        logger.info("blackjack marker scan failed for %s",
                    stored.round_id, exc_info=True)
        return DeliveryCheck.UNKNOWN, None


async def reconcile_undelivered(client: discord.Client) -> int:
    """Сверка недоставленных раздач старше DELIVERY_GRACE_SECONDS.

    Политика:
    - 'pending'/'failed' — отправка доказуемо не состоялась → refund.
    - 'sending'/'unknown'/'delivered'-без-id — только поиск маркера:
      FOUND → delivered; иначе UNKNOWN → +1 попытка, после
      MAX_RECONCILE_ATTEMPTS → 'disputed' (админ-диагностика).
      Автоматический refund НЕ выполняется — неоднозначная доставка
      никогда не рефандится. Раздача завершается обычным тайм-аутом.
    """
    cutoff = datetime.now(UTC) - timedelta(seconds=DELIVERY_GRACE_SECONDS)
    try:
        rounds = await blackjack_store.list_undelivered(cutoff)
    except Exception:
        logger.warning("blackjack list_undelivered failed", exc_info=True)
        return 0
    handled = 0
    for stored in rounds:
        try:
            if stored.delivery_state in ("pending", "failed"):
                # 'pending': send не вызывался; 'failed': pre-send сбой
                if await blackjack_store.refund_undelivered(stored.round_id):
                    handled += 1
                continue
            status, msg = await check_delivery(client, stored)
            if status is DeliveryCheck.FOUND and msg is not None:
                try:
                    # sent_version неизвестна (сообщение отправлено
                    # раньше) → card_rendered=-1: sync догонит
                    # актуальное состояние карточки.
                    await blackjack_store.mark_message(
                        stored.round_id, msg.id)
                    handled += 1
                    logger.info("blackjack reconcile: delivery confirmed "
                                "for %s (msg %s)", stored.round_id, msg.id)
                except Exception:
                    logger.exception("blackjack reconcile mark failed")
                continue
            if status is DeliveryCheck.CONFIRMED_NOT_SENT:
                # теоретически unreachable (pending обработан выше),
                # но на случай гонки состояний — только через refund-guard
                if await blackjack_store.refund_undelivered(
                        stored.round_id):
                    handled += 1
                continue
            # UNKNOWN — попытка зачтена, спорный счётчик
            try:
                attempts = await blackjack_store.note_reconcile_attempt(
                    stored.round_id)
                if attempts >= MAX_RECONCILE_ATTEMPTS:
                    await blackjack_store.mark_disputed(stored.round_id)
                    logger.error(
                        "blackjack DISPUTED delivery round_id=%s "
                        "guild=%s user=%s stake=%s state=%s — "
                        "требуется проверка администратора",
                        stored.round_id, stored.guild_id, stored.user_id,
                        stored.stake, stored.delivery_state)
            except Exception:
                logger.exception("blackjack reconcile attempt mark failed")
        except Exception:
            logger.warning("blackjack reconcile failed for %s",
                           stored.round_id, exc_info=True)
    return handled


async def sync_round_card(client: discord.Client, round_id: str) -> bool:
    """Единая точка серверного обновления публичной карточки
    (sweeper / recovery / повторная синхронизация).

    Сериализация через claim_card_render CAS по version: только
    актуальный рендерер выполняет Discord HTTP; порядок завершения
    запросов не важен — устаревший claim отсекается до отправки,
    свежий вид всегда строится из актуального состояния БД.
    При ошибке Discord API claim откатывается — следующий проход
    повторит (list_needing_sync). Финансовое состояние не трогается.
    """
    from views.blackjack_view import build_game_view
    render_op = uuid.uuid4().hex
    try:
        stored = await blackjack_store.get_round(round_id)
        if stored is None or stored.message_id is None:
            return False
        if not await blackjack_store.claim_card_render(
                stored.round_id, render_op):
            return False
        # Coalescing: после claim перечитываем состояние — между
        # get_round и claim version могла уйти вперёд; рендерим
        # самое свежее, промежуточные версии пропускаем.
        fresh = await blackjack_store.get_round(round_id)
        if fresh is None:
            await blackjack_store.finish_card_render(
                stored.round_id, render_op, False)
            return False
        try:
            channel = client.get_channel(fresh.channel_id)
            if channel is None:
                channel = await client.fetch_channel(fresh.channel_id)
            msg = await channel.fetch_message(fresh.message_id)
            view, png = build_game_view(fresh)
            kwargs = {"view": view,
                      "allowed_mentions": discord.AllowedMentions.none()}
            if png is not None:
                kwargs["attachments"] = [png]
            await msg.edit(**kwargs)
        except Exception:
            try:
                await blackjack_store.finish_card_render(
                    stored.round_id, render_op, False)
            except Exception:
                logger.exception("blackjack finish_card_render(fail) %s",
                                 round_id)
            logger.info("blackjack card sync edit failed %s", round_id,
                        exc_info=True)
            return False
        try:
            await blackjack_store.finish_card_render(
                stored.round_id, render_op, True)
        except Exception:
            logger.exception("blackjack finish_card_render(ok) %s",
                             round_id)
        return True
    except Exception:
        logger.info("blackjack card sync skipped %s", round_id,
                    exc_info=True)
        return False


async def render_round_card(
    interaction: discord.Interaction, stored
) -> bool:
    """Та же сериализация для deferred component-interaction:
    публичная карточка обновляется через edit_original_response
    под claim-CAS (hit/stand/close/stale-refresh)."""
    from views.blackjack_view import build_game_view
    render_op = uuid.uuid4().hex
    try:
        if not await blackjack_store.claim_card_render(
                stored.round_id, render_op):
            return False
        # coalescing: рендерим самое свежее состояние после claim
        fresh = await blackjack_store.get_round(stored.round_id)
        if fresh is None:
            await blackjack_store.finish_card_render(
                stored.round_id, render_op, False)
            return False
        view, f = build_game_view(fresh)
        kwargs = {"view": view,
                  "allowed_mentions": discord.AllowedMentions.none()}
        if f is not None:
            kwargs["attachments"] = [f]
        await interaction.edit_original_response(**kwargs)
    except Exception:
        try:
            await blackjack_store.finish_card_render(
                stored.round_id, render_op, False)
        except Exception:
            logger.exception("blackjack finish_card_render(fail) %s",
                             stored.round_id)
        logger.info("blackjack interaction card edit failed %s",
                    stored.round_id, exc_info=True)
        return False
    try:
        await blackjack_store.finish_card_render(
            stored.round_id, render_op, True)
    except Exception:
        logger.exception("blackjack finish_card_render(ok) %s",
                         stored.round_id)
    return True


async def sweep_once() -> int:
    """Один проход: reconcile доставки → тайм-аут → догонка карточек.

    Reconcile идёт ПЕРВЫМ: доказанно недоставленные раздачи
    возвращаются до того, как expire_due мог бы рассчитать их
    как обычную игру (expire_due также исключает pending/failed).
    Затем expire_due → sync затронутых карт, и отдельный проход
    list_needing_sync — карточки с неподтверждённым рендером
    (ошибка Discord API, рестарт между settle и edit).
    """
    # Чистое окружение: sweeper не создаёт таблицы сам — без
    # ensure_schema каждый проход падал бы с UndefinedTableError
    # до первой раздачи. Идемпотентно (флаг после первого успеха).
    try:
        await blackjack_store.ensure_schema()
    except Exception:
        logger.warning("blackjack ensure_schema failed",
                       exc_info=True)
    client = _client
    if client is not None:
        try:
            await reconcile_undelivered(client)
        except Exception:
            logger.warning("blackjack reconcile pass failed", exc_info=True)
    try:
        settled = await blackjack_store.expire_due()
    except Exception:
        logger.warning("blackjack expire_due failed", exc_info=True)
        settled = []
    for stored in settled:
        if client is not None:
            await sync_round_card(client, stored.round_id)
    if client is not None:
        try:
            pending = await blackjack_store.list_needing_sync()
        except Exception:
            logger.warning("blackjack list_needing_sync failed",
                           exc_info=True)
            pending = []
        for stored in pending:
            await sync_round_card(client, stored.round_id)
    return len(settled)


async def recover_active(client: discord.Client) -> int:
    """После рестарта: перечитать активные раздачи, обновить сообщения
    и сверить недоставленные. Идемпотентно — безопасен при повторном
    запуске."""
    global _client
    _client = client
    # Схема до recover/sweeper: на чистом окружении read-пути
    # (list_active, list_undelivered, expire_due) не создают
    # таблицы — без ensure_schema старт падал бы с
    # UndefinedTableError. Идемпотентно.
    try:
        await blackjack_store.ensure_schema()
    except Exception:
        logger.warning("blackjack ensure_schema failed",
                       exc_info=True)
    try:
        active = await blackjack_store.list_active()
    except Exception:
        logger.warning("blackjack recovery list failed", exc_info=True)
        active = []
    n = 0
    for stored in active:
        if await sync_round_card(client, stored.round_id):
            n += 1
    if n:
        logger.info("blackjack: восстановлено %d активных раздач", n)
    # сверка доставки — отдельный проход, не зависит от list_active
    try:
        await reconcile_undelivered(client)
    except Exception:
        logger.warning("blackjack recover reconcile failed", exc_info=True)
    return n


_client: discord.Client | None = None


async def blackjack_sweeper_loop(interval: float = 20.0) -> None:
    """Фоновый цикл тайм-аута и reconcile (запускается из bot.setup_hook)."""
    import asyncio
    while True:
        try:
            await sweep_once()
        except Exception:
            logger.warning("blackjack sweeper iteration failed", exc_info=True)
        await asyncio.sleep(interval)
