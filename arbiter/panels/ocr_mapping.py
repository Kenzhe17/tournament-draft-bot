"""OcrMappingView — привязка игровых ников (FF) к Discord-никам в турнире.

После OCR организатор/капитан для каждого распознанного игрока выбирает
соответствие из ростера команд. Автоматические совпадения по имени
подставляются сразу. «Готово» → OcrConfirmView с итоговой статистикой.
"""

from __future__ import annotations

import logging
import secrets

import discord
from discord import ui

from config import replace_emojis
from utils.design.components import ButtonRole, design_button
from utils.design.embeds import DesignEmbed

logger = logging.getLogger(__name__)

PAGE_SIZE = 4  # 4 select-ряда + 1 ряд кнопок = 5 action rows (лимит Discord)
_PENDING: dict[str, dict] = {}


def _new_token() -> str:
    return secrets.token_hex(6)


def create_pending(guild_id: int, tournament_id: str, match_type: str,
                   match_index: int, team_a: int, team_b: int,
                   match_label: str, ocr_players: list[dict],
                   roster: list[str], auto_map: dict[int, str]) -> str:
    token = _new_token()
    _PENDING[token] = {
        "guild_id": guild_id,
        "tournament_id": tournament_id,
        "match_type": match_type,
        "match_index": match_index,
        "team_a": team_a,
        "team_b": team_b,
        "match_label": match_label,
        "ocr_players": ocr_players,
        "roster": roster,
        "mapping": dict(auto_map),  # {ocr_index: roster_name}
        "auto_map": dict(auto_map),
        "page": 0,
    }
    return token


def get_state(token: str) -> dict | None:
    return _PENDING.get(token)


def _drop(token: str) -> None:
    _PENDING.pop(token, None)


