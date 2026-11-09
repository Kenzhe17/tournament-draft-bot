"""Cog: VAC — временная блокировка участия в турнирах.

/adming команды: /vac, /unvac, /vacs. VAC хранится в PostgreSQL
(storage/vac_store.py), истекает по expires_at без таймеров.
При выдаче игрок вытесняется из активных MM-очередей/ready-check.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services import vac_service
from utils.design import colors
from utils.design.layouts import (
    DesignView,
    error_view,
    moderation_view,
    success_view,
)

# re-export для читаемости
__all__ = ["VacCog", "setup"]
from utils.permissions import is_admin_check

logger = logging.getLogger(__name__)

_PER_PAGE = 5


class VacsView(discord.ui.LayoutView):
    """Пагинированный список активных VAC (Components V2)."""

    def __init__(self, guild_id: int, rows: list[dict], page: int = 0):
        super().__init__(timeout=180.0)
        self.guild_id = guild_id
        self.rows = rows
        self.page = max(0, min(page, self.total_pages(rows) - 1))
        self._render()

    @staticmethod
    def total_pages(rows: list[dict]) -> int:
        return max(1, (len(rows) + _PER_PAGE - 1) // _PER_PAGE)

    def _render(self) -> None:
        container = discord.ui.Container()
        container.add_item(
            discord.ui.TextDisplay(content="> ## Активные VAC — r!z3")
        )
        container.add_item(discord.ui.Separator())

        page_rows = self.rows[
            self.page * _PER_PAGE : (self.page + 1) * _PER_PAGE
        ]
        now = datetime.now(timezone.utc)
        lines = []
        for i, r in enumerate(page_rows, start=self.page * _PER_PAGE + 1):
            expires = r["expires_at"]
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            left = expires - now
            total_min = max(0, int(left.total_seconds()) // 60)
            days, rem = divmod(total_min, 1440)
            hours, minutes = divmod(rem, 60)
            left_str = (
                f"{days}д {hours}ч" if days
                else f"{hours}ч {minutes}м" if hours
                else f"{minutes}м"
            )
            lines.append(
                f"{i}. <@{r['user_id']}> — осталось {left_str} "
                f"(до <t:{int(expires.timestamp())}:R>)"
            )
        container.add_item(discord.ui.TextDisplay(content="\n".join(lines)))
        container.add_item(
            discord.ui.TextDisplay(
                content=(
                    f"\nВсего активных VAC: {len(self.rows)}\n"
                    f"Страница {self.page + 1}/{self.total_pages(self.rows)}"
                )
            )
        )

        row = discord.ui.ActionRow()
        for label, delta, disabled in (
            ("Назад", -1, self.page == 0),
            ("Далее", 1, self.page >= self.total_pages(self.rows) - 1),
        ):
            btn = discord.ui.Button(
                style=discord.ButtonStyle.secondary,
                label=label,
                custom_id=f"vac_page:{self.guild_id}:{self.page + delta}",
                disabled=disabled,
            )
            btn.callback = self._page_callback
            row.add_item(btn)
        container.add_item(row)
        self.add_item(container)

    async def _page_callback(self, interaction: discord.Interaction) -> None:
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Недостаточно прав.", ephemeral=True
            )
            return
        try:
            page = int(interaction.data["custom_id"].rsplit(":", 1)[1])
        except Exception:
            return
        rows = await vac_service.list_active_vacs(self.guild_id)
        await interaction.response.edit_message(
            view=VacsView(self.guild_id, rows, page)
        )


def _fmt_duration(delta) -> str:
    total = int(delta.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days} дн.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes or not parts:
        parts.append(f"{minutes} мин.")
    return " ".join(parts)


class VacCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _eject_from_mm(self, guild_id: int, user_id: int) -> None:
        """Вытеснить пользователя из активных MM-очередей и ready-check."""
        try:
            from storage.mm_search_store import mm_search_store
            from storage.ready_check_store import ready_check_store
            from models.ready_check import ReadyCheckStatus

            for search in mm_search_store.get_by_guild(guild_id):
                name = next(
                    (n for n, u in search.player_user_ids.items() if u == user_id),
                    None,
                )
                if name is None:
                    continue
                search.remove_player(name)
                if not search.players:
                    search.timer_started = False
                mm_search_store.set(search)
                logger.info("VAC: %s removed from MM queue %s", user_id, search.channel_id)

            for rc in await ready_check_store.list_active():
                if rc.guild_id != guild_id or rc.player_by_id(user_id) is None:
                    continue
                # Отменить подбор, вернуть готовых в очередь без штрафов
                mm = self.bot.get_cog("MMCog")
                keep = {
                    p.user_id for p in rc.players
                    if p.is_ready and p.user_id != user_id
                }
                rc.status = ReadyCheckStatus.CANCELLED
                rc.cancel_reason = "игрок получил VAC"
                await ready_check_store.save(rc)
                if mm is not None:
                    await mm._finish_cancelled(rc, keep_user_ids=keep)
                logger.info("VAC: ready check %s cancelled for user %s", rc.match_id, user_id)
        except Exception:
            logger.exception("VAC: eject_from_mm failed")

    async def _mod_log(self, title: str, lines: list[str], color: int) -> None:
        try:
            from config import MOD_LOG_CHANNEL_ID
            channel = self.bot.get_channel(MOD_LOG_CHANNEL_ID)
            if channel is None:
                return
            view = moderation_view(title, "\n".join(lines))
            # moderation_view возвращает DesignView — цвет уже задан
            await channel.send(view=view)
        except Exception:
            logger.exception("VAC: mod log send failed")

    @app_commands.command(name="vac", description="Выдать VAC игроку")
    @app_commands.describe(player="Игрок", duration="30m, 2h, 1d, 7d, 14d, 30d, 1d12h")
    @app_commands.checks.cooldown(1, 2)
    async def vac_cmd(
        self,
        interaction: discord.Interaction,
        player: discord.User,
        duration: str,
    ) -> None:
        if interaction.guild is None:
            return
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Эта команда доступна только администраторам.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Требуется право Administrator.", ephemeral=True
            )
            return
        if player.bot or player.id == interaction.user.id or player.id == interaction.guild.owner_id:
            await interaction.response.send_message(
                "Нельзя выдать VAC себе, боту или владельцу сервера.",
                ephemeral=True,
            )
            return

        delta = vac_service.parse_duration(duration)
        if delta is None:
            await interaction.response.send_message(
                error_view(
                    "Неверная длительность",
                    "Поддерживаются форматы: `30m`, `2h`, `1d`, `7d`, `14d`, `30d`, `1d12h`.",
                ),
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        existing = await vac_service.get_active_vac(guild_id, player.id)
        if existing is not None:
            ts = int(existing["expires_at"].timestamp())
            await interaction.response.send_message(
                view=DesignView.simple(
                    title="VAC уже активен",
                    body=(
                        f"Участник: <@{player.id}>\n"
                        f"Текущий VAC истекает: <t:{ts}:R> (<t:{ts}:f>)"
                    ),
                    accent=colors.WARNING,
                    timeout=60.0,
                ),
                ephemeral=True,
            )
            return

        row, err = await vac_service.issue_vac(
            guild_id, player.id, interaction.user.id, delta
        )
        if err == "already_active" or row is None:
            await interaction.followup.send(
                "У игрока уже есть активный VAC.", ephemeral=True
            )
            return

        await self._eject_from_mm(guild_id, player.id)

        expires_ts = int(row["expires_at"].timestamp())
        view = success_view(
            "VAC выдан",
            f"Участник: <@{player.id}>\n"
            f"Длительность: {_fmt_duration(delta)}\n"
            f"Выдан: <@{interaction.user.id}>\n"
            f"Истекает: <t:{expires_ts}:R> (<t:{expires_ts}:f>)\n\n"
            "Участие в турнирах запрещено до окончания блокировки.",
        )
        view.container.accent_color = colors.ERROR
        await interaction.response.send_message(view=view)
        await self._mod_log(
            "VAC выдан",
            [
                f"Участник: <@{player.id}>",
                f"Выдал: <@{interaction.user.id}>",
                f"Срок: {_fmt_duration(delta)}",
                f"Истекает: <t:{expires_ts}:f>",
            ],
            colors.ERROR,
        )

    @app_commands.command(name="unvac", description="Снять VAC с игрока")
    @app_commands.describe(player="Игрок")
    @app_commands.checks.cooldown(1, 2)
    async def unvac_cmd(
        self,
        interaction: discord.Interaction,
        player: discord.User,
    ) -> None:
        if interaction.guild is None:
            return
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Эта команда доступна только администраторам.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Требуется право Administrator.", ephemeral=True
            )
            return

        row = await vac_service.revoke_vac(
            interaction.guild.id, player.id, interaction.user.id
        )
        if row is None:
            await interaction.response.send_message(
                "У игрока нет активного VAC.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            view=success_view(
                "VAC снят",
                f"Участник: <@{player.id}>\n"
                f"Снял: <@{interaction.user.id}>\n\n"
                "Игрок снова может участвовать в турнирах.",
            )
        )
        await self._mod_log(
            "VAC снят",
            [f"Участник: <@{player.id}>", f"Снял: <@{interaction.user.id}>"],
            colors.SUCCESS,
        )

    @app_commands.command(name="vacs", description="Список активных VAC")
    @app_commands.describe(player="Показать VAC конкретного игрока")
    @app_commands.checks.cooldown(1, 3)
    async def vacs_cmd(
        self,
        interaction: discord.Interaction,
        player: discord.User | None = None,
    ) -> None:
        if interaction.guild is None:
            return
        if not is_admin_check(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Эта команда доступна только администраторам.", ephemeral=True
            )
            return
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Требуется право Administrator.", ephemeral=True
            )
            return

        rows = await vac_service.list_active_vacs(interaction.guild.id)
        if player is not None:
            rows = [r for r in rows if int(r["user_id"]) == player.id]
        if not rows:
            body = (
                f"У <@{player.id}> нет активного VAC."
                if player is not None
                else "Активных VAC нет."
            )
            await interaction.response.send_message(
                view=success_view("Активные VAC", body),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            view=VacsView(interaction.guild.id, rows).build(),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VacCog(bot))
