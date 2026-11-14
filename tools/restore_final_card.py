"""Восстановить итоговую карточку завершённого турнира из архива.

Использование (на сервере, где лежит DATA_DIR с
tournament_archive.json, или локально с тем же DATA_DIR):

    python tools/restore_final_card.py 1196165488565289012_1794677935308

Что делает:
- читает архивный снимок tournament_archive.json;
- восстанавливает Tournament из data (phase/winner/статистика);
- находит исходное сообщение турнира по tournament.message_id;
- редактирует его в итоговую CV2-карточку победителя.

Безопасность: НЕ пересчитывает ELO, не начисляет монеты/XP,
не пишет в историю — только отображение результата.
Новый активный турнир в канале не затрагивается.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord

from config import DISCORD_TOKEN
from services.tournament_lifecycle import restore_final_card

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("restore_final_card")


class _Client(discord.Client):
    def __init__(self) -> None:
        super().__init__(intents=discord.Intents.none())


async def _main(tournament_id: str) -> int:
    client = _Client()

    @client.event
    async def on_ready() -> None:
        try:
            ok = await restore_final_card(tournament_id, client=client)
            logger.info(
                "restore %s -> %s", tournament_id,
                "OK" if ok else "FAILED")
        finally:
            await client.close()

    await client.start(DISCORD_TOKEN)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python tools/restore_final_card.py <tournament_id>")
        return 2
    tid = sys.argv[1].strip()
    try:
        return asyncio.run(_main(tid))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
