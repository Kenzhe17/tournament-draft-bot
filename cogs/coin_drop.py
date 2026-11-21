"""Lucky Coins Event Module - Spontaneous coin drops in chat."""

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Set

import discord
from utils.design import compose, smart_view

from discord import app_commands
from discord.ext import commands

from config import get_emoji, BOT_OWNER_ID, NOTIFICATION_CHANNEL_ID
from storage.user_balance_store import user_balance_store
from storage.drop_reward_store import drop_reward_store
from storage.drop_pending_store import drop_pending_store

if TYPE_CHECKING:
    from bot import TournamentBot

logger = logging.getLogger(__name__)


# Custom check for bot owner
def is_bot_owner():
    def predicate(interaction: discord.Interaction):
        return interaction.user.id == BOT_OWNER_ID
    return app_commands.check(predicate)


# Глобальный словарь для отслеживания количества сообщений по каналам: {channel_id: message_count}
CHANNEL_MESSAGE_COUNTERS: dict[int, int] = {}

# Глобальный словарь для отслеживания последнего ивента: {channel_id: last_event_time}
LAST_EVENT_TIME: dict[int, datetime] = {}


# ==========================================
# Вспомогательные функции работы с БД
# ==========================================

async def add_balance(guild_id: int, user_id: int, amount: int) -> None:
    """Зачисление монет пользователю."""
    if amount <= 0:
        return
    await user_balance_store.add_balance(guild_id, user_id, amount)


_DROP_BACKOFF_BASE = 30.0
_DROP_BACKOFF_CAP = 1800.0
_DROP_WORKER_INTERVAL = 15.0


def _drop_backoff(attempts: int) -> float:
    """Ограниченный экспоненциальный backoff: 30с, 60с, 120с, ...
    максимум 30 минут. Без бесконечного быстрого повторения."""
    return min(_DROP_BACKOFF_BASE * (2 ** max(0, attempts - 1)),
               _DROP_BACKOFF_CAP)


async def grant_drop_reward(cog, guild_id: int, user_id: int,
                            amount: int, grant_key: str,
                            kind: str) -> str:
    """Фиксация победителя в надёжном хранилище + попытка выплаты.

    Хранилища по приоритету:
    1. drop_reward_store.record() — PostgreSQL (grant_key PK);
    2. drop_pending_store.add() — атомарный JSON-файл, переживает
       рестарт процесса (используется только при недоступности PG).

    Награда НЕ считается зафиксированной, пока обязательство не
    попало хотя бы в одно из них — «failed» означает, что
    объявлять победителя нельзя.

    Возвращает:
      "credited" — монеты уже на балансе;
      "queued"   — обязательство durable, worker доведёт до
                   completed (включая случай, когда строку PG
                   уже держит lease другого обработчика);
      "other"    — grant_key уже принадлежит другому участнику
                   (повторная фиксация с другим user_id);
      "failed"   — зафиксировать не удалось нигде.
    """
    if amount <= 0:
        return "credited"
    try:
        row = await drop_reward_store.record(
            grant_key, kind, guild_id, user_id, amount)
    except Exception:
        logger.critical(
            "drop reward PG record failed key=%s guild=%s user=%s "
            "amount=%s kind=%s — durable pending file fallback",
            grant_key, guild_id, user_id, amount, kind)
        try:
            item = drop_pending_store.add({
                "grant_key": grant_key, "kind": kind,
                "guild_id": guild_id, "user_id": user_id,
                "amount": amount,
            })
        except Exception:
            logger.critical(
                "drop reward fixation FAILED key=%s — ни PG, ни "
                "pending-файл недоступны", grant_key)
            return "failed"
        if int(item.get("user_id") or -1) != int(user_id):
            return "other"
        return await cog._settle_pending_item(item)
    owner = int((row or {}).get("user_id") or -1)
    if owner != int(user_id):
        logger.warning(
            "drop reward grant_key %s уже принадлежит user=%s — "
            "отклонён user=%s", grant_key, owner, user_id)
        return "other"
    if (row or {}).get("status") == "completed":
        return "credited"
    # Единый claim-механизм: кнопка и worker не обрабатывают
    # одну награду параллельно (lease + fencing по attempts).
    claimed = await drop_reward_store.claim_key(grant_key)
    if claimed is None:
        # Строку уже держит worker — он доведёт начисление.
        return "queued"
    return ("credited" if await cog._process_drop_reward(claimed)
            else "queued")


