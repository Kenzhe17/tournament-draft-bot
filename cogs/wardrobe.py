"""Гардероб игрока — коллекция вещей из каталога Free Fire.

/player wardrobe — V2-интерфейс:
- каталог по категориям (одежда, оружие, визуал и т.д.)
- поиск по названию
- покупка за монеты (цена по редкости)
- 'образ' — один экипированный предмет
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.groups import player_group, bind_commands
from config import get_emoji, replace_emojis
from services import ff_catalog
from storage.user_balance_store import user_balance_store
from storage.wardrobe_store import wardrobe_store
from utils.design import compose

logger = logging.getLogger(__name__)

PAGE_SIZE = 25

# Категории гардероба: ключ -> (emoji, название, типы каталога FF)
CATEGORIES: dict[str, tuple[str, str, frozenset[str]]] = {
    "outfit": (
        "👕",
        "Одежда",
        frozenset({"Top", "Bottom", "Shoe", "Head", "Mask", "Fist Skins"}),
    ),
    "accessories": (
        "🎒",
        "Аксессуары",
        frozenset(
            {"Bag Skins", "Parachute", "Gloo Walls", "Skyboard", "Skywings", "Vehicle Skins"}
        ),
    ),
    "weapons": (
        "🔫",
        "Оружие",
        frozenset(
            {
                "Weapon Skins",
                "Grenade Skin",
                "Skill Skins",
                "Finishing Move",
                "Final Shot",
                "Battle Card",
            }
        ),
    ),
    "characters": ("🧍", "Персонажи", frozenset({"Characters"})),
    "pets": (
        "🐾",
        "Питомцы",
        frozenset({"Pets", "Pyro Pet", "Pet Skin", "Pet Emote", "Pet Skill"}),
    ),
    "visual": (
        "✨",
        "Визуал",
        frozenset(
            {
                "Avatars",
                "Banners",
                "Emote",
                "Super Emote",
                "Titles",
                "Music",
                "Voice Packs",
                "Photo Pose",
                "Photo Frame",
                "Arrival Animation",
                "Profile Pin",
                "Posture",
                "Hyper Book",
                "Sticker",
                "Avatar Frame",
            }
        ),
    ),
}

# Цена по редкости (монеты)
RARITY_PRICES = {
    "WHITE": 500,
    "GREEN": 1_500,
    "BLUE": 4_000,
    "PURPLE": 10_000,
    "PURPLE_PLUS": 20_000,
    "ORANGE": 35_000,
    "ORANGE_PLUS": 60_000,
    "RED": 100_000,
    "NONE": 200,
}

RARITY_LABELS = {
    "WHITE": "Обычная",
    "GREEN": "Редкая",
    "BLUE": "Эпическая",
    "PURPLE": "Легендарная",
    "PURPLE_PLUS": "Легендарная+",
    "ORANGE": "Мифическая",
    "ORANGE_PLUS": "Мифическая+",
    "RED": "Запредельная",
    "NONE": "—",
}

_CATEGORY_SIZES: dict[str, int] | None = None


def _category_sizes() -> dict[str, int]:
    global _CATEGORY_SIZES
    if _CATEGORY_SIZES is None:
        _CATEGORY_SIZES = {
            key: len(ff_catalog.items_of_types(types))
            for key, (_, _, types) in CATEGORIES.items()
        }
    return _CATEGORY_SIZES


def price_for(item: dict) -> int:
    return RARITY_PRICES.get(str(item.get("rarity") or "NONE").upper(), 1_000)


def _fmt_money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _select_values(interaction: discord.Interaction) -> list[str]:
    return (interaction.data or {}).get("values") or []


class WardrobeSearchModal(discord.ui.Modal, title="Поиск в каталоге"):
    query = discord.ui.TextInput(
        label="Название предмета",
        placeholder="Например: Misha, AK, броня…",
        min_length=2,
        max_length=50,
        required=True,
    )

    def __init__(self, view: "WardrobeView") -> None:
        super().__init__()
        self._view = view

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        results = ff_catalog.search_items(self.query.value, limit=PAGE_SIZE * 20)
        if not results:
            await interaction.followup.send(
                replace_emojis("Ничего не найдено по этому запросу."),
                ephemeral=True,
            )
            return
        new_view = WardrobeView(
            self._view.guild_id,
            self._view.user_id,
            mode="results",
            query=self.query.value.strip(),
            back=("main", None, 0, None),
        )
        embed = await new_view.build_embed()
        await interaction.edit_original_response(view=compose(embed, new_view))


class WardrobeView(discord.ui.View):
    """Единый stateful-view гардероба (обычный View + compose -> V2)."""

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        *,
        mode: str = "main",
        category: str | None = None,
        page: int = 0,
        item_id: int | None = None,
        back: tuple | None = None,
        query: str | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.user_id = user_id
        self.mode = mode
        self.category = category
        self.page = page
        self.item_id = item_id
        self.back = back
        self.query = query
        self._build_items()

    # ------------------------------------------------------------------
    # Данные
    # ------------------------------------------------------------------
    def _catalog_items(self) -> list[dict]:
        if self.mode == "category" and self.category in CATEGORIES:
            return ff_catalog.items_of_types(CATEGORIES[self.category][2])
        if self.mode == "owned":
            ids = wardrobe_store.get_owned_ids(self.guild_id, self.user_id)
            items = []
            for iid in ids:
                it = ff_catalog.get_item(iid)
                if it:
                    items.append(it)
            items.sort(key=lambda x: str(x.get("name") or "").lower())
            return items
        if self.mode == "results" and self.query:
            return ff_catalog.search_items(self.query, limit=PAGE_SIZE * 20)
        return []

    def _page_items(self) -> tuple[list[dict], int]:
        items = self._catalog_items()
        start = self.page * PAGE_SIZE
        return items[start : start + PAGE_SIZE], len(items)

    # ------------------------------------------------------------------
    # Embed
    # ------------------------------------------------------------------
    async def build_embed(self) -> discord.Embed:
        balance = await user_balance_store.get_balance(self.guild_id, self.user_id)
        owned = wardrobe_store.count(self.guild_id, self.user_id)

        if self.mode == "main":
            equipped_id = wardrobe_store.get_equipped_id(self.guild_id, self.user_id)
            equipped_line = "—"
            thumb = None
            if equipped_id:
                it = ff_catalog.get_item(equipped_id)
                if it:
                    equipped_line = f"**{it.get('name')}** `{it.get('type')}`"
                    thumb = ff_catalog.icon_url(equipped_id)
            embed = discord.Embed(
                title=f"ГАРДЕРОБ",
                description=(
                    f" Вещей: **{owned}**\n"
                    f" Баланс: **{_fmt_money(balance)}** {get_emoji('money')}\n"
                    f" Образ: {equipped_line}\n\n"
                    f" Выберите категорию ниже или воспользуйтесь поиском."
                ),
                color=discord.Color.from_rgb(240, 178, 50),
            )
            if thumb:
                embed.set_thumbnail(url=thumb)
            return embed

        if self.mode == "detail":
            return await self._detail_embed(balance)

        # Списочные режимы
        page_items, total = self._page_items()
        cat_name = "Каталог"
        if self.mode == "category" and self.category in CATEGORIES:
            cat_name = CATEGORIES[self.category][1]
        elif self.mode == "owned":
            cat_name = "Мои вещи"
        elif self.mode == "results":
            cat_name = f"Поиск: {self.query}"

        pages = max(1, -(-total // PAGE_SIZE))
        embed = discord.Embed(
            title=f" {cat_name}",
            description=(
                f" Найдено: **{total}** · "
                f"Страница **{self.page + 1}/{pages}**\n"
                f" Выберите предмет из списка."
            ),
            color=discord.Color.from_rgb(88, 101, 242),
        )
        if page_items:
            embed.set_thumbnail(url=ff_catalog.icon_url(page_items[0].get("itemID")))
        return embed

    async def _detail_embed(self, balance: int) -> discord.Embed:
        item = ff_catalog.get_item(self.item_id)
        if not item:
            return discord.Embed(
                title="Предмет не найден",
                description="Возможно, он был удалён из каталога.",
                color=discord.Color.red(),
            )
        price = price_for(item)
        owned = wardrobe_store.is_owned(self.guild_id, self.user_id, int(item["itemID"]))
        equipped_id = wardrobe_store.get_equipped_id(self.guild_id, self.user_id)
        is_equipped = equipped_id == int(item["itemID"])
        rarity = str(item.get("rarity") or "NONE")

        status = "**В гардеробе**" if owned else f"💰 **{_fmt_money(price)}** {get_emoji('money')}"
        if is_equipped:
            status += " · ⭐ **Ваш образ**"

        embed = discord.Embed(
            title=f" {item.get('name', '?')}",
            description=(
                f" {item.get('description') or '—'}\n\n"
                f" Тип: **{item.get('type') or '—'}**\n"
                f" Редкость: **{RARITY_LABELS.get(rarity, rarity)}**\n"
                f" Статус: {status}\n"
                f" Баланс: {_fmt_money(balance)} {get_emoji('money')}"
            ),
            color=ff_catalog.rarity_color(rarity),
        )
        embed.set_thumbnail(url=ff_catalog.icon_url(item["itemID"]))
        return embed

    # ------------------------------------------------------------------
    # Компоненты
    # ------------------------------------------------------------------
    def _nav_button(self, label: str, style, cb, *, disabled=False) -> discord.ui.Button:
        btn = discord.ui.Button(label=label, style=style, disabled=disabled)
        btn.callback = cb
        return btn

    def _back_state(self) -> tuple:
        return self.back or ("main", None, 0, None)

    async def _go(self, interaction: discord.Interaction, view: "WardrobeView") -> None:
        embed = await view.build_embed()
        await interaction.response.edit_message(view=compose(embed, view))

    def _build_items(self) -> None:
        if self.mode == "main":
            self._build_main()
        elif self.mode == "detail":
            self._build_detail()
        else:
            self._build_list()

    def _build_main(self) -> None:
        sizes = _category_sizes()
        select = discord.ui.Select(
            placeholder="Выберите категорию…",
            custom_id="wardrobe:category",
            options=[
                discord.SelectOption(
                    label=name,
                    value=key,
                    description=f"{sizes.get(key, 0)} вещей",
                )
                for key, (emoji, name, _types) in CATEGORIES.items()
            ],
        )

        async def on_category(interaction: discord.Interaction) -> None:
            values = _select_values(interaction)
            if not values or values[0] not in CATEGORIES:
                return
            view = WardrobeView(
                self.guild_id,
                self.user_id,
                mode="category",
                category=values[0],
                back=("main", None, 0, None),
            )
            await self._go(interaction, view)

        select.callback = on_category
        self.add_item(select)

        async def on_search(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(WardrobeSearchModal(self))

        async def on_owned(interaction: discord.Interaction) -> None:
            view = WardrobeView(
                self.guild_id,
                self.user_id,
                mode="owned",
                back=("main", None, 0, None),
            )
            await self._go(interaction, view)

        self.add_item(self._nav_button("Поиск", discord.ButtonStyle.primary, on_search))
        self.add_item(self._nav_button("Мои вещи", discord.ButtonStyle.secondary, on_owned))

    def _list_view(self, page: int) -> "WardrobeView":
        return WardrobeView(
            self.guild_id,
            self.user_id,
            mode=self.mode,
            category=self.category,
            page=page,
            back=self.back,
            query=self.query,
        )

    def _build_list(self) -> None:
        page_items, total = self._page_items()
        pages = max(1, -(-total // PAGE_SIZE))

        async def on_pick(interaction: discord.Interaction) -> None:
            values = _select_values(interaction)
            if not values:
                return
            try:
                item_id = int(values[0])
            except ValueError:
                return
            view = WardrobeView(
                self.guild_id,
                self.user_id,
                mode="detail",
                item_id=item_id,
                back=(self.mode, self.category, self.page, self.query),
            )
            await self._go(interaction, view)

        if page_items:
            select = discord.ui.Select(
                placeholder="Выберите предмет…",
                custom_id="wardrobe:item",
                options=[
                    discord.SelectOption(
                        label=str(it.get("name") or "?")[:100],
                        value=str(it.get("itemID")),
                        description=(
                            f"{it.get('type') or '—'} · "
                            f"{RARITY_LABELS.get(str(it.get('rarity') or ''), '')}"
                        )[:100],
                    )
                    for it in page_items
                ],
            )
            select.callback = on_pick
            self.add_item(select)

        async def on_prev(interaction: discord.Interaction) -> None:
            await self._go(interaction, self._list_view(max(0, self.page - 1)))

        async def on_next(interaction: discord.Interaction) -> None:
            await self._go(interaction, self._list_view(self.page + 1))

        async def on_back(interaction: discord.Interaction) -> None:
            mode, cat, page, query = self._back_state()
            view = WardrobeView(
                self.guild_id,
                self.user_id,
                mode=mode,
                category=cat,
                page=page,
                query=query,
                back=("main", None, 0, None),
            )
            await self._go(interaction, view)

        self.add_item(
            self._nav_button(
                "Назад", discord.ButtonStyle.secondary, on_prev, disabled=self.page <= 0
            )
        )
        self.add_item(
            self._nav_button(
                "Вперёд",
                discord.ButtonStyle.secondary,
                on_next,
                disabled=self.page + 1 >= pages,
            )
        )
        self.add_item(self._nav_button("Назад", discord.ButtonStyle.gray, on_back))

    def _build_detail(self) -> None:
        item = ff_catalog.get_item(self.item_id)

        async def on_buy(interaction: discord.Interaction) -> None:
            if not item:
                return
            price = price_for(item)
            try:
                await user_balance_store.subtract_balance(
                    self.guild_id, self.user_id, price
                )
            except ValueError:
                await interaction.response.send_message(
                    replace_emojis(
                        f"Недостаточно монет! Нужно **{_fmt_money(price)}** {get_emoji('money')}."
                    ),
                    ephemeral=True,
                )
                return
            wardrobe_store.add(self.guild_id, self.user_id, int(item["itemID"]))
            view = WardrobeView(
                self.guild_id,
                self.user_id,
                mode="detail",
                item_id=self.item_id,
                back=self.back,
            )
            await self._go(interaction, view)

        async def on_equip(interaction: discord.Interaction) -> None:
            if not item:
                return
            equipped_id = wardrobe_store.get_equipped_id(self.guild_id, self.user_id)
            new_id = (
                None if equipped_id == int(item["itemID"]) else int(item["itemID"])
            )
            wardrobe_store.set_equipped(self.guild_id, self.user_id, new_id)
            view = WardrobeView(
                self.guild_id,
                self.user_id,
                mode="detail",
                item_id=self.item_id,
                back=self.back,
            )
            await self._go(interaction, view)

        async def on_back(interaction: discord.Interaction) -> None:
            mode, cat, page, query = self._back_state()
            view = WardrobeView(
                self.guild_id,
                self.user_id,
                mode=mode,
                category=cat,
                page=page,
                query=query,
                back=("main", None, 0, None),
            )
            await self._go(interaction, view)

        if item and not wardrobe_store.is_owned(self.guild_id, self.user_id, int(item["itemID"])):
            self.add_item(
                self._nav_button(
                    f"Купить · {_fmt_money(price_for(item))}",
                    discord.ButtonStyle.success,
                    on_buy,
                )
            )
        if item and wardrobe_store.is_owned(self.guild_id, self.user_id, int(item["itemID"])):
            equipped_id = wardrobe_store.get_equipped_id(self.guild_id, self.user_id)
            is_eq = equipped_id == int(item["itemID"])
            self.add_item(
                self._nav_button(
                    "Снять образ" if is_eq else "Сделать образом",
                    discord.ButtonStyle.primary if not is_eq else discord.ButtonStyle.secondary,
                    on_equip,
                )
            )
        self.add_item(self._nav_button("Назад", discord.ButtonStyle.gray, on_back))


class WardrobeCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        bind_commands(player_group, self, ["wardrobe"])

    # Слэш-команда скрыта: декоратор закомментирован, код сохранён.
    # @player_group.command(name="wardrobe", description="Гардероб — коллекция вещей Free Fire")
    async def wardrobe(self, interaction: discord.Interaction) -> None:
        """Открыть свой гардероб."""
        view = WardrobeView(interaction.guild_id, interaction.user.id, mode="main")
        embed = await view.build_embed()
        await interaction.response.send_message(view=compose(embed, view))


async def setup(bot) -> None:
    await bot.add_cog(WardrobeCog(bot))
