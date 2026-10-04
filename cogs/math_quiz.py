"""Модуль математической дуэли - /mathquiz команда."""

import asyncio
import random
from typing import TYPE_CHECKING, Dict, List

import discord
from discord import app_commands
from discord.ext import commands

from storage.user_balance_store import user_balance_store
from config import replace_emojis, get_emoji

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
        players_list = "\n".join([f"{get_emoji('white_dot')} {p.mention}" for p in self.players])
        total_bank = len(self.players) * self.bet

        embed = discord.Embed(
            title=f"{get_emoji('a_star')} **МАТЕМАТИЧЕСКАЯ ДУЭЛЬ | ЛОББИ**",
            description=(
                f"{get_emoji('white_arrow')} **Создатель:** {self.host.mention}\n"
                f"{get_emoji('white_arrow')} **Ставка:** `{self.bet:,}` {get_emoji('money')}\n"
                f"{get_emoji('white_arrow')} **Призовой банк:** `{total_bank:,}` {get_emoji('money')}\n\n"
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
            await interaction.followup.send(f"{get_emoji('white_dot')} Вы уже состоите в этом лобби!", ephemeral=True)
            return

        if len(self.players) >= 6:
            await interaction.followup.send(f"{get_emoji('white_dot')} Лобби уже заполнено (максимум 6 игроков)!", ephemeral=True)
            return

        balance = await user_balance_store.get_balance(self.guild_id, interaction.user.id)
        if balance < self.bet:
            await interaction.followup.send(f"{get_emoji('white_dot')} Недостаточно монет для ставки! Ваш баланс: `{balance:,}` {get_emoji('money')}.", ephemeral=True)
            return

        await user_balance_store.subtract_balance(self.guild_id, interaction.user.id, self.bet)
        self.players.append(interaction.user)

        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Выйти", style=discord.ButtonStyle.red, custom_id="math_quiz_leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user not in self.players:
            await interaction.followup.send(f"{get_emoji('white_dot')} Вы не состоите в этом лобби!", ephemeral=True)
            return

        if interaction.user == self.host:
            await interaction.followup.send(f"{get_emoji('white_dot')} Создатель не может выйти. Используйте кнопку «Отмена».", ephemeral=True)
            return

        await user_balance_store.add_balance(self.guild_id, interaction.user.id, self.bet)
        self.players.remove(interaction.user)

        await interaction.edit_original_response(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Старт", style=discord.ButtonStyle.blurple, custom_id="math_quiz_start")
    async def start_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user != self.host:
            await interaction.followup.send(f"{get_emoji('white_dot')} Только создатель игры может запустить старт!", ephemeral=True)
            return

        if len(self.players) < 2:
            await interaction.followup.send(f"{get_emoji('white_dot')} Для начала игры требуется минимум 2 участника!", ephemeral=True)
            return

        self.started = True
        self.stop()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.gray, custom_id="math_quiz_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if interaction.user != self.host:
            await interaction.followup.send(f"{get_emoji('white_dot')} Только создатель может отменить игру!", ephemeral=True)
            return

        for player in self.players:
            await user_balance_store.add_balance(self.guild_id, player.id, self.bet)

        self.stop()
        cancel_embed = discord.Embed(
            title=f"{get_emoji('white_arrow')} **Игра отменена**",
            description="Создатель распустил лобби. Все ставки возвращены на счета.",
            color=0xED4245
        )
        await interaction.edit_original_response(embed=cancel_embed, view=None)


# ==========================================
# Основной Cog Модуль
# ==========================================

class MathQuizCog(commands.Cog):
    def __init__(self, bot: "TournamentBot"):
        self.bot = bot

    @app_commands.command(name="mathquiz", description="Математическая дуэль на скорость (2-6 игроков)")
    @app_commands.describe(bet="Размер ставки (от 20 до 1 000 монет)")
    async def math_quiz(self, interaction: discord.Interaction, bet: int):
        await interaction.response.defer()

        if bet < 20 or bet > 1000:
            await interaction.followup.send(f"{get_emoji('white_dot')} Ставка должна составлять от 20 до 1 000 монет!", ephemeral=True)
            return

        guild_id = interaction.guild_id
        balance = await user_balance_store.get_balance(guild_id, interaction.user.id)
        if balance < bet:
            await interaction.followup.send(f"{get_emoji('white_dot')} Недостаточно монет для ставки! Ваш баланс: `{balance:,}` {get_emoji('money')}.", ephemeral=True)
            return

        await user_balance_store.subtract_balance(guild_id, interaction.user.id, bet)

        lobby_view = QuizLobbyView(guild_id, interaction.user, bet)
        message = await interaction.followup.send(embed=lobby_view.build_embed(), view=lobby_view)

        # Create thread for the game
        thread_name = f"🧮 Мат. дуэль - {interaction.user.display_name}"
        try:
            thread = await message.create_thread(
                name=thread_name,
                auto_archive_duration=60
            )
            await thread.send(f"{get_emoji('white_arrow')} Лобби создано! Ожидание игроков...")
        except Exception as e:
            print(f"Failed to create thread: {e}")

        await lobby_view.wait()

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

            leaderboard = "\n".join([f"{get_emoji('white_dot')} {p.mention} — **{scores[p.id]}** баллов" for p in players])
            round_embed = discord.Embed(
                title=f"{get_emoji('a_sparkle')} **МАТЕМАТИЧЕСКАЯ ДУЭЛЬ | РАУНД {round_idx}/10**",
                description=(
                    f"{get_emoji('white_arrow')} **Призовой банк:** `{total_bank:,}` {get_emoji('money')}\n\n"
                    f"### {get_emoji('white_dot')} Пример: **`{expression} = ?`**\n\n"
                    f"**Текущий счёт:**\n{leaderboard}"
                ),
                color=0xF1C40F
            )
            round_embed.set_footer(text="Отправляйте ответ числом прямо в чат")
            await message.edit(embed=round_embed, view=None)

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
                            f"{get_emoji('a_star')} {user_msg.author.mention} первым ответил верно! **({expression} = {correct_answer})**"
                        )
                        messages_to_clean.append(confirm_msg)

                except asyncio.TimeoutError:
                    round_solved = True
                    timeout_msg = await interaction.channel.send(
                        f"{get_emoji('white_arrow')} Время вышло! Никто не дал верный ответ. Правильно: **{correct_answer}**"
                    )
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
            result_text = f"{get_emoji('a_sparkle')} **Победитель:** {winner.mention}\n{get_emoji('white_arrow')} **Выигрыш:** `{total_bank:,}` {get_emoji('money')}!"
        else:
            split_prize = total_bank // len(winners)
            for winner in winners:
                await user_balance_store.add_balance(guild_id, winner.id, split_prize)
            winners_mentions = ", ".join([w.mention for w in winners])
            result_text = f"{get_emoji('a_sparkle')} **Ничья между:** {winners_mentions}\n{get_emoji('white_arrow')} **Каждый получает:** `{split_prize:,}` {get_emoji('money')}!"

        final_board = "\n".join([f"{get_emoji('white_dot')} {p.mention} — **{scores[p.id]}** баллов" for p in players])
        final_embed = discord.Embed(
            title=f"{get_emoji('a_star')} **ИГРА ЗАВЕРШЕНА | ФИНАЛЬНЫЕ ИТОГИ**",
            description=f"{result_text}\n\n**Финальный счёт:**\n{final_board}",
            color=0x2ECC71
        )
        await message.edit(embed=final_embed)


async def setup(bot: "TournamentBot"):
    await bot.add_cog(MathQuizCog(bot))
