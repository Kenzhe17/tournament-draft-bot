"""MatchPanel — Discord-панель управления матчем (порт ui/match-panel.js).

Панель = embed с состоянием + View с кнопками. Состояние всегда в БД,
поэтому панели переживают рестарты бота (persistent views).

custom_id кодируют match_id: arb:m:{match_id}:{action}
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import ui

from arbiter.enums import MatchStatus, VetoKind
from arbiter.models import Match, MapResult, new_id, utcnow
from arbiter.services.match_service import MatchError, match_service
from arbiter.services.org_service import OrgError, org_service
from storage.db import get_pool
from utils.design.components import ButtonRole, design_button
from utils.design.embeds import DesignEmbed
from utils.design.emojis import emoji

logger = logging.getLogger(__name__)

_STATUS_LABEL = {
    MatchStatus.PENDING: "Ожидание",
    MatchStatus.VETO: "Вето/Баны",
    MatchStatus.LIVE: "Идёт матч",
    MatchStatus.DISPUTED: "Диспут",
    MatchStatus.COMPLETE: "Завершён",
    MatchStatus.CANCELLED: "Отменён",
}

_STATUS_CATEGORY = {
    MatchStatus.PENDING: "neutral",
    MatchStatus.VETO: "warning",
    MatchStatus.LIVE: "info",
    MatchStatus.DISPUTED: "warning",
    MatchStatus.COMPLETE: "success",
    MatchStatus.CANCELLED: "error",
}

# Кнопки: (action, label, emoji_key, role)
_ACTIONS = [
    ("veto", "Вето", "sword", ButtonRole.SECONDARY),
    ("start", "Старт", "check", ButtonRole.SUCCESS),
    ("score", "Счёт", "stats", ButtonRole.PRIMARY),
    ("pause", "Пауза", "timer", ButtonRole.SECONDARY),
    ("warn", "Варн", "warning", ButtonRole.SECONDARY),
    ("claim", "Судья", "profile", ButtonRole.SECONDARY),
    ("close", "Завершить", "check", ButtonRole.DANGER),
    ("cancel", "Отмена", "cross", ButtonRole.DANGER),
    ("refresh", "Обновить", "refresh", ButtonRole.SECONDARY),
]


def _cid(match_id: str, action: str) -> str:
    return f"arb:m:{match_id}:{action}"


def parse_panel_custom_id(custom_id: str) -> tuple[str, str] | None:
    """arb:m:{match_id}:{action} -> (match_id, action)."""
    parts = custom_id.split(":")
    if len(parts) == 4 and parts[0] == "arb" and parts[1] == "m":
        return parts[2], parts[3]
    return None


def build_match_embed(
    match: Match,
    map_results: list[MapResult] | None = None,
    vetoes: list | None = None,
    refs: list | None = None,
) -> DesignEmbed:
    """Единый embed состояния матча."""
    embed = DesignEmbed(
        category=_STATUS_CATEGORY.get(match.status, "neutral"),
        title=f"Матч {match.public_code} — BO{match.best_of}",
        description=f"**{match.team_a_name}** vs **{match.team_b_name}**",
    )
    embed.add_kv("Статус", _STATUS_LABEL.get(match.status, match.status.value))
    embed.add_kv("Счёт", f"**{match.team_a_score}** — **{match.team_b_score}**")
    if match.final_map:
        embed.add_kv("Финальная карта", match.final_map)
    if match.dispute_reason:
        embed.add_kv("Диспут", match.dispute_reason[:200])

    if map_results:
        lines = [
            f"Карта {mr.map_index + 1} ({mr.map_name}): "
            f"**{mr.team_a_score}**—**{mr.team_b_score}**"
            for mr in map_results
        ]
        embed.add_section("Карты", "\n".join(lines) if lines else "—")

    if vetoes:
        lines = [f"{v.kind.value}: {v.team_slot} → {v.map_name}" for v in vetoes]
        embed.add_section("Veto/Баны", "\n".join(lines))

    if refs:
        names = [f"<@{a.discord_user_id}>" if a.discord_user_id else "—" for a in refs]
        embed.add_kv("Судьи", ", ".join(names) if names else "—")

    embed.set_footer(text=f"ID: {match.id}")
    return embed


async def render_match_panel(channel: discord.abc.Messageable, match: Match,
                             *, view: ui.View | None = None) -> discord.Message:
    """Отправить/обновить панель матча в канале."""
    results = await match_service.list_map_results(match.id)
    vetoes = await match_service.list_veto_actions(match.id)
    refs = await match_service.list_active_refs(match.id)
    # маппим profile_id -> discord_user_id для отображения
    refs_display = []
    for a in refs:
        p = await org_service.get_profile_by_id(a.user_profile_id)
        refs_display.append(type("R", (), {"discord_user_id": p.discord_user_id if p else None})())
    embed = build_match_embed(match, results, vetoes, refs_display)
    v = view or MatchPanelView(match.id)
    return await channel.send(embed=embed, view=v)


class MatchPanelView(ui.View):
    """Persistent view панели матча."""

    def __init__(self, match_id: str):
        super().__init__(timeout=None)
        self.match_id = match_id
        self._build_buttons()

    def _build_buttons(self) -> None:
        for action, label, ek, role in _ACTIONS:
            btn = design_button(
                label, emoji=ek, role=role,
                custom_id=_cid(self.match_id, action),
            )
            btn.callback = self._make_callback(action)
            self.add_item(btn)

    def _make_callback(self, action: str):
        async def cb(interaction: discord.Interaction) -> None:
            await self._handle(interaction, action)
        return cb

    # ---------- state ----------

    async def _match(self) -> Match | None:
        return await match_service.get_match(self.match_id)

    async def _is_privileged(self, interaction: discord.Interaction, match: Match) -> bool:
        org = await org_service.get_org_by_id(match.organization_id)
        if not org:
            return False
        profile = await org_service.get_or_create_profile(interaction.user)
        member = await org_service.get_member(org.id, profile.id)
        if member and member.role.value in ("OWNER", "ADMIN", "MANAGER", "HEAD_REF", "REFEREE"):
            return True
        return await match_service.is_referee_of(match.id, interaction.user.id)

    async def _deny(self, interaction: discord.Interaction) -> bool:
        await interaction.response.send_message(
            "Недостаточно прав (судья/админ).", ephemeral=True)
        return True

    async def _refresh_message(self, interaction: discord.Interaction, match: Match) -> None:
        results = await match_service.list_map_results(match.id)
        vetoes = await match_service.list_veto_actions(match.id)
        refs = await match_service.list_active_refs(match.id)
        refs_display = []
        for a in refs:
            p = await org_service.get_profile_by_id(a.user_profile_id)
            refs_display.append(type("R", (), {"discord_user_id": p.discord_user_id if p else None})())
        await interaction.response.edit_message(
            embed=build_match_embed(match, results, vetoes, refs_display), view=self)

    # ---------- actions ----------

    async def _handle(self, interaction: discord.Interaction, action: str) -> None:
        match = await self._match()
        if not match:
            await interaction.response.send_message("Матч не найден.", ephemeral=True)
            return

        if action == "refresh":
            await self._refresh_message(interaction, match)
            return

        if action == "claim":
            try:
                await match_service.claim_referee(match, interaction.user.id)
                await interaction.response.send_message(
                    "Вы назначены судьёй матча.", ephemeral=True)
                await self._refresh_message(interaction, match)
            except MatchError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
            return

        if action == "cancel":
            org = await org_service.get_org_by_id(match.organization_id)
            profile = await org_service.get_or_create_profile(interaction.user)
            member = await org_service.get_member(org.id, profile.id) if org else None
            is_admin = member and member.role.value in ("OWNER", "ADMIN", "MANAGER")
            is_creator = match.created_by_id and str(interaction.user.id) == str(match.created_by_id)
            if not (is_admin or is_creator or await match_service.is_referee_of(match.id, interaction.user.id)):
                await self._deny(interaction)
                return
            await match_service.cancel_match(match, reason="Отменено через панель",
                                             actor_id=str(interaction.user.id))
            fresh = await self._match()
            await self._refresh_message(interaction, fresh)
            return

        # остальные — только судьи/админы
        if not await self._is_privileged(interaction, match):
            await self._deny(interaction)
            return

        try:
            if action == "veto":
                maps = await match_service.available_maps(match)
                if not maps:
                    await interaction.response.send_message(
                        "Нет доступных карт для вето.", ephemeral=True)
                else:
                    await interaction.response.send_message(
                        embed=DesignEmbed(category="warning", title="Veto / Баны",
                                          description="Выберите карту для бана."),
                        view=VetoView(self.match_id, maps), ephemeral=True)
            elif action == "start":
                m = match
                if m.status == MatchStatus.PENDING:
                    m = await match_service.transition(m, MatchStatus.VETO, str(interaction.user.id))
                if m.status == MatchStatus.VETO:
                    m = await match_service.transition(m, MatchStatus.LIVE, str(interaction.user.id))
                await self._refresh_message(interaction, m)
            elif action == "score":
                await interaction.response.send_modal(ScoreModal(self.match_id))
            elif action == "pause":
                await interaction.response.send_modal(PauseModal(self.match_id))
            elif action == "warn":
                await interaction.response.send_modal(WarnModal(self.match_id))
            elif action == "close":
                fresh = await self._match()
                winner = await match_service.winner(fresh)
                if fresh.external_ref and winner is None:
                    await interaction.followup.send(
                        "Сначала укажите счёт через «Счёт» (например 1:0), "
                        "чтобы определить победителя.", ephemeral=True)
                    return
                await match_service.complete_match(
                    fresh, winner_slot=winner,
                    result_a=(f"{fresh.team_a_score}-{fresh.maps_to_win - fresh.team_a_score}"
                              if winner == "teamA" else None),
                    result_b=(f"{fresh.team_b_score}-{fresh.maps_to_win - fresh.team_b_score}"
                              if winner == "teamB" else None),
                    actor_id=str(interaction.user.id),
                )
                await self._refresh_message(interaction, await self._match())
                await interaction.followup.send(
                    f"Матч {fresh.public_code} завершён. Победитель: "
                    f"**{fresh.team_a_name if winner == 'teamA' else fresh.team_b_name if winner == 'teamB' else '—'}**",
                    ephemeral=True)
                # авто-архивация комнаты матча
                try:
                    from arbiter.services.room_service import room_service
                    if interaction.guild:
                        settings = await org_service.get_settings(fresh.organization_id)
                        log_ch = (int(settings.match_log_channel_id)
                                  if settings and settings.match_log_channel_id else None)
                        await room_service.archive_and_delete(interaction.guild, fresh.id, log_ch)
                except Exception as e:
                    logger.warning("Room archive failed: %s", e)
                # legacy bridge: применить результат к драфт-турниру
                if fresh.external_ref and interaction.guild:
                    try:
                        from arbiter.services.legacy_bridge import resolve_legacy_match
                        resolved = await resolve_legacy_match(
                            interaction.guild, interaction.client,
                            fresh, winner, interaction.user,
                        )
                        if resolved:
                            await interaction.followup.send(
                                "✅ Результат применён к турниру (Elo/стата/история).",
                                ephemeral=True)
                    except Exception as e:
                        logger.warning("legacy_bridge resolve failed: %s", e)
        except MatchError as e:
            try:
                await interaction.response.send_message(str(e), ephemeral=True)
            except discord.InteractionResponded:
                await interaction.followup.send(str(e), ephemeral=True)


# ---------- Veto sub-view ----------

class VetoView(ui.View):
    def __init__(self, match_id: str, maps: list[str]):
        super().__init__(timeout=300)
        self.match_id = match_id
        options = [discord.SelectOption(label=m[:100], value=m) for m in maps[:25]]
        self.select = ui.Select(
            placeholder="Выберите карту", custom_id="arb:veto:select",
            min_values=1, max_values=1, options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = self.select.values[0] if self.select.values else None
        if not value:
            await interaction.response.send_message("Карта не выбрана.", ephemeral=True)
            return
        match = await match_service.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Матч не найден", ephemeral=True)
            return
        try:
            await match_service.add_veto_action(
                match, VetoKind.BAN, "teamA", value,
                source="manual", actor_id=str(interaction.user.id),
            )
            await interaction.response.send_message(
                f"Карта `{value}` забанена.", ephemeral=True)
        except MatchError as e:
            await interaction.response.send_message(str(e), ephemeral=True)


# ---------- Modals ----------

class ScoreModal(ui.Modal, title="Счёт матча"):
    a_score = ui.TextInput(label="Счёт команды A", placeholder="0", required=True, max_length=3)
    b_score = ui.TextInput(label="Счёт команды B", placeholder="0", required=True, max_length=3)
    map_index = ui.TextInput(label="Индекс карты (0-based, необязательно)",
                             required=False, max_length=3)
    note = ui.TextInput(label="Комментарий", style=discord.TextStyle.paragraph,
                        required=False, max_length=500)

    def __init__(self, match_id: str):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        match = await match_service.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Матч не найден", ephemeral=True)
            return
        try:
            a, b = int(self.a_score.value), int(self.b_score.value)
            await match_service.set_score(match, a, b, actor_id=str(interaction.user.id))
            # Если указан индекс карты — записать MapResult
            if self.map_index.value.strip().isdigit():
                idx = int(self.map_index.value)
                await match_service.record_map_result(
                    match, idx, f"Map {idx+1}", a, b,
                    note=self.note.value or None,
                    actor_id=str(interaction.user.id),
                )
            embed = DesignEmbed(category="success", title="Счёт обновлён",
                                description=f"**{match.team_a_name}** {a} — {b} **{match.team_b_name}**")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        except (ValueError, MatchError) as e:
            await interaction.response.send_message(str(e), ephemeral=True)


class PauseModal(ui.Modal, title="Пауза"):
    minutes = ui.TextInput(label="Минуты", placeholder="5", required=True, max_length=3)
    reason = ui.TextInput(label="Причина", required=True, max_length=200)

    def __init__(self, match_id: str):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        match = await match_service.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Матч не найден", ephemeral=True)
            return
        try:
            mins = int(self.minutes.value)
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO arb_pause_logs (id, organization_id, match_id, pause_type, "
                    "duration_minutes, reason, actor_id, created_at) "
                    "VALUES ($1,$2,$3,'team',$4,$5,$6,$7)",
                    new_id(), match.organization_id, match.id, mins,
                    self.reason.value, str(interaction.user.id), utcnow(),
                )
            await interaction.response.send_message(
                f"Пауза {mins} мин записана.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Ошибка: {e}", ephemeral=True)


class WarnModal(ui.Modal, title="Предупреждение"):
    player = ui.TextInput(label="Игрок/команда", required=True, max_length=100)
    rule = ui.TextInput(label="Правило", required=True, max_length=100)
    note = ui.TextInput(label="Комментарий", style=discord.TextStyle.paragraph,
                        required=False, max_length=500)

    def __init__(self, match_id: str):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        match = await match_service.get_match(self.match_id)
        if not match:
            await interaction.response.send_message("Матч не найден", ephemeral=True)
            return
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO arb_warnings (id, organization_id, match_id, team_name, "
                    "player, rule, note, actor_id, created_at) "
                    "VALUES ($1,$2,$3,NULL,$4,$5,$6,$7,$8)",
                    new_id(), match.organization_id, match.id,
                    self.player.value, self.rule.value, self.note.value or None,
                    str(interaction.user.id), utcnow(),
                )
            await interaction.response.send_message("Предупреждение записано.", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"Ошибка: {e}", ephemeral=True)


def match_panel_view(match_id: str) -> MatchPanelView:
    return MatchPanelView(match_id)
