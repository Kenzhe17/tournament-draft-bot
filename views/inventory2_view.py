"""SHOP 2.0 — CV2-инвентарь `/inventory`.

Экраны: сводка экипировки → коллекция (фильтр по типу +
пагинация) → карточка предмета (экипировка/снятие/цвета) →
история покупок / гардероб / назад в магазин.
"""

from __future__ import annotations

import discord

from models.shop_item import ShopItem
from storage.shop_ops_store import shop_ops_store
from storage.shop_store import inventory_store, shop_store
from storage.user_balance_store import user_balance_store
from utils.design import colors
from utils.design.layouts import DesignView, edit_card

PAGE_SIZE = 25

_TYPE_FILTERS = [
    ("all", "ВСЕ"),
    ("tag", "ТЕГИ"),
    ("icon", "ЗНАЧКИ"),
    ("frame", "РАМКИ"),
    ("color", "ЦВЕТА"),
    ("role", "РОЛИ"),
    ("bundle", "НАБОРЫ"),
]


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _esc(s: str) -> str:
    return str(s).replace("*", "\\*").replace("_", "\\_").replace("`", "\\`")


def _kind_of(item: ShopItem) -> str:
    if item.bundle:
        return "bundle"
    if item.item_type == "service":
        return "service"
    if item.item_type == "role":
        return "color" if item.category == "color_roles" else "role"
    return item.cosmetic_type.value


def _owned_items(guild_id: int, user_id: int) -> list[tuple[object, ShopItem]]:
    """(PlayerCosmetic, ShopItem) только для существующих товаров."""
    out = []
    for cos in inventory_store.get_player_inventory(guild_id, user_id):
        item = shop_store.get_item(cos.item_id)
        if item is not None and item.item_type != "service":
            out.append((cos, item))
    return out


def _equipped_lines(guild_id: int, user_id: int) -> list[str]:
    lines = []
    for cos, item in _owned_items(guild_id, user_id):
        if not cos.equipped:
            continue
        kind = _kind_of(item)
        if kind == "tag":
            lines.append(f"Тег: **{item.value}** — {_esc(item.name)}")
        elif kind == "icon":
            try:
                from config import get_emoji
                icon = get_emoji(item.value) or ""
            except Exception:
                icon = ""
            lines.append(f"Значок: {icon} {_esc(item.name)}")
        elif kind == "frame":
            lines.append(f"Рамка: {item.value} {_esc(item.name)}")
        elif kind == "color":
            lines.append(f"Цвет ника: {_esc(item.name)} `{item.value}`")
        elif kind == "role":
            lines.append(f"Роль: {_esc(item.name)}")
    return lines or ["Ничего не экипировано"]


async def build_inventory_view(guild_id: int, user_id: int) -> DesignView:
    balance = await user_balance_store.get_balance(guild_id, user_id)
    owned = _owned_items(guild_id, user_id)
    equipped_count = sum(1 for c, _ in owned if c.equipped)

    # Активный образ Free Fire (если есть)
    ff_line = ""
    try:
        from storage.wardrobe_store import wardrobe_store
        from services import ff_catalog
        ff_id = wardrobe_store.get_equipped_id(guild_id, user_id)
        if ff_id:
            ff_item = ff_catalog.get_item(ff_id)
            if ff_item:
                ff_line = f"Образ FF: **{_esc(ff_item.get('name', ff_id))}**\n"
    except Exception:
        pass

    view = DesignView(title="ИНВЕНТАРЬ", accent=colors.CASE)
    view.text(
        "**r!z3 · ИНВЕНТАРЬ**\n"
        f"Баланс: **{_money(balance)}** монет · "
        f"Предметов: {len(owned)} · Экипировано: {equipped_count}\n\n"
        "**ЭКИПИРОВАНО**\n" + "\n".join(_equipped_lines(guild_id, user_id))
        + ("\n\n" + ff_line.rstrip() if ff_line else "")
    )
    view.sep()
    view.text("**РАЗДЕЛЫ**")

    row = discord.ui.ActionRow()
    for key, label in [
        ("collection", "КОЛЛЕКЦИЯ"),
        ("favorites", "ИЗБРАННОЕ"),
        ("history", "ИСТОРИЯ"),
        ("wardrobe", "ГАРДЕРОБ"),
        ("shop", "В МАГАЗИН"),
    ]:
        btn = discord.ui.Button(
            label=label, style=discord.ButtonStyle.secondary
        )

        async def _cb(interaction, _key=key):
            if _key == "collection":
                await edit_card(interaction, await build_collection_view(
                    guild_id, user_id
                ))
            elif _key == "favorites":
                from views.shop2_view import build_favorites_view
                await edit_card(interaction, await build_favorites_view(
                    guild_id, user_id
                ))
            elif _key == "history":
                from views.shop2_view import build_purchases_view
                await edit_card(interaction, await build_purchases_view(
                    guild_id, user_id
                ))
            elif _key == "wardrobe":
                from views.shop2_view import _open_wardrobe
                await _open_wardrobe(interaction)
            elif _key == "shop":
                from views.shop2_view import build_main_view
                await edit_card(interaction, await build_main_view(
                    guild_id, user_id
                ))
        btn.callback = _cb
        row.add_item(btn)
    view.container.add_item(row)
    view.text(
        "Питомцы управляются через `/pet`. "
        "Цветные роли назначаются кнопкой «Активировать» в коллекции."
    )
    return view


