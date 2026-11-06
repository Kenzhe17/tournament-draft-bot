"""Cog: Arbiter — esports referee & tournament ops (порт Arbiter)."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from arbiter.db import init_arbiter_schema
from arbiter.enums import OrgRole, TournamentFormat
from arbiter.panels.match_panel import MatchPanelView, build_match_embed
from arbiter.services.bracket_service import BracketError, bracket_service
from arbiter.services.match_service import MatchError, match_service
from arbiter.services.ops_service import OpsError, ops_service
from arbiter.services.org_service import OrgError, org_service
from arbiter.services.ocr_service import OcrError, normalize_results, ocr_service
from arbiter.services.tournament_service import TournamentError, tournament_service
from utils.design.embeds import DesignEmbed

logger = logging.getLogger(__name__)

_ROLE_CHOICES = [
    app_commands.Choice(name="Admin", value="ADMIN"),
    app_commands.Choice(name="Manager", value="MANAGER"),
    app_commands.Choice(name="Head Ref", value="HEAD_REF"),
    app_commands.Choice(name="Referee", value="REFEREE"),
    app_commands.Choice(name="Player", value="PLAYER"),
]


def _err_embed(message: str) -> DesignEmbed:
    return DesignEmbed(category="error", title="Ошибка", description=message)


class ArbiterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self) -> None:
        ""6 Persistent views для активных матчей."""
        try:
            await init_arbiter_schema()
            orgs = await _list_orgs()
            for org in orgs:
                for m in await match_service.list_active_matches(org.id):
                    self.bot.add_view(MatchPanelView(m.id))
        except Exception as e:
            logger.error("Failed to register arbiter views: %s", e)

    async def _ensure_schema(self) -> None:
        try:
            await init_arbiter_schema()
        except Exception as e:
            logger.error("Arbiter schema init failed: %s", e)

    arbiter = app_commands.Group(name="arb", description="Arbiter — организация, матчи и турниры")

    # ---------- org setup ----------

    @arbiter.command(name="setup", description="Создать/инициализировать организацию на сервере")
    async def setup(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        if not interaction.guild:
            await interaction.followup.send(embed=_err_embed("Команда только на сервере."), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send(embed=_err_embed("Требуется участник сервера."), ephemeral=True)
            return
        try:
            org = await org_service.get_or_create_org(interaction.guild, interaction.user)
            member = await org_service.get_member_role_for_user(org.id, interaction.user.id)
            is_owner = member == OrgRole.OWNER
            embed = DesignEmbed(category="success", title="Организация готова",
                                description=f"**{org.name}** инициализирована.")
            embed.add_kv("Ваша роль", member.value if member else "—")
            if is_owner:
                embed.add_kv("Далее", "`/arb member add` — добавить судей и админов")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("arb setup failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    # ---------- members ----------

    @arbiter.command(name="member", description="Управление членами организации")
    @app_commands.describe(action="Действие", user="Пользователь", role="Роль")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="Добавить", value="add"),
            app_commands.Choice(name="Удалить", value="remove"),
            app_commands.Choice(name="Список", value="list"),
        ]
    )
    async def member(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        user: discord.User | None = None,
        role: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена. Используйте /arb setup.")

            if action.value == "list":
                members = await org_service.list_members(org.id)
                embed = DesignEmbed(category="info", title="Члены организации",
                                    description=f"**{org.name}**")
                for m, p in members[:20]:
                    embed.add_kv(f"{p.display_name or p.discord_user_id}", m.role.value)
                if not members:
                    embed.description = "Нет членов."
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # add / remove требуют ADMIN+
            profile = await org_service.get_or_create_profile(interaction.user)
            actor = await org_service.get_member(org.id, profile.id)
            if not actor or actor.role not in (OrgRole.OWNER, OrgRole.ADMIN):
                raise OrgError("Только Owner/Admin могут управлять членами.")

            if not user:
                raise OrgError("Укажите пользователя.")

            if action.value == "add":
                if not role:
                    raise OrgError("Укажите роль.")
                await org_service.add_member(org.id, user.id, OrgRole(role.value),
                                             actor_profile_id=profile.id)
                await interaction.followup.send(
                    embed=DesignEmbed(category="success", title="Участник добавлен",
                                      description=f"{user.mention} → **{role.value}**"),
                    ephemeral=True,
                )
            else:
                ok = await org_service.remove_member(org.id, user.id,
                                                     actor_profile_id=profile.id)
                if not ok:
                    raise OrgError("Пользователь не найден в организации.")
                await interaction.followup.send(
                    embed=DesignEmbed(category="success", title="Участник удалён",
                                      description=f"{user.mention} удалён из организации."),
                    ephemeral=True,
                )
        except OrgError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("arb member failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    # ---------- settings ----------

    @arbiter.command(name="settings", description="Настройки организации (роли, каналы)")
    @app_commands.describe(
        admin_role="Роль админов",
        referee_role="Роль судей",
        match_category="Категория для комнат матчей",
        match_log_channel="Канал логов матчей",
        evidence_channel="Канал доказательств",
    )
    async def settings(
        self,
        interaction: discord.Interaction,
        admin_role: discord.Role | None = None,
        referee_role: discord.Role | None = None,
        match_category: discord.CategoryChannel | None = None,
        match_log_channel: discord.TextChannel | None = None,
        evidence_channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена. Используйте /arb setup.")
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id)
            if not member or member.role not in (OrgRole.OWNER, OrgRole.ADMIN):
                raise OrgError("Только Owner/Admin могут менять настройки.")

            fields = {}
            if admin_role is not None:
                fields["admin_role_id"] = str(admin_role.id)
            if referee_role is not None:
                fields["referee_role_id"] = str(referee_role.id)
            if match_category is not None:
                fields["match_category_id"] = str(match_category.id)
            if match_log_channel is not None:
                fields["match_log_channel_id"] = str(match_log_channel.id)
            if evidence_channel is not None:
                fields["evidence_channel_id"] = str(evidence_channel.id)

            s = await org_service.update_settings(org.id, **fields)
            embed = DesignEmbed(category="success", title="Настройки обновлены",
                                description=f"**{org.name}**")
            embed.add_kv("Admin Role", f"<@&{s.admin_role_id}>" if s.admin_role_id else "—")
            embed.add_kv("Referee Role", f"<@&{s.referee_role_id}>" if s.referee_role_id else "—")
            embed.add_kv("Match Category", f"<#{s.match_category_id}>" if s.match_category_id else "—")
            embed.add_kv("Log Channel", f"<#{s.match_log_channel_id}>" if s.match_log_channel_id else "—")
            embed.add_kv("Evidence Channel", f"<#{s.evidence_channel_id}>" if s.evidence_channel_id else "—")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except OrgError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("arb settings failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    # ---------- matches ----------

    arbiter_match = app_commands.Group(name="match", description="Управление матчами",
                                       parent=arbiter)

    @arbiter_match.command(name="create", description="Создать матч и отправить панель")
    @app_commands.describe(
        team_a="Название команды A",
        team_b="Название команды B",
        best_of="Формат BO (1-99)",
        channel="Канал для панели (по умолчанию — текущий)",
    )
    async def match_create(
        self,
        interaction: discord.Interaction,
        team_a: str,
        team_b: str,
        best_of: int = 1,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена. Используйте /arb setup.")
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id)
            if not member or member.role.value not in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF", "REFEREE"):
                raise OrgError("Только судьи/админы могут создавать матчи.")

            target = channel or interaction.channel
            match = await match_service.create_match(
                org.id, team_a, team_b, best_of=best_of,
                channel_id=str(target.id), created_by_id=profile.id,
            )
            view = MatchPanelView(match.id)
            msg = await target.send(embed=build_match_embed(match), view=view)
            self.bot.add_view(view)
            await match_service.set_control_message(match, str(target.id), str(msg.id))
            await interaction.followup.send(
                embed=DesignEmbed(category="success", title="Матч создан",
                                  description=f"**{team_a}** vs **{team_b}** • BO{best_of}\n"
                                              f"Код: `{match.public_code}`"),
                ephemeral=True,
            )
        except (OrgError, MatchError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("arb match create failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_match.command(name="list", description="Список матчей организации")
    async def match_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена.")
            matches = await match_service.list_matches(org.id, limit=15)
            embed = DesignEmbed(category="info", title="Матчи", description=f"**{org.name}**")
            if not matches:
                embed.description = "Нет матчей."
            for m in matches:
                embed.add_kv(
                    f"{m.public_code} • {m.status.value}",
                    f"**{m.team_a_name}** {m.team_a_score}—{m.team_b_score} **{m.team_b_name}** • BO{m.best_of}",
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except OrgError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_match.command(name="panel", description="Обновить/отправить панель существующего матча")
    @app_commands.describe(match_id="ID или публичный код матча")
    async def match_panel(self, interaction: discord.Interaction, match_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            match = await match_service.get_match_by_code(match_id)
            if not match:
                raise MatchError("Матч не найден.")
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org or match.organization_id != org.id:
                raise OrgError("Матч не принадлежит этой организации.")
            target = interaction.channel
            view = MatchPanelView(match.id)
            msg = await target.send(embed=build_match_embed(match), view=view)
            self.bot.add_view(view)
            await match_service.set_control_message(match, str(target.id), str(msg.id))
            await interaction.followup.send("Панель отправлена.", ephemeral=True)
        except (OrgError, MatchError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_match.command(name="checkin", description="Отметить участие (check-in)")
    @app_commands.describe(match_id="ID или код матча", game_account="Игровой аккаунт")
    async def match_checkin(self, interaction: discord.Interaction, match_id: str,
                            game_account: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            match = await match_service.get_match_by_code(match_id)
            if not match:
                raise MatchError("Матч не найден.")
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org or match.organization_id != org.id:
                raise OrgError("Матч не принадлежит этой организации.")
            await ops_service.checkin(org.id, match.id, interaction.user.id,
                                      game_account=game_account)
            await interaction.followup.send(
                embed=DesignEmbed(category="success", title="Check-in",
                                  description="Вы отмечены участником матча."),
                ephemeral=True)
        except (OrgError, MatchError, OpsError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_match.command(name="evidence", description="Прикрепить доказательство к матчу")
    @app_commands.describe(match_id="ID или код матча", url="Ссылка на доказательство",
                           note="Комментарий")
    async def match_evidence(self, interaction: discord.Interaction, match_id: str,
                             url: str, note: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            match = await match_service.get_match_by_code(match_id)
            if not match:
                raise MatchError("Матч не найден.")
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org or match.organization_id != org.id:
                raise OrgError("Матч не принадлежит этой организации.")
            profile = await org_service.get_or_create_profile(interaction.user)
            await ops_service.add_evidence(org.id, match.id, url, note=note,
                                           submitted_by_id=profile.id)
            await interaction.followup.send(
                embed=DesignEmbed(category="success", title="Доказательство добавлено",
                                  description=url[:200]), ephemeral=True)
        except (OrgError, MatchError, OpsError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_match.command(name="warn", description="Выдать предупреждение в матче")
    @app_commands.describe(match_id="ID или код матча", player="Игрок/команда",
                           rule="Правило", note="Комментарий")
    async def match_warn(self, interaction: discord.Interaction, match_id: str,
                         player: str, rule: str, note: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            match = await match_service.get_match_by_code(match_id)
            if not match:
                raise MatchError("Матч не найден.")
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org or match.organization_id != org.id:
                raise OrgError("Матч не принадлежит этой организации.")
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id)
            if not member or member.role.value not in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF", "REFEREE"):
                raise OrgError("Только судьи/админы.")
            await ops_service.add_warning(org.id, match.id, player, rule,
                                          note=note, actor_id=profile.id)
            await interaction.followup.send(
                embed=DesignEmbed(category="warning", title="Предупреждение",
                                  description=f"**{player}** — {rule}"), ephemeral=True)
        except (OrgError, MatchError, OpsError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_match.command(name="pause", description="Записать паузу в матче")
    @app_commands.describe(match_id="ID или код матча", minutes="Минуты",
                           reason="Причина")
    async def match_pause(self, interaction: discord.Interaction, match_id: str,
                          minutes: int, reason: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            match = await match_service.get_match_by_code(match_id)
            if not match:
                raise MatchError("Матч не найден.")
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org or match.organization_id != org.id:
                raise OrgError("Матч не принадлежит этой организации.")
            profile = await org_service.get_or_create_profile(interaction.user)
            await ops_service.add_pause(org.id, match.id, minutes, reason,
                                        actor_id=profile.id)
            await interaction.followup.send(
                embed=DesignEmbed(category="info", title="Пауза записана",
                                  description=f"{minutes} мин — {reason}"), ephemeral=True)
        except (OrgError, MatchError, OpsError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_match.command(name="timeline", description="Хронология событий матча")
    @app_commands.describe(match_id="ID или код матча")
    async def match_timeline(self, interaction: discord.Interaction, match_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            match = await match_service.get_match_by_code(match_id)
            if not match:
                raise MatchError("Матч не найден.")
            events = await ops_service.match_timeline(match.id, limit=20)
            embed = DesignEmbed(category="info", title=f"Таймлайн {match.public_code}",
                                description=f"**{match.team_a_name}** vs **{match.team_b_name}**")
            for ev in events[:15]:
                actor = ev.get("actor_discord_id") or "system"
                ts = ev["created_at"].strftime("%H:%M") if ev.get("created_at") else ""
                embed.add_kv(f"{ts} {ev['action']}", f"<@{actor}>" if actor != "system" else actor)
            if not events:
                embed.description = "Событий нет."
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, MatchError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    # ---------- OCR ----------

    @arbiter.command(name="ocr", description="Распознать скриншот результата Free Fire (OCR)")
    @app_commands.describe(image="Скриншот таблицы результатов")
    async def ocr_parse(self, interaction: discord.Interaction,
                        image: discord.Attachment) -> None:
        await interaction.response.defer(ephemeral=True)
        if not ocr_service.enabled:
            await interaction.followup.send(
                embed=DesignEmbed(category="warning", title="OCR не настроен",
                                  description="Задайте `OCR_SERVICE_URL` (battles-recon)."),
                ephemeral=True)
            return
        try:
            await interaction.followup.send("Распознаю скриншот…", ephemeral=True)
            raw = await ocr_service.process_attachment(image)
            players = normalize_results(raw)
            if not players:
                await interaction.followup.send(
                    embed=DesignEmbed(category="warning", title="OCR",
                                      description="Не удалось распознать игроков. Попробуйте другой скриншот."),
                    ephemeral=True)
                return
            embed = DesignEmbed(category="info", title="OCR: результаты",
                                description=f"Распознано игроков: **{len(players)}**")
            for p in players[:20]:
                embed.add_kv(
                    f"#{p['placement']} {p['name']}",
                    f"Kills {p['kills']} • Deaths {p['deaths']} • "
                    f"Assists {p['assists']} • DMG {p['damage']}",
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except OcrError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("ocr failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)


    # ---------- tournaments ----------

    arbiter_tournament = app_commands.Group(name="tournament", description="Турниры и сетки",
                                            parent=arbiter)

    @arbiter_tournament.command(name="create", description="Создать турнир")
    @app_commands.describe(name="Название", game="Игра", best_of="Формат BO",
                           format_="Формат сетки")
    @app_commands.choices(format_=[
        app_commands.Choice(name="Single Elimination", value="single_elimination"),
        app_commands.Choice(name="Double Elimination", value="double_elimination"),
        app_commands.Choice(name="Round Robin", value="round_robin"),
    ])
    async def tournament_create(
        self, interaction: discord.Interaction, name: str,
        game: str | None = None, best_of: int = 1,
        format_: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена. /arb setup")
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id)
            if not member or member.role.value not in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF"):
                raise OrgError("Только организаторы.")
            fmt = TournamentFormat(format_.value) if format_ else TournamentFormat.SINGLE_ELIMINATION
            t = await tournament_service.create_tournament(
                org.id, name, game_title=game, format=fmt, best_of=best_of,
                created_by_id=profile.id)
            embed = DesignEmbed(category="success", title="Турнир создан",
                                description=f"**{t.name}**")
            embed.add_kv("Код", f"`{t.public_code}`")
            embed.add_kv("Формат", fmt.value)
            embed.add_kv("BO", str(best_of))
            embed.add_kv("Статус", t.status.value)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, TournamentError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_tournament.command(name="add", description="Добавить команду в турнир")
    @app_commands.describe(tournament_id="ID или код", team="Название команды")
    async def tournament_add(self, interaction: discord.Interaction,
                             tournament_id: str, team: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            t = await tournament_service.get_tournament(tournament_id)
            if not t:
                raise TournamentError("Турнир не найден.")
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org or t.organization_id != org.id:
                raise OrgError("Турнир не принадлежит этой организации.")
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id)
            if not member or member.role.value not in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF"):
                raise OrgError("Только организаторы.")
            entry = await bracket_service.add_entry(t.id, team)
            await interaction.followup.send(
                embed=DesignEmbed(category="success", title="Команда добавлена",
                                  description=f"**{team}** → seed {entry.seed}"),
                ephemeral=True)
        except (OrgError, TournamentError, BracketError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_tournament.command(name="bracket", description="Сгенерировать сетку и активировать турнир")
    @app_commands.describe(tournament_id="ID или код", channel="Канал для матчей")
    async def tournament_bracket(self, interaction: discord.Interaction,
                                 tournament_id: str,
                                 channel: discord.TextChannel | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            t = await tournament_service.get_tournament(tournament_id)
            if not t:
                raise TournamentError("Турнир не найден.")
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org or t.organization_id != org.id:
                raise OrgError("Турнир не принадлежит этой организации.")
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id)
            if not member or member.role.value not in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF"):
                raise OrgError("Только организаторы.")
            entries = await bracket_service.list_entries(t.id)
            if len(entries) < 2:
                raise BracketError("Нужно минимум 2 команды.")
            # regenerate: delete old nodes
            from storage.db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM arb_bracket_nodes WHERE tournament_id=$1", t.id)
            if t.format == TournamentFormat.SINGLE_ELIMINATION:
                await bracket_service.generate_single_elimination(
                    t.id, entries, best_of=t.best_of, third_place=t.third_place)
            elif t.format == TournamentFormat.ROUND_ROBIN:
                await bracket_service.generate_round_robin(t.id, entries, best_of=t.best_of)
            else:
                raise BracketError("double_elimination пока не поддерживается")
            t = await tournament_service.activate(t)
            matches = await tournament_service.ensure_node_matches(
                t, org.id, str(channel.id) if channel else None, profile.id)
            embed = DesignEmbed(category="success", title="Сетка создана",
                                description=f"**{t.name}** • {len(entries)} команд • {len(matches)} матчей")
            for m in matches[:10]:
                embed.add_kv(m.public_code, f"**{m.team_a_name}** vs **{m.team_b_name}**")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, TournamentError, BracketError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_tournament.command(name="standings", description="Таблица турнира (round robin)")
    @app_commands.describe(tournament_id="ID или код")
    async def tournament_standings(self, interaction: discord.Interaction,
                                   tournament_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            t = await tournament_service.get_tournament(tournament_id)
            if not t:
                raise TournamentError("Турнир не найден.")
            rows = await bracket_service.standings_round_robin(t.id)
            embed = DesignEmbed(category="info", title=f"Standings — {t.name}")
            for i, r in enumerate(rows[:20], 1):
                embed.add_kv(f"{i}. {r['name']}",
                             f"W {r['wins']} / L {r['losses']} • maps {r['maps_w']}:{r['maps_l']}")
            if not rows:
                embed.description = "Нет завершённых матчей."
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, TournamentError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_tournament.command(name="list", description="Список турниров")
    async def tournament_list(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена.")
            ts = await tournament_service.list_tournaments(org.id)
            embed = DesignEmbed(category="info", title="Турниры", description=f"**{org.name}**")
            for t in ts[:15]:
                champ = f" • 🏆 {t.champion_name}" if t.champion_name else ""
                embed.add_kv(f"{t.public_code} • {t.status.value}{champ}",
                             f"**{t.name}** • {t.format.value} • BO{t.best_of}")
            if not ts:
                embed.description = "Нет турниров."
            await interaction.followup.send(embed=embed, ephemeral=True)
        except OrgError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)


async def _list_orgs():
    from storage.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, discord_guild_id, name FROM arb_organizations")
    return [type("O", (), {"id": r["id"], "discord_guild_id": r["discord_guild_id"],
                           "name": r["name"]})() for r in rows]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ArbiterCog(bot))
