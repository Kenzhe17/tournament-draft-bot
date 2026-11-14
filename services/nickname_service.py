"""Платная смена серверного ника — валидация и транзакционная покупка.

Сервис отделён от стандартной ветки BuyButton (инвентарь/косметика):
услуга не создаёт предмет инвентаря и не учитывается в его лимите.

Гарантии:
- идентичность — только interaction.user (свой ник);
- баланс проверяется и списывается атомарно (subtract_balance);
- каждая покупка — операция с уникальным op_id в
  nickname_ops_store (reserved → applied/refunded/unknown);
- блокировка параллельных покупок одного пользователя (asyncio.Lock
  + проверка активной операции);
- возврат идемпотентен: add_balance_once по ключу nickref:{op_id}
  (в PG — UNIQUE-грант, в JSON — защита статусом операции);
- неизвестный исход Discord (сетевой сбой) разрешается проверкой
  фактического ника, а не повторной покупкой;
- recover_pending() при старте бота разрешает операции,
  прерванные перезапуском.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata

import discord

from storage.nickname_ops_store import (
    STATUS_APPLIED,
    STATUS_REFUNDED,
    STATUS_UNKNOWN,
    nickname_ops_store,
)
from storage.user_balance_store import user_balance_store

logger = logging.getLogger(__name__)

SERVICE_ITEM_ID = "nickname_change"
NICKNAME_CHANGE_PRICE = 1000
NICK_EDIT_REASON = "r!z3 shop nickname change"

# Параллельные покупки одного пользователя сериализуются.
_user_locks: dict[tuple[int, int], asyncio.Lock] = {}
_locks_guard = asyncio.Lock()


async def _user_lock(guild_id: int, user_id: int) -> asyncio.Lock:
    key = (guild_id, user_id)
    async with _locks_guard:
        lock = _user_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[key] = lock
        return lock


# ---------------------------------------------------------------- validation

_CONTROL_OR_FORMAT = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_MENTIONS = re.compile(r"@everyone|@here", re.IGNORECASE)
_INVITE = re.compile(r"(?:discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)", re.IGNORECASE)


def validate_nickname(nick: str, current_nick: str) -> str | None:
    """Проверить новый ник. Возвращает текст ошибки или None."""
    nick = (nick or "").strip()
    if not nick:
        return "Ник не может быть пустым."
    if len(nick) > 32:
        return "Ник длиннее 32 символов."
    if _CONTROL_OR_FORMAT.search(nick):
        return "Ник содержит запрещённые управляющие символы или невидимое форматирование."
    if _MENTIONS.search(nick):
        return "Ник не может содержать @everyone или @here."
    if _INVITE.search(nick):
        return "Ник не может содержать приглашения в Discord."
    # Discord запрещает часть символов в никах — отсекаем явно.
    for ch in nick:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            return "Ник содержит недопустимые символы."
    if nick.casefold() == (current_nick or "").strip().casefold():
        return "Новый ник совпадает с текущим."
    return None


# ------------------------------------------------------------------- checks


async def check_preconditions(
    interaction: discord.Interaction,
) -> tuple[discord.Member | None, str | None]:
    """Проверки до резервирования. Возвращает (member, error)."""
    guild = interaction.guild
    if guild is None:
        return None, "Команда доступна только на сервере."

    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.HTTPException:
            member = None
    if member is None:
        return None, "Не удалось получить данные участника сервера."

    me = guild.me
    if me is not None and hasattr(me, "guild_permissions"):
        if not me.guild_permissions.manage_nicknames:
            return None, (
                "Бот не имеет права «Управление никами» — "
                "обратитесь к администратору сервера."
            )

    return member, None


def check_nickname_rules(member: discord.Member, new_nick: str) -> str | None:
    """Правила Discord для ников, проверяемые без списания."""
    current = member.nick or member.display_name
    return validate_nickname(new_nick, current)


def check_hierarchy(member: discord.Member, bot_member: discord.Member | None) -> str | None:
    """Иерархия ролей: бот не может менять ник участнику выше себя."""
    if bot_member is None:
        return None  # проверит Discord при edit
    if member.top_role >= bot_member.top_role and member != bot_member:
        return (
            "Роль участника выше роли бота — Discord не позволит "
            "изменить его ник."
        )
    return None


# ------------------------------------------------------------------ purchase


async def _refund_once(guild_id: int, user_id: int, price: int, op_id: str) -> None:
    """Идемпотентный возврат: DB-грант nickref:{op_id} или статус операции."""
    try:
        await user_balance_store.add_balance_once(
            guild_id, user_id, price, f"nickref:{op_id}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("nickname refund failed op=%s: %s", op_id, exc)


async def _resolve_op_by_nick(
    guild: discord.Guild | None, op: dict
) -> str:
    """Разрешить операцию с неизвестным исходом по фактическому нику.

    Возвращает финальный статус ('applied'/'refunded').
    """
    op_id = op["op_id"]
    guild_id = op["guild_id"]
    user_id = op["user_id"]
    new_nick = op["new_nick"]

    applied = False
    if guild is not None:
        try:
            member = await guild.fetch_member(user_id)
            applied = (member.nick or "") == new_nick or (
                not member.nick and member.display_name == new_nick
            )
        except discord.HTTPException as exc:
            logger.warning("nick op %s: member fetch failed: %s", op_id, exc)

    if applied:
        await nickname_ops_store.set_status(op_id, STATUS_APPLIED)
        return STATUS_APPLIED

    # Ник не изменился (или участник недоступен) — подтверждённый
    # отказ: возвращаем монеты идемпотентно.
    await nickname_ops_store.set_status(op_id, STATUS_REFUNDED)
    await _refund_once(guild_id, user_id, int(op["price"]), op_id)
    return STATUS_REFUNDED


async def purchase_nickname_change(
    interaction: discord.Interaction,
    new_nick: str,
    price: int,
) -> tuple[str, dict]:
    """Выполнить покупку смены ника.

    Returns (result, payload):
      result: 'success' | 'error'
      payload: {'message': str, 'balance': int|None, 'op_id': str|None}
    """
    guild = interaction.guild
    user_id = interaction.user.id

    lock = await _user_lock(guild.id if guild else 0, user_id)
    async with lock:
        # Активная операция — не допускаем параллельную покупку.
        active = nickname_ops_store.active_op_for_user(
            interaction.guild_id or 0, user_id
        )
        if active is not None:
            return "error", {
                "message": (
                    "У вас уже есть незавершённая операция смены ника. "
                    "Дождитесь её завершения."
                ),
                "op_id": active["op_id"],
            }

        member, err = await check_preconditions(interaction)
        if err:
            return "error", {"message": err}

        err = check_nickname_rules(member, new_nick)
        if err:
            return "error", {"message": err}

        bot_member = guild.me if guild else None
        err = check_hierarchy(member, bot_member)
        if err:
            return "error", {"message": err}

        old_nick = member.nick or member.display_name
        new_nick = new_nick.strip()

        # 1. Операция reserved — до списания.
        op = await nickname_ops_store.create(
            guild_id=interaction.guild_id or 0,
            user_id=user_id,
            old_nick=old_nick,
            new_nick=new_nick,
            price=price,
        )
        op_id = op["op_id"]

        # 2. Атомарное резервирование: свежий баланс, списание под
        #    транзакцией. ValueError = недостаточно монет.
        try:
            new_balance = await user_balance_store.subtract_balance(
                interaction.guild_id or 0, user_id, price
            )
        except ValueError:
            await nickname_ops_store.set_status(op_id, STATUS_REFUNDED)
            return "error", {
                "message": (
                    f"Недостаточно монет. Нужно: {price}, "
                    "проверьте баланс в профиле."
                ),
                "op_id": op_id,
            }
        except Exception as exc:  # noqa: BLE001
            await nickname_ops_store.set_status(op_id, STATUS_REFUNDED)
            logger.error("nickname charge failed op=%s: %s", op_id, exc)
            return "error", {
                "message": "Не удалось проверить баланс. Попробуйте позже.",
                "op_id": op_id,
            }

        await nickname_ops_store.mark_charged(op_id)

        # 3. Discord edit.
        try:
            await member.edit(
                nick=new_nick, reason=NICK_EDIT_REASON
            )
        except discord.Forbidden:
            # Подтверждённый отказ Discord (права/иерархия).
            await nickname_ops_store.set_status(op_id, STATUS_REFUNDED)
            await _refund_once(
                interaction.guild_id or 0, user_id, price, op_id
            )
            return "error", {
                "message": (
                    "Discord запретил смену ника: проверьте права бота "
                    "и иерархию ролей. Монеты возвращены."
                ),
                "balance": await _safe_balance(
                    interaction.guild_id or 0, user_id
                ),
                "op_id": op_id,
            }
        except discord.HTTPException as exc:
            await nickname_ops_store.set_status(op_id, STATUS_REFUNDED)
            await _refund_once(
                interaction.guild_id or 0, user_id, price, op_id
            )
            return "error", {
                "message": (
                    f"Discord отклонил новый ник ({exc.status}). "
                    "Монеты возвращены."
                ),
                "balance": await _safe_balance(
                    interaction.guild_id or 0, user_id
                ),
                "op_id": op_id,
            }
        except Exception as exc:  # noqa: BLE001 - неизвестный исход
            # Сетевой сбой/таймаут: НЕ повторяем покупку — проверяем
            # фактический ник.
            logger.warning(
                "nickname edit unknown outcome op=%s: %s", op_id, exc
            )
            await nickname_ops_store.mark_unknown(op_id)
            final = await _resolve_op_by_nick(guild, {**op, "op_id": op_id})
            if final == STATUS_APPLIED:
                return "success", {
                    "message": "НИК ИЗМЕНЁН",
                    "balance": new_balance,
                    "op_id": op_id,
                    "old_nick": old_nick,
                    "new_nick": new_nick,
                }
            return "error", {
                "message": (
                    "Не удалось связаться с Discord. Монеты возвращены, "
                    "ник не изменён."
                ),
                "balance": await _safe_balance(
                    interaction.guild_id or 0, user_id
                ),
                "op_id": op_id,
            }

        # 4. Финальное подтверждение.
        await nickname_ops_store.set_status(op_id, STATUS_APPLIED)
        return "success", {
            "message": "НИК ИЗМЕНЁН",
            "balance": new_balance,
            "op_id": op_id,
            "old_nick": old_nick,
            "new_nick": new_nick,
        }


async def _safe_balance(guild_id: int, user_id: int) -> int | None:
    try:
        return await user_balance_store.get_balance(guild_id, user_id)
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------- audit hook


async def record_nickname_audit(
    interaction: discord.Interaction,
    *,
    op_id: str,
    old_nick: str,
    new_nick: str,
    price: int,
    result: str,
) -> None:
    """Запись в журнал аудита (не ломает сценарий при сбое)."""
    try:
        from services import tournament_audit_service as audit_svc

        await audit_svc.record_interaction(
            interaction,
            target_user_id=interaction.user.id,
            action=audit_svc.A.SHOP_NICKNAME_CHANGE,
            source="shop",
            before={"nick": old_nick},
            after={
                "nick": new_nick,
                "price": price,
                "result": result,
                "op_id": op_id,
            },
            dedup_key=f"nick:{op_id}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("nickname audit failed: %s", exc)


# ---------------------------------------------------------------- recovery


async def recover_pending(bot: discord.Client) -> int:
    """Разрешить операции, прерванные перезапуском бота.

    Для каждой незавершённой операции проверяется фактический ник
    участника: совпал → applied (списание сохраняется), иначе →
    refunded с идемпотентным возвратом.
    """
    ops = nickname_ops_store.unresolved_ops()
    if not ops:
        return 0

    resolved = 0
    for op in ops:
        guild = bot.get_guild(int(op["guild_id"]))
        try:
            await _resolve_op_by_nick(guild, op)
            resolved += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "nickname recovery failed op=%s: %s", op.get("op_id"), exc
            )
    if resolved:
        logger.info("nickname ops recovery: resolved %d/%d", resolved, len(ops))
    return resolved
