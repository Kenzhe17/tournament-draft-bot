"""SHOP 2.0 — единый CV2-интерфейс магазина r!z3 CLASSIC.

DesignView/LayoutView: Container + TextDisplay + Separator +
ActionRow(Button/Select) + Section(Thumbnail). Эфемерное сообщение
редактируется на месте при навигации. Покупки — через
services/shop_purchase_service (per-user lock, идемпотентный
refund, журнал shop_ops_store).
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING

import discord

from models.shop_item import CosmeticRarity, ShopItem
from services import shop_purchase_service as purchase_svc
from storage.shop_ops_store import shop_ops_store
from storage.shop_store import inventory_store, shop_store
from storage.rep_balance_store import (
    rep_balance_store,
    rep_price_for_amount,
    rep_price_for_item,
)
from storage.user_balance_store import user_balance_store
from utils.design import colors
from utils.design.layouts import DesignView, edit_card

if TYPE_CHECKING:
    pass

PAGE_SIZE = 25  # лимит опций Discord Select

RARITY_LABEL = {
    CosmeticRarity.BASIC: "Basic",
    CosmeticRarity.PREMIUM: "Premium",
    CosmeticRarity.ELITE: "Elite",
    CosmeticRarity.SPECIAL: "Special",
}
RARITY_ORDER = [
    CosmeticRarity.BASIC,
    CosmeticRarity.PREMIUM,
    CosmeticRarity.ELITE,
    CosmeticRarity.SPECIAL,
]
KIND_LABEL = {
    "icon": "ЗНАЧОК",
    "tag": "ТЕГ",
    "frame": "РАМКА",
    "color": "ЦВЕТНАЯ РОЛЬ",
    "role": "РОЛЬ",
    "service": "УСЛУГА",
    "bundle": "НАБОР",
}

# Категории главного меню: key -> (название, описание)
MAIN_CATEGORIES = [
    ("personal", "ПЕРСОНАЛИЗАЦИЯ", "Значки, теги, рамки и наборы"),
    ("color_roles", "ЦВЕТНЫЕ РОЛИ", "Цвет вашего ника в Discord"),
    ("roles", "DISCORD РОЛИ", "Роли и доступы за монеты"),
    ("wardrobe", "ГАРДЕРОБ FREE FIRE", "Образы и предметы FF"),
    ("pets", "ПИТОМЦЫ", "Компаньоны и яйцо загадки"),
    ("cases", "КЕЙСЫ", "Открытие кейсов с наградами"),
    ("services", "УСЛУГИ", "Смена ника и сервисы"),
]
EXTRA_BUTTONS = [
    ("favorites", "ИЗБРАННОЕ"),
    ("purchases", "МОИ ПОКУПКИ"),
    ("inventory", "ИНВЕНТАРЬ"),
]

_SUBCATS = [
    ("icons", "ЗНАЧКИ", "icons"),
    ("tags", "ТЕГИ", "tags"),
    ("frames", "РАМКИ", "frames"),
    ("bundles", "НАБОРЫ", "bundles"),
]


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _esc(s: str) -> str:
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("~", "\\~")
        .replace("`", "\\`")
    )


def _icon_emoji(value: str) -> str:
    try:
        from config import get_emoji
        return get_emoji(value) or ""
    except Exception:
        return ""


def _kind_of(item: ShopItem) -> str:
    if item.bundle:
        return "bundle"
    if item.item_type == "service":
        return "service"
    if item.item_type == "role":
        return "color" if item.category == "color_roles" else "role"
    return item.cosmetic_type.value  # icon/tag/frame/color


def _owner_id() -> int:
    try:
        from config import BOT_OWNER_ID
        return int(BOT_OWNER_ID or 0)
    except Exception:
        return 0


def _owner_guild_ok(guild_id: int | None) -> bool:
    """Разрешённый guild для управления каталогом.

    Если задан GUILD_ID — только он; иначе — любой guild из
    ALLOWED_GUILD_IDS (пустой список = все)."""
    try:
        from config import GUILD_ID, guild_allowed
        gid = int(GUILD_ID or 0)
        if gid:
            return guild_id == gid
        return guild_allowed(guild_id)
    except Exception:
        return False


def _is_admin(user, guild_id: int | None = None) -> bool:
    """Управление каталогом — ТОЛЬКО владелец бота на
    разрешённом guild.

    ADMIN_USER_IDS, Discord Administrator, организаторы и
    модераторы прав не имеют."""
    uid = getattr(user, "id", 0)
    if not (uid and uid == _owner_id()):
        return False
    if guild_id is None:
        guild_id = getattr(user, "guild_id", None)
    return _owner_guild_ok(guild_id)


def _is_admin_uid(user_id: int, guild_id: int | None = None) -> bool:
    """Admin-флаг только по uid владельца — для перестроения
    главного меню без объекта user."""
    return bool(
        user_id and user_id == _owner_id()
        and _owner_guild_ok(guild_id)
    )


async def _require_owner(interaction: discord.Interaction) -> bool:
    """Проверка прав владельца внутри callback/modal.

    user_id из сохранённого состояния интерфейса не доверяем —
    проверяем interaction.user.id и guild заново при каждом
    действии.
    """
    if _is_admin(
        interaction.user, getattr(interaction, "guild_id", None)
    ):
        return True
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Управление каталогом доступно только владельцу бота.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Управление каталогом доступно только владельцу бота.",
                ephemeral=True,
            )
    except Exception:
        pass
    return False


async def _profile_lines(guild_id: int, user_id: int) -> list[str]:
    balance = await user_balance_store.get_balance(guild_id, user_id)
    rep_bal = 0
    try:
        rep_bal = await rep_balance_store.get_balance(guild_id, user_id)
    except Exception:
        rep_bal = 0
    inv = inventory_store.get_player_inventory(guild_id, user_id)
    equipped = sum(1 for c in inv if c.equipped)
    return [
        "**ВАШ ПРОФИЛЬ**",
        f"Баланс: **{_money(balance)}** монет · REP: **{rep_bal}**",
        f"Предметов: {len(inv)} · Экипировано: {equipped}",
    ]


def _status_of(guild_id: int, user_id: int, item: ShopItem) -> str:
    owned = {
        c.item_id: c
        for c in inventory_store.get_player_inventory(guild_id, user_id)
    }
    cos = owned.get(item.id)
    if cos is None:
        return "Не приобретён"
    if item.bundle:
        have = sum(
            1 for cid in item.bundle if cid in owned
        )
        return f"Набор: {have}/{len(item.bundle)} компонентов"
    if cos.equipped:
        return "Экипирован"
    if item.item_type == "role" and item.category != "color_roles":
        return "Роль выдана"
    return "В коллекции"


# ---------------------------------------------------------------- main menu


class _CatSelect(discord.ui.Select):
    def __init__(self, guild_id: int, user_id: int):
        options = [
            discord.SelectOption(
                label=label[:100], value=key, description=desc[:100]
            )
            for key, label, desc in MAIN_CATEGORIES
        ]
        super().__init__(
            placeholder="Выберите категорию магазина",
            min_values=1, max_values=1, options=options,
        )
        self.guild_id = guild_id
        self.user_id = user_id

    async def callback(self, interaction: discord.Interaction) -> None:
        cat = self.values[0]
        if cat == "wardrobe":
            await _open_wardrobe(interaction)
            return
        if cat == "personal":
            await edit_card(interaction, await build_subcat_view(
                self.guild_id, self.user_id
            ))
            return
        builder = _CATEGORY_BUILDERS.get(cat)
        if builder is not None:
            await edit_card(interaction, await builder(
                self.guild_id, self.user_id
            ))
            return
        view = await build_browse_view(
            self.guild_id, self.user_id, cat, {}
        )
        await edit_card(interaction, view)


class _ExtraButton(discord.ui.Button):
    def __init__(self, key: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.key = key

    async def callback(self, interaction: discord.Interaction) -> None:
        gid, uid = interaction.guild_id, interaction.user.id
        if self.key == "favorites":
            await edit_card(interaction, await build_favorites_view(gid, uid))
        elif self.key == "purchases":
            await edit_card(interaction, await build_purchases_view(gid, uid))
        elif self.key == "inventory":
            from views.inventory2_view import build_inventory_view
            await edit_card(interaction, await build_inventory_view(gid, uid))


class _AdminButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="УПРАВЛЕНИЕ", style=discord.ButtonStyle.danger
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not _is_admin(
            interaction.user, getattr(interaction, "guild_id", None)
        ):
            await interaction.response.send_message(
                "Нет прав.", ephemeral=True
            )
            return
        await edit_card(interaction, await build_admin_view(
            interaction.guild_id, interaction.user.id
        ))


async def build_main_view(guild_id: int, user_id: int) -> DesignView:
    lines = ["**r!z3 · МАГАЗИН**"] + await _profile_lines(guild_id, user_id)
    view = DesignView(title=None, accent=colors.CASE)
    view.text("\n".join(lines))
    view.sep()
    view.text("**КАТАЛОГ**")
    sel = _CatSelect(guild_id, user_id)
    row = discord.ui.ActionRow()
    row.add_item(sel)
    view.container.add_item(row)
    view.sep()
    view.text("**ДОПОЛНИТЕЛЬНО**")
    row2 = discord.ui.ActionRow()
    for key, label in EXTRA_BUTTONS:
        row2.add_item(_ExtraButton(key, label))
    if _is_admin_uid(user_id, guild_id):
        row2.add_item(_AdminButton())
    view.container.add_item(row2)
    return view


async def open_shop(interaction: discord.Interaction) -> None:
    """Точка входа /shop. Вызывается после defer — отвечаем followup'ом."""
    view = await build_main_view(interaction.guild_id, interaction.user.id)
    await interaction.followup.send(view=view, ephemeral=True)