# ==========================================
# UI Компоненты Ивента
# ==========================================

# 1. Режим «На скорость» (Speed Drop)
class SpeedDropView(discord.ui.View):
    def __init__(self, reward_amount: int, event_id: str):
        super().__init__(timeout=60.0)
        self.reward_amount = reward_amount
        self.event_id = event_id
        self.claimed = False

    @discord.ui.button(
        label="Забрать монеты!",
        style=discord.ButtonStyle.green,
        custom_id="speed_coin_drop_btn"  # Will be dynamically updated
    )
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Флаг занимаем ДО ответа: два быстрых клика не должны
        # оба пройти проверку claimed и получить двойную выплату.
        if self.claimed:
            await interaction.response.send_message(
                f" Кто-то оказался быстрее вас!",
                ephemeral=True
            )
            return

        # Defer — только ack «бот думает», победитель ещё НЕ
        # объявлен: фиксация обязательства идёт после defer,
        # иначе медленный PG даст Discord 10062.
        await interaction.response.defer()

        # Сначала durable-фиксация победителя (PG → pending-файл):
        # claimed НЕ потребляется и ивент не завершается, пока
        # обязательство не сохранено в надёжном хранилище.
        cog = interaction.client.get_cog("CoinDropCog")
        result = await grant_drop_reward(
            cog, interaction.guild_id, interaction.user.id,
            self.reward_amount, f"speeddrop:{self.event_id}", "speed")

        if result == "other":
            await interaction.followup.send(
                "Награда по этому событию уже зафиксирована за "
                "другим участником.", ephemeral=True)
            return
        if result == "failed":
            # Ни PG, ни durable-файл не приняли обязательство —
            # claimed не потреблён, ивент жив, клик можно повторить.
            await interaction.followup.send(
                "Временная ошибка сохранения — попробуйте забрать "
                "награду ещё раз.", ephemeral=True)
            return

        # Зафиксировано — теперь занимаем claim (гонка двух кликов:
        # второй увидит claimed и получит «кто-то быстрее»).
        if self.claimed:
            await interaction.followup.send(
                " Кто-то оказался быстрее вас!", ephemeral=True)
            return
        self.claimed = True
        self.stop()

        result_embed = discord.Embed(
            title=f"**ИВЕНТ ЗАВЕРШЁН | НАГРАДА ЗАБРАНА**",
            description=(
                f" **Счастливчик:** {interaction.user.mention}\n"
                f" **Выигрыш:** **`{self.reward_amount:,}`** {get_emoji('money')}\n\n"
                + ("*Монеты зачислены на ваш баланс. Проверить: `/balance`*"
                   if result == "credited" else
                   "*Зачисление подтверждается — при задержке монеты "
                   "появятся автоматически, повторное начисление "
                   "исключено.*")
            ),
            color=0x2ECC71
        )
        try:
            await interaction.edit_original_response(view=smart_view(await interaction.original_response(), None))
            await interaction.followup.send( view=compose(result_embed))
        except discord.HTTPException:
            # Ответ Discord потерян — монеты уже зачислены
            # идемпотентно, пользователь не пострадает.
            logger.warning("speed drop notify failed user=%s",
                           interaction.user.id)


