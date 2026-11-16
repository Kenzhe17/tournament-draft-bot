# SHOP 2.0 — аудит (feat/shop-2.0)

## Хранилища

- Каталог: `data/shop_items.json` ← `storage/shop_store.py::ShopStore` (in-memory dict + JSON).
  `_use_db` — мёртвый флаг, PostgreSQL не используется.
- Инвентарь: `data/player_inventory.json` ← `InventoryStore`, ключ `"guild:user"` → `[PlayerCosmetic{item_id, equipped}]`.
  Экипировка: один предмет на `cosmetic_type` (tag/icon/color/frame независимо).
- Баланс: PG `user_balance`, `subtract_balance`/`add_balance` атомарны (`FOR UPDATE`), `add_balance_once` — идемпотентные гранты.
- Услуга «Смена ника»: `nickname_ops_store` (reserved→applied/refunded/unknown) + per-user lock + `recover_pending` — эталонная схема.
- Кейсы: `data/cases.json` + `data/case_history.json`, всё синхронно-файловое; PG-таблицы `cases`/`case_history` созданы, но не используются.
- Питомцы: PG `pets`, один питомец на пользователя (app-level `has_pet_slot`, без unique constraint).
- Гардероб FF: `data/wardrobes.json`, один «образ» на пользователя; `/player wardrobe` выключен (декоратор закомментирован).

## Пути покупки/продажи/выдачи

| Путь | Файл | Защита |
|---|---|---|
| `/shop` → BuyButton (иконки/теги/роли) | views/shop_view.py:824 | pre-check → subtract → grant; ГОНКА двойного списания |
| `/shop` → BuyCaseButton | views/shop_view.py:953 | open_case сам списывает; `if result:` ловит error-dict как успех |
| `/shop` → BuyPetButton / OpenEggButton | views/shop_view.py:1804/1879 | has_pet_slot→subtract→create_pet; гонка |
| `/economy sell` | cogs/sell.py | remove→add_balance неатомарно; роль не снимается |
| `/economy gift` | cogs/tournament.py:2532 | fee→transfer_cosmetic→refund |
| nickname service | services/nickname_service.py | lock + op-state + идемпотентный refund |
| wardrobe on_buy | cogs/wardrobe.py:467 | subtract→add без recheck; двойное списание |
| case item drop | storage/case_store.py:293 | add_cosmetic после pre-check — гонка с другими покупками |

## Критические дефекты (исправлены в SHOP 2.0)

1. `shop_items.py:13` `clear_all()` при каждом импорте — стирает каталог.
2. Нет идемпотентности покупок косметики (нет operation_id).
3. Шансы кейсов захардкожены неверно (50/20/30 вместо 40/10/50).
4. `_update_drop_rates_if_needed` перезаписывает кастомные drop_rates при загрузке.
5. `BuyCaseButton` принимает `{"type":"error"}` за успешное открытие.
6. Питомцы: двойной клик → 2 питомца (нет unique constraint на owners).
7. `add_balance` рефанд упирается в MAX_BALANCE=100000 — часть возврата теряется.
8. Продажа предмета-«роли» не снимает Discord-роль.
9. Лимит инвентаря 20 — только текст, нигде не применяется.
10. `CosmeticType.FRAME` — заглушка без предметов и рендера.

## Состав каталога (до изменений)

- icons: 20 шт (8 basic@700, 4 premium@1750, 6 elite@3500, 2 special@5950)
- tags: 17 шт (3@700, 9@1750, 3@3500, 3@5950)
- services: nickname_change@1000 (item_type="service")
- roles: 0 шт; color_roles: 0; frames: 0; bundles: 0
- cases: basic@200, premium@500, elite@1000, special@2000
- pets: 7 видов + mystery egg@500
- wardrobe FF: ~34k предметов, цены по rarity 200–100k