# ---------------------------------------------------------------- browse


def _item_source(category: str, sub: str | None) -> list[ShopItem]:
    """Товары категории с учётом подкатегории."""
    if category == "personal":
        cats = [sub] if sub else [s[2] for s in _SUBCATS]
        items = []
        for c in cats:
            items.extend(shop_store.get_items_by_category(c))
        return items
    if category == "services":
        return shop_store.get_items_by_category("services")
    return shop_store.get_items_by_category(category)


def _apply_filters(
    items: list[ShopItem],
    state: dict,
    guild_id: int,
    user_id: int,
) -> list[ShopItem]:
    owned = {
        c.item_id
        for c in inventory_store.get_player_inventory(guild_id, user_id)
    }
    rarity = state.get("rarity")
    owned_f = state.get("owned")  # None | "yes" | "no"
    query = (state.get("query") or "").strip().lower()
    sort = state.get("sort", "price_asc")

    out = []
    for it in items:
        if not purchase_svc.item_available_now(it):
            continue
        if rarity and it.rarity.value != rarity:
            continue
        is_owned = it.id in owned
        if owned_f == "yes" and not is_owned:
            continue
        if owned_f == "no" and is_owned:
            continue
        if query and query not in (it.name + " " + it.description).lower():
            continue
        out.append(it)
    if sort == "price_desc":
        out.sort(key=lambda i: i.price, reverse=True)
    elif sort == "name":
        out.sort(key=lambda i: i.name.lower())
    else:
        out.sort(key=lambda i: i.price)
    return out


class _ItemSelect(discord.ui.Select):
    def __init__(self, items: list[ShopItem], gid: int, uid: int,
                 cat: str, sub: str | None, state: dict,
                 back=None):
        owned_ids = {
            c.item_id
            for c in inventory_store.get_player_inventory(gid, uid)
        }
        options = []
        for it in items[:PAGE_SIZE]:
            kind = KIND_LABEL.get(_kind_of(it), it.category.upper())
            owned_mark = " ✓" if it.id in owned_ids else ""
            label = f"{it.name}{owned_mark}"[:100]
            desc = (f"{kind} · {_money(it.price)} монет + "
                f"{rep_price_for_item(it)} REP")[:100]
            options.append(discord.SelectOption(
                label=label, value=it.id, description=desc
            ))
        super().__init__(
            placeholder="Выберите товар",
            min_values=1, max_values=1, options=options or [
                discord.SelectOption(label="—", value="__none__")
            ],
        )
        self.gid, self.uid, self.cat, self.sub, self.state = (
            gid, uid, cat, sub, dict(state)
        )
        self.back = back

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "__none__":
            return
        item = shop_store.get_item(self.values[0])
        if item is None:
            await interaction.response.send_message(
                "Товар не найден.", ephemeral=True
            )
            return
        back = self.back or (
            "browse", self.cat,
            self.sub if self.cat == "personal" else None, self.state,
        )
        await edit_card(interaction, await build_item_card_view(
            self.gid, self.uid, item, back=back
        ))


class _BrowseButton(discord.ui.Button):
    def __init__(self, action: str, label: str, gid: int, uid: int,
                 cat: str, sub: str | None, state: dict):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.action, self.gid, self.uid = action, gid, uid
        self.cat, self.sub, self.state = cat, sub, state

    async def callback(self, interaction: discord.Interaction) -> None:
        st = dict(self.state)
        if self.action == "prev":
            st["page"] = max(0, st.get("page", 0) - 1)
        elif self.action == "next":
            st["page"] = st.get("page", 0) + 1
        elif self.action == "search":
            await interaction.response.send_modal(
                _SearchModal(self.gid, self.uid, self.cat, self.sub, st)
            )
            return
        elif self.action == "rarity":
            cur = st.get("rarity")
            idx = RARITY_ORDER.index(cur) + 1 if cur in RARITY_ORDER else 0
            st["rarity"] = (
                RARITY_ORDER[idx].value
                if idx < len(RARITY_ORDER) else None
            )
            st["page"] = 0
        elif self.action == "owned":
            cur = st.get("owned")
            st["owned"] = {None: "yes", "yes": "no", "no": None}[cur]
            st["page"] = 0
        elif self.action == "sort":
            order = ["price_asc", "price_desc", "name"]
            st["sort"] = order[(order.index(st.get("sort", "price_asc")) + 1) % 3]
            st["page"] = 0
        elif self.action == "back":
            if self.cat == "personal":
                await edit_card(interaction, await build_subcat_view(self.gid, self.uid))
            else:
                await edit_card(interaction, await build_main_view(self.gid, self.uid))
            return
        await edit_card(interaction, await build_browse_view(
            self.gid, self.uid, self.cat, st, sub=self.sub
        ))