# 2. Режим «Розыгрыш / 60 секунд» (Raffle Drop)
class RaffleDropView(discord.ui.View):
    def __init__(self, reward_amount: int, event_id: str):
        super().__init__(timeout=60.0)
        self.reward_amount = reward_amount
        self.event_id = event_id
        self.participants: Set[int] = set()  # Store user IDs instead of User objects

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"**СПОНТАННЫЙ ИВЕНТ | ЛОТЕРЕЯ МОНЕТ**",
            description=(
                f" В чате разыгрывается мешок с монетами!\n"
                f" Награда: **`{self.reward_amount:,}`** {get_emoji('money')}\n"
                f" Участников: **`{len(self.participants)}`**\n\n"
                f" *Победитель решится случайно <t:{int(datetime.now().timestamp()) + 60}:R>*"
            ),
            color=0xF1C40F
        )

    @discord.ui.button(
        label="Участвовать в розыгрыше",
        style=discord.ButtonStyle.blurple,
        custom_id="raffle_coin_drop_btn"  # Will be dynamically updated
    )
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user.id in self.participants:
            await interaction.followup.send(
                f" Вы уже участвуете в этом розыгрыше!",
                ephemeral=True
            )
            return

        self.participants.add(interaction.user.id)
        await interaction.edit_original_response( view=compose(self.build_embed(), self))


# ==========================================
# Cog Модуль Автоматического Спавна
# ==========================================

from cogs.groups import admin_group, bind_commands


class CoinDropCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot
        self._default_channel_id = NOTIFICATION_CHANNEL_ID
        self._drop_lock = asyncio.Lock()
        self._reward_task: asyncio.Task | None = None

    def _target_channel_id(self, guild) -> int:
        from services.discord_cfg import channel_id as _dc
        gid = guild.id if guild else None
        return _dc(gid, "discord.notification_channel_id",
                   self._default_channel_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Игнорируем сообщения ботов и системные сообщения
        if message.author.bot or not message.guild:
            return

        # Проверяем нужный канал
        if message.channel.id != self._target_channel_id(message.guild):
            return

        channel_id = message.channel.id

        # Проверяем 30-минутный cooldown перед счётом
        last_event = LAST_EVENT_TIME.get(channel_id)
        if last_event:
            time_since_last = datetime.now() - last_event
            if time_since_last < timedelta(minutes=30):
                return  # В кулдауне, не считаем сообщения

        # Считаем сообщения
        CHANNEL_MESSAGE_COUNTERS[channel_id] = CHANNEL_MESSAGE_COUNTERS.get(channel_id, 0) + 1

        # Каждые 100 сообщений запускаем ивент
        if CHANNEL_MESSAGE_COUNTERS[channel_id] >= 100:
            CHANNEL_MESSAGE_COUNTERS[channel_id] = 0  # Сбрасываем счетчик
            await self.trigger_drop(message.channel)

    async def trigger_drop(self, channel: discord.TextChannel):
        """Логика генерации случайного ивента (сумма 100-500, шанс 50/50)."""
        # Check if coin drops are enabled: Control Center (PG) →
        # legacy settings.json fallback внутри peek.
        from services.settings_service import settings_service
        if not settings_service.peek(channel.guild.id if channel.guild else None,
                                      "feature.coin_drop_enabled"):
            return

        # Защита от параллельного запуска (естественный ивент + forcedrop).
        async with self._drop_lock:
            # Повторная проверка кулдауна под локом.
            last_event = LAST_EVENT_TIME.get(channel.id)
            if last_event and (datetime.now() - last_event) < timedelta(minutes=30):
                return

            # Записываем время ивента до отправки — повторный триггер
            # в окне отправки не создаст второй дроп.
            LAST_EVENT_TIME[channel.id] = datetime.now()

            reward = random.randint(100, 500)
            is_speed_mode = random.choice([True, False])

            # Генерируем уникальный ID для ивента (чтобы избежать конфликтов кнопок)
            event_id = str(datetime.now().timestamp())

        if is_speed_mode:
            # === РЕЖИМ 1: НА СКОРОСТЬ ===
            view = SpeedDropView(reward, event_id)
            # Обновляем custom_id кнопки чтобы был уникальным
            for item in view.children:
                item.custom_id = f"speed_coin_drop_{event_id}"

            embed = discord.Embed(
                title=f"**СПОНТАННЫЙ ИВЕНТ | БЫСТРЫЕ МОНЕТЫ**",
                description=(
                    f" В чате обнаружен мешок с монетами!\n"
                    f" Нажмите кнопку как можно быстрее — кто успеет первым, тот забрал куш.\n\n"
                    f" Награда: **`{reward:,}`** {get_emoji('money')} | *Режим: На скорость*"
                ),
                color=0xF1C40F
            )
            await channel.send( view=compose(embed, view))

        else:
            # === РЕЖИМ 2: РОЗЫГРЫШ (60 СЕКУНД) ===
            view = RaffleDropView(reward, event_id)
            # Обновляем custom_id кнопки чтобы был уникальным
            for item in view.children:
                item.custom_id = f"raffle_coin_drop_{event_id}"

            embed = view.build_embed()
            msg = await channel.send( view=compose(embed, view))

            # Ожидание 60 секунд для сбора участников — в фоне,
            # чтобы не держать обработчик сообщения.
            self.bot.loop.create_task(
                self._raffle_finish(msg, view, reward, channel))

    async def _raffle_finish(self, msg: discord.Message, view: "RaffleDropView",
                             reward: int, channel: discord.TextChannel) -> None:
        """Финализация розыгрыша через 60 секунд (фоновая задача)."""
        try:
            await asyncio.sleep(60.0)
            if not view.participants:
                no_p_embed = discord.Embed(
                    title=f"**ЛОТЕРЕЯ ЗАВЕРШЕНА | НИКОГО**",
                    description=f" Никто не успел принять участие за 60 секунд. Мешок с {get_emoji('money')} сгорел!",
                    color=0xED4245
                )
                try:
                    await msg.edit(view=smart_view(msg, None))
                    await msg.reply( view=compose(no_p_embed))
                except discord.HTTPException:
                    pass
                return

            winner_id = random.choice(list(view.participants))
            winner = channel.guild.get_member(winner_id)
            if not winner:
                # Если пользователь не найден, выбираем другого
                view.participants.discard(winner_id)
                if view.participants:
                    winner_id = random.choice(list(view.participants))
                    winner = channel.guild.get_member(winner_id)

            if winner:
                result = await grant_drop_reward(
                    self, channel.guild.id, winner.id, reward,
                    f"raffledrop:{view.event_id}", "raffle")
                if result in ("failed", "other"):
                    # Обязательство не зафиксировано durable —
                    # НЕ объявляем победителя и НЕ обещаем выплату.
                    # CRITICAL-лог содержит все данные для
                    # ручного восстановления.
                    logger.critical(
                        "raffle winner NOT persisted event=%s "
                        "winner=%s amount=%s result=%s",
                        view.event_id, winner.id, reward, result)
                    try:
                        await msg.edit(view=smart_view(msg, None))
                        await msg.reply(view=compose(discord.Embed(
                            title="**ЛОТЕРЕЯ ОТМЕНЕНА | ТЕХНИЧЕСКАЯ ОШИБКА**",
                            description=(
                                "Розыгрыш не завершён: временная "
                                "ошибка сохранения данных. Победитель "
                                "не объявлен — обратитесь к "
                                "администратору."),
                            color=0xED4245)))
                    except discord.HTTPException:
                        pass
                    return

                result_embed = discord.Embed(
                    title=f"**ЛОТЕРЕЯ ЗАВЕРШЕНА | ПОБЕДИТЕЛЬ**",
                    description=(
                        f" **Победитель:** {winner.mention}\n"
                        f" **Выигрыш:** **`{reward:,}`** {get_emoji('money')}\n"
                        f" **Всего участников:** `{len(view.participants)}`\n\n"
                        + ("*Монеты зачислены на ваш баланс. Проверить: `/balance`*"
                           if result == "credited" else
                           "*Зачисление подтверждается — при задержке монеты "
                           "появятся автоматически.*")
                    ),
                    color=0x2ECC71
                )
                try:
                    await msg.edit(view=smart_view(msg, None))
                    await msg.reply( view=compose(result_embed))
                except discord.HTTPException:
                    pass
        except Exception:
            import logging
            logging.getLogger(__name__).exception("coin_drop raffle finish failed")

    # Админ-команда для принудительного вызова ивента
    async def cog_load(self) -> None:
        bind_commands(admin_group, self, ["forcedrop"])
        # Восстановление долговременных наград: незавершённые
        # обязательства из drop_coin_rewards подхватываются
        # автоматически после рестарта/восстановления PG.
        self._reward_task = asyncio.get_running_loop().create_task(
            self._reward_worker())

    async def cog_unload(self) -> None:
        task = self._reward_task
        self._reward_task = None
        if task:
            task.cancel()

    async def _process_drop_reward(self, row: dict) -> bool:
        """Одна попытка начисления по зафиксированному обязательству.
        row — результат claim_key/claim_due: содержит attempts
        текущего lease. complete/retry fencing'ятся по attempts —
        устаревший worker не сможет перезаписать чужую обработку.
        Идемпотентно: add_balance_once дедуплицирует по grant_key
        даже при повторной обработке после потерянного ответа."""
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
            # Начисление закоммичено, но отметка потеряна:
            # lease истечёт → worker повторит → add_balance_once
            # вернёт существующий грант без двойного кредита.
            logger.exception(
                "drop reward complete failed key=%s", key)
        logger.info("drop reward credited key=%s user=%s amount=%s",
                    key, row.get("user_id"), row.get("amount"))
        return True

    async def _settle_pending_item(self, item: dict) -> str:
        """Pending-файл → PG record → claim → process.
        При повторном сбое PG элемент остаётся в файле до
        следующего тика worker (файл переживает рестарт)."""
        key = item["grant_key"]
        try:
            row = await drop_reward_store.record(
                key, item["kind"], int(item["guild_id"]),
                int(item["user_id"]), int(item["amount"]))
        except Exception:
            logger.warning("drop reward still unpersisted key=%s", key)
            return "queued"
        drop_pending_store.remove(key)
        logger.info("drop reward persisted from pending file key=%s", key)
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
        """Перенос обязательств из durable pending-файла в PG.
        Вызывается worker'ом каждые _DROP_WORKER_INTERVAL секунд
        и после восстановления PostgreSQL."""
        for item in drop_pending_store.all():
            try:
                await self._settle_pending_item(item)
            except Exception:
                logger.exception(
                    "drop pending drain failed key=%s",
                    item.get("grant_key"))
                # PG может лежать — остальные элементы подождут
                # следующего тика.
                return

    async def _reward_worker(self) -> None:
        """Фоновый цикл восстановления наград. Тикает каждые
        _DROP_WORKER_INTERVAL секунд; ошибки PG не роняют цикл —
        backoff задаётся next_attempt_at на уровне строк."""
        while True:
            try:
                await self._drain_pending_file()
                for row in await drop_reward_store.claim_due(limit=10):
                    await self._process_drop_reward(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("drop reward worker tick failed")
            await asyncio.sleep(_DROP_WORKER_INTERVAL)

    @admin_group.command(name="forcedrop", description="Принудительно запустить ивент 'Счастливые монеты' (Только владелец бота)")
    @is_bot_owner()
    async def force_drop(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.trigger_drop(interaction.channel)
        await interaction.followup.send(f" Ивент успешно вызван принудительно!", ephemeral=True)


async def setup(bot: "TournamentBot"):
    await bot.add_cog(CoinDropCog(bot))
