"""Модуль математической дуэли - /mathquiz команда."""

import asyncio
import random
import logging
from typing import TYPE_CHECKING, Dict, List

import discord
from utils.design import compose
from discord import app_commands
from discord.ext import commands

from storage.user_balance_store import user_balance_store
from config import replace_emojis, get_emoji

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from bot import TournamentBot


# ==========================================
# UI Компоненты Лобби
# ==========================================

class QuizLobbyView(discord.ui.View):
    def __init__(self, guild_id: int, host: discord.User, bet: int):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.host = host
        self.bet = bet
        self.players: List[discord.User] = [host]
        self.started = False

    def build_embed(self) -> discord.Embed:
        players_list = "\n".join([f" {p.mention}" for p in self.players])
        total_bank = len(self.players) * self.bet

        embed = discord.Embed(
            title=f"**МАТЕМАТИЧЕСКАЯ ДУЭЛЬ | ЛОББИ**",
            description=(
                f" **Создатель:** {self.host.mention}\n"
                f" **Ставка:** `{self.bet:,}` {get_emoji('money')}\n"
                f" **Призовой банк:** `{total_bank:,}` {get_emoji('money')}\n\n"
                f"**Участники ({len(self.players)}/6):**\n{players_list}"
            ),
            color=0x3498DB
        )
        embed.set_footer(text="Для старта требуется от 2 до 6 участников")
        return embed

    @discord.ui.button(label="Присоединиться", style=discord.ButtonStyle.green, custom_id="math_quiz_join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user in self.players:
            await interaction.followup.send(f" Вы уже состоите в этом лобби!", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        if len(self.players) >= 6:
            await interaction.followup.send(f" Лобби уже заполнено (максимум 6 игроков)!", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
        if balance < self.bet:
            await interaction.followup.send(f" Недостаточно монет для ставки! Ваш баланс: `{balance:,}` {get_emoji('money')}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, self.bet)
        self.players.append(interaction.user)

        await interaction.edit_original_response( view=compose(self.build_embed(), self))

    @discord.ui.button(label="Выйти", style=discord.ButtonStyle.red, custom_id="math_quiz_leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user not in self.players:
            await interaction.followup.send(f" Вы не состоите в этом лобби!", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        if interaction.user == self.host:
            await interaction.followup.send(f" Создатель не может выйти. Используйте кнопку «Отмена».", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        await user_balance_store.add_balance(self.guild_id, interaction.user.id, self.bet)
        self.players.remove(interaction.user)

        await interaction.edit_original_response( view=compose(self.build_embed(), self))

    @discord.ui.button(label="Старт", style=discord.ButtonStyle.blurple, custom_id="math_quiz_start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user != self.host:
            await interaction.followup.send(f" Только создатель игры может запустить старт!", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        if len(self.players) < 2:
            await interaction.followup.send(f" Для начала игры требуется минимум 2 участника!", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        self.started = True
        self.stop()

        # Запустить игру в фоне и передать сообщение лобби
        import asyncio
        bot = interaction.guild._state._get_client()
        original_message = await interaction.original_response()
        asyncio.create_task(self.start_game(interaction, bot, original_message))

    async def start_game(self, interaction: discord.Interaction, bot, original_message):
        """Запустить игру после старта лобби."""
        import random
        from storage.user_balance_store import user_balance_store
        from config import get_emoji

        players = self.players
        scores = {p.id: 0 for p in players}
        total_bank = len(players) * self.bet

        examples = []
        for _ in range(10):
            num1 = random.randint(10, 99)
            operator = random.choice(["+", "-"])

            if operator == "+":
                num2 = random.randint(10, 99)
                answer = num1 + num2
            else:
                num2 = random.randint(10, num1)
                answer = num1 - num2

            examples.append((f"{num1} {operator} {num2}", answer))

        for round_idx in range(1, 11):
            expression, correct_answer = examples[round_idx - 1]

            leaderboard = "\n".join([f" {p.mention} — **{scores[p.id]}** баллов" for p in players])
            round_embed = discord.Embed(
                title=f"**МАТЕМАТИЧЕСКАЯ ДУЭЛЬ | РАУНД {round_idx}/10**",
                description=(
                    f" **Призовой банк:** `{total_bank:,}` {get_emoji('money')}\n\n"
                    f"###  Пример: **`{expression} = ?`**\n\n"
                    f"**Текущий счёт:**\n{leaderboard}"
                ),
                color=0xF1C40F
            )
            round_embed.set_footer(text="Отправляйте ответ числом прямо в чат")
            await original_message.edit( view=compose(round_embed, None), allowed_mentions=discord.AllowedMentions.none())

            round_solved = False

            def check_answer(m):
                return (
                    m.channel.id == interaction.channel_id
                    and m.author.id in scores
                    and m.content.strip().isdigit()
                )

            while not round_solved:
                try:
                    user_msg = await bot.wait_for("message", check=check_answer, timeout=25.0)

                    if int(user_msg.content.strip()) == correct_answer:
                        scores[user_msg.author.id] += 1
                        round_solved = True

                        confirm_msg = await interaction.channel.send(
                            f" {user_msg.author.mention} первым ответил верно! **({expression} = {correct_answer})**", allowed_mentions=discord.AllowedMentions.none())

                except asyncio.TimeoutError:
                    round_solved = True
                    timeout_msg = await interaction.channel.send(
                        f" Время вышло! Никто не дал верный ответ. Правильно: **{correct_answer}**", allowed_mentions=discord.AllowedMentions.none())

            # Удалить все сообщения в канале после round_embed (включая ответы участников)
            try:
                async for msg in interaction.channel.history(after=original_message, limit=None):
                    try:
                        await msg.delete()
                    except:
                        pass
            except:
                pass

        # Определить победителя
        max_score = max(scores.values())
        winners = [p for p in players if scores[p.id] == max_score]

        if len(winners) == 1:
            winner = winners[0]
            prize = total_bank
            await user_balance_store.add_balance(self.guild_id, winner.id, prize)
            result_text = f" **Победитель:** {winner.mention} получает `{prize:,}` {get_emoji('money')}!"
        else:
            # Ничья - разделить приз
            prize_per_player = total_bank // len(winners)
            for winner in winners:
                await user_balance_store.add_balance(self.guild_id, winner.id, prize_per_player)
            winner_mentions = ", ".join([w.mention for w in winners])
            result_text = f" **Ничья!** {winner_mentions} получают по `{prize_per_player:,}` {get_emoji('money')}!"

        final_embed = discord.Embed(
            title=f"**МАТЕМАТИЧЕСКАЯ ДУЭЛЬ | РЕЗУЛЬТАТ**",
            description=(
                f" **Призовой банк:** `{total_bank:,}` {get_emoji('money')}\n\n"
                f"**Итоговый счёт:**\n"
                + "\n".join([f" {p.mention} — **{scores[p.id]}** баллов" for p in players])
                + f"\n\n{result_text}"
            ),
            color=0xF1C40F
        )
        await original_message.edit( view=compose(final_embed), allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.gray, custom_id="math_quiz_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user != self.host:
            await interaction.followup.send(f" Только создатель может отменить игру!", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        for player in self.players:
            await user_balance_store.add_balance(self.guild_id, player.id, self.bet)

        self.stop()
        cancel_embed = discord.Embed(
            title=f"**Игра отменена**",
            description="Создатель распустил лобби. Все ставки возвращены на счета.",
            color=0xED4245
        )
        await interaction.edit_original_response( view=compose(cancel_embed, None))


# ==========================================
# Основной Cog Модуль
# ==========================================

from cogs.groups import games_group, bind_commands


class MathQuizCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(games_group, self, ["mathquiz"])

    @games_group.command(name="mathquiz", description="Математическая дуэль на скорость (2-6 игроков)")
    @app_commands.describe(bet="Размер ставки (от 20 до 1 000 монет)")
    async def math_quiz(self, interaction: discord.Interaction, bet: int):
        user_id = interaction.user.id

        # Check if user is in a thread
        if not isinstance(interaction.channel, discord.Thread):
            await interaction.response.send_message(
                "Игры можно запускать только в игровых тредах. Используйте `/thread` для создания треда или `/delete` для удаления текущего.",
                ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        if bet < 20 or bet > 1000:
            await interaction.response.send_message(f" Ставка должна составлять от 20 до 1 000 монет!", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        guild_id = interaction.guild_id
        balance = await user_balance_store.get_balance(guild_id, interaction.user.id)
        if balance < bet:
            await interaction.response.send_message(f" Недостаточно монет для ставки! Ваш баланс: `{balance:,}` {get_emoji('money')}.", ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
            return

        await user_balance_store.subtract_balance(guild_id, interaction.user.id, bet)

        # Create lobby view directly in the thread
        lobby_view = QuizLobbyView(guild_id, interaction.user, bet)
        await interaction.response.send_message( view=compose(lobby_view.build_embed(), lobby_view), allowed_mentions=discord.AllowedMentions.none())

        if not lobby_view.started:
            return

        players = lobby_view.players
        scores: Dict[int, int] = {p.id: 0 for p in players}
        total_bank = len(players) * bet

        examples = []
        for _ in range(10):
            num1 = random.randint(10, 99)
            operator = random.choice(["+", "-"])

            if operator == "+":
                num2 = random.randint(10, 99)
                answer = num1 + num2
            else:
                num2 = random.randint(10, num1)
                answer = num1 - num2

            examples.append((f"{num1} {operator} {num2}", answer))

        for round_idx in range(1, 11):
            expression, correct_answer = examples[round_idx - 1]

            leaderboard = "\n".join([f" {p.mention} — **{scores[p.id]}** баллов" for p in players])
            round_embed = discord.Embed(
                title=f"**МАТЕМАТИЧЕСКАЯ ДУЭЛЬ | РАУНД {round_idx}/10**",
                description=(
                    f" **Призовой банк:** `{total_bank:,}` {get_emoji('money')}\n\n"
                    f"###  Пример: **`{expression} = ?`**\n\n"
                    f"**Текущий счёт:**\n{leaderboard}"
                ),
                color=0xF1C40F
            )
            round_embed.set_footer(text="Отправляйте ответ числом прямо в чат")
            await message.edit( view=compose(round_embed, None), allowed_mentions=discord.AllowedMentions.none())

            round_solved = False
            messages_to_clean: List[discord.Message] = []

            def check_answer(m: discord.Message) -> bool:
                return (
                    m.channel.id == interaction.channel_id
                    and m.author.id in scores
                    and m.content.strip().isdigit()
                )

            while not round_solved:
                try:
                    user_msg = await self.bot.wait_for("message", check=check_answer, timeout=25.0)
                    messages_to_clean.append(user_msg)

                    if int(user_msg.content.strip()) == correct_answer:
                        scores[user_msg.author.id] += 1
                        round_solved = True

                        confirm_msg = await interaction.channel.send(
                            f" {user_msg.author.mention} первым ответил верно! **({expression} = {correct_answer})**", allowed_mentions=discord.AllowedMentions.none())
                        messages_to_clean.append(confirm_msg)

                except asyncio.TimeoutError:
                    round_solved = True
                    timeout_msg = await interaction.channel.send(
                        f" Время вышло! Никто не дал верный ответ. Правильно: **{correct_answer}**", allowed_mentions=discord.AllowedMentions.none())
                    messages_to_clean.append(timeout_msg)

            await asyncio.sleep(2.0)
            if messages_to_clean:
                try:
                    await interaction.channel.delete_messages(messages_to_clean)
                except (discord.Forbidden, discord.HTTPException):
                    pass

            # Удалить все сообщения после раунда перед следующим раундом
            if round_idx < 10:
                try:
                    async for msg in interaction.channel.history(after=message, limit=None):
                        try:
                            await msg.delete()
                        except (discord.Forbidden, discord.HTTPException):
                            pass
                except (discord.Forbidden, discord.HTTPException):
                    pass

        max_score = max(scores.values())
        winners = [p for p in players if scores[p.id] == max_score]

        if len(winners) == 1:
            winner = winners[0]
            await user_balance_store.add_balance(guild_id, winner.id, total_bank)
            result_text = f" **Победитель:** {winner.mention}\n **Выигрыш:** `{total_bank:,}` {get_emoji('money')}!"
        else:
            split_prize = total_bank // len(winners)
            for winner in winners:
                await user_balance_store.add_balance(guild_id, winner.id, split_prize)
            winners_mentions = ", ".join([w.mention for w in winners])
            result_text = f" **Ничья между:** {winners_mentions}\n **Каждый получает:** `{split_prize:,}` {get_emoji('money')}!"

        final_board = "\n".join([f" {p.mention} — **{scores[p.id]}** баллов" for p in players])
        final_embed = discord.Embed(
            title=f"**ИГРА ЗАВЕРШЕНА | ФИНАЛЬНЫЕ ИТОГИ**",
            description=f"{result_text}\n\n**Финальный счёт:**\n{final_board}",
            color=0x2ECC71
        )

        await message.edit( view=compose(final_embed, None), allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: "TournamentBot"):
    await bot.add_cog(MathQuizCog(bot))