class _SearchModal(discord.ui.Modal, title="Поиск по магазину"):
    query = discord.ui.TextInput(
        label="Название товара", placeholder="например, LEGEND или рамка",
        max_length=60,
    )

    def __init__(self, gid, uid, cat, sub, state):
        super().__init__()
        self.gid, self.uid, self.cat, self.sub, self.state = (
            gid, uid, cat, sub, state
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        st = dict(self.state)
        st["query"] = self.query.value.strip()
        st["page"] = 0
        await edit_card(interaction, await build_browse_view(
            self.gid, self.uid, self.cat, st, sub=self.sub
        ))


async def build_subcat_view(guild_id: int, user_id: int) -> DesignView:
    view = DesignView(title="ПЕРСОНАЛИЗАЦИЯ", accent=colors.CASE)
    view.text(
        "**r!z3 · МАГАЗИН — ПЕРСОНАЛИЗАЦИЯ**\n"
        "Выберите раздел:"
    )
    view.sep()
    row = discord.ui.ActionRow()
    for key, label, cat in _SUBCATS:
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)

        async def _cb(interaction, _cat=cat):
            await edit_card(interaction, await build_browse_view(
                guild_id, user_id, "personal", {}, sub=_cat
            ))
        btn.callback = _cb
        row.add_item(btn)
    view.container.add_item(row)
    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="Назад", style=discord.ButtonStyle.secondary)

    async def _back(interaction):
        await edit_card(interaction, await build_main_view(guild_id, user_id))
    b.callback = _back
    back.add_item(b)
    view.container.add_item(back)
    return view


async def build_browse_view(
    guild_id: int,
    user_id: int,
    category: str,
    state: dict,
    sub: str | None = None,
) -> DesignView:
    st = {"page": 0, "rarity": None, "owned": None,
          "sort": "price_asc", "query": ""}
    st.update(state or {})

    items = _apply_filters(
        _item_source(category, sub), st, guild_id, user_id
    )
    total_pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(st.get("page", 0), total_pages - 1)
    page_items = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    cat_label = dict((k, l) for k, l, _ in MAIN_CATEGORIES).get(
        category, category.upper()
    )
    if category == "personal" and sub:
        sub_label = dict((k, l) for k, l, _ in _SUBCATS).get(sub, sub)
        title = f"ПЕРСОНАЛИЗАЦИЯ — {sub_label}"
    elif category == "personal":
        title = "ПЕРСОНАЛИЗАЦИЯ"
    else:
        title = cat_label

    filters = []
    if st.get("rarity"):
        filters.append(f"редкость: {st['rarity']}")
    if st.get("owned") == "yes":
        filters.append("только купленные")
    elif st.get("owned") == "no":
        filters.append("только не купленные")
    if st.get("query"):
        filters.append(f"поиск: «{st['query']}»")
    filt_txt = " · ".join(filters) if filters else "без фильтров"

    view = DesignView(title=title, accent=colors.CASE)
    view.text(
        f"**{title}**\n"
        f"Товаров: {len(items)} · Страница {page + 1}/{total_pages}\n"
        f"Фильтры: {filt_txt}"
    )
    view.sep()

    if page_items:
        sel = _ItemSelect(
            page_items, guild_id, user_id, category,
            sub if category == "personal" else None, st,
        )
        row = discord.ui.ActionRow()
        row.add_item(sel)
        view.container.add_item(row)
    else:
        view.text("Ничего не найдено — измените фильтры.")

    nav1 = discord.ui.ActionRow()
    nav1.add_item(_BrowseButton(
        "prev", "◀ Назад", guild_id, user_id, category,
        sub if category == "personal" else None, st))
    nav1.add_item(_BrowseButton(
        "next", "Вперёд ▶", guild_id, user_id, category,
        sub if category == "personal" else None, st))
    nav1.add_item(_BrowseButton(
        "search", "🔍 Поиск", guild_id, user_id, category,
        sub if category == "personal" else None, st))
    nav1.add_item(_BrowseButton(
        "rarity", "Редкость", guild_id, user_id, category,
        sub if category == "personal" else None, st))
    nav1.add_item(_BrowseButton(
        "owned", "Наличие", guild_id, user_id, category,
        sub if category == "personal" else None, st))
    view.container.add_item(nav1)

    nav2 = discord.ui.ActionRow()
    nav2.add_item(_BrowseButton(
        "sort", "Сортировка", guild_id, user_id, category,
        sub if category == "personal" else None, st))
    nav2.add_item(_BrowseButton(
        "back", "В меню", guild_id, user_id, category,
        sub if category == "personal" else None, st))
    view.container.add_item(nav2)
    return view


# ---------------------------------------------------------------- item card


def _preview_text(item: ShopItem) -> str:
    kind = _kind_of(item)
    if kind == "tag":
        return f"**{item.value}** ВашНик"
    if kind == "icon":
        return f"ВашНик {_icon_emoji(item.value)}"
    if kind == "frame":
        return f"{item.value} **ВашНик** {item.value}"
    if kind == "color":
        return f"▌ ВашНик — цвет `{item.value}`"
    if item.bundle:
        parts = []
        for cid in item.bundle or []:
            comp = shop_store.get_item(cid)
            if comp:
                parts.append(f"• {_esc(comp.name)}")
        return "\n".join(parts) or "—"
    if kind == "role":
        return f"Discord-роль: <@&{item.role_id}>"
    return item.description or "—"


async def _card_body(
    guild_id: int, user_id: int, item: ShopItem, confirm: bool
) -> str:
    kind = KIND_LABEL.get(_kind_of(item), item.category.upper())
    rarity = RARITY_LABEL.get(item.rarity, item.rarity.value)
    status = _status_of(guild_id, user_id, item)
    balance = await user_balance_store.get_balance(guild_id, user_id)
    rep_price = rep_price_for_item(item)
    rep_bal = 0
    try:
        rep_bal = await rep_balance_store.get_balance(guild_id, user_id)
    except Exception:
        rep_bal = 0
    lines = [
        f"**{kind} · {_esc(item.name.upper() if item.name.isascii() else item.name)}**",
        f"Редкость: {rarity} · Стоимость: **{_money(item.price)}** монет + **{rep_price}** REP",
    ]
    if item.bundle:
        lines.append("**СОСТАВ НАБОРА**")
        for cid in item.bundle or []:
            comp = shop_store.get_item(cid)
            mark = "✓" if comp and any(
                c.item_id == cid
                for c in inventory_store.get_player_inventory(
                    guild_id, user_id
                )
            ) else "·"
            lines.append(f"{mark} {_esc(comp.name if comp else cid)}")
    lines += [
        "",
        "**ПРЕДПРОСМОТР**",
        _preview_text(item),
        "",
        "**СТАТУС**",
        status,
        "",
        "**ВАШ БАЛАНС**",
        f"{_money(balance)} монет · REP: {rep_bal}",
    ]
    if item.description:
        lines += ["", _esc(item.description)]
    if confirm:
        lines += [
            "",
            "**Подтвердите покупку.**",
            f"Списание: {_money(item.price)} монет + {rep_price} REP",
            f"После покупки: ~{_money(max(0, balance - item.price))} монет, "
            f"REP ~{max(0, rep_bal - rep_price)}",
        ]
    return "\n".join(lines)


