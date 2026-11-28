"""Спонтанные события 2.0 — Coin Drop cog.

Двенадцать... ровно 11 игровых типов (services/event_games.py):
speed, raffle, chests, quick_math, minority, dice, growing,
word, last_click, guess_number, krokodil.

Ключевые гарантии:
- Активити-гейт: sliding 5-мин окно (services/activity_tracker.py),
  оценка ТОЛЬКО на входящем сообщении; кулдаун 15 мин от успешного
  старта персистентен в event_channel_state (переживает рестарт).
- Одно активное событие на guild/channel — частичный UNIQUE-
  индекс spontaneous_events; естественный триггер и /forcedrop
  делят один атомарный claim.
- Приз выбирается и фиксируется ДО публикации карточки
  (services/event_rewards.py): coins/xp/rep/item.
- Выплаты — event_rewards ledger (как drop_coin_rewards):
  grant_key PK, lease+fencing, exactly-once через
  add_balance_once / add_xp_once / award_rep_logged(event_key) /
  инвентарь по item_id. Legacy drop_coin_rewards и pending-файл
  продолжают обслуживаться тем же worker'ом.
- Одна публичная карточка на событие; результаты — edit той же
  карточки с убранными кнопками. Кнопки — persistent DynamicItem
  ``se2:{event_id}:{action}`` (dispatch и после рестарта);
  модалки пересоздаются в callback — состояние в PostgreSQL.
- Дедлайны — серверное UTC время (deadline_at/close_at в БД +
  time.time() на клиенте); скрытые дедлайны (burn, last_click)
  не раскрываются в карточке.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import discord
from utils.design import compose, smart_view

from discord import app_commands
from discord.ext import commands

from config import get_emoji, BOT_OWNER_ID, NOTIFICATION_CHANNEL_ID, DATA_DIR
from services import event_config, event_games, event_rewards
from services.activity_tracker import ActivityGate
from storage.drop_reward_store import drop_reward_store
from storage.drop_pending_store import DropPendingStore, drop_pending_store
from storage.event_reward_store import event_reward_store
from storage.event_store import event_store, _json_field
from storage.user_balance_store import user_balance_store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


# Custom check for bot owner
def is_bot_owner():
    def predicate(interaction: discord.Interaction):
        return interaction.user.id == BOT_OWNER_ID
    return app_commands.check(predicate)


# ==========================================
# Legacy coin-drop grant path (drop_coin_rewards)
# ==========================================
# Сохранён для обратной совместимости: старые pending-обязательства
# и возможные внешние вызовы. Новые события используют
# event_reward_store/event_pending_store.


async def add_balance(guild_id: int, user_id: int, amount: int) -> None:
    """Зачисление монет пользователю."""
    if amount <= 0:
        return
    await user_balance_store.add_balance(guild_id, user_id, amount)


_DROP_BACKOFF_BASE = 30.0
_DROP_BACKOFF_CAP = 1800.0
_DROP_WORKER_INTERVAL = 15.0
# Троттлинг перерисовки карточки «Растущий куш» и join-обновлений.
_CARD_EDIT_MIN_INTERVAL = 4.0


def _drop_backoff(attempts: int) -> float:
    return min(_DROP_BACKOFF_BASE * (2 ** max(0, attempts - 1)),
               _DROP_BACKOFF_CAP)


async def grant_drop_reward(cog, guild_id: int, user_id: int,
                            amount: int, grant_key: str,
                            kind: str) -> str:
    """Legacy-протокол фиксации награды drop_coin_rewards.

    Возвращает "credited" | "queued" | "other" | "failed".
    """
    if amount <= 0:
        return "credited"

    async def _fixate() -> str:
        item = drop_pending_store.get(grant_key)
        if item is not None:
            owner = int(item.get("user_id") or -1)
            if owner != int(user_id):
                return "other"
            return await cog._settle_pending_item(item)
        try:
            row = await drop_reward_store.record(
                grant_key, kind, guild_id, user_id, amount)
        except Exception:
            logger.critical(
                "drop reward PG record failed key=%s guild=%s "
                "user=%s amount=%s kind=%s — durable pending file",
                grant_key, guild_id, user_id, amount, kind)
            try:
                item = drop_pending_store.add({
                    "grant_key": grant_key, "kind": kind,
                    "guild_id": guild_id, "user_id": user_id,
                    "amount": amount,
                })
            except Exception:
                logger.critical(
                    "drop reward fixation FAILED key=%s — ни PG, "
                    "ни pending-файл недоступны", grant_key)
                return "failed"
            owner = int(item.get("user_id") or -1)
            if owner != int(user_id):
                return "other"
            return await cog._settle_pending_item(item)
        owner = int((row or {}).get("user_id") or -1)
        if owner != int(user_id):
            logger.warning(
                "drop reward grant_key %s уже принадлежит "
                "user=%s — отклонён user=%s",
                grant_key, owner, user_id)
            return "other"
        if (row or {}).get("status") == "completed":
            return "credited"
        claimed = await drop_reward_store.claim_key(grant_key)
        if claimed is None:
            return "queued"
        return ("credited" if await cog._process_drop_reward(claimed)
                else "queued")

    lock = getattr(cog, "_grant_lock", None)
    if lock is not None:
        async with lock:
            return await _fixate()
    return await _fixate()


# ==========================================
# Новые события 2.0: pending-файл и грант-протокол
# ==========================================

# Durable JSON-мост для event_rewards при недоступности PG —
# тот же атомарный DropPendingStore, отдельный файл.
event_pending_store = DropPendingStore(
    DATA_DIR / "event_pending_rewards.json")


async def grant_event_reward(cog, guild_id: int, user_id: int,
                             prize: dict[str, Any], grant_key: str
                             ) -> str:
    """Фиксация победителя event-награды (все 4 типа).

    Протокол идентичен grant_drop_reward, но поверх
    event_reward_store (kind=тип награды, item_id для item).
    """
    rtype = str(prize.get("type") or "")
    if rtype == "item":
        # Для item amount = замороженная компенсация за дубль
        # (dup_comp зафиксирован в призе при select_prize) —
        # событие помнит обязательство, смена настройки позже
        # его не меняет.
        amount = int(prize.get("dup_comp") or 0)
    else:
        amount = int(prize.get("amount") or 0)
    item_id = prize.get("item_id")

    async def _fixate() -> str:
        item = event_pending_store.get(grant_key)
        if item is not None:
            owner = int(item.get("user_id") or -1)
            if owner != int(user_id):
                return "other"
            return await cog._settle_event_pending(item)
        try:
            row = await event_reward_store.record(
                grant_key, rtype, guild_id, user_id,
                amount=amount, item_id=item_id)
        except Exception:
            logger.critical(
                "event reward PG record failed key=%s guild=%s "
                "user=%s type=%s — durable pending file",
                grant_key, guild_id, user_id, rtype)
            try:
                item = event_pending_store.add({
                    "grant_key": grant_key, "kind": rtype,
                    "guild_id": guild_id, "user_id": user_id,
                    "amount": amount, "item_id": item_id,
                })
            except Exception:
                logger.critical(
                    "event reward fixation FAILED key=%s — ни PG, "
                    "ни pending-файл недоступны", grant_key)
                return "failed"
            owner = int(item.get("user_id") or -1)
            if owner != int(user_id):
                return "other"
            return await cog._settle_event_pending(item)
        owner = int((row or {}).get("user_id") or -1)
        if owner != int(user_id):
            logger.warning(
                "event reward grant_key %s уже принадлежит "
                "user=%s — отклонён user=%s",
                grant_key, owner, user_id)
            return "other"
        if (row or {}).get("status") == "completed":
            return "credited"
        claimed = await event_reward_store.claim_key(grant_key)
        if claimed is None:
            return "queued"
        return ("credited" if await cog._process_event_reward(claimed)
                else "queued")

    lock = getattr(cog, "_grant_lock", None)
    if lock is not None:
        async with lock:
            return await _fixate()
    return await _fixate()


# ==========================================
# Сессия активного события
# ==========================================

class _EventSession:
    """In-memory состояние активного события.

    Источник истины — строка spontaneous_events (state/actions/
    deadline); объект — рабочая копия для UI и обработчиков.
    Все изменения actions проходят через session.lock и
    event_store.patch_actions (jsonb ||) — рестарт не теряет ходы.
    """

    def __init__(self, row: dict[str, Any]):
        self.event_id = str(row["event_id"])
        self.guild_id = int(row["guild_id"])
        self.channel_id = int(row["channel_id"])
        self.game = str(row["game_type"])
        self.prize = _json_field(row, "prize") or {}
        self.config = _json_field(row, "config") or {}
        self.actions = _json_field(row, "actions") or {}
        self.message_id = row.get("message_id")
        self.lock = asyncio.Lock()
        # Сериализует refresh и final edit карточки — иначе
        # «живой» снапшот мог перезаписать финальный результат.
        self.card_lock = asyncio.Lock()
        self.finished = False
        self.message: discord.Message | None = None
        self.task: asyncio.Task | None = None
        self.refresher: asyncio.Task | None = None
        self.last_card_edit = 0.0

    @property
    def start_ts(self) -> float:
        return float(self.config.get("started_ts")
                     or self.config.get("start_ts") or 0.0)

    @property
    def deadline_ts(self) -> float:
        return float(getattr(self, "_deadline_ts", 0.0))

    @property
    def end_ts(self) -> float:
        """Эффективный конец события (min публичного и скрытого)."""
        return float(getattr(self, "_end_ts", self.deadline_ts))

    def set_deadlines(self, deadline_ts: float,
                      close_ts: float | None) -> None:
        self._deadline_ts = deadline_ts
        ends = [deadline_ts]
        if close_ts is not None:
            ends.append(close_ts)
        self._end_ts = min(ends)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "_EventSession":
        s = cls(row)
        dl = row.get("deadline_at")
        dl_ts = dl.timestamp() if hasattr(dl, "timestamp") else 0.0
        cl = row.get("close_at")
        cl_ts = cl.timestamp() if hasattr(cl, "timestamp") else None
        s.set_deadlines(dl_ts, cl_ts)
        return s


# ==========================================
# UI: карточка события и persistent-кнопки
# ==========================================

T_SE2 = re.compile(
    r"^se2:(?P<eid>[0-9a-f]{8}):(?P<rest>[a-z0-9_:]+)$")

_MODAL_TITLES = {
    "quick_math": "Быстрый счёт",
    "word": "Перепутанное слово",
    "guess_number": "Угадай число",
    "krokodil": "Крокодил",
}


def _prize_line(prize: dict[str, Any], game: str = "") -> str:
    rtype = prize.get("type")
    if game == "growing":
        return (f"**{event_rewards.prize_public_label(prize)}** "
                f"{_PUBLIC_TYPE_LABEL.get(rtype, '')}".strip())
    label = event_rewards.prize_public_label(prize)
    suffix = _PUBLIC_TYPE_LABEL.get(rtype, "")
    if rtype == "item":
        return f"**{label}**"
    return f"**{label}** {suffix}".strip()


_PUBLIC_TYPE_LABEL = {
    "coins": get_emoji("money"),
    "xp": "✨ XP",
    "rep": "🏆 REP",
    "item": "",
}


def _game_instructions(game: str) -> str:
    return {
        "speed": "Нажмите кнопку как можно быстрее — кто первый, тот забрал приз.",
        "raffle": "Нажмите «Участвовать» — победитель будет выбран случайно.",
        "chests": "Выберите один из 5 сундуков. Победит случайный "
                  "игрок, выбравший правильный.",
        "quick_math": "Решите пример — ответьте через кнопку и модальное окно.",
        "minority": "Тайно выберите красных или синих. Победит меньшая "
                    "сторона (при равенстве — никто).",
        "dice": "Бросьте кубик (1–100) — один бросок на игрока, "
                "побеждает максимум.",
        "growing": "Нажмите «Забрать куш» — сумма растёт, но куш может "
                   "сгореть в любой момент.",
        "word": "Угадайте слово по перемешанным буквам.",
        "last_click": "Кликните кнопку — победит последний кликнувший "
                      "перед скрытым дедлайном.",
        "guess_number": "Угадайте число от 1 до 20 (одна попытка).",
        "krokodil": "Угадайте слово по подсказке.",
    }.get(game, "")


def _components_have_marker(components, marker: str,
                            cid_prefix: str) -> bool:
    """Рекурсивный поиск маркера карточки в дереве Components V2:
    TextDisplay с ``marker`` в content или кнопка с custom_id
    ``cid_prefix*`` (persistent se2:*)."""
    stack = list(components or [])
    while stack:
        comp = stack.pop()
        cid = getattr(comp, "custom_id", None)
        if isinstance(cid, str) and cid.startswith(cid_prefix):
            return True
        txt = getattr(comp, "content", None)
        if isinstance(txt, str) and marker in txt:
            return True
        stack.extend(getattr(comp, "children", None) or [])
        accessory = getattr(comp, "accessory", None)
        if accessory is not None:
            stack.append(accessory)
    return False


def _actions_summary(game: str, actions: dict[str, Any]) -> dict[str, int]:
    """Счётчики для публичной карточки (без раскрытия секретов)."""
    out: dict[str, int] = {}
    if game == "raffle":
        out["participants"] = sum(
            1 for k in actions if k.startswith("p:"))
    elif game == "chests":
        out["choices"] = sum(
            1 for k in actions if k.startswith("choice:"))
    elif game == "minority":
        # Распределение голосов СКРЫТО до завершения — публично
        # только суммарное число участников.
        out["participants"] = sum(
            1 for k in actions if k.startswith("choice:"))
    elif game == "dice":
        out["rolls"] = sum(1 for k in actions if k.startswith("roll:"))
    elif game == "last_click":
        out["clicks"] = sum(1 for k in actions if k.startswith("click:"))
    elif game == "growing":
        out["claims"] = (1 if "gclaim" in actions else
                         sum(1 for k in actions
                             if k.startswith("claim:")))
    return out


def _build_card(session: _EventSession,
                now: float | None = None) -> discord.Embed:
    """Публичная карточка активного события. Секреты не раскрываются."""
    game = session.game
    cfg = session.config
    prize = session.prize
    title = f"**СПОНТАННЫЙ ИВЕНТ | {event_games.GAME_DEFS[game]['title'].upper()}**"
    now = time.time() if now is None else now

    if game == "growing":
        dur = float(event_games.GAME_DEFS[game]["duration"])
        elapsed = max(0.0, now - session.start_ts)
        value = event_games.growing_value(prize, elapsed, dur)
        prize_part = (f"**{value:,} → {int(prize.get('amount') or 0):,}** "
                      f"{_PUBLIC_TYPE_LABEL.get(prize.get('type'), '')}")
        desc = (
            " В чате растёт куш! Сумма увеличивается с каждой секундой,\n"
            " но в любой момент может **сгореть** — кто успеет нажать\n"
            " «Забрать куш» первым, тот и забирает текущую сумму.\n\n"
            f" Текущий куш: {prize_part}\n"
        )
    else:
        desc = (
            f" {event_games.GAME_DEFS[game]['title']}!\n\n"
            f" {_game_instructions(game)}\n\n"
            f" Приз: {_prize_line(prize, game)}\n"
        )
        riddle = event_games.scramble_display(game, cfg)
        if riddle:
            desc += f"\n {riddle}\n"
        if game == "chests":
            desc += "\n Выберите один из пяти сундуков ниже.\n"
        if game != "last_click":
            ts = int(session.deadline_ts)
            desc += f"\n ⏱ Завершение <t:{ts}:R>"

    summ = _actions_summary(game, session.actions)
    if summ:
        if game == "raffle":
            desc += f"\n\n Участников: **{summ['participants']}**"
        elif game == "chests":
            desc += f"\n\n Выбрали сундук: **{summ['choices']}**"
        elif game == "minority":
            desc += f"\n\n Участников: **{summ['participants']}**"
        elif game == "dice":
            desc += f"\n\n Бросков: **{summ['rolls']}**"
        elif game == "last_click":
            desc += f"\n\n Кликов: **{summ['clicks']}**"
        elif game == "growing":
            desc += f"\n\n Клеймов: **{summ['claims']}**"

    color = 0xF1C40F
    if game == "growing":
        color = 0xE67E22
    embed = discord.Embed(title=title, description=desc, color=color)
    # Невидимый маркер для восстановления карточки, если
    # message_id не сохранился (крах между send и записью).
    embed.set_footer(text=f"se2:{session.event_id}")
    return embed


def _build_view(session: _EventSession) -> discord.ui.View:
    """Кнопки карточки. custom_id ``se2:{eid}:{action}`` —
    persistent dispatch через DynamicItem после рестарта."""
    eid = session.event_id
    view = discord.ui.View(timeout=None)
    game = session.game

    def _btn(label: str, style: discord.ButtonStyle, action: str,
             emoji: str | None = None, row: int = 0):
        btn = discord.ui.Button(
            label=label, style=style, emoji=emoji,
            custom_id=f"se2:{eid}:{action}", row=row)
        # callback НЕ назначаем: диспетч идёт через persistent
        # DynamicItem Se2Button — единый код-путь и после рестарта.
        view.add_item(btn)

    if game == "speed":
        _btn("Забрать!", discord.ButtonStyle.green, "claim",
             emoji="⚡")
    elif game == "raffle":
        _btn("Участвовать", discord.ButtonStyle.blurple, "join",
             emoji="🎟️")
    elif game == "chests":
        n = int(event_games.GAME_DEFS[game].get("chests", 5))
        for i in range(n):
            _btn(f"Сундук {i + 1}", discord.ButtonStyle.secondary,
                 f"chest:{i}", row=0)
    elif game in ("quick_math", "word", "krokodil", "guess_number"):
        _btn("Ответить", discord.ButtonStyle.primary, "answer",
             emoji="✏️")
    elif game == "minority":
        _btn("За красных", discord.ButtonStyle.danger, "red",
             emoji="🔴", row=0)
        _btn("За синих", discord.ButtonStyle.primary, "blue",
             emoji="🔵", row=0)
    elif game == "dice":
        _btn("Бросить кубик", discord.ButtonStyle.blurple, "roll",
             emoji="🎲")
    elif game == "growing":
        _btn("Забрать куш", discord.ButtonStyle.green, "claim",
             emoji="💰")
    elif game == "last_click":
        _btn("Кликнуть", discord.ButtonStyle.secondary, "click",
             emoji="👆")
    return view


_GAME_ACTIONS: dict[str, set[str]] = {
    "speed": {"claim"},
    "raffle": {"join"},
    "chests": {"chest"},          # chest:<0..4>
    "quick_math": {"answer"},
    "minority": {"red", "blue"},
    "dice": {"roll"},
    "growing": {"claim"},
    "word": {"answer"},
    "last_click": {"click"},
    "guess_number": {"answer"},
    "krokodil": {"answer"},
}


def _action_allowed(game: str, action: str) -> bool:
    """Допустимость действия для типа события (защита от
    поддельных/устаревших custom_id)."""
    allowed = _GAME_ACTIONS.get(game)
    if not allowed:
        return False
    if action in allowed:
        return True
    if "chest" in allowed and action.startswith("chest:"):
        try:
            idx = int(action.split(":", 1)[1])
        except (ValueError, IndexError):
            return False
        n = int(event_games.GAME_DEFS.get(game, {}).get("chests", 5))
        return 0 <= idx < n
    return False


class EventAnswerModal(discord.ui.Modal):
    """Модалка ответа для quick_math/word/guess_number/krokodil.

    Живёт только в памяти процесса — после рестарта кнопка
    «Ответить» просто пересоздаёт её; состояние в PostgreSQL.
    """

    def __init__(self, cog: "CoinDropCog", event_id: str,
                 game: str):
        super().__init__(title=_MODAL_TITLES.get(game, "Ответ"))
        self.cog = cog
        self.event_id = event_id
        self.game = game
        self.answer = discord.ui.TextInput(
            label="Ваш ответ", required=True, max_length=100)
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_modal_answer(
            interaction, self.event_id, self.game,
            str(self.answer.value))


class Se2Button(discord.ui.DynamicItem[discord.ui.Button],
                 template=T_SE2):
    """Persistent-диспетчер кнопок ``se2:*`` — срабатывает и после
    рестарта бота (сессия и действия восстанавливаются из PG)."""

    def __init__(self, custom_id: str) -> None:
        super().__init__(discord.ui.Button(
            style=discord.ButtonStyle.secondary,
            label="…",
            custom_id=custom_id,
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(str(interaction.data.get("custom_id")
                       or item.custom_id))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("CoinDropCog")
        if cog is None:
            return
        m = T_SE2.match(str(interaction.data.get("custom_id") or ""))
        if not m:
            return
        # rest может содержать ':' (chest:3) — передаём целиком.
        await cog.handle_button(interaction, m.group("rest"))


class EventCardTemplateView(discord.ui.View):
    """Регистрируется один раз в cog_load — dynamic-item шаблон."""

    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(Se2Button("se2:00000000:claim"))


# ==========================================
# Cog
# ==========================================

from cogs.groups import admin_group, bind_commands


class CoinDropCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot
        self._default_channel_id = NOTIFICATION_CHANNEL_ID
        self._drop_lock = asyncio.Lock()
        self._grant_lock = asyncio.Lock()
        self._sessions: dict[str, _EventSession] = {}
        self._by_channel: dict[tuple[int, int], str] = {}
        self._gate = ActivityGate()
        self._reward_task: asyncio.Task | None = None
        self._rng = random.Random()

    # ---------------------------------------------------------- helpers

    def _target_channel_id(self, guild) -> int:
        from services.discord_cfg import channel_id as _dc
        gid = guild.id if guild else None
        return _dc(gid, "discord.notification_channel_id",
                   self._default_channel_id)

    def _session_for(self, event_id: str) -> _EventSession | None:
        return self._sessions.get(event_id)

    def _active_for(self, guild_id: int,
                    channel_id: int) -> _EventSession | None:
        eid = self._by_channel.get((guild_id, channel_id))
        if eid is None:
            return None
        s = self._sessions.get(eid)
        if s is None or s.finished:
            self._by_channel.pop((guild_id, channel_id), None)
            self._sessions.pop(eid, None)
            return None
        return s

    def _register_session(self, session: _EventSession) -> None:
        self._sessions[session.event_id] = session
        self._by_channel[(session.guild_id, session.channel_id)] = \
            session.event_id

    def _unregister_session(self, session: _EventSession) -> None:
        self._sessions.pop(session.event_id, None)
        if self._by_channel.get((session.guild_id,
                                 session.channel_id)) == \
                session.event_id:
            self._by_channel.pop((session.guild_id,
                                  session.channel_id), None)

    # ---------------------------------------------------------- messages

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Боты, вебхуки и системные сообщения не считаются.
        # Обычные сообщения и Discord replies — считаются.
        if message.author.bot or message.webhook_id \
                or not message.guild:
            return
        mtype = getattr(message, "type", None)
        allowed_types = {discord.MessageType.default}
        reply_type = getattr(discord.MessageType, "reply", None)
        if reply_type is not None:
            allowed_types.add(reply_type)
        if mtype not in allowed_types:
            return
        # Команды (!...) не считаются.
        if (message.content or "").startswith("!"):
            return
        if message.channel.id != self._target_channel_id(message.guild):
            return

        gid = message.guild.id
        cid = message.channel.id
        key = (gid, cid)
        settings = event_config.event_settings(gid)
        if not event_config.feature_enabled(gid):
            return

        # Пороги гейта — per-key (guild/channel), настройки
        # разных серверов не протекают друг в друга.
        self._gate.configure(
            key,
            window_s=settings.get("window_minutes", 5) * 60.0,
            min_messages=settings.get("min_messages", 10),
            min_unique_users=settings.get("min_unique_users", 3),
            max_per_author=settings.get("max_per_author", 4),
            min_interval_s=settings.get(
                "min_message_interval_seconds", 10),
            min_active_authors=settings.get(
                "min_active_authors_60s", 2))

        now = time.time()
        # Сообщение учитывается ВСЕГДА (в т.ч. во время кулдауна и
        # активного события) — гейт «тёплый» к моменту оценки.
        # Запуск события — ТОЛЬКО на новом засчитанном сообщении:
        # флуд/дубли/незасчитанные не могут спровоцировать старт.
        if not self._gate.record(key, message.author.id,
                                 message.content or "", now):
            return
        if not self._gate.evaluate(key, now):
            return
        await self._try_start(message.channel, message.guild,
                              settings, natural=True)

    # ---------------------------------------------------------- start

    async def _try_start(self, channel, guild, settings: dict,
                         natural: bool,
                         force_type: str | None = None) -> str:
        """Атомарный запуск события. Возвращает статус-строку."""
        gid = guild.id if guild else channel.guild.id
        cid = channel.id
        async with self._drop_lock:
            if self._active_for(gid, cid) is not None:
                return "active"
            if natural:
                # Кулдаун — persisted, от успешного старта.
                if await event_store.cooldown_active(gid, cid):
                    return "cooldown"
            last_type = await event_store.last_event_type(gid, cid)

            games = event_games.enabled_games(
                settings.get("disabled_types"),
                settings.get("type_weights"))
            if force_type:
                games = [g for g in games if g == force_type]
                if not games:
                    return "disabled_type"
            elif not games:
                return "no_games"

            rng = self._rng
            chosen = None
            prize = None
            game = None
            # До len(games) попыток: тип без возможности оплаты
            # (item без каталога и т.п.) пропускается до публикации.
            for _ in range(len(games)):
                game = event_games.pick_game(
                    games, settings.get("type_weights"), rng,
                    last_type=last_type)
                if game is None:
                    break
                prize = event_rewards.select_prize(
                    gid, settings, game, rng)
                if prize is not None:
                    chosen = game
                    break
                games = [g for g in games if g != game]
            if chosen is None or prize is None:
                return "no_prize"
            game = chosen

            cfg = event_games.make_config(game, prize, settings, rng)
            start_ts = time.time()
            cfg["started_ts"] = start_ts
            # Кулдаун фиксируется в config: при сбое между
            # публикацией карточки и mark_started он будет
            # восстановлен на finish тем же окном.
            cfg["cooldown_s"] = int(
                settings.get("cooldown_minutes", 15)) * 60
            deadline_ts = event_games.public_deadline(
                game, cfg, start_ts)
            close_ts = event_games.close_deadline(
                game, cfg, start_ts, deadline_ts)
            event_id = uuid.uuid4().hex[:8]
            try:
                row = await event_store.create(
                    event_id, gid, cid, game, prize, cfg,
                    datetime.fromtimestamp(deadline_ts, timezone.utc),
                    (datetime.fromtimestamp(close_ts, timezone.utc)
                     if close_ts else None))
            except Exception:
                logger.exception("event create failed guild=%s", gid)
                return "db_error"
            if row is None:
                # UNIQUE-индекс: активное событие появилось в гонке.
                return "active"

        # Публикация карточки — вне лока (сетевой вызов).
        session = _EventSession.from_row(row)
        try:
            msg = await channel.send(
                view=compose(_build_card(session), _build_view(session)))
        except Exception:
            logger.exception("event card send failed id=%s", event_id)
            try:
                await event_store.finish(event_id, "cancelled",
                                         {"reason": "send_failed"})
            except Exception:
                pass
            return "send_failed"
        session.message = msg
        # message_id — часть восстановления карточки; сбой не
        # должен ронять запуск — повторим с backoff.
        try:
            await event_store.set_message_id(event_id, msg.id)
        except Exception:
            logger.warning("set_message_id failed id=%s — retry",
                           event_id)
            asyncio.get_running_loop().create_task(
                self._ensure_message_id(session))

        async with self._drop_lock:
            # Кулдаун и last_type — только после успешной публикации.
            # Окно — ровно от зафиксированного started_ts (не от
            # момента публикации), как и при backfill на finish.
            cooldown_s = int(session.config.get("cooldown_s")
                             or settings.get("cooldown_minutes", 15)
                             * 60)
            await event_store.mark_started(
                gid, cid,
                datetime.fromtimestamp(
                    session.start_ts + cooldown_s, timezone.utc),
                game, event_id,
                started_at=datetime.fromtimestamp(
                    session.start_ts, timezone.utc))
            self._register_session(session)

        # Финиш-задача на эффективный конец (скрытый дедлайн раньше
        # публичного — growing/last_click учитываются через close_at).
        delay = max(0.0, session.end_ts - time.time())
        session.task = asyncio.get_running_loop().create_task(
            self._finish_at(session, delay))
        if game == "growing":
            session.refresher = asyncio.get_running_loop().create_task(
                self._growing_refresher(session))
        logger.info("event started id=%s guild=%s channel=%s game=%s "
                    "prize=%s", event_id, gid, cid, game,
                    prize.get("type"))
        return "started"

    # ---------------------------------------------------------- finish

    async def _finish_at(self, session: _EventSession,
                         delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self._finish_event(session, "timeout")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event finish task failed id=%s",
                             session.event_id)

    async def _growing_refresher(self, session: _EventSession) -> None:
        """Периодическая перерисовка растущего куша (троттлинг
        _CARD_EDIT_MIN_INTERVAL — без per-second edits)."""
        try:
            while not session.finished:
                await asyncio.sleep(_CARD_EDIT_MIN_INTERVAL)
                if session.finished:
                    break
                await self._refresh_card(session, force=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("growing refresher failed id=%s",
                             session.event_id)

    async def _ensure_message_id(self, session: _EventSession,
                                 attempts: int = 5) -> None:
        """Retry сохранения message_id после send — карточка
        уже опубликована, id нужно для финальной правки."""
        msg = session.message
        if msg is None:
            return
        for i in range(attempts):
            try:
                await event_store.set_message_id(
                    session.event_id, msg.id)
                return
            except Exception:
                logger.warning("set_message_id retry %d id=%s",
                               i + 1, session.event_id)
                await asyncio.sleep(5 * (i + 1))

    async def _resolve_card_message(self, session: _EventSession):
        """Найти сообщение карточки: session.message → fetch по
        message_id → скан истории по footer-маркеру ``se2:{eid}``
        (восстановление при потере message_id)."""
        msg = session.message
        if msg is not None:
            return msg
        channel = await self._resolve_channel(session)
        if channel is None:
            return None
        if session.message_id:
            try:
                msg = await channel.fetch_message(session.message_id)
                session.message = msg
                return msg
            except Exception:
                pass
        # message_id потерян (крах между send и записью) —
        # ищем карточку по маркеру. Components V2 не даёт
        # embeds: маркер ищем и в footer эмбеда (legacy), и в
        # дереве компонентов — TextDisplay с текстом ``se2:{eid}``
        # или кнопка с custom_id ``se2:{eid}:*``. Окно — до 100
        # сообщений, не «последние 25».
        try:
            marker = f"se2:{session.event_id}"
            cid_prefix = f"se2:{session.event_id}:"
            async for m in channel.history(limit=100):
                author = getattr(m, "author", None)
                if author is not None and \
                        getattr(author, "bot", False) is not True:
                    continue
                found = False
                for emb in getattr(m, "embeds", []) or []:
                    if (getattr(emb, "footer", None)
                            and emb.footer.text == marker):
                        found = True
                        break
                if not found:
                    found = _components_have_marker(
                        getattr(m, "components", None),
                        marker, cid_prefix)
                if found:
                    session.message = m
                    try:
                        await event_store.set_message_id(
                            session.event_id, m.id)
                    except Exception:
                        pass
                    return m
        except Exception:
            pass
        return None

    async def _refresh_card(self, session: _EventSession,
                            force: bool = False) -> None:
        """Обновить публичную карточку (троттлинг).

        Не редактирует карточку завершённого события — финальный
        edit не должен перезаписываться «живым» снапшотом."""
        if session.finished:
            return
        async with session.card_lock:
            await self._refresh_card_locked(session, force=force)

    async def _refresh_card_locked(self, session: _EventSession,
                                   force: bool) -> None:
        now = time.time()
        if not force and now - session.last_card_edit < \
                _CARD_EDIT_MIN_INTERVAL:
            return
        msg = await self._resolve_card_message(session)
        if msg is None or session.finished:
            return
        session.last_card_edit = now
        try:
            await msg.edit(
                view=compose(_build_card(session, now),
                             _build_view(session)))
        except discord.HTTPException:
            logger.warning("event card refresh failed id=%s",
                           session.event_id)

    async def _resolve_channel(self, session: _EventSession):
        guild = self.bot.get_guild(session.guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(session.guild_id)
            except Exception:
                return None
        channel = guild.get_channel(session.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(
                    session.channel_id)
            except Exception:
                return None
        return channel

    async def _finish_event(self, session: _EventSession,
                            reason: str, *, locked: bool = False) -> None:
        """Единственный финализатор: outcome → durable grant →
        finish() (атомарно) → edit карточки.

        Идемпотентно: повторный вызов (overdue-sweeper после
        рестарта) видит state != active и завершается; grant_key
        защищает от двойной выплаты.

        locked=True — вызывающий уже держит session.lock
        (asyncio.Lock не реентерабелен).
        """
        if locked:
            await self._finish_event_inner(session, reason)
            return
        async with session.lock:
            await self._finish_event_inner(session, reason)

    async def _finish_event_inner(self, session: _EventSession,
                                  reason: str) -> None:
        if session.finished:
            return
        session.finished = True
        current = asyncio.current_task()
        for t in (session.task, session.refresher):
            # Не отменяем саму себя: _finish_event часто
            # вызывается ИЗ finish-задачи.
            if t is not None and t is not current:
                t.cancel()
        now = time.time()
        outcome = event_games.compute_outcome(
            session.game, session.config, session.actions,
            session.prize, now)
        await self._settle_and_finish(session, outcome, reason)

    async def _settle_and_finish(self, session: _EventSession,
                                 outcome: dict[str, Any],
                                 reason: str, *,
                                 already_settled: bool = False
                                 ) -> None:
        """Финализация active → settling → finished.

        1. ``settle`` атомарно фиксирует outcome (победители,
           выплата) в БД ДО начислений — случайный победитель
           никогда не перевыбирается, даже после рестарта.
        2. Обязательства по всем победителям фиксируются в
           event_rewards / pending-файле. Если ни одно
           обязательство не удалось сохранить надёжно —
           событие НЕ закрывается: остаётся 'settling',
           свипер повторит.
        3. ``finish`` атомарен — карточку редактирует только
           победивший UPDATE процесс.
        """
        eid = session.event_id
        if not already_settled:
            # Outcome вычисляется из АТОМАРНО зафиксированного
            # снимка actions: settle() берёт строку FOR UPDATE,
            # вызывает builder внутри транзакции и только потом
            # переводит в settling — патчи либо попали в снимок,
            # либо отклонены PG-гардом state='active'.
            holder: dict[str, Any] = {}

            def _outcome_builder(row: dict[str, Any]) -> dict[str, Any]:
                computed = event_games.compute_outcome(
                    str(row.get("game_type") or session.game),
                    _json_field(row, "config") or session.config,
                    _json_field(row, "actions")
                    or session.actions or {},
                    _json_field(row, "prize") or session.prize,
                    time.time())
                holder["outcome"] = computed
                return computed

            try:
                settled = await event_store.settle(
                    eid, _outcome_builder)
            except Exception:
                # PG недоступен — остаёмся 'active', свипер
                # просроченных повторит финализацию.
                logger.exception("event settle failed id=%s", eid)
                self._unregister_session(session)
                return
            if settled is not None and holder.get("outcome"):
                # Авторитетный outcome — из снимка БД, а не из
                # possibly-устаревшего session.actions.
                outcome = holder["outcome"]
            if settled is None:
                # Уже settling/finished: если settling — обязаны
                # использовать СОХРАНЁННЫЙ outcome (не наш RNG).
                try:
                    cur = await event_store.get(eid)
                except Exception:
                    cur = None
                if cur is not None and \
                        cur.get("state") == "settling":
                    stored = _json_field(cur, "result") or {}
                    so = stored.get("outcome")
                    if isinstance(so, dict) and so:
                        outcome = so
                else:
                    self._unregister_session(session)
                    return

        winners = [int(w) for w in outcome.get("winners") or []]
        grant_status = "none"
        obligations_ok = True
        rewards_info: dict[str, Any] = {}
        if winners:
            # Фиксация обязательства ДО закрытия события:
            # crash между grant и finish → свипер доведёт
            # (grant_key идемпотентен).
            prize = dict(session.prize)
            if outcome.get("payout") is not None:
                prize["amount"] = int(outcome["payout"])
            results = []
            for uid in winners:
                try:
                    res = await grant_event_reward(
                        self, session.guild_id, uid, prize,
                        f"event:{eid}:{uid}")
                except Exception:
                    logger.exception("event grant raised id=%s", eid)
                    res = "failed"
                results.append(res)
            # Фактический способ исполнения каждой награды
            # (item/owned/comp:N) — для честного текста карточки.
            reward_modes: dict[int, str] = {}
            try:
                for r in await event_reward_store.get_many(
                        [f"event:{eid}:{u}" for u in winners]):
                    uid_r = int(r.get("user_id") or 0)
                    mode_r = r.get("result")
                    if uid_r and mode_r:
                        reward_modes[uid_r] = str(mode_r)
            except Exception:
                pass
            rewards_info = {
                str(u): {"status": res,
                         "mode": reward_modes.get(u)}
                for u, res in zip(winners, results)}
            if any(r in ("failed", "other") for r in results):
                grant_status = "failed"
                obligations_ok = False
            elif all(r == "credited" for r in results):
                grant_status = "credited"
            else:
                grant_status = "queued"

        if not obligations_ok:
            # Нет надёжно сохранённого обязательства — событие
            # остаётся 'settling', finished не ставим, карточку
            # не закрываем. Свипер повторит доведение.
            logger.critical(
                "event %s settling: payout obligation NOT durable "
                "(grant=%s) — stays settling for retry",
                eid, grant_status)
            self._unregister_session(session)
            return

        # Восстановление кулдауна/last_type ДО перехода в
        # finished: если процесс упал между публикацией
        # карточки и mark_started, channel_state мог не
        # записаться — окно считается от started_ts из config,
        # то же, что и при штатном mark_started. Запись до
        # finish гарантирует: либо channel_state durable до
        # finished, либо событие остаётся settling и свипер
        # повторит backfill идемпотентно (guard по
        # last_event_started_at не даст старому событию
        # затереть более новое).
        try:
            cooldown_s = int(session.config.get("cooldown_s")
                             or 15 * 60)
            if cooldown_s > 0:
                await event_store.ensure_channel_state(
                    session.guild_id, session.channel_id,
                    datetime.fromtimestamp(
                        session.start_ts + cooldown_s,
                        timezone.utc),
                    session.game, session.event_id,
                    started_at=datetime.fromtimestamp(
                        session.start_ts, timezone.utc))
        except Exception:
            logger.warning("channel state backfill failed id=%s",
                           session.event_id)

        result = {
            "reason": outcome.get("reason"),
            "winners": winners,
            "payout": outcome.get("payout"),
            "detail": outcome.get("detail"),
            "grant": grant_status,
            "outcome": outcome,
            "rewards": rewards_info,
            "finished_ts": time.time(),
        }
        try:
            closed = await event_store.finish(
                eid, "finished", result)
        except Exception:
            logger.exception("event finish DB failed id=%s", eid)
            closed = None
        if closed is None:
            # Другой процесс уже закрыл событие — не дублируем
            # Discord-правки и не объявляем результат повторно.
            self._unregister_session(session)
            return

        await self._edit_final_card(session, outcome, grant_status,
                                    rewards=rewards_info)
        self._unregister_session(session)
        logger.info("event finished id=%s game=%s reason=%s "
                    "winners=%s grant=%s",
                    eid, session.game,
                    outcome.get("reason"), winners, grant_status)

    async def _resume_settling(self, row: dict[str, Any]) -> None:
        """Довести 'settling'-событие: обязательства уже
        зафиксированы (или фиксируются повторно идемпотентно),
        outcome берётся из сохранённого result — RNG не
        перевыбирается."""
        stored = _json_field(row, "result") or {}
        outcome = stored.get("outcome")
        if not isinstance(outcome, dict) or not outcome:
            # Settling без валидного сохранённого outcome НЕ
            # закрываем: finish() отклонит переход, а
            # синтезированный «no_winner» скрыл бы рассинхрон.
            # Оставляем в settling — критический лог для
            # ручного разбора (не должно возникать: settle()
            # всегда пишет outcome).
            logger.critical(
                "settling event %s has no stored outcome — "
                "manual reconciliation required",
                row.get("event_id"))
            return
        session = _EventSession.from_row(row)
        reason = str(stored.get("reason") or "resume")
        await self._settle_and_finish(session, outcome, reason,
                                      already_settled=True)

    async def _edit_final_card(self, session: _EventSession,
                               outcome: dict[str, Any],
                               grant_status: str,
                               rewards: dict[str, Any] | None = None
                               ) -> None:
        """Сериализовано с refresh через session.card_lock —
        финальный результат не может быть перезаписан
        «живым» снапшотом карточки."""
        async with session.card_lock:
            await self._edit_final_card_inner(
                session, outcome, grant_status, rewards=rewards)

    async def _edit_final_card_inner(self, session: _EventSession,
                                     outcome: dict[str, Any],
                                     grant_status: str,
                                     rewards: dict[str, Any] | None = None
                                     ) -> None:
        """Edit исходной карточки: финальный результат, кнопки
        убраны. Чужие сообщения не трогаем — только message_id
        события. Повторные попытки при сбое Discord; исход
        фиксируется в result.card_edit для свипера."""
        msg = await self._resolve_card_message(session)
        if msg is None:
            logger.warning("event card missing id=%s", session.event_id)
            try:
                await event_store.mark_card_edit(
                    session.event_id, "missing")
            except Exception:
                pass
            return

        game = session.game
        prize = session.prize
        winners = [int(w) for w in outcome.get("winners") or []]
        reason = str(outcome.get("reason") or "")

        if winners:
            title = (f"**ИВЕНТ ЗАВЕРШЁН | "
                     f"{event_games.GAME_DEFS[game]['title'].upper()}**")
            mentions = " ".join(f"<@{u}>" for u in winners)
            if grant_status == "failed":
                prize_txt = ("⛔ Награда не зафиксирована — "
                             "обратитесь к администратору.")
            else:
                if session.game == "growing" and \
                        outcome.get("payout") is not None:
                    rtype = prize.get("type")
                    prize_txt = (f"**{int(outcome['payout']):,}** "
                                 f"{_PUBLIC_TYPE_LABEL.get(rtype, '')}")
                elif prize.get("type") == "item":
                    # Фактический итог item-награды из
                    # event_rewards.result (item/owned/comp:N) —
                    # не заявляем выдачу до подтверждения.
                    item_name = (prize.get('item_name')
                                 or 'предмет')
                    mode = None
                    wstatus = None
                    for u in winners:
                        info = (rewards or {}).get(str(u)) or {}
                        if info.get("mode") or info.get("status"):
                            mode = info.get("mode")
                            wstatus = info.get("status")
                            break
                    if grant_status == "queued" or \
                            wstatus in (None, "queued", "pending",
                                        "processing"):
                        prize_txt = (f"**{item_name}**\n"
                                     "*Предмет или компенсация "
                                     "будут зафиксированы — "
                                     "повторное начисление "
                                     "исключено.*")
                    elif mode and str(mode).startswith("comp:"):
                        comp = str(mode).split(":", 1)[1]
                        prize_txt = (f"**{item_name}** — уже был "
                                     f"у вас, компенсация "
                                     f"**{comp}** монет")
                    elif mode == "owned":
                        prize_txt = (f"**{item_name}** — уже есть "
                                     "у вас")
                    else:
                        prize_txt = (f"**{item_name}** — добавлен "
                                     "в инвентарь")
                else:
                    prize_txt = _prize_line(prize, game)
                if grant_status == "queued" and \
                        prize.get("type") != "item":
                    prize_txt += ("\n*Зачисление подтверждается — "
                                  "повторное начисление исключено.*")
                elif prize.get("type") == "coins":
                    prize_txt += "\n*Проверить баланс: `/balance`*"
            detail = outcome.get("detail") or ""
            desc = (
                f" **Победитель:** {mentions}\n"
                f" **Приз:** {prize_txt}\n"
            )
            if detail:
                desc += f" _{detail}_\n"
        else:
            if reason == "tie":
                title = "**ИВЕНТ ЗАВЕРШЁН | НИЧЬЯ**"
            elif reason == "burned":
                title = "**ИВЕНТ ЗАВЕРШЁН | КУШ СГОРЕЛ**"
            elif reason == "no_correct":
                title = "**ИВЕНТ ЗАВЕРШЁН | НИКТО НЕ УГАДАЛ**"
            else:
                title = "**ИВЕНТ ЗАВЕРШЁН | БЕЗ ПОБЕДИТЕЛЯ**"
            detail = outcome.get("detail") or reason
            desc = (f" {event_games.GAME_DEFS[game]['title']}: "
                    f"победителя нет.\n")
            if detail:
                desc += f" _{detail}_\n"
            if session.game == "growing":
                desc += " Куш сгорел — приз не был выплачен."

        embed = discord.Embed(title=title, description=desc,
                              color=0x2ECC71 if winners else 0xED4245)
        # Финальный edit с одной повторной попыткой; итог
        # фиксируется в result.card_edit — свипер повторит при
        # 'failed'/'missing'.
        ok = False
        for attempt in range(2):
            try:
                await msg.edit(
                    view=compose(embed, None),
                    allowed_mentions=discord.AllowedMentions(
                        users=True, roles=False, everyone=False))
                ok = True
                break
            except discord.HTTPException:
                logger.warning("event final edit failed "
                               "id=%s attempt=%d",
                               session.event_id, attempt + 1)
                if attempt == 0:
                    await asyncio.sleep(2.0)
            except Exception:
                logger.exception("event final edit error id=%s",
                                 session.event_id)
                break
        try:
            await event_store.mark_card_edit(
                session.event_id, "ok" if ok else "failed")
        except Exception:
            pass

    # ---------------------------------------------------------- buttons

    async def handle_button(self, interaction: discord.Interaction,
                            action: str) -> None:
        """Обработчик persistent-кнопки ``se2:*``."""
        m = T_SE2.match(str(interaction.data.get("custom_id") or ""))
        if not m:
            return
        event_id = m.group("eid")
        session = self._session_for(event_id)
        if session is None:
            # Возможно, рестарт: восстанавливаем из БД.
            row = await event_store.get(event_id)
            if row is None or row.get("state") != "active":
                await interaction.response.send_message(
                    "Событие уже завершено.", ephemeral=True)
                return
            session = _EventSession.from_row(row)
            self._register_session(session)
            delay = max(0.0, session.end_ts - time.time())
            if delay <= 0:
                asyncio.get_running_loop().create_task(
                    self._finish_event(session, "timeout"))
            else:
                session.task = asyncio.get_running_loop().create_task(
                    self._finish_at(session, delay))
            if session.game == "growing":
                session.refresher = asyncio.get_running_loop() \
                    .create_task(self._growing_refresher(session))

        if interaction.user.bot:
            return
        # Валидация контекста: действие должно относиться к
        # этому событию, каналу и карточке.
        if interaction.guild_id is not None and \
                int(interaction.guild_id) != session.guild_id:
            return
        if getattr(interaction, "channel_id", None) and \
                int(interaction.channel_id) != session.channel_id:
            return
        msg = getattr(interaction, "message", None)
        if session.message_id and msg is not None and \
                getattr(msg, "id", None) != session.message_id:
            await interaction.response.send_message(
                "Это устаревшая карточка события.", ephemeral=True)
            return
        if not _action_allowed(session.game, action):
            await interaction.response.send_message(
                "Неизвестное действие для этого события.",
                ephemeral=True)
            return
        async with session.lock:
            # Дедлайн перепроверяется ПОСЛЕ получения блокировки —
            # действие, ждавшее lock, не должно пройти.
            now = time.time()
            if session.finished or now >= session.end_ts:
                await interaction.response.send_message(
                    "Время события истекло.", ephemeral=True)
                return
            # Действия, открывающие модалку, не defer'ятся.
            if action == "answer":
                if self._modal_game(session.game) is None:
                    await interaction.response.send_message(
                        "Это действие недоступно.", ephemeral=True)
                    return
                await interaction.response.send_modal(
                    EventAnswerModal(self, session.event_id,
                                     session.game))
                return
            await interaction.response.defer(ephemeral=True)
            await self._apply_action(session, interaction, action)

    @staticmethod
    def _modal_game(game: str) -> str | None:
        return game if game in _MODAL_TITLES else None

    async def _apply_action(self, session: _EventSession,
                            interaction: discord.Interaction,
                            action: str) -> None:
        """Применить действие под session.lock (defer уже сделан)."""
        game = session.game
        uid = interaction.user.id
        now = time.time()
        actions = session.actions
        eid = session.event_id

        async def _persist(patch: dict[str, Any],
                          must_absent: list[str] | None = None
                          ) -> bool:
            """Сохранить действие; False — не сохранено (БД
            отклонила: дедлайн/закрыто/ключ занят) — результат
            ОБЯЗАТЕЛЬНО проверяется."""
            try:
                ok = await event_store.patch_actions(
                    eid, patch, must_absent=must_absent)
            except Exception:
                logger.warning("patch_actions failed id=%s", eid)
                return False
            if not ok:
                logger.info("patch_actions rejected id=%s patch=%s",
                            eid, list(patch))
                return False
            actions.update(patch)
            return True

        async def _ack(text: str) -> None:
            try:
                await interaction.followup.send(text, ephemeral=True)
            except Exception:
                pass

        async def _deny(text: str) -> None:
            await _ack(text)

        if game == "speed" and action == "claim":
            if "winner" in actions:
                await _deny("Кто-то уже успел первым!")
                return
            if not await _persist({"winner": uid},
                                  must_absent=["winner"]):
                await _deny("Временная ошибка — попробуйте ещё раз.")
                return
            await self._finish_event(session, "first_click",
                                     locked=True)
            await _ack("Вы первый! Результат — в карточке события.")
            return

        if game == "raffle" and action == "join":
            key = f"p:{uid}"
            if key in actions:
                await _deny("Вы уже участвуете в розыгрыше!")
                return
            if not await _persist({key: 1}, must_absent=[key]):
                await _deny("Временная ошибка — попробуйте ещё раз.")
                return
            await _ack("Вы в игре! Победителя выберут случайно.")
            await self._refresh_card(session)
            return

        if game == "chests" and action.startswith("chest:"):
            try:
                idx = int(action.split(":", 1)[1])
            except (ValueError, IndexError):
                await _deny("Некорректный сундук.")
                return
            n = int(event_games.GAME_DEFS[game].get("chests", 5))
            if not (0 <= idx < n):
                await _deny("Некорректный сундук.")
                return
            ckey = f"choice:{uid}"
            if ckey in actions:
                await _deny("Вы уже сделали выбор — он не меняется.")
                return
            # Первый выбор фиксируется на уровне PG (must_absent) —
            # гонка двух сундуков одного игрока не перезапишет.
            if not await _persist({ckey: idx}, must_absent=[ckey]):
                await _deny("Временная ошибка — попробуйте ещё раз.")
                return
            await _ack(f"Вы выбрали сундук **{idx + 1}**. "
                       "Удачи!")
            await self._refresh_card(session)
            return

        if game == "minority" and action in ("red", "blue"):
            # Перевыбор разрешён до дедлайна (последний выбор важен),
            # но результат _persist() проверяется — выбор
            # подтверждается только после записи в PG.
            if not await _persist({f"choice:{uid}": action}):
                await _deny("Временная ошибка — попробуйте ещё раз.")
                return
            side = "красных" if action == "red" else "синих"
            await _ack(f"Вы за {side}. Выбор можно изменить до конца.")
            await self._refresh_card(session)
            return

        if game == "dice" and action == "roll":
            rkey = f"roll:{uid}"
            if rkey in actions:
                await _deny(f"Ваш бросок уже сделан: "
                            f"**{int(actions[rkey])}**")
                return
            value = self._rng.randint(1, 100)
            if not await _persist({rkey: value}, must_absent=[rkey]):
                await _deny("Временная ошибка — попробуйте ещё раз.")
                return
            await _ack(f"Ваш бросок: **{value}**. Один бросок на игрока.")
            await self._refresh_card(session)
            return

        if game == "growing" and action == "claim":
            # Скрытый burn-дедлайн: клейм валиден только до него.
            burn_ts = session.start_ts + float(
                session.config.get("burn_after", 0.0))
            if now >= burn_ts:
                await _deny("Куш уже сгорел — поздно.")
                return
            # Глобальный атомарный ключ: первый клейм фиксируется
            # must_absent-гардом на уровне PG — два simultaneous
            # клейма разных игроков не могут оба сохраниться
            # (в отличие от per-user ключей claim:<uid>).
            if "gclaim" in actions or any(
                    k.startswith("claim:") for k in actions):
                await _deny("Куш уже забран другим игроком!")
                return
            elapsed = max(0.0, now - session.start_ts)
            if not await _persist(
                    {"gclaim": {"uid": uid, "elapsed": elapsed}},
                    must_absent=["gclaim"]):
                await _deny("Временная ошибка — попробуйте ещё раз.")
                return
            await self._finish_event(session, "claimed",
                                     locked=True)
            await _ack("Вы успели забрать куш!")
            return

        if game == "last_click" and action == "click":
            ckey = f"click:{uid}"
            if ckey in actions:
                await _deny("Вы уже кликали — один клик на игрока.")
                return
            if not await _persist({ckey: now}, must_absent=[ckey]):
                await _deny("Временная ошибка — попробуйте ещё раз.")
                return
            await _ack("Клик засчитан! Победит последний кликнувший.")
            await self._refresh_card(session)
            return

        await _deny("Неизвестное действие.")

    async def handle_modal_answer(self, interaction: discord.Interaction,
                                  event_id: str, game: str,
                                  text: str) -> None:
        """Ответ модалки: defer ephemeral → проверка → итог."""
        session = self._session_for(event_id)
        if session is None:
            row = await event_store.get(event_id)
            if row is None or row.get("state") != "active":
                await interaction.response.send_message(
                    "Событие уже завершено.", ephemeral=True)
                return
            session = _EventSession.from_row(row)
            self._register_session(session)
        # Валидация: модалка должна соответствовать событию,
        # его каналу и типу игры.
        if session.game != game or game not in _MODAL_TITLES:
            await interaction.response.send_message(
                "Модалка не относится к этому событию.",
                ephemeral=True)
            return
        if interaction.guild_id is not None and \
                int(interaction.guild_id) != session.guild_id:
            return
        if getattr(interaction, "channel_id", None) and \
                int(interaction.channel_id) != session.channel_id:
            return
        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            return
        uid = interaction.user.id
        async with session.lock:
            # Дедлайн перепроверяется ПОСЛЕ получения блокировки.
            now = time.time()
            if session.finished or now >= session.end_ts:
                await interaction.followup.send(
                    "Время события истекло.", ephemeral=True)
                return
            actions = session.actions
            cfg = session.config

            async def _persist(patch, must_absent=None):
                try:
                    ok = await event_store.patch_actions(
                        session.event_id, patch,
                        must_absent=must_absent)
                except Exception:
                    return False
                if not ok:
                    return False
                actions.update(patch)
                return True

            async def _deny(msg):
                try:
                    await interaction.followup.send(msg, ephemeral=True)
                except Exception:
                    pass

            if "winner" in actions:
                await _deny("Победитель уже найден — событие завершено.")
                return

            correct = False
            if game == "quick_math":
                accepted = cfg.get("accepted") or []
                correct = event_games.answer_matches(text, accepted)
            elif game == "guess_number":
                gkey = f"guess:{uid}"
                if gkey in actions:
                    await _deny("Попытка уже использована — "
                                "одна попытка на игрока.")
                    return
                accepted = cfg.get("accepted") or []
                correct = event_games.answer_matches(text, accepted)
                if correct:
                    # Атомарно ОДНИМ патчем: потребление попытки +
                    # победитель. must_absent по обоим ключам —
                    # либо всё фиксируется, либо ничего (нет
                    # состояния «попытка сожжена, победитель
                    # потерян» и наоборот).
                    if not await _persist(
                            {gkey: 1, "winner": uid},
                            must_absent=[gkey, "winner"]):
                        await _deny("Победитель уже найден — "
                                    "событие завершено.")
                        return
                    await self._finish_event(session, "correct_answer",
                                             locked=True)
                    await _deny("Верно! Вы победили — "
                                "смотрите карточку.")
                    return
                # Неверно — фиксируем попытку (must_absent —
                # попытка не перезаписывается в PG).
                if not await _persist({gkey: 1},
                                      must_absent=[gkey]):
                    await _deny("Временная ошибка — попробуйте ещё раз.")
                    return
                await _deny("Неверно — попытка использована.")
                return
            elif game == "word":
                accepted = cfg.get("accepted") or []
                correct = event_games.answer_matches(text, accepted)
            elif game == "krokodil":
                accepted = cfg.get("accepted") or []
                correct = event_games.answer_matches(text, accepted)
            else:
                await _deny("Это действие недоступно.")
                return

            if not correct:
                await _deny("Неверно — попробуйте ещё раз.")
                return
            # Победитель фиксируется атомарно в PG: при гонке
            # двух верных ответов должен победить только один.
            if not await _persist({"winner": uid},
                                  must_absent=["winner"]):
                await _deny("Победитель уже найден — событие завершено.")
                return
            await self._finish_event(session, "correct_answer",
                                     locked=True)
            await _deny("Верно! Вы победили — смотрите карточку.")

    # ---------------------------------------------------------- recovery

    async def _restore_sessions(self) -> None:
        """Восстановление активных событий после рестарта:
        сессии из БД, финиш-задачи по оставшемуся времени,
        просроченные — финализируются сразу (без двойных выплат
        благодаря grant_key и атомарному finish())."""
        try:
            rows = await event_store.list_active()
        except Exception:
            logger.exception("event restore: list_active failed")
            return
        now = time.time()
        for row in rows:
            try:
                state = str(row.get("state") or "")
                if state == "settling":
                    # Обязательства/выплаты доводятся из
                    # сохранённого outcome — без перевыбора RNG.
                    await self._resume_settling(row)
                    continue
                session = _EventSession.from_row(row)
                self._register_session(session)
                delay = session.end_ts - now
                if delay <= 0:
                    asyncio.get_running_loop().create_task(
                        self._finish_event(session, "timeout"))
                else:
                    session.task = \
                        asyncio.get_running_loop().create_task(
                            self._finish_at(session, delay))
                    if session.game == "growing":
                        session.refresher = \
                            asyncio.get_running_loop().create_task(
                                self._growing_refresher(session))
                logger.info("event restored id=%s game=%s "
                            "ends_in=%.0fs", session.event_id,
                            session.game, max(0.0, delay))
            except Exception:
                logger.exception("event restore failed row=%s",
                                 (row or {}).get("event_id"))

    # ---------------------------------------------------------- worker

    async def _process_event_reward(self, row: dict) -> bool:
        """Одна попытка начисления event-награды (все 4 типа).
        Идемпотентно на уровне целевого хранилища."""
        key = row["grant_key"]
        attempts = int(row.get("attempts") or 0)
        user_name = ""
        try:
            gid = int(row["guild_id"])
            uid = int(row["user_id"])
            guild = self.bot.get_guild(gid)
            member = guild.get_member(uid) if guild else None
            if member is not None:
                user_name = member.display_name
        except Exception:
            pass
        reward_result = ""
        try:
            reward_result = await event_rewards.apply_reward(
                row, user_name=user_name) or ""
        except Exception as exc:
            delay = _drop_backoff(attempts or 1)
            try:
                ok = await event_reward_store.retry(
                    key, attempts, f"{type(exc).__name__}: {exc}",
                    delay)
                if not ok:
                    logger.info(
                        "event reward retry skipped — lease lost "
                        "key=%s", key)
            except Exception:
                logger.critical(
                    "event reward retry-mark failed key=%s — row "
                    "stays processing until lease expiry", key)
            logger.warning(
                "event reward deferred key=%s attempts=%s next_in=%.0fs",
                key, attempts, delay)
            return False
        try:
            ok = await event_reward_store.complete(
                key, attempts, result=reward_result or None)
            if not ok:
                logger.info(
                    "event reward complete skipped — lease lost "
                    "key=%s", key)
            else:
                # Карточка события могла ещё показывать
                # «подтверждается» — после успешного (в т.ч.
                # позднего retry) синхронизируем фактический
                # итог.
                asyncio.get_running_loop().create_task(
                    self._sync_reward_card(key))
        except Exception:
            logger.exception(
                "event reward complete failed key=%s", key)
        logger.info("event reward credited key=%s user=%s kind=%s",
                    key, row.get("user_id"), row.get("kind"))
        return True

    async def _sync_reward_card(self, grant_key: str) -> None:
        """После успешного (в т.ч. позднего retry) завершения
        event-награды обновить финальную карточку события —
        она могла показывать «зачисление подтверждается»."""
        try:
            # grant_key = event:{event_id}:{user_id}
            parts = str(grant_key).split(":")
            if len(parts) != 3 or parts[0] != "event":
                return
            eid = parts[1]
            row = await event_store.get(eid)
            if row is None or row.get("state") != "finished":
                return
            stored = _json_field(row, "result") or {}
            outcome = stored.get("outcome")
            if not isinstance(outcome, dict) or not outcome:
                return
            session = _EventSession.from_row(row)
            # Актуальные статусы/режимы наград всех победителей —
            # grant_status ПЕРЕСЧИТЫВАЕТСЯ по живому состоянию
            # event_rewards, а не берётся из устаревшего result.
            winners = [int(w) for w in outcome.get("winners") or []]
            rewards: dict[str, Any] = {}
            statuses: list[str] = []
            try:
                rows = await event_reward_store.get_many(
                    [f"event:{eid}:{u}" for u in winners])
                by_uid = {int(r.get("user_id") or 0): r
                          for r in rows}
                for u in winners:
                    r = by_uid.get(u)
                    if r is None:
                        # Строки нет — обязательство не зафиксировано.
                        rewards[str(u)] = {"status": "missing",
                                           "mode": None}
                        statuses.append("missing")
                        continue
                    st = ("credited" if r.get("status") == "completed"
                          else str(r.get("status") or "pending"))
                    statuses.append(st)
                    rewards[str(u)] = {
                        "status": st, "mode": r.get("result")}
            except Exception:
                rewards = stored.get("rewards") or {}
                statuses = []
            if statuses:
                if all(s == "credited" for s in statuses):
                    grant_status = "credited"
                elif any(s == "missing" for s in statuses):
                    grant_status = "failed"
                else:
                    grant_status = "queued"
            else:
                grant_status = str(stored.get("grant") or "none")
            # Актуальный grant/rewards — в spontaneous_events.
            # result (jsonb ||), чтобы последующие retry карточки
            # и свипер видели свежий итог, а не снимок на момент
            # finish. patch_result мёржит только finished-строки.
            try:
                await event_store.patch_result(eid, {
                    "grant": grant_status, "rewards": rewards})
            except Exception:
                logger.warning("reward result persist failed "
                               "id=%s", eid)
            await self._edit_final_card(
                session, outcome, grant_status,
                rewards=rewards or None)
        except Exception:
            logger.exception("reward card sync failed key=%s",
                             grant_key)

    async def _settle_event_pending(self, item: dict) -> str:
        """event pending-файл → PG record → claim → process."""
        key = item["grant_key"]
        prize = {"type": item.get("kind"),
                 "amount": int(item.get("amount") or 0),
                 "item_id": item.get("item_id")}
        try:
            row = await event_reward_store.record(
                key, prize["type"], int(item["guild_id"]),
                int(item["user_id"]), amount=prize["amount"],
                item_id=prize["item_id"])
        except Exception:
            logger.warning("event reward still unpersisted key=%s",
                           key)
            return "queued"
        event_pending_store.remove(key)
        logger.info("event reward persisted from pending file key=%s",
                    key)
        owner = int((row or {}).get("user_id") or -1)
        if owner != int(item["user_id"]):
            return "other"
        if (row or {}).get("status") == "completed":
            return "credited"
        claimed = await event_reward_store.claim_key(key)
        if claimed is None:
            return "queued"
        return ("credited" if await self._process_event_reward(claimed)
                else "queued")

    async def _drain_event_pending(self) -> None:
        for item in event_pending_store.all():
            try:
                async with self._grant_lock:
                    await self._settle_event_pending(item)
            except Exception:
                logger.exception(
                    "event pending drain failed key=%s",
                    item.get("grant_key"))
                return

    async def _sweep_overdue(self) -> None:
        """Закрыть активные события с истёкшим эффективным
        дедлайном (страховка на случай потерянной finish-задачи)."""
        try:
            rows = await event_store.overdue()
        except Exception:
            logger.exception("event overdue sweep failed")
            return
        for row in rows:
            try:
                session = self._session_for(str(row["event_id"]))
                if session is not None and session.finished:
                    # Прошлая попытка финализации упала (напр. PG
                    # был недоступен) — пересоздаём из строки БД.
                    self._unregister_session(session)
                    session = None
                if session is None:
                    session = _EventSession.from_row(row)
                    self._register_session(session)
                await self._finish_event(session, "timeout")
            except Exception:
                logger.exception("overdue finalize failed id=%s",
                                 (row or {}).get("event_id"))

    async def _sweep_settling(self) -> None:
        """Довести settling-события: фиксация обязательств могла
        не пройти (PG/файл недоступны) — событие НЕ закрыто,
        повторяем идемпотентно."""
        try:
            rows = await event_store.list_settling()
        except Exception:
            logger.exception("event settling sweep failed")
            return
        for row in rows:
            try:
                await self._resume_settling(row)
            except Exception:
                logger.exception("settling resume failed id=%s",
                                 (row or {}).get("event_id"))

    async def _sweep_failed_cards(self) -> None:
        """Повторить финальную правку карточек, у которых edit
        не подтверждён (result.card_edit != 'ok')."""
        try:
            rows = await event_store.list_failed_cards()
        except Exception:
            logger.exception("failed-cards sweep failed")
            return
        for row in rows:
            try:
                stored = _json_field(row, "result") or {}
                outcome = stored.get("outcome")
                if not isinstance(outcome, dict) or not outcome:
                    continue
                session = _EventSession.from_row(row)
                await self._edit_final_card(
                    session, outcome,
                    str(stored.get("grant") or "none"),
                    rewards=stored.get("rewards")
                    if isinstance(stored.get("rewards"), dict)
                    else None)
            except Exception:
                logger.exception("card retry failed id=%s",
                                 (row or {}).get("event_id"))

    # ---------------------------------------------------------- legacy

    async def _process_drop_reward(self, row: dict) -> bool:
        """Legacy: начисление монет drop_coin_rewards."""
        key = row["grant_key"]
        attempts = int(row.get("attempts") or 0)
        try:
            if not user_balance_store._use_db:
                raise RuntimeError(
                    "drop rewards require PostgreSQL idempotency")
            await user_balance_store.add_balance_once(
                int(row["guild_id"]), int(row["user_id"]),
                int(row["amount"]), key)
        except Exception as exc:
            delay = _drop_backoff(attempts or 1)
            try:
                ok = await drop_reward_store.retry(
                    key, attempts, f"{type(exc).__name__}: {exc}", delay)
                if not ok:
                    logger.info(
                        "drop reward retry skipped — lease lost key=%s",
                        key)
            except Exception:
                logger.critical(
                    "drop reward retry-mark failed key=%s — "
                    "row stays processing until lease expiry", key)
            logger.warning(
                "drop reward deferred key=%s attempts=%s next_in=%.0fs",
                key, attempts, delay)
            return False
        try:
            ok = await drop_reward_store.complete(key, attempts)
            if not ok:
                logger.info(
                    "drop reward complete skipped — lease lost key=%s",
                    key)
        except Exception:
            logger.exception(
                "drop reward complete failed key=%s", key)
        logger.info("drop reward credited key=%s user=%s amount=%s",
                    key, row.get("user_id"), row.get("amount"))
        return True

    async def _settle_pending_item(self, item: dict) -> str:
        key = item["grant_key"]
        try:
            row = await drop_reward_store.record(
                key, item["kind"], int(item["guild_id"]),
                int(item["user_id"]), int(item["amount"]))
        except Exception:
            logger.warning("drop reward still unpersisted key=%s", key)
            return "queued"
        drop_pending_store.remove(key)
        logger.info("drop reward persisted from pending file key=%s",
                    key)
        owner = int((row or {}).get("user_id") or -1)
        if owner != int(item["user_id"]):
            return "other"
        if (row or {}).get("status") == "completed":
            return "credited"
        claimed = await drop_reward_store.claim_key(key)
        if claimed is None:
            return "queued"
        return ("credited" if await self._process_drop_reward(claimed)
                else "queued")

    async def _drain_pending_file(self) -> None:
        for item in drop_pending_store.all():
            try:
                async with self._grant_lock:
                    await self._settle_pending_item(item)
            except Exception:
                logger.exception(
                    "drop pending drain failed key=%s",
                    item.get("grant_key"))
                return

    async def _reward_worker(self) -> None:
        """Единый цикл: legacy drop-награды + event-награды +
        закрытие просроченных событий. Тикает каждые
        _DROP_WORKER_INTERVAL секунд; ошибки PG не роняют цикл."""
        while True:
            try:
                await self._drain_pending_file()
                for row in await drop_reward_store.claim_due(limit=10):
                    await self._process_drop_reward(row)
                await self._drain_event_pending()
                for row in await event_reward_store.claim_due(limit=10):
                    await self._process_event_reward(row)
                await self._sweep_overdue()
                await self._sweep_settling()
                await self._sweep_failed_cards()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("reward worker tick failed")
            await asyncio.sleep(_DROP_WORKER_INTERVAL)

    # ---------------------------------------------------------- commands

    async def cog_load(self) -> None:
        bind_commands(admin_group, self, ["forcedrop"])
        # Persistent DynamicItem-диспетчер кнопок se2:*.
        try:
            self.bot.add_view(EventCardTemplateView())
        except Exception as exc:
            logger.warning("event view registration failed: %s", exc)
        self._reward_task = asyncio.get_running_loop().create_task(
            self._reward_worker())
        asyncio.get_running_loop().create_task(
            self._restore_sessions())

    async def cog_unload(self) -> None:
        task = self._reward_task
        self._reward_task = None
        if task:
            task.cancel()
        for s in list(self._sessions.values()):
            for t in (s.task, s.refresher):
                if t is not None:
                    t.cancel()
        self._sessions.clear()
        self._by_channel.clear()

    async def trigger_drop(self, channel: discord.TextChannel,
                           force_type: str | None = None) -> str:
        """Ручной запуск (/forcedrop): обходит активити-гейт и
        кулдаун, НО не обходит guard активного события и feature-
        flag. Только для владельца — тестовый контроль."""
        gid = channel.guild.id if channel.guild else None
        if not event_config.feature_enabled(gid):
            return "disabled"
        settings = event_config.event_settings(gid)
        return await self._try_start(channel, channel.guild, settings,
                                     natural=False,
                                     force_type=force_type)

    @admin_group.command(
        name="forcedrop",
        description="Принудительно запустить спонтанное событие "
                    "(только владелец бота; активное событие не "
                    "обходит)")
    @app_commands.describe(game_type="Тип события (пусто — случайный)")
    @app_commands.choices(game_type=[
        app_commands.Choice(name=g, value=g)
        for g in event_games.GAME_ORDER])
    @is_bot_owner()
    async def force_drop(self, interaction: discord.Interaction,
                         game_type: str | None = None):
        await interaction.response.defer(ephemeral=True)
        status = await self.trigger_drop(interaction.channel,
                                         force_type=game_type)
        msgs = {
            "started": "✅ Событие запущено!",
            "active": "⛔ На канале уже идёт событие — дубликат "
                      "не создан.",
            "cooldown": "⏳ Кулдаун ещё активен.",
            "disabled": "⛔ Спонтанные события выключены "
                        "(feature.coin_drop_enabled).",
            "disabled_type": "⛔ Указанный тип события отключён.",
            "no_games": "⛔ Нет доступных типов событий.",
            "no_prize": "⛔ Не удалось выбрать приз.",
            "db_error": "⛔ Ошибка базы данных.",
            "send_failed": "⛔ Не удалось отправить карточку.",
        }
        await interaction.followup.send(
            msgs.get(status, f"Статус: {status}"), ephemeral=True)


async def setup(bot: "TournamentBot"):
    await bot.add_cog(CoinDropCog(bot))