async def build_collection_view(
    guild_id: int,
    user_id: int,
    type_filter: str = "all",
    page: int = 0,
) -> DesignView:
    owned = [
        (cos, item) for cos, item in _owned_items(guild_id, user_id)
        if type_filter == "all" or _kind_of(item) == type_filter
    ]
    # Стабильная сортировка: экипированные первыми, затем по имени
    owned.sort(key=lambda p: (not p[0].equipped, p[1].name.lower()))

    total_pages = max(1, (len(owned) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    chunk = owned[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    filt_label = dict(_TYPE_FILTERS).get(type_filter, type_filter)
    view = DesignView(title="КОЛЛЕКЦИЯ", accent=colors.CASE)
    view.text(
        f"**КОЛЛЕКЦИЯ** · {filt_label}\n"
        f"Предметов: {len(owned)} · Страница {page + 1}/{total_pages}"
    )
    view.sep()

    if chunk:
        sel = discord.ui.Select(
            placeholder="Выберите предмет", min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label=("✓ " if cos.equipped else "") + item.name[:97],
                    value=item.id,
                    description=(
                        f"{_kind_of(item)} · "
                        f"{'экипирован' if cos.equipped else 'в коллекции'}"
                    )[:100],
                ) for cos, item in chunk
            ],
        )

        async def _sel_cb(interaction):
            item = shop_store.get_item(sel.values[0])
            if item is None:
                await interaction.response.send_message(
                    "Предмет не найден.", ephemeral=True
                )
                return
            from views.shop2_view import build_item_card_view
            await edit_card(interaction, await build_item_card_view(
                guild_id, user_id, item,
                back=("invcol", type_filter, page),
                avatar_url=None,
            ))
        sel.callback = _sel_cb
        row = discord.ui.ActionRow()
        row.add_item(sel)
        view.container.add_item(row)
    else:
        view.text("В этой категории пусто.")

    nav = discord.ui.ActionRow()
    if page > 0:
        b = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)

        async def _prev(i):
            await edit_card(i, await build_collection_view(
                guild_id, user_id, type_filter, page - 1
            ))
        b.callback = _prev
        nav.add_item(b)
    if page < total_pages - 1:
        b2 = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)

        async def _next(i):
            await edit_card(i, await build_collection_view(
                guild_id, user_id, type_filter, page + 1
            ))
        b2.callback = _next
        nav.add_item(b2)
    # Пустой ActionRow запрещён Discord (50035) — добавляем только
    # когда есть хотя бы одна кнопка навигации.
    if nav.children:
        view.container.add_item(nav)

    frow = discord.ui.ActionRow()
    fsel = discord.ui.Select(
        placeholder="Фильтр типа", min_values=1, max_values=1,
        options=[
            discord.SelectOption(label=label, value=key)
            for key, label in _TYPE_FILTERS
        ],
    )

    async def _fsel_cb(interaction):
        await edit_card(interaction, await build_collection_view(
            guild_id, user_id, fsel.values[0], 0
        ))
    fsel.callback = _fsel_cb
    frow.add_item(fsel)
    view.container.add_item(frow)

    nav2 = discord.ui.ActionRow()
    back = discord.ui.Button(label="В инвентарь",
                             style=discord.ButtonStyle.secondary)

    async def _back(i):
        await edit_card(i, await build_inventory_view(guild_id, user_id))
    back.callback = _back
    nav2.add_item(back)
    view.container.add_item(nav2)
    return view