class _CardButton(discord.ui.Button):
    def __init__(self, action: str, label: str, style,
                 gid: int, uid: int, item_id: str,
                 back=None, confirm_item=None):
        super().__init__(label=label, style=style)
        self.action, self.gid, self.uid = action, gid, uid
        self.item_id = item_id
        self.back = back
        self.confirm_item = confirm_item

    async def callback(self, interaction: discord.Interaction) -> None:
        item = shop_store.get_item(self.item_id)
        gid, uid = self.gid, self.uid
        if self.action == "back":
            if self.back and self.back[0] == "browse":
                _, cat, sub, st = self.back
                await edit_card(interaction, await build_browse_view(
                    gid, uid, cat, st, sub=sub
                ))
            elif self.back and self.back[0] == "invcol":
                _, tf, pg = self.back
                from views.inventory2_view import build_collection_view
                await edit_card(interaction, await build_collection_view(
                    gid, uid, tf, pg
                ))
            elif self.back and self.back[0] == "main":
                await edit_card(interaction, await build_main_view(gid, uid))
            return
        if self.action == "fav":
            added = shop_ops_store.toggle_favorite(gid, uid, self.item_id)
            await edit_card(interaction, await build_item_card_view(
                gid, uid, item, back=self.back
            ))
            await interaction.followup.send(
                "Добавлено в избранное." if added else "Убрано из избранного.",
                ephemeral=True,
            )
            return
        if self.action == "buy_ask":
            await edit_card(interaction, await build_item_card_view(
                gid, uid, item, back=self.back, confirm=True
            ))
            return
        if self.action == "buy_cancel":
            await edit_card(interaction, await build_item_card_view(
                gid, uid, item, back=self.back
            ))
            return
        if self.action == "buy":
            await _do_purchase(interaction, gid, uid, item, self.back)
            return
        if self.action == "equip":
            await _do_equip(interaction, gid, uid, item, self.back)
            return
        if self.action == "activate":
            await _do_activate(interaction, gid, uid, item, self.back)
            return
        if self.action == "service":
            from views.nickname_view import show_nickname_service_card
            await show_nickname_service_card(interaction, item)
            return


async def _do_purchase(
    interaction: discord.Interaction,
    gid: int, uid: int, item: ShopItem, back,
) -> None:
    result = await purchase_svc.purchase(gid, uid, item.id, interaction.guild)
    if result.ok and not result.refunded:
        view = DesignView(title="ПОКУПКА УСПЕШНА", accent=colors.SUCCESS)
        granted = ", ".join(result.granted) or item.id
        view.text(
            f"**{_esc(item.name)}** — приобретено.\n"
            f"Списано: {_money(item.price)} монет + {result.rep_price} REP.\n"
            f"Выдано: {granted}"
        )
        row = discord.ui.ActionRow()
        b = discord.ui.Button(label="Назад к товару",
                              style=discord.ButtonStyle.secondary)

        async def _cb(i):
            await edit_card(i, await build_item_card_view(
                gid, uid, item, back=back
            ))
        b.callback = _cb
        row.add_item(b)
        view.container.add_item(row)
        await edit_card(interaction, view)
    else:
        view = DesignView(title="ПОКУПКА", accent=colors.ERROR)
        view.text(result.message)
        row = discord.ui.ActionRow()
        b = discord.ui.Button(label="Назад", style=discord.ButtonStyle.secondary)

        async def _cb2(i):
            await edit_card(i, await build_item_card_view(
                gid, uid, item, back=back
            ))
        b.callback = _cb2
        row.add_item(b)
        view.container.add_item(row)
        await edit_card(interaction, view)


async def _do_equip(
    interaction: discord.Interaction,
    gid: int, uid: int, item: ShopItem, back,
) -> None:
    cos = next(
        (c for c in inventory_store.get_player_inventory(gid, uid)
         if c.item_id == item.id), None,
    )
    if cos is not None and cos.equipped:
        inventory_store.unequip_cosmetic(gid, uid, item.id)
        msg = "Снято с экипировки."
    else:
        inventory_store.equip_cosmetic(gid, uid, item.id)
        msg = "Экипировано."
    await edit_card(interaction, await build_item_card_view(
        gid, uid, item, back=back
    ))
    await interaction.followup.send(msg, ephemeral=True)


async def _do_activate(
    interaction: discord.Interaction,
    gid: int, uid: int, item: ShopItem, back,
) -> None:
    guild = interaction.guild
    cos = next(
        (c for c in inventory_store.get_player_inventory(gid, uid)
         if c.item_id == item.id), None,
    )
    if cos is not None and cos.equipped:
        ok, warn = await purchase_svc.deactivate_color_role(
            guild, uid, item.id
        )
        msg = "Цвет снят." if ok else warn
    else:
        ok, warn = await purchase_svc.activate_color_role(
            guild, uid, item.id
        )
        msg = (
            f"Цвет «{item.name}» активирован."
            if ok else warn
        )
        if ok and warn:
            msg += " " + warn
    await edit_card(interaction, await build_item_card_view(
        gid, uid, item, back=back
    ))
    await interaction.followup.send(msg, ephemeral=True)


async def build_item_card_view(
    guild_id: int,
    user_id: int,
    item: ShopItem,
    back=None,
    confirm: bool = False,
    avatar_url: str | None = None,
) -> DesignView:
    body = await _card_body(guild_id, user_id, item, confirm)
    view = DesignView(title=None, accent=colors.CASE)
    if avatar_url:
        view.component(discord.ui.Section(
            discord.ui.TextDisplay(content=body),
            accessory=discord.ui.Thumbnail(media=avatar_url),
        ))
    else:
        view.text(body)
    view.sep()

    owned = any(
        c.item_id == item.id
        for c in inventory_store.get_player_inventory(guild_id, user_id)
    )
    kind = _kind_of(item)
    fav = shop_ops_store.is_favorite(guild_id, user_id, item.id)

    row = discord.ui.ActionRow()
    if confirm:
        row.add_item(_CardButton(
            "buy", f"Подтвердить — {_money(item.price)} + {rep_price_for_item(item)} REP",
            discord.ButtonStyle.success, guild_id, user_id, item.id, back))
        row.add_item(_CardButton(
            "buy_cancel", "Отмена", discord.ButtonStyle.secondary,
            guild_id, user_id, item.id, back))
    elif item.item_type == "service":
        row.add_item(_CardButton(
            "service", "Открыть услугу", discord.ButtonStyle.primary,
            guild_id, user_id, item.id, back))
    elif not owned:
        row.add_item(_CardButton(
            "buy_ask", f"Купить — {_money(item.price)} + {rep_price_for_item(item)} REP",
            discord.ButtonStyle.success, guild_id, user_id, item.id, back))
    elif kind in ("icon", "tag", "frame"):
        cos = next(
            c for c in inventory_store.get_player_inventory(
                guild_id, user_id
            ) if c.item_id == item.id
        )
        row.add_item(_CardButton(
            "equip", "Снять" if cos.equipped else "Экипировать",
            discord.ButtonStyle.primary, guild_id, user_id, item.id, back))
    elif kind == "color":
        cos = next(
            (c for c in inventory_store.get_player_inventory(
                guild_id, user_id
            ) if c.item_id == item.id), None,
        )
        row.add_item(_CardButton(
            "activate",
            "Снять цвет" if cos and cos.equipped else "Активировать",
            discord.ButtonStyle.primary, guild_id, user_id, item.id, back))
    # role/bundle-owned: без кнопки покупки (уже есть)

    if kind != "service":
        row.add_item(_CardButton(
            "fav", "★" if fav else "☆", discord.ButtonStyle.secondary,
            guild_id, user_id, item.id, back))
    row.add_item(_CardButton(
        "back", "Назад", discord.ButtonStyle.secondary,
        guild_id, user_id, item.id, back))
    view.container.add_item(row)
    return view


# ---------------------------------------------------------------- wardrobe


async def _open_wardrobe(interaction: discord.Interaction) -> None:
    """Открыть существующий гардероб FF отдельным ephemeral."""
    try:
        from cogs.wardrobe import WardrobeView
        from utils.design.adapter import compose
        view = WardrobeView(
            interaction.guild_id, interaction.user.id, mode="main"
        )
        embed = await view.build_embed()
        await interaction.response.send_message(
            view=compose(embed, view), ephemeral=True
        )
    except Exception:
        await interaction.response.send_message(
            "Гардероб временно недоступен.", ephemeral=True
        )


# ---------------------------------------------------------------- favorites