def mapping_embed(state: dict) -> DesignEmbed:
    players = state["ocr_players"]
    mapping = state["mapping"]
    embed = DesignEmbed(
        category="info", title="OCR: привязка игроков",
        description=(f"{state['match_label']} • Игроков распознано: "
                     f"**{len(players)}**\n"
                     "Для каждого игрового ника выберите имя в турнире "
                     "(список под полем ввода). Авто-совпадения уже подставлены."))
    total_pages = max(1, -(-len(players) // PAGE_SIZE))
    page = state["page"]
    embed.set_footer(text=f"Страница {page + 1}/{total_pages} • "
                          f"привязано {len(mapping)}/{len(players)}")
    for i in range(page * PAGE_SIZE, min((page + 1) * PAGE_SIZE, len(players))):
        p = players[i]
        roster_name = mapping.get(i)
        stat = (f"K{p.get('kills', 0)} D{p.get('deaths', 0)} "
                f"A{p.get('assists', 0)} DMG{p.get('damage', 0)}")
        embed.add_kv(
            f"#{i + 1} {p.get('name', '?')}",
            f"→ **{roster_name}**\n{stat}" if roster_name
            else f"→ _не привязан_\n{stat}")
    return embed


class _PlayerSelect(ui.Select):
    def __init__(self, token: str, ocr_index: int, ocr_name: str,
                 roster: list[str], current: str | None):
        options = [discord.SelectOption(label=n[:100], value=n)
                   for n in roster[:25]]
        options.append(discord.SelectOption(label="— пропустить —",
                                            value="__skip__"))
        super().__init__(
            placeholder=f"{ocr_name[:80]} → игрок турнира",
            options=options,
            min_values=1, max_values=1,
            custom_id=f"ocr_map_{token}_{ocr_index}")
        self.token = token
        self.ocr_index = ocr_index

    async def callback(self, interaction: discord.Interaction) -> None:
        state = _PENDING.get(self.token)
        if not state:
            await interaction.response.send_message(
                "Сессия привязки истекла — запустите OCR заново.", ephemeral=True)
            return
        value = self.values[0]
        if value == "__skip__":
            state["mapping"].pop(self.ocr_index, None)
        else:
            state["mapping"][self.ocr_index] = value
        view = OcrMappingView(self.token, state["page"])
        await interaction.response.edit_message(
            embed=mapping_embed(state), view=view)


class OcrMappingView(ui.View):
    def __init__(self, token: str, page: int = 0):
        super().__init__(timeout=600)
        state = _PENDING.get(token)
        if not state:
            return
        state["page"] = page
        players = state["ocr_players"]
        mapping = state["mapping"]
        roster = state["roster"]

        for j, i in enumerate(range(page * PAGE_SIZE,
                                    min((page + 1) * PAGE_SIZE, len(players)))):
            p = players[i]
            self.add_item(_PlayerSelect(token, i, p.get("name", "?"),
                                        roster, mapping.get(i)))

        total_pages = max(1, -(-len(players) // PAGE_SIZE))
        row_btns = []
        if page > 0:
            row_btns.append(design_button(
                label="Назад", role=ButtonRole.SECONDARY, emoji="back",
                custom_id=f"ocr_pg_{token}_prev",
                callback=lambda i: self._goto(i, page - 1)))
        if page < total_pages - 1:
            row_btns.append(design_button(
                label="Далее", role=ButtonRole.SECONDARY, emoji="forward",
                custom_id=f"ocr_pg_{token}_next",
                callback=lambda i: self._goto(i, page + 1)))
        for b in row_btns:
            self.add_item(b)

        self.add_item(design_button(
            label="Готово → превью", role=ButtonRole.PRIMARY, emoji="check",
            custom_id=f"ocr_done_{token}",
            callback=lambda i: self._finish(i)))
        self.add_item(design_button(
            label="Только авто-совпадения", role=ButtonRole.SECONDARY,
            emoji="cross", custom_id=f"ocr_skip_{token}",
            callback=lambda i: self._skip(i)))

    async def _goto(self, interaction: discord.Interaction, page: int) -> None:
        token = self._token_from(interaction)
        state = _PENDING.get(token)
        if not state:
            return
        await interaction.response.edit_message(
            embed=mapping_embed(state), view=OcrMappingView(token, page))

    async def _finish(self, interaction: discord.Interaction) -> None:
        token = self._token_from(interaction)
        state = _PENDING.pop(token, None)
        if not state:
            await interaction.response.send_message(
                "Сессия истекла.", ephemeral=True)
            return
        matched, unmatched = _build_result(state)
        if not matched:
            await interaction.response.edit_message(
                embed=DesignEmbed(category="warning", title="Нет привязок",
                                  description="Привяжите хотя бы одного игрока."),
                view=None)
            return
        from arbiter.panels.ocr_confirm import OcrConfirmView
        confirm = OcrConfirmView(
            state["guild_id"], state["tournament_id"], state["match_type"],
            state["match_index"], matched, unmatched, state["match_label"],
            cid_suffix=f"_{token}")
        embed = _result_embed(state, matched, unmatched)
        await interaction.response.edit_message(embed=embed, view=confirm)

    async def _skip(self, interaction: discord.Interaction) -> None:
        """Пропустить ручную привязку — только авто-совпадения по имени."""
        token = self._token_from(interaction)
        state = _PENDING.get(token)
        if not state:
            return
        # Сбросить ручные правки — оставить только авто-совпадения
        state["mapping"] = dict(state.get("auto_map", {}))
        await self._finish(interaction)

    def _token_from(self, interaction: discord.Interaction) -> str:
        # custom_id содержит token: ocr_done_<token> / ocr_pg_<token>_*
        cid = interaction.data.get("custom_id", "")
        for prefix in ("ocr_done_", "ocr_pg_", "ocr_skip_"):
            if cid.startswith(prefix):
                rest = cid[len(prefix):]
                return rest.rsplit("_", 1)[0] if prefix == "ocr_pg_" else rest
        return ""


def _build_result(state: dict) -> tuple[dict[str, dict], list[dict]]:
    matched: dict[str, dict] = {}
    unmatched: list[dict] = []
    for i, p in enumerate(state["ocr_players"]):
        roster_name = state["mapping"].get(i)
        if roster_name:
            matched[roster_name] = {
                "kills": p.get("kills", 0),
                "deaths": p.get("deaths", 0),
                "assists": p.get("assists", 0),
                "damage": p.get("damage", 0),
                "ocr_name": p.get("name", "?"),
            }
        else:
            unmatched.append(p)
    return matched, unmatched


def _result_embed(state: dict, matched: dict[str, dict],
                  unmatched: list[dict]) -> DesignEmbed:
    embed = DesignEmbed(
        category="info", title=f"OCR → {state['match_label']}",
        description=(f"Сопоставлено: **{len(matched)}**"
                     + (f" • не привязано: **{len(unmatched)}**"
                        if unmatched else "")))
    for name, kd in list(matched.items())[:20]:
        embed.add_kv(
            name,
            f"K {kd['kills']} • D {kd['deaths']} • A {kd['assists']} • "
            f"DMG {kd['damage']}" +
            (f"  _(OCR: {kd['ocr_name']})_" if kd["ocr_name"] != name else ""))
    if unmatched:
        embed.add_kv("Не привязаны",
                     ", ".join(p.get("name", "?") for p in unmatched[:10]))
    embed.set_footer(text="В temp_match_stats запишутся K/D/A/DMG.")
    return embed
