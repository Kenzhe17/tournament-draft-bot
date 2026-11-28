"""Мастер создания команды (/team create → Squad Hub).

Поток: ephemeral-карточка с select'ами (тип вступления, цель)
→ модалка (название, описание, пароль если нужен) → команда
создаётся и открывается личная панель.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import discord

from models.team import (
    JOIN_APPLICATIONS, JOIN_MODE_LABELS, JOIN_MODES, JOIN_PASSWORD,
    PURPOSE_LABELS, PURPOSES, Team, validate_description,
    validate_team_name,
)
from services import squad_service as svc
from storage import squad_store
from storage.team_store import team_store, TeamStoreError
from utils.design import colors
from utils.design.components import btn, option, select
from utils.design.layouts import DesignView

logger = logging.getLogger(__name__)

_NO_MENTIONS = discord.AllowedMentions.none()

_CREATE_STATE: dict[tuple[int, int], dict[str, Any]] = {}
_STATE_TTL = 600


def _draft(guild_id: int, user_id: int) -> dict[str, Any]:
    key = (int(guild_id), int(user_id))
    st = _CREATE_STATE.get(key)
    if st is None or time.time() - st.get("_ts", 0) > _STATE_TTL:
        st = {"join_mode": JOIN_APPLICATIONS,
              "purpose": PURPOSES[-1], "_ts": time.time()}
        _CREATE_STATE[key] = st
    st["_ts"] = time.time()
    return st


def _b(label: str, style: str, cid: str, cb) -> discord.ui.Button:
    b = btn(label, style=style, custom_id=cid)
    b.callback = cb  # type: ignore[assignment]
    return b


def _s(options: list, cid: str, placeholder: str, cb) -> discord.ui.Select:
    s = select(options, custom_id=cid, placeholder=placeholder)
    s.callback = cb  # type: ignore[assignment]
    return s


async def open_create_flow(interaction: discord.Interaction) -> None:
    """Открыть мастер создания (из /team create или кнопки хаба)."""
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Создание команды работает только на сервере.",
            ephemeral=True)
        return
    # Уже в команде?
    existing = team_store.get_by_member_in_guild(guild.id,
                                                 interaction.user.id)
    if existing is not None:
        await interaction.response.send_message(
            f"Вы уже состоите в команде **{existing.team_name}**.",
            ephemeral=True)
        return
    await _render_wizard(interaction)


async def _render_wizard(interaction: discord.Interaction,
                         *, edit: bool = False) -> None:
    guild = interaction.guild
    uid = interaction.user.id
    d = _draft(guild.id, uid)

    view = DesignView(title="Создание команды", accent=colors.BRAND)
    view.text(
        "Настройте команду — после создания вы сразу попадёте "
        "в её панель.\n"
        f"-# Вступление: {JOIN_MODE_LABELS[d['join_mode']]} · "
        f"Цель: {PURPOSE_LABELS[d['purpose']]}")

    async def _mode_cb(i):
        val = (i.data.get("values") or [JOIN_APPLICATIONS])[0]
        _draft(guild.id, i.user.id)["join_mode"] = val
        await _render_wizard_edit(i)

    async def _purpose_cb(i):
        val = (i.data.get("values") or [PURPOSES[-1]])[0]
        _draft(guild.id, i.user.id)["purpose"] = val
        await _render_wizard_edit(i)

    view.row(_s(
        [option(label=JOIN_MODE_LABELS[m], value=m)
         for m in JOIN_MODES],
        "tc:mode", "Тип вступления", _mode_cb))
    view.row(_s(
        [option(label=PURPOSE_LABELS[p], value=p) for p in PURPOSES],
        "tc:purpose", "Цель команды", _purpose_cb))

    async def _next(i):
        d = _draft(guild.id, i.user.id)
        await i.response.send_modal(
            CreateTeamModal(d["join_mode"], d["purpose"]))

    async def _cancel(i):
        from views.squad_hub_view import hub_menu_view
        await i.response.edit_message(view=hub_menu_view())

    view.row(
        _b("Далее", "primary", "tc:next", _next),
        _b("Отмена", "secondary", "tc:cancel", _cancel))

    if edit:
        await interaction.response.edit_message(view=view)
    else:
        await interaction.response.send_message(
            view=view, ephemeral=True, allowed_mentions=_NO_MENTIONS)


async def _render_wizard_edit(interaction: discord.Interaction) -> None:
    # edit_message — первый ответ; defer() сломал бы его
    # (InteractionResponded).
    await _render_wizard(interaction, edit=True)


class CreateTeamModal(discord.ui.Modal):
    """Модалка создания: название + описание (+ пароль для режима
    «По паролю»)."""

    def __init__(self, join_mode: str, purpose: str):
        super().__init__(title="Новая команда")
        self.join_mode = join_mode
        self.purpose = purpose
        self.name = discord.ui.TextInput(
            label="Название команды", style=discord.TextStyle.short,
            required=True, min_length=2, max_length=24,
            placeholder="2–24 символа")
        self.desc = discord.ui.TextInput(
            label="Описание", style=discord.TextStyle.paragraph,
            required=False, max_length=150)
        self.add_item(self.name)
        self.add_item(self.desc)
        if join_mode == JOIN_PASSWORD:
            self.password = discord.ui.TextInput(
                label="Пароль команды", style=discord.TextStyle.short,
                required=True, min_length=2, max_length=64)
            self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            return
        uid = interaction.user.id

        err = validate_team_name(self.name.value)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        err = validate_description(self.desc.value or "")
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return
        if self.join_mode == JOIN_PASSWORD:
            pw = getattr(self, "password", None)
            pw_val = pw.value if pw is not None else ""
            if len(pw_val) < 2:
                await interaction.response.send_message(
                    "Пароль: минимум 2 символа.", ephemeral=True)
                return
        else:
            pw_val = ""

        if team_store.get_by_member_in_guild(guild.id, uid) is not None:
            await interaction.response.send_message(
                "Вы уже состоите в команде этого сервера.",
                ephemeral=True)
            return

        team = Team(
            team_id="",
            team_name=self.name.value.strip(),
            captain_id=uid,
            guild_id=guild.id,
            join_mode=self.join_mode,
            purpose=self.purpose,
            description=(self.desc.value or "").strip(),
        )
        if pw_val:
            team.set_password(pw_val)

        try:
            await team_store.create(team)
        except TeamStoreError as exc:
            await interaction.response.send_message(
                _err_text(exc), ephemeral=True)
            return
        except Exception as exc:
            logger.warning("team create failed: %s", exc)
            await interaction.response.send_message(
                "Ошибка базы данных при создании команды.",
                ephemeral=True)
            return

        await squad_store.audit(guild.id, team.team_id, uid,
                                "team_create",
                                metadata={"name": team.team_name,
                                          "join_mode": team.join_mode})
        svc.schedule_hub_refresh(guild)

        from views.team_panel_view import send_team_panel
        await send_team_panel(
            interaction, team.team_id,
            notice=(f"Команда **{team.team_name}** создана — "
                    "вы капитан."))


def _err_text(exc: TeamStoreError) -> str:
    return getattr(exc, "message", "") or str(exc)