async def build_favorites_view(guild_id: int, user_id: int) -> DesignView:
    favs = shop_ops_store.favorites(guild_id, user_id)
    items = [
        it for it in (shop_store.get_item(i) for i in favs)
        if it is not None
    ]
    view = DesignView(title="ИЗБРАННОЕ", accent=colors.CASE)
    if not items:
        view.text(
            "**ИЗБРАННОЕ**\nУ вас пока нет товаров в избранном.\n"
            "Нажмите ☆ на карточке товара, чтобы добавить его."
        )
    else:
        view.text("**ИЗБРАННОЕ**\nВыберите товар:")
        view.sep()
        row = discord.ui.ActionRow()
        sel = _ItemSelect(items[:PAGE_SIZE], guild_id, user_id,
                          "favorites", None, {}, back=("main",))
        row.add_item(sel)
        view.container.add_item(row)
    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="В меню", style=discord.ButtonStyle.secondary)

    async def _cb(i):
        await edit_card(i, await build_main_view(guild_id, user_id))
    b.callback = _cb
    back.add_item(b)
    view.container.add_item(back)
    return view


# ---------------------------------------------------------------- purchases


_KIND_RU = {
    "purchase": "Покупка", "pet": "Питомец", "egg": "Яйцо",
    "case": "Кейс", "equip": "Экипировка",
}


async def build_purchases_view(
    guild_id: int, user_id: int, page: int = 0
) -> DesignView:
    ops = shop_ops_store.get_for_user(guild_id, user_id, limit=200)
    per = 15
    total_pages = max(1, (len(ops) + per - 1) // per)
    page = min(max(0, page), total_pages - 1)
    chunk = ops[page * per:(page + 1) * per]

    view = DesignView(title="МОИ ПОКУПКИ", accent=colors.CASE)
    if not ops:
        view.text("**МОИ ПОКУПКИ**\nОпераций пока нет.")
    else:
        lines = ["**МОИ ПОКУПКИ**", f"Страница {page + 1}/{total_pages}", ""]
        for op in chunk:
            ts = time.strftime(
                "%d.%m %H:%M", time.localtime(op.get("created_at", 0))
            )
            name = op.get("item_id", "?")
            item = shop_store.get_item(name)
            if item:
                name = item.name
            status = {
                "done": "✓", "refunded": "↩ возврат",
                "failed": "✗", "reserved": "…",
            }.get(op.get("status", ""), op.get("status", ""))
            lines.append(
                f"`{ts}` {_KIND_RU.get(op.get('kind'), op.get('kind'))} · "
                f"**{_esc(name)}** · {_money(op.get('price', 0))} · {status}"
            )
        view.text("\n".join(lines))

    nav = discord.ui.ActionRow()
    if page > 0:
        b = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)

        async def _prev(i):
            await edit_card(i, await build_purchases_view(
                guild_id, user_id, page - 1
            ))
        b.callback = _prev
        nav.add_item(b)
    if page < total_pages - 1:
        b2 = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)

        async def _next(i):
            await edit_card(i, await build_purchases_view(
                guild_id, user_id, page + 1
            ))
        b2.callback = _next
        nav.add_item(b2)
    back = discord.ui.Button(label="В меню", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_main_view(guild_id, user_id))
    back.callback = _back
    nav.add_item(back)
    view.container.add_item(nav)
    return view


# ---------------------------------------------------------------- pets


_PET_RARITY_ORDER = ["common", "uncommon", "epic", "legendary"]


async def build_pets_view(guild_id: int, user_id: int) -> DesignView:
    from models.pets_catalog import PETS_CATALOG, MYSTERY_EGG_DATA
    from storage.pet_store import pet_store

    has_pet = not await pet_store.has_pet_slot(user_id)
    view = DesignView(title="ПИТОМЦЫ", accent=colors.CASE)
    lines = [
        "**r!z3 · МАГАЗИН — ПИТОМЦЫ**",
        "Выберите питомца или яйцо загадки.",
        "",
        f"Статус: {'уже есть питомец' if has_pet else 'слот свободен'}",
    ]
    view.text("\n".join(lines))
    view.sep()

    options = [discord.SelectOption(
        label="Яйцо загадки", value="__egg__",
        description=(f"{_money(MYSTERY_EGG_DATA['price'])} монет + "
                f"{rep_price_for_amount(MYSTERY_EGG_DATA['price'])} REP · случайный питомец"),
    )]
    for pid, p in PETS_CATALOG.items():
        owned = has_pet  # один питомец — если есть, всех «нет в наличии слота»
        options.append(discord.SelectOption(
            label=p["name"][:100], value=pid,
            description=(
                f"{p['rarity']} · {_money(p['price'])} монет + "
                f"{rep_price_for_amount(p['price'])} REP"
            )[:100],
        ))
    sel = discord.ui.Select(
        placeholder="Выберите питомца", min_values=1, max_values=1,
        options=options,
    )

    async def _sel_cb(interaction):
        val = sel.values[0]
        if val == "__egg__":
            await edit_card(interaction, await _egg_card(
                guild_id, user_id
            ))
        else:
            await edit_card(interaction, await _pet_card(
                guild_id, user_id, val
            ))
    sel.callback = _sel_cb
    row = discord.ui.ActionRow()
    row.add_item(sel)
    view.container.add_item(row)

    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="В меню", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_main_view(guild_id, user_id))
    b.callback = _back
    back.add_item(b)
    view.container.add_item(back)
    return view


async def _pet_card(guild_id: int, user_id: int, pet_id: str) -> DesignView:
    from models.pets_catalog import PETS_CATALOG
    from storage.pet_store import pet_store
    from storage.user_balance_store import user_balance_store as ubs

    p = PETS_CATALOG.get(pet_id)
    if p is None:
        return await build_pets_view(guild_id, user_id)
    balance = await ubs.get_balance(guild_id, user_id)
    has_pet = not await pet_store.has_pet_slot(user_id)

    ab = p.get("abilities", {})
    ab_lines = []
    for key in sorted(ab):
        a = ab[key]
        val = a.get("max_val")
        unit = a.get("unit", "")
        desc = a.get("desc_template", "").format(val=val)
        ab_lines.append(f"**{a.get('name')}** — {desc}")

    rep_price = rep_price_for_amount(p["price"])
    rep_bal = 0
    try:
        rep_bal = await rep_balance_store.get_balance(guild_id, user_id)
    except Exception:
        rep_bal = 0
    body = (
        f"**ПИТОМЕЦ · {p['name'].upper()}**\n"
        f"Редкость: {p['rarity']} · Стоимость: **{_money(p['price'])}** монет + **{rep_price}** REP\n\n"
        f"{p.get('description', '')}\n\n"
        + ("\n".join(ab_lines) if ab_lines else "")
        + f"\n\n**ВАШ БАЛАНС**\n{_money(balance)} монет · REP: {rep_bal}\n"
        f"**СТАТУС**\n{'Слот занят — уже есть питомец' if has_pet else 'Доступен'}"
    )
    view = DesignView(title=None, accent=colors.CASE)
    img = p.get("image_url")
    if img:
        view.component(discord.ui.Section(
            discord.ui.TextDisplay(content=body),
            accessory=discord.ui.Thumbnail(media=img),
        ))
    else:
        view.text(body)
    view.sep()

    row = discord.ui.ActionRow()
    if not has_pet:
        buy = discord.ui.Button(
            label=f"Купить — {_money(p['price'])} + {rep_price} REP",
            style=discord.ButtonStyle.success,
        )

        async def _buy(i, _pid=pet_id, _price=p["price"]):
            ok, msg, pet = await purchase_svc.purchase_pet(
                guild_id, user_id, _pid, _price
            )
            res = DesignView(
                title="ПИТОМЕЦ", accent=colors.SUCCESS if ok else colors.ERROR
            )
            res.text(msg + (f"\n\n{pet.pet_name}" if pet else ""))
            bk = discord.ui.ActionRow()
            bb = discord.ui.Button(label="К питомцам",
                                   style=discord.ButtonStyle.secondary)

            async def _bk(j):
                await edit_card(j, await build_pets_view(guild_id, user_id))
            bb.callback = _bk
            bk.add_item(bb)
            res.container.add_item(bk)
            await edit_card(i, res)
        buy.callback = _buy
        row.add_item(buy)
    back = discord.ui.Button(label="Назад", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_pets_view(guild_id, user_id))
    back.callback = _back
    row.add_item(back)
    view.container.add_item(row)
    return view


