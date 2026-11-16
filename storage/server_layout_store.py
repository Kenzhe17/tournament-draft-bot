"""Durable guild-scoped layout plans and message references on the bot volume."""
from __future__ import annotations

import json
from pathlib import Path

from config import DATA_DIR
from storage.json_atomic import atomic_write_json


class ServerLayoutStore:
    def __init__(self, root: Path | None = None):
        self.root = root or DATA_DIR / 'server_layout'

    def read(self, guild_id: int) -> dict:
        path = self.root / f'{int(guild_id)}.json'
        if not path.exists():
            return {'guild_id': int(guild_id), 'bindings': {}}
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('guild_id') != int(guild_id):
            raise ValueError('Layout state belongs to another server.')
        return data

    def write(self, guild_id: int, data: dict):
        if data.get('guild_id') != int(guild_id):
            raise ValueError('Layout state belongs to another server.')
        self.root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.root / f'{int(guild_id)}.json', data)

    def archive_plan(self, guild_id: int, plan: dict):
        if plan.get('guild_id') != guild_id:
            raise ValueError('Plan belongs to another server.')
        token = str(plan.get('id', ''))
        if not token or not all(c in '0123456789abcdef' for c in token):
            raise ValueError('Invalid plan ID.')
        target = self.root / 'history' / str(guild_id) / f'{token}.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, plan)

    def binding(self, guild_id: int, key: str) -> int:
        return int(self.read(guild_id).get('bindings', {}).get(key) or 0)


server_layout_store = ServerLayoutStore()
