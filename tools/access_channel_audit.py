"""Read-only диагностика каналов ротации доступа (TZ §6).

Сканирует все текстовые каналы сервера, имена которых похожи на
канал панели ротации, и проверяет наличие в них сообщения панели
(компоненты с custom_id ``access:*`` от бота). Ничего не создаёт,
не изменяет и не удаляет — только читает через Discord API.

Запуск:
    set DISCORD_TOKEN=...   (Windows) / export DISCORD_TOKEN=...
    python tools/access_channel_audit.py [GUILD_ID]

Отчёт: ID канала, название, категория, наличие панели и её
message ID. Основной канал — ACCESS_ROTATION_CHANNEL_ID
(по умолчанию 1573700340417699890).
"""

from __future__ import annotations

import asyncio
import os
import sys

import discord

PINNED_CHANNEL_ID = int(
    os.getenv("ACCESS_ROTATION_CHANNEL_ID") or "1573700340417699890"
)

# Названия-кандидаты: точные и «украшенные» варианты.
NAME_HINTS = (
    "доступ-ротация",
    "access-rotation",
    "access_rotation",
    "доступ",
    "ротация",
)

_PANEL_CUSTOM_IDS = {
    "access:join", "access:leave", "access:guide", "access:orgpanel",
}


def _normalized(name: str) -> str:
    return "".join(
        ch for ch in name.lower() if ch.isalnum() or ch in "-_"
    )


def _is_candidate(channel: discord.TextChannel) -> bool:
    if channel.id == PINNED_CHANNEL_ID:
        return True
    norm = _normalized(channel.name)
    return any(h in norm for h in NAME_HINTS)


def _component_custom_ids(component) -> list[str]:
    ids = []
    cid = getattr(component, "custom_id", None)
    if cid:
        ids.append(cid)
    for child in getattr(component, "children", None) or []:
        ids.extend(_component_custom_ids(child))
    return ids


async def _find_panel(channel: discord.TextChannel,
                      bot_id: int):
    """Первое сообщение панели в последних 100 сообщениях канала."""
    try:
        async for message in channel.history(limit=100):
            author = getattr(message, "author", None)
            if author is None or author.id != bot_id:
                continue
            ids: list[str] = []
            for comp in getattr(message, "components", None) or []:
                ids.extend(_component_custom_ids(comp))
            if any(cid in _PANEL_CUSTOM_IDS for cid in ids):
                return message
    except discord.HTTPException as exc:
        return ("error", exc)
    except Exception as exc:
        return ("error", exc)
    return None


async def main() -> int:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN is not set", file=sys.stderr)
        return 2
    guild_filter = int(sys.argv[1]) if len(sys.argv) > 1 else None

    intents = discord.Intents.none()
    intents.guilds = True
    client = discord.Client(intents=intents)

    report: list[dict] = []

    @client.event
    async def on_ready():
        bot_id = client.user.id
        for guild in client.guilds:
            if guild_filter and guild.id != guild_filter:
                continue
            print(f"\n=== Guild {guild.name} ({guild.id}) ===")
            print(f"Pinned panel channel: {PINNED_CHANNEL_ID}")
            candidates = [
                c for c in guild.text_channels if _is_candidate(c)
            ]
            if not candidates:
                print("  (подходящих каналов не найдено)")
            for ch in sorted(candidates, key=lambda c: c.id):
                cat = ch.category.name if ch.category else "—"
                pinned = " [PINNED]" if ch.id == PINNED_CHANNEL_ID else ""
                print(f"\n  channel {ch.id}: {ch.name}{pinned}")
                print(f"    category: {cat} | type: {ch.type}")
                try:
                    me = guild.me
                    if me is not None:
                        perms = ch.permissions_for(me)
                        print(
                            "    bot perms: view="
                            f"{perms.view_channel} send="
                            f"{perms.send_messages} history="
                            f"{perms.read_message_history}")
                except Exception as exc:
                    print(f"    bot perms: <error {exc}>")
                found = await _find_panel(ch, bot_id)
                if isinstance(found, tuple):
                    print(f"    panel: <history error: {found[1]}>")
                elif found is not None:
                    print(
                        f"    panel: FOUND message_id={found.id} "
                        f"author={found.author}")
                    report.append({
                        "guild_id": guild.id,
                        "channel_id": ch.id,
                        "channel_name": ch.name,
                        "category": cat,
                        "pinned": ch.id == PINNED_CHANNEL_ID,
                        "panel_message_id": found.id,
                    })
                else:
                    print("    panel: none")
                    report.append({
                        "guild_id": guild.id,
                        "channel_id": ch.id,
                        "channel_name": ch.name,
                        "category": cat,
                        "pinned": ch.id == PINNED_CHANNEL_ID,
                        "panel_message_id": None,
                    })
        await client.close()

    await client.start(token)
    if report:
        print("\n--- JSON ---")
        import json
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