async def _egg_card(guild_id: int, user_id: int) -> DesignView:
    from models.pets_catalog import MYSTERY_EGG_DATA
    from storage.pet_store import pet_store

    balance = await user_balance_store.get_balance(guild_id, user_id)
    has_pet = not await pet_store.has_pet_slot(user_id)
    chances = MYSTERY_EGG_DATA.get("absolute_chances", {})
    chance_txt = "\n".join(
        f"{k}: {v * 100:.1f}%" for k, v in chances.items()
    )
    egg_price = MYSTERY_EGG_DATA["price"]
    egg_rep = rep_price_for_amount(egg_price)
    rep_bal = 0
    try:
        rep_bal = await rep_balance_store.get_balance(guild_id, user_id)
    except Exception:
        rep_bal = 0
    body = (
        "**ПИТОМЕЦ · ЗАГАДОЧНОЕ ЯЙЦО**\n"
        f"Стоимость: **{_money(egg_price)}** монет + **{egg_rep}** REP\n\n"
        f"{MYSTERY_EGG_DATA.get('description', '')}\n\n"
        f"**ШАНСЫ**\n{chance_txt}\n\n"
        f"**ВАШ БАЛАНС**\n{_money(balance)} монет · REP: {rep_bal}\n"
        f"**СТАТУС**\n{'Слот занят' if has_pet else 'Доступно'}"
    )
    view = DesignView(title=None, accent=colors.CASE)
    img = MYSTERY_EGG_DATA.get("image_url")
    if img:
        view.component(discord.ui.Section(
            discord.ui.TextDisplay(content=body),
            accessory=discord.ui.Thumbnail(media=img),
        ))
    else:
        view.text(body)
    view.sep()
    row = discord.ui.ActionRow()
    if not has_pet:
        buy = discord.ui.Button(
            label=f"Открыть — {_money(egg_price)} + {egg_rep} REP",
            style=discord.ButtonStyle.success,
        )

        async def _buy(i):
            ok, msg, pet = await purchase_svc.open_egg_safe(
                guild_id, user_id, MYSTERY_EGG_DATA["price"]
            )
            res = DesignView(
                title="ЯЙЦО", accent=colors.SUCCESS if ok else colors.ERROR
            )
            res.text(msg + (f"\n\n{pet.pet_name}" if pet else ""))
            bk = discord.ui.ActionRow()
            bb = discord.ui.Button(label="К питомцам",
                                   style=discord.ButtonStyle.secondary)

            async def _bk(j):
                await edit_card(j, await build_pets_view(guild_id, user_id))
            bb.callback = _bk
            bk.add_item(bb)
            res.container.add_item(bk)
            await edit_card(i, res)
        buy.callback = _buy
        row.add_item(buy)
    back = discord.ui.Button(label="Назад", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_pets_view(guild_id, user_id))
    back.callback = _back
    row.add_item(back)
    view.container.add_item(row)
    return view


# ---------------------------------------------------------------- cases


def _case_odds(case) -> dict[str, float]:
    """Реальные шансы из drop_rates (нормализованные)."""
    rates = case.drop_rates or {}
    total = sum(rates.values()) or 1.0
    coins = sum(v for k, v in rates.items() if k.startswith("coins_"))
    items = sum(v for k, v in rates.items() if k.startswith("item_"))
    nothing = rates.get("nothing", 0.0)
    return {
        "coins": coins / total * 100,
        "item": items / total * 100,
        "nothing": nothing / total * 100,
    }


def _case_rewards_preview(case) -> list[str]:
    lines = []
    for key in sorted(case.drop_rates or {}):
        rate = case.drop_rates[key]
        if key.startswith("coins_"):
            amount = key.split("_", 1)[1]
            lines.append(f"Монеты ×{amount} — {rate * 100:.0f}%")
        elif key.startswith("item_"):
            rarity = key.split("_", 1)[1].upper()
            lines.append(f"Предмет ({rarity}) — {rate * 100:.0f}%")
        elif key == "nothing":
            lines.append(f"Ничего — {rate * 100:.0f}%")
    return lines


async def build_cases_view(guild_id: int, user_id: int) -> DesignView:
    from storage.case_store import case_store

    cases = [c for c in case_store.get_all_cases() if c.is_active]
    view = DesignView(title="КЕЙСЫ", accent=colors.CASE)
    view.text(
        "**r!z3 · МАГАЗИН — КЕЙСЫ**\nВыберите кейс для просмотра шансов."
    )
    view.sep()
    if not cases:
        view.text("Активных кейсов нет.")
    else:
        row = discord.ui.ActionRow()
        sel = discord.ui.Select(
            placeholder="Выберите кейс", min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=c.name[:100], value=c.id,
                    description=f"{_money(c.price)} монет"[:100],
                ) for c in cases[:25]
            ],
        )

        async def _sel_cb(interaction):
            await edit_card(interaction, await _case_card(
                guild_id, user_id, sel.values[0]
            ))
        sel.callback = _sel_cb
        row.add_item(sel)
        view.container.add_item(row)
    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="В меню", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_main_view(guild_id, user_id))
    b.callback = _back
    back.add_item(b)
    view.container.add_item(back)
    return view


async def _case_card(
    guild_id: int, user_id: int, case_id: str, confirm: bool = False,
    result: dict | None = None,
) -> DesignView:
    from storage.case_store import case_store

    case = case_store.get_case(case_id)
    if case is None:
        return await build_cases_view(guild_id, user_id)

    if result is not None:
        return _case_result_view(guild_id, user_id, case, result)

    odds = _case_odds(case)
    balance = await user_balance_store.get_balance(guild_id, user_id)
    rewards = _case_rewards_preview(case)
    case_rep = rep_price_for_amount(case.price)
    rep_bal = 0
    try:
        rep_bal = await rep_balance_store.get_balance(guild_id, user_id)
    except Exception:
        rep_bal = 0
    body = (
        f"**КЕЙС · {case.name.upper()}**\n"
        f"Стоимость: **{_money(case.price)}** монет + **{case_rep}** REP\n\n"
        f"**ШАНСЫ**\n"
        f"Монеты: {odds['coins']:.0f}% · "
        f"Предмет: {odds['item']:.0f}% · "
        f"Ничего: {odds['nothing']:.0f}%\n\n"
        f"**ВОЗМОЖНЫЕ НАГРАДЫ**\n" + "\n".join(rewards[:10])
        + f"\n\n**ВАШ БАЛАНС**\n{_money(balance)} монет · REP: {rep_bal}"
    )
    if confirm:
        body += "\n\n**Подтвердите открытие кейса.**"
    view = DesignView(title=None, accent=colors.CASE)
    view.text(body)
    view.sep()
    row = discord.ui.ActionRow()
    if confirm:
        open_btn = discord.ui.Button(
            label=f"Открыть — {_money(case.price)} + {case_rep} REP",
            style=discord.ButtonStyle.success,
        )

        async def _open(i):
            if getattr(open_btn, "_busy", False):
                return
            open_btn._busy = True
            try:
                res = await purchase_svc.open_case_safe(
                    guild_id, user_id, case_id, i.guild
                )
                await edit_card(i, await _case_card(
                    guild_id, user_id, case_id, result=res
                ))
            except Exception:
                open_btn._busy = False
                if not i.response.is_done():
                    await i.response.send_message(
                        "Ошибка открытия кейса.", ephemeral=True
                    )
        open_btn.callback = _open
        row.add_item(open_btn)
        cancel = discord.ui.Button(label="Отмена",
                                   style=discord.ButtonStyle.secondary)

        async def _cancel(i):
            await edit_card(i, await _case_card(
                guild_id, user_id, case_id
            ))
        cancel.callback = _cancel
        row.add_item(cancel)
    else:
        ask = discord.ui.Button(
            label=f"Открыть за {_money(case.price)} + {case_rep} REP",
            style=discord.ButtonStyle.primary,
        )

        async def _ask(i):
            await edit_card(i, await _case_card(
                guild_id, user_id, case_id, confirm=True
            ))
        ask.callback = _ask
        row.add_item(ask)
    back = discord.ui.Button(label="Назад", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_cases_view(guild_id, user_id))
    back.callback = _back
    row.add_item(back)
    view.container.add_item(row)
    return view


