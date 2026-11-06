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
from arbiter.services.br_service import BrError, br_service
from arbiter.services.match_service import MatchError, match_service
from arbiter.services.ops_service import OpsError, ops_service
from arbiter.services.org_service import OrgError, org_service
from arbiter.services.ocr_service import OcrError, normalize_results, ocr_service
from arbiter.services.room_service import RoomError, room_service
from arbiter.services.tournament_service import TournamentError, tournament_service
from storage.db import get_pool
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

    @arbiter_match.command(name="room", description="Создать/удалить комнату матча")
    @app_commands.describe(match_id="ID или код", action="Действие")
    @app_commands.choices(action=[
        app_commands.Choice(name="Создать", value="create"),
        app_commands.Choice(name="Удалить", value="delete"),
    ])
    async def match_room(self, interaction: discord.Interaction, match_id: str,
                         action: app_commands.Choice[str]) -> None:
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
            if action.value == "create":
                existing = await room_service.get_room(match.id)
                if existing and existing.get("category_id"):
                    raise RoomError("Комната уже существует.")
                res = await room_service.create_match_room(interaction.guild, match.id)
                embed = DesignEmbed(category="success", title="Комната создана",
                                    description=f"Категория: <#{res['category_id']}>")
                embed.add_kv("Team A text", f"<#{res['team_a_text']}>")
                embed.add_kv("Team A voice", f"<#{res['team_a_voice']}>")
                embed.add_kv("Team B text", f"<#{res['team_b_text']}>")
                embed.add_kv("Team B voice", f"<#{res['team_b_voice']}>")
                embed.add_kv("Main", f"<#{res['main_text']}> / <#{res['main_voice']}>")
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                settings = await org_service.get_settings(org.id)
                await room_service.archive_and_delete(
                    interaction.guild, match.id,
                    int(settings.match_log_channel_id) if settings and settings.match_log_channel_id else None)
                await interaction.followup.send(
                    embed=DesignEmbed(category="success", title="Комната удалена",
                                      description="История заархивирована в лог-канал."),
                    ephemeral=True)
        except (OrgError, MatchError, RoomError) as e:
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

    @arbiter_match.command(name="ocr", description="OCR скриншота результата для матча (драфт/Arbiter)")
    @app_commands.describe(match_id="Код матча (public_code с панели)",
                           image="Скриншот таблицы результатов")
    async def match_ocr(self, interaction: discord.Interaction,
                        match_id: str, image: discord.Attachment) -> None:
        await interaction.response.defer(ephemeral=True)
        if not ocr_service.enabled:
            await interaction.followup.send(
                embed=DesignEmbed(category="warning", title="OCR не настроен",
                                  description="Задайте `OCR_SERVICE_URL` (battles-recon)."),
                ephemeral=True)
            return
        try:
            match = await match_service.get_match_by_code(match_id)
            if not match:
                await interaction.followup.send(embed=_err_embed("Матч не найден."), ephemeral=True)
                return
            await interaction.followup.send("Распознаю скриншот…", ephemeral=True)
            raw = await ocr_service.process_attachment(image)
            players = normalize_results(raw)
            if not players:
                await interaction.followup.send(
                    embed=DesignEmbed(category="warning", title="OCR",
                                      description="Не удалось распознать игроков."),
                    ephemeral=True)
                return

            if not match.external_ref:
                # Обычный arbiter-матч — просто показать таблицу
                embed = DesignEmbed(category="info", title="OCR: результаты",
                                    description=f"Матч **{match.public_code}** • "
                                                f"игроков: **{len(players)}**")
                for p in players[:20]:
                    embed.add_kv(f"#{p['placement']} {p['name']}",
                                 f"K {p['kills']} • D {p['deaths']} • "
                                 f"A {p['assists']} • DMG {p['damage']}")
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # Legacy draft match — сопоставить и предложить применить
            from arbiter.services.legacy_bridge import _parse_ext_ref
            from arbiter.services.ocr_apply import (
                auto_index_map, match_ocr_players, roster_names)
            from arbiter.panels.ocr_confirm import OcrConfirmView
            from arbiter.panels.ocr_mapping import (
                OcrMappingView, create_pending, get_state, mapping_embed)
            from storage.json_store import store

            parsed = _parse_ext_ref(match.external_ref)
            if not parsed:
                await interaction.followup.send(embed=_err_embed("Битая связь с турниром."), ephemeral=True)
                return
            tournament_id, m_type, m_index, team_a, team_b = parsed
            tournament = next(
                (t for t in store.get_by_guild(interaction.guild_id)
                 if t and t.id == tournament_id), None)
            if not tournament:
                await interaction.followup.send(embed=_err_embed("Турнир не найден."), ephemeral=True)
                return

            stage = {"qualifier": "Квалифаер", "semifinal": "Полуфинал",
                     "final": "Финал"}.get(m_type, m_type)
            match_label = f"{stage} #{m_index + 1}"
            roster = roster_names(tournament, team_a, team_b)
            auto_map = auto_index_map(tournament, team_a, team_b, players)

            if len(auto_map) < len(players):
                # Есть нераспознанные — показываем UI привязки ников
                token = create_pending(
                    interaction.guild_id, tournament_id, m_type, m_index,
                    team_a, team_b, match_label, players, roster, auto_map)
                state = get_state(token)
                await interaction.followup.send(
                    embed=mapping_embed(state),
                    view=OcrMappingView(token), ephemeral=True)
                return

            matched, unmatched = match_ocr_players(tournament, team_a, team_b, players)
            embed = DesignEmbed(
                category="info", title=f"OCR → {match_label}",
                description=(f"Матч **{match.public_code}** • "
                             f"сопоставлено: **{len(matched)}**"))
            for name, kd in list(matched.items())[:20]:
                embed.add_kv(name,
                             f"K {kd['kills']} • D {kd['deaths']} • "
                             f"A {kd['assists']} • DMG {kd['damage']}"
                             + (f"  _(OCR: {kd['ocr_name']})_" if kd["ocr_name"] != name else ""))
            embed.set_footer(text="В temp_match_stats запишутся K/D/A/DMG.")

            if not matched:
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            view = OcrConfirmView(interaction.guild_id, tournament_id, m_type,
                                  m_index, matched, unmatched, match_label,
                                  cid_suffix=f"_{match.public_code}")
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except OcrError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)
        except Exception as e:
            logger.exception("match ocr failed")
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
            elif t.format == TournamentFormat.DOUBLE_ELIMINATION:
                await bracket_service.generate_double_elimination(t.id, entries, best_of=t.best_of)
            else:
                raise BracketError("Неизвестный формат сетки")
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


    # ---------- BR lobbies ----------

    arbiter_br = app_commands.Group(name="br", description="Battle Royale лобби",
                                    parent=arbiter)

    async def _require_org_staff(self, interaction: discord.Interaction):
        org = await org_service.get_org_by_guild(interaction.guild_id)
        if not org:
            raise OrgError("Организация не настроена. /arb setup")
        profile = await org_service.get_or_create_profile(interaction.user)
        member = await org_service.get_member(org.id, profile.id)
        if not member or member.role.value not in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF", "REFEREE"):
            raise OrgError("Только судьи/админы.")
        return org, profile

    @arbiter_br.command(name="create", description="Создать BR-лобби")
    @app_commands.describe(name="Название", game="Игра", games="Игр запланировано",
                           kill_points="Очки за килл")
    async def br_create(self, interaction: discord.Interaction, name: str, game: str,
                        games: int = 6, kill_points: int = 1) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org, profile = await self._require_org_staff(interaction)
            lobby = await br_service.create_lobby(
                org.id, name, game, games_planned=games, kill_points=kill_points,
                channel_id=str(interaction.channel_id), created_by_id=profile.id)
            embed = DesignEmbed(category="success", title="BR-лобби создано",
                                description=f"**{lobby.name}** • {lobby.game}")
            embed.add_kv("Код", f"`{lobby.public_code}`")
            embed.add_kv("Игр", str(games))
            embed.add_kv("Kill pts", str(kill_points))
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, BrError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_br.command(name="add-team", description="Добавить команду в лобби")
    @app_commands.describe(lobby_id="ID или код лобби", team="Название команды")
    async def br_add_team(self, interaction: discord.Interaction,
                          lobby_id: str, team: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org, _ = await self._require_org_staff(interaction)
            lobby = await br_service.get_lobby(lobby_id)
            if not lobby or lobby.organization_id != org.id:
                raise BrError("Лобби не найдено.")
            t = await br_service.add_team(lobby.id, team)
            await interaction.followup.send(
                embed=DesignEmbed(category="success", title="Команда добавлена",
                                  description=f"**{t.name}** • seed {t.seed}"),
                ephemeral=True)
        except (OrgError, BrError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_br.command(name="log", description="Записать результат игры (placement + kills)")
    @app_commands.describe(lobby_id="ID или код", team="Команда", game="Номер игры",
                           placement="Место", kills="Киллы")
    async def br_log(self, interaction: discord.Interaction, lobby_id: str, team: str,
                     game: int, placement: int, kills: int) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org, profile = await self._require_org_staff(interaction)
            lobby = await br_service.get_lobby(lobby_id)
            if not lobby or lobby.organization_id != org.id:
                raise BrError("Лобби не найдено.")
            res = await br_service.log_game(lobby, team, game, placement, kills,
                                            actor_id=profile.id)
            await interaction.followup.send(
                embed=DesignEmbed(category="success", title="Игра записана",
                                  description=f"**{team}** • игра {game} • "
                                              f"место {placement} • {kills} kills → **{res.points} pts**"),
                ephemeral=True)
        except (OrgError, BrError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_br.command(name="adjust", description="Изменить очки/киллы команды")
    @app_commands.describe(lobby_id="ID или код", team="Команда", points="±очки",
                           kills="±киллы", reason="Причина")
    async def br_adjust(self, interaction: discord.Interaction, lobby_id: str, team: str,
                        points: int, kills: int = 0, reason: str = "") -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org, profile = await self._require_org_staff(interaction)
            lobby = await br_service.get_lobby(lobby_id)
            if not lobby or lobby.organization_id != org.id:
                raise BrError("Лобби не найдено.")
            await br_service.add_adjustment(lobby.id, team, points, kills=kills,
                                            reason=reason, actor_id=profile.id)
            await interaction.followup.send(
                embed=DesignEmbed(category="warning", title="Корректировка",
                                  description=f"**{team}**: {points:+d} pts, {kills:+d} kills"),
                ephemeral=True)
        except (OrgError, BrError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_br.command(name="standings", description="Таблица лобби")
    @app_commands.describe(lobby_id="ID или код")
    async def br_standings(self, interaction: discord.Interaction, lobby_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена.")
            lobby = await br_service.get_lobby(lobby_id)
            if not lobby or lobby.organization_id != org.id:
                raise BrError("Лобби не найдено.")
            table = await br_service.standings(lobby.id)
            embed = DesignEmbed(category="info", title=f"Standings — {lobby.name}",
                                description=f"{lobby.game} • {lobby.public_code}")
            for i, r in enumerate(table[:20], 1):
                avg = f"{r['avg_placement']:.1f}" if r["avg_placement"] else "—"
                embed.add_kv(f"{i}. {r['name']}",
                             f"**{r['total_points']} pts** • {r['games_played']} игр • "
                             f"{r['kills']} kills • avg #{avg}")
            if not table:
                embed.description = "Нет команд."
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, BrError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter_br.command(name="close", description="Завершить лобби")
    @app_commands.describe(lobby_id="ID или код")
    async def br_close(self, interaction: discord.Interaction, lobby_id: str) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org, profile = await self._require_org_staff(interaction)
            lobby = await br_service.get_lobby(lobby_id)
            if not lobby or lobby.organization_id != org.id:
                raise BrError("Лобби не найдено.")
            await br_service.finalize(lobby, profile.id)
            table = await br_service.standings(lobby.id)
            embed = DesignEmbed(category="success", title="Лобби завершено",
                                description=f"**{lobby.name}**")
            for i, r in enumerate(table[:5], 1):
                embed.add_kv(f"{i}. {r['name']}", f"**{r['total_points']} pts**")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, BrError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)


    # ---------- player / standalone ----------

    @arbiter.command(name="profile", description="Профиль Arbiter (участник/судья)")
    @app_commands.describe(user="Пользователь (по умолчанию — вы)")
    async def arb_profile(self, interaction: discord.Interaction,
                          user: discord.User | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            target = user or interaction.user
            profile = await org_service.get_or_create_profile(target)
            org = await org_service.get_org_by_guild(interaction.guild_id)
            role = None
            if org:
                m = await org_service.get_member(org.id, profile.id)
                role = m.role.value if m else None
            embed = DesignEmbed(category="profile", title="Профиль",
                                author=target, description=profile.bio or "")
            embed.add_kv("Discord", f"<@{profile.discord_user_id}>")
            if profile.display_name:
                embed.add_kv("Имя", profile.display_name)
            if profile.country_code:
                embed.add_kv("Страна", profile.country_code)
            if role:
                embed.add_kv("Роль в org", role)
            embed.add_kv("Open to work", "Да" if profile.open_to_work else "Нет")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            logger.exception("arb profile failed")
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter.command(name="log", description="Записать событие без матча (standalone log)")
    @app_commands.describe(kind="Тип", event="Событие/турнир", teams="Команды",
                           subject="Субъект", summary="Описание", result="Результат")
    @app_commands.choices(kind=[
        app_commands.Choice(name="Заметка", value="note"),
        app_commands.Choice(name="Счёт", value="score"),
        app_commands.Choice(name="Доказательство", value="evidence"),
        app_commands.Choice(name="Предупреждение", value="warning"),
        app_commands.Choice(name="Список", value="list"),
    ])
    async def arb_log(self, interaction: discord.Interaction,
                      kind: app_commands.Choice[str], event: str | None = None,
                      teams: str | None = None, subject: str | None = None,
                      summary: str | None = None, result: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            profile = await org_service.get_or_create_profile(interaction.user)
            pool = await get_pool()
            from arbiter.models import new_id, utcnow, dumps
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO arb_standalone_logs (id, user_profile_id, kind, event, "
                    "teams, subject, summary, details, result, created_at) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
                    new_id(), profile.id, kind.value, event, teams, subject,
                    summary, None, result, utcnow())
            await interaction.followup.send(
                embed=DesignEmbed(category="success", title="Записано",
                                  description=summary or event or kind.value),
                ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter.command(name="logs", description="Ваши standalone-логи")
    async def arb_logs(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            profile = await org_service.get_or_create_profile(interaction.user)
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM arb_standalone_logs WHERE user_profile_id=$1 "
                    "ORDER BY created_at DESC LIMIT 15", profile.id)
            embed = DesignEmbed(category="info", title="Мои логи")
            for r in rows:
                ts = r["created_at"].strftime("%d.%m %H:%M")
                embed.add_kv(f"{ts} • {r['kind']}",
                             (r["summary"] or r["event"] or "—")[:100])
            if not rows:
                embed.description = "Логов нет."
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    # ---------- rules / shift / announce ----------

    @arbiter.command(name="rule", description="Rulebook организации")
    @app_commands.describe(action="Действие", key="Ключ правила", title="Заголовок",
                           body="Текст правила", category="Категория")
    @app_commands.choices(action=[
        app_commands.Choice(name="Добавить/Обновить", value="set"),
        app_commands.Choice(name="Список", value="list"),
        app_commands.Choice(name="Показать", value="get"),
    ])
    async def arb_rule(self, interaction: discord.Interaction,
                       action: app_commands.Choice[str], key: str | None = None,
                       title: str | None = None, body: str | None = None,
                       category: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена.")
            if action.value == "set":
                profile = await org_service.get_or_create_profile(interaction.user)
                member = await org_service.get_member(org.id, profile.id)
                if not member or member.role.value not in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF"):
                    raise OrgError("Только организаторы.")
                if not key or not title or body is None:
                    raise OrgError("Нужны key, title, body.")
                await ops_service.upsert_rule(org.id, key, title, body,
                                              category=category, actor_id=profile.id)
                await interaction.followup.send(
                    embed=DesignEmbed(category="success", title="Правило сохранено",
                                      description=f"**{title}** (`{key}`)"), ephemeral=True)
            elif action.value == "list":
                rules = await ops_service.list_rules(org.id)
                embed = DesignEmbed(category="info", title="Rulebook", description=f"**{org.name}**")
                for r in rules[:20]:
                    embed.add_kv(f"{r['key']} • {r['title']}",
                                 (r["body"] or "")[:120])
                if not rules:
                    embed.description = "Правил нет."
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                if not key:
                    raise OrgError("Укажите key.")
                r = await ops_service.get_rule(org.id, key)
                if not r:
                    raise OrgError("Правило не найдено.")
                embed = DesignEmbed(category="info", title=r["title"],
                                    description=r["body"])
                embed.set_footer(text=f"key: {r['key']}")
                await interaction.followup.send(embed=embed, ephemeral=True)
        except (OrgError, OpsError) as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter.command(name="shift", description="Дежурство судьи (on/off shift)")
    @app_commands.describe(on_shift="Задействовать")
    async def arb_shift(self, interaction: discord.Interaction, on_shift: bool) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org = await org_service.get_org_by_guild(interaction.guild_id)
            if not org:
                raise OrgError("Организация не настроена.")
            profile = await org_service.get_or_create_profile(interaction.user)
            await ops_service.set_shift(org.id, interaction.user.id, on_shift)
            await interaction.followup.send(
                embed=DesignEmbed(category="success" if on_shift else "neutral",
                                  title="Дежурство",
                                  description="Вы на смене." if on_shift else "Смена снята."),
                ephemeral=True)
        except OrgError as e:
            await interaction.followup.send(embed=_err_embed(str(e)), ephemeral=True)

    @arbiter.command(name="announce", description="Объявление для организации")
    @app_commands.describe(title="Заголовок", body="Текст",
                           target="Аудитория")
    @app_commands.choices(target=[
        app_commands.Choice(name="Все", value="everyone"),
        app_commands.Choice(name="Судьи", value="staff"),
        app_commands.Choice(name="Игроки", value="players"),
    ])
    async def arb_announce(self, interaction: discord.Interaction, title: str, body: str,
                           target: app_commands.Choice[str] | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._ensure_schema()
        try:
            org, profile = await self._require_org_staff(interaction)
            tk = target.value if target else "everyone"
            await ops_service.create_announcement(org.id, title, body,
                                                  target_kind=tk, actor_id=profile.id)
            # отправляем в текущий канал
            embed = DesignEmbed(category="info", title=title, description=body)
            await interaction.channel.send(embed=embed)
            await interaction.followup.send("Объявление отправлено.", ephemeral=True)
        except (OrgError, OpsError) as e:
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