def _case_result_view(guild_id, user_id, case, result: dict) -> DesignView:
    if not isinstance(result, dict) or result.get("type") == "error":
        view = DesignView(title="КЕЙС", accent=colors.ERROR)
        view.text(
            f"**КЕЙС · {case.name}**\n"
            f"Не удалось открыть: {result.get('value', 'ошибка')}"
        )
    else:
        rtype = result.get("type")
        if rtype == "coins":
            text = f"Выигрыш: **{result.get('value')}** монет"
        elif rtype == "item":
            text = f"Выигрыш: предмет **{result.get('value')}**"
        else:
            text = "Ничего — повезёт в следующий раз."
        view = DesignView(title="КЕЙС ОТКРЫТ", accent=colors.SUCCESS)
        view.text(f"**КЕЙС · {case.name}**\n{text}")
    row = discord.ui.ActionRow()
    again = discord.ui.Button(label="Открыть ещё",
                              style=discord.ButtonStyle.primary)

    async def _again(i):
        await edit_card(i, await _case_card(
            guild_id, user_id, case.id, confirm=True
        ))
    again.callback = _again
    row.add_item(again)
    back = discord.ui.Button(label="К кейсам", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_cases_view(guild_id, user_id))
    back.callback = _back
    row.add_item(back)
    view.container.add_item(row)
    return view


# ---------------------------------------------------------------- services


async def build_services_view(guild_id: int, user_id: int) -> DesignView:
    items = shop_store.get_items_by_category("services")
    view = DesignView(title="УСЛУГИ", accent=colors.CASE)
    view.text("**r!z3 · МАГАЗИН — УСЛУГИ**\nВыберите услугу:")
    view.sep()
    row = discord.ui.ActionRow()
    sel = _ItemSelect(items, guild_id, user_id, "services", None, {})
    row.add_item(sel)
    view.container.add_item(row)
    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="В меню", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_main_view(guild_id, user_id))
    b.callback = _back
    back.add_item(b)
    view.container.add_item(back)
    return view


# ---------------------------------------------------------------- color roles


async def build_color_roles_view(guild_id: int, user_id: int) -> DesignView:
    """Цветные роли — это обычный browse по категории color_roles."""
    return await build_browse_view(guild_id, user_id, "color_roles", {})


# ---------------------------------------------------------------- admin


_ADMIN_PAGE = 25  # лимит опций Discord Select


async def build_admin_view(
    guild_id: int, user_id: int, page: int = 0
) -> DesignView:
    # Все товары включая скрытые — владелец должен видеть
    # is_active=False и иметь возможность вернуть товар в продажу.
    items = sorted(
        shop_store.get_all_items_raw(), key=lambda i: i.id
    )
    total_pages = max(1, (len(items) + _ADMIN_PAGE - 1) // _ADMIN_PAGE)
    page = min(max(0, page), total_pages - 1)
    chunk = items[page * _ADMIN_PAGE:(page + 1) * _ADMIN_PAGE]
    view = DesignView(title="УПРАВЛЕНИЕ КАТАЛОГОМ", accent=colors.ERROR)
    view.text(
        "**УПРАВЛЕНИЕ КАТАЛОГОМ**\n"
        f"Товаров: {len(items)} · Страница {page + 1}/{total_pages}\n"
        "Выберите товар для изменения. Действия журналируются."
    )
    view.sep()
    if chunk:
        row = discord.ui.ActionRow()
        sel = discord.ui.Select(
            placeholder="Товар", min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=("🟢 " if it.is_active else "⚫ ") + it.name[:95],
                    value=it.id,
                    description=(
                        f"{_money(it.price)} · {it.category}"
                    )[:100],
                ) for it in chunk
            ],
        )

        async def _sel_cb(interaction):
            if not await _require_owner(interaction):
                return
            item = shop_store.get_item(sel.values[0])
            if item:
                await edit_card(interaction, await _admin_item_view(
                    guild_id, user_id, item, page=page
                ))
        sel.callback = _sel_cb
        row.add_item(sel)
        view.container.add_item(row)
    nav = discord.ui.ActionRow()
    if page > 0:
        prev = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)

        async def _prev(i):
            if not await _require_owner(i):
                return
            await edit_card(i, await build_admin_view(
                guild_id, user_id, page - 1
            ))
        prev.callback = _prev
        nav.add_item(prev)
    if page < total_pages - 1:
        nxt = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)

        async def _next(i):
            if not await _require_owner(i):
                return
            await edit_card(i, await build_admin_view(
                guild_id, user_id, page + 1
            ))
        nxt.callback = _next
        nav.add_item(nxt)
    if len(nav.children) > 0:
        view.container.add_item(nav)
    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="В меню", style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_main_view(guild_id, user_id))
    b.callback = _back
    back.add_item(b)
    view.container.add_item(back)

    journal = discord.ui.ActionRow()
    jb = discord.ui.Button(label="ЖУРНАЛ ОПЕРАЦИЙ",
                           style=discord.ButtonStyle.secondary)

    async def _journal(i):
        if not await _require_owner(i):
            return
        await edit_card(i, await _admin_journal_view(guild_id))
    jb.callback = _journal
    journal.add_item(jb)
    view.container.add_item(journal)
    return view


async def _admin_journal_view(guild_id: int) -> DesignView:
    ops = shop_ops_store.recent(guild_id=guild_id, limit=30)
    view = DesignView(title="ЖУРНАЛ ОПЕРАЦИЙ", accent=colors.ERROR)
    if not ops:
        view.text("Операций пока нет.")
    else:
        lines = ["**ЖУРНАЛ ОПЕРАЦИЙ** (последние 30)", ""]
        for op in ops:
            ts = time.strftime(
                "%d.%m %H:%M", time.localtime(op.get("created_at", 0))
            )
            lines.append(
                f"`{ts}` u{op.get('user_id')} · "
                f"{_KIND_RU.get(op.get('kind'), op.get('kind'))} · "
                f"**{_esc(op.get('item_id', '?'))}** · "
                f"{_money(op.get('price', 0))} · {op.get('status')}"
            )
        view.text("\n".join(lines))
    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="К управлению",
                          style=discord.ButtonStyle.secondary)

    async def _back(i):
        if not await _require_owner(i):
            return
        await edit_card(i, await build_admin_view(
            guild_id, i.user.id
        ))
    b.callback = _back
    back.add_item(b)
    view.container.add_item(back)
    return view


async def _persist_admin_item(
    guild_id: int, user_id: int, new_item: ShopItem, detail: str,
) -> bool:
    """Сохранить правку каталога в PG с подтверждением.

    True — изменение записано в PostgreSQL и применено к кэшу.
    False — запись не подтверждена: кэш НЕ меняем, пользователь
    получает ошибку (следующий sync_to_store не затрёт PG).
    """
    from storage import shop_catalog

    ok = await shop_catalog.save_item_confirmed(
        new_item, actor_id=user_id, source="discord_admin",
    )
    if not ok:
        return False
    shop_store.upsert_item(new_item)
    shop_ops_store.record(
        guild_id=guild_id, user_id=user_id, kind="admin",
        item_id=new_item.id, price=new_item.price, status="done",
        detail=detail,
    )
    return True


async def _admin_item_view(
    guild_id: int, user_id: int, item: ShopItem, page: int = 0
) -> DesignView:
    view = DesignView(title="ТОВАР", accent=colors.ERROR)
    view.text(
        f"**{_esc(item.name)}** (`{item.id}`)\n"
        f"Категория: {item.category} · Тип: {item.item_type}\n"
        f"Цена: {_money(item.price)} монет + "
        f"{rep_price_for_item(item)} REP · Редкость: {item.rarity.value}\n"
        f"Статус: {'активен' if item.is_active else 'СКРЫТ'}\n"
        f"Сезон: {item.available_from or '—'} — {item.available_until or '—'}"
    )
    view.sep()
    row = discord.ui.ActionRow()

    toggle = discord.ui.Button(
        label="Скрыть" if item.is_active else "Включить",
        style=discord.ButtonStyle.secondary,
    )

    async def _toggle(i):
        if not await _require_owner(i):
            return
        new_item = replace(item, is_active=not item.is_active)
        ok = await _persist_admin_item(
            guild_id, user_id, new_item, f"active={new_item.is_active}"
        )
        if not ok:
            await i.response.send_message(
                "Не удалось сохранить изменение в базе данных.",
                ephemeral=True,
            )
            return
        await edit_card(i, await _admin_item_view(
            guild_id, user_id, new_item
        ))
    toggle.callback = _toggle
    row.add_item(toggle)

    price_btn = discord.ui.Button(label="Цена", style=discord.ButtonStyle.secondary)

    class _PriceModal(discord.ui.Modal, title="Новая цена"):
        val = discord.ui.TextInput(label="Цена (монеты)", max_length=9)

        async def on_submit(self, interaction):
            if not await _require_owner(interaction):
                return
            try:
                new_price = int(self.val.value.strip())
                if new_price < 0:
                    raise ValueError
            except ValueError:
                await interaction.response.send_message(
                    "Некорректная цена.", ephemeral=True
                )
                return
            new_item = replace(item, price=new_price)
            ok = await _persist_admin_item(
                guild_id, user_id, new_item, "price_change"
            )
            if not ok:
                await interaction.response.send_message(
                    "Не удалось сохранить изменение в базе данных.",
                    ephemeral=True,
                )
                return
            await edit_card(interaction, await _admin_item_view(
                guild_id, user_id, new_item, page=page
            ))

    async def _price(i):
        await i.response.send_modal(_PriceModal())
    price_btn.callback = _price
    row.add_item(price_btn)

    rep_btn = discord.ui.Button(label="REP-цена", style=discord.ButtonStyle.secondary)

    class _RepModal(discord.ui.Modal, title="REP-цена товара"):
        val = discord.ui.TextInput(
            label="REP (пусто = по тарифу)", required=False, max_length=4
        )

        async def on_submit(self, interaction):
            if not await _require_owner(interaction):
                return
            raw = self.val.value.strip()
            rep_price = item.rep_price
            if raw == "":
                rep_price = None
            else:
                try:
                    rep_price = int(raw)
                    if rep_price < 0 or rep_price > 999:
                        raise ValueError
                    if item.price > 0 and rep_price == 0:
                        raise ValueError("paid_free_rep")
                except ValueError as ve:
                    if str(ve) == "paid_free_rep":
                        msg = (
                            "Платный товар не может иметь REP-цену 0 — "
                            "двойная оплата обязательна."
                        )
                    else:
                        msg = "REP: целое 1–999 или пусто (=тариф)."
                    await interaction.response.send_message(
                        msg, ephemeral=True
                    )
                    return
            new_item = replace(item, rep_price=rep_price)
            ok = await _persist_admin_item(
                guild_id, user_id, new_item,
                f"rep_price={new_item.rep_price}",
            )
            if not ok:
                await interaction.response.send_message(
                    "Не удалось сохранить изменение в базе данных.",
                    ephemeral=True,
                )
                return
            await edit_card(interaction, await _admin_item_view(
                guild_id, user_id, new_item, page=page
            ))

    async def _rep(i):
        await i.response.send_modal(_RepModal())
    rep_btn.callback = _rep
    row.add_item(rep_btn)

    season_btn = discord.ui.Button(label="Сезон", style=discord.ButtonStyle.secondary)

    class _SeasonModal(discord.ui.Modal, title="Период продажи"):
        start = discord.ui.TextInput(
            label="С (YYYY-MM-DD или пусто)", required=False, max_length=30
        )
        end = discord.ui.TextInput(
            label="До (YYYY-MM-DD или пусто)", required=False, max_length=30
        )

        async def on_submit(self, interaction):
            if not await _require_owner(interaction):
                return
            new_item = replace(
                item,
                available_from=self.start.value.strip() or None,
                available_until=self.end.value.strip() or None,
            )
            ok = await _persist_admin_item(
                guild_id, user_id, new_item,
                f"season {new_item.available_from}..{new_item.available_until}",
            )
            if not ok:
                await interaction.response.send_message(
                    "Не удалось сохранить изменение в базе данных.",
                    ephemeral=True,
                )
                return
            await edit_card(interaction, await _admin_item_view(
                guild_id, user_id, new_item, page=page
            ))

    async def _season(i):
        await i.response.send_modal(_SeasonModal())
    season_btn.callback = _season
    row.add_item(season_btn)
    view.container.add_item(row)

    back = discord.ui.ActionRow()
    b = discord.ui.Button(label="К списку", style=discord.ButtonStyle.secondary)

    async def _back(i):
        if not await _require_owner(i):
            return
        await edit_card(i, await build_admin_view(guild_id, user_id, page=page))
    b.callback = _back
    back.add_item(b)
    view.container.add_item(back)
    return view


# ---------------------------------------------------------------- entry points


_CATEGORY_BUILDERS = {
    "color_roles": build_color_roles_view,
    "pets": build_pets_view,
    "cases": build_cases_view,
    "services": build_services_view,
}


async def open_category(
    interaction: discord.Interaction, category: str
) -> None:
    """Прямой вход в категорию (совместимость со старыми вызовами)."""
    gid, uid = interaction.guild_id, interaction.user.id
    if category == "wardrobe":
        await _open_wardrobe(interaction)
        return
    if category == "personal":
        await edit_card(interaction, await build_subcat_view(gid, uid))
        return
    builder = _CATEGORY_BUILDERS.get(category)
    if builder is None:
        builder = lambda g, u: build_browse_view(g, u, category, {})
    await interaction.response.send_message(
        view=await builder(gid, uid), ephemeral=True
    )
