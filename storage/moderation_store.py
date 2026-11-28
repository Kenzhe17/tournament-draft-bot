"""PostgreSQL source of truth for r!z3 moderation.

All records are server-scoped. Audit history is append-only. The store deliberately
has no JSON fallback: moderation must fail closed when PostgreSQL is unavailable.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from typing import Any

from storage.db import get_pool

_SCHEMA = """
CREATE TABLE IF NOT EXISTS moderation_settings (
    guild_id BIGINT PRIMARY KEY,
    moderator_role_id BIGINT,
    head_role_id BIGINT,
    log_channel_id BIGINT,
    reports_channel_id BIGINT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS moderation_cases (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    actor_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('warn','timeout','kick','ban','note','gif_timeout')),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','active','completed','revoked','unknown','expired')),
    expires_at TIMESTAMPTZ,
    message_url TEXT,
    request_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS moderation_cases_target_idx ON moderation_cases(guild_id,target_id,created_at DESC);
CREATE INDEX IF NOT EXISTS moderation_cases_active_idx ON moderation_cases(guild_id,status,expires_at);
CREATE TABLE IF NOT EXISTS moderation_reports (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    reporter_id BIGINT NOT NULL,
    target_id BIGINT NOT NULL,
    reason TEXT NOT NULL,
    message_url TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','claimed','escalated','resolved','dismissed')),
    assignee_id BIGINT,
    resolution TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS moderation_reports_queue_idx ON moderation_reports(guild_id,status,created_at);
CREATE TABLE IF NOT EXISTS moderation_appeals (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    case_id BIGINT NOT NULL REFERENCES moderation_cases(id),
    appellant_id BIGINT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','accepted','rejected')),
    reviewer_id BIGINT,
    resolution TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (case_id,appellant_id)
);
CREATE INDEX IF NOT EXISTS moderation_appeals_queue_idx ON moderation_appeals(guild_id,status,created_at);
CREATE TABLE IF NOT EXISTS moderation_events (
    id BIGSERIAL PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    case_id BIGINT,
    report_id BIGINT,
    appeal_id BIGINT,
    actor_id BIGINT NOT NULL,
    action TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    event_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS moderation_events_case_idx ON moderation_events(guild_id,case_id,created_at);
"""


def _dict(row):
    return dict(row) if row is not None else None


class ModerationStore:
    def __init__(self):
        self._ready = False
        self._init_lock = asyncio.Lock()

    async def ensure(self):
        if self._ready:
            return
        async with self._init_lock:
            if not self._ready:
                pool = await get_pool()
                async with pool.acquire() as conn:
                    await conn.execute(_SCHEMA)
                self._ready = True

    async def settings(self, guild_id: int) -> dict:
        await self.ensure()
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _dict(await conn.fetchrow('SELECT * FROM moderation_settings WHERE guild_id=$1',guild_id)) or {}

    async def put_settings(self, guild_id: int, **values) -> dict:
        """Partial upsert, so interrupted /mod setup can be retried."""
        allowed = ('moderator_role_id','head_role_id','log_channel_id','reports_channel_id')
        values = {k: int(v) for k,v in values.items() if k in allowed and v}
        if not values:
            return await self.settings(guild_id)
        await self.ensure()
        columns = ', '.join(values)
        inserts = ', '.join(f'${i+2}' for i in range(len(values)))
        updates = ', '.join(f'{key}=EXCLUDED.{key}' for key in values)
        sql = (f'INSERT INTO moderation_settings(guild_id,{columns}) VALUES($1,{inserts}) '
               f'ON CONFLICT(guild_id) DO UPDATE SET {updates}, updated_at=now() RETURNING *')
        pool = await get_pool()
        async with pool.acquire() as conn:
            return _dict(await conn.fetchrow(sql,guild_id,*values.values()))

    async def event(self,guild_id:int,actor_id:int,action:str,*,case_id=None,report_id=None,appeal_id=None,detail=None,event_key=None):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            await conn.execute('''INSERT INTO moderation_events(guild_id,actor_id,action,case_id,report_id,appeal_id,detail,event_key)
                VALUES($1,$2,$3,$4,$5,$6,$7::jsonb,$8) ON CONFLICT(event_key) DO NOTHING''',
                guild_id,actor_id,action,case_id,report_id,appeal_id,json.dumps(detail or {},ensure_ascii=False),event_key)

    async def create_case(self,guild_id,target_id,actor_id,kind,reason,request_key,*,expires_at=None,message_url=None):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            row=await conn.fetchrow('''INSERT INTO moderation_cases(guild_id,target_id,actor_id,kind,reason,status,request_key,expires_at,message_url)
                VALUES($1,$2,$3,$4,$5,'pending',$6,$7,$8)
                ON CONFLICT(request_key) DO UPDATE SET request_key=EXCLUDED.request_key RETURNING *''',
                guild_id,target_id,actor_id,kind,reason,request_key,expires_at,message_url)
            return _dict(row)

    async def case_status(self,guild_id,case_id,status,actor_id,*,details=None,expected=None):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                sql='UPDATE moderation_cases SET status=$3,updated_at=now() WHERE guild_id=$1 AND id=$2'
                args=[guild_id,case_id,status]
                if expected:
                    sql+=' AND status=ANY($4::text[])'
                    args.append(list(expected))
                row=await conn.fetchrow(sql+' RETURNING *',*args)
                if row:
                    await conn.execute('''INSERT INTO moderation_events(guild_id,case_id,actor_id,action,detail)
                        VALUES($1,$2,$3,$4,$5::jsonb)''',guild_id,case_id,actor_id,'case.'+status,json.dumps(details or {},ensure_ascii=False))
                return _dict(row)

    async def case(self,guild_id,case_id):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            return _dict(await conn.fetchrow('SELECT * FROM moderation_cases WHERE guild_id=$1 AND id=$2',guild_id,case_id))

    async def cases(self,guild_id,*,target_id=None,limit=15):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            if target_id is None:
                rows=await conn.fetch('SELECT * FROM moderation_cases WHERE guild_id=$1 ORDER BY id DESC LIMIT $2',guild_id,min(50,limit))
            else:
                rows=await conn.fetch('SELECT * FROM moderation_cases WHERE guild_id=$1 AND target_id=$2 ORDER BY id DESC LIMIT $3',guild_id,target_id,min(50,limit))
            return [dict(r) for r in rows]

    async def create_report(self,guild_id,reporter_id,target_id,reason,message_url=None):
        """1 report per reporter per minute; PG advisory lock handles multi-process races."""
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.fetchval('SELECT pg_advisory_xact_lock($1,$2)',guild_id % (2**31-1), reporter_id % (2**31-1))
                existing=await conn.fetchval('''SELECT id FROM moderation_reports
                    WHERE guild_id=$1 AND reporter_id=$2 AND created_at>now()-interval '60 seconds' LIMIT 1''',guild_id,reporter_id)
                if existing:
                    return None
                row=await conn.fetchrow('''INSERT INTO moderation_reports(guild_id,reporter_id,target_id,reason,message_url)
                    VALUES($1,$2,$3,$4,$5) RETURNING *''',guild_id,reporter_id,target_id,reason,message_url)
                await conn.execute('''INSERT INTO moderation_events(guild_id,report_id,actor_id,action)
                    VALUES($1,$2,$3,'report.created')''',guild_id,row['id'],reporter_id)
                return dict(row)

    async def report(self,guild_id,report_id):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            return _dict(await conn.fetchrow('SELECT * FROM moderation_reports WHERE guild_id=$1 AND id=$2',guild_id,report_id))

    async def reports(self,guild_id,*,limit=25,include_closed=False):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            clause='' if include_closed else "AND status IN ('open','claimed','escalated')"
            rows=await conn.fetch(f'''SELECT * FROM moderation_reports WHERE guild_id=$1 {clause}
                ORDER BY CASE WHEN status='escalated' THEN 0 ELSE 1 END, id DESC LIMIT $2''',guild_id,min(100,limit))
            return [dict(r) for r in rows]

    async def report_action(self,guild_id,report_id,actor_id,action,*,note=''):
        permitted={'claim','resolve','dismiss','escalate'}
        if action not in permitted:
            raise ValueError('Unknown report action')
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                if action=='claim':
                    row=await conn.fetchrow('''UPDATE moderation_reports SET status='claimed',assignee_id=$3,updated_at=now()
                        WHERE guild_id=$1 AND id=$2 AND status='open' RETURNING *''',guild_id,report_id,actor_id)
                else:
                    new_status={'resolve':'resolved','dismiss':'dismissed','escalate':'escalated'}[action]
                    row=await conn.fetchrow('''UPDATE moderation_reports SET status=$4,resolution=$5,updated_at=now()
                        WHERE guild_id=$1 AND id=$2 AND status IN ('open','claimed','escalated')
                        AND (assignee_id IS NULL OR assignee_id=$3 OR $6)
                        RETURNING *''',guild_id,report_id,actor_id,new_status,note,action=='escalate')
                if row:
                    await conn.execute('''INSERT INTO moderation_events(guild_id,report_id,actor_id,action,detail)
                        VALUES($1,$2,$3,$4,$5::jsonb)''',guild_id,report_id,actor_id,'report.'+action,json.dumps({'note':note},ensure_ascii=False))
                return _dict(row)

    async def create_appeal(self,guild_id,case_id,user_id,reason):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row=await conn.fetchrow('SELECT target_id FROM moderation_cases WHERE guild_id=$1 AND id=$2',guild_id,case_id)
                if not row or row['target_id']!=user_id:
                    return None
                appeal=await conn.fetchrow('''INSERT INTO moderation_appeals(guild_id,case_id,appellant_id,reason)
                    VALUES($1,$2,$3,$4) ON CONFLICT(case_id,appellant_id) DO NOTHING RETURNING *''',guild_id,case_id,user_id,reason)
                if appeal:
                    await conn.execute('''INSERT INTO moderation_events(guild_id,case_id,appeal_id,actor_id,action)
                        VALUES($1,$2,$3,$4,'appeal.created')''',guild_id,case_id,appeal['id'],user_id)
                return _dict(appeal)

    async def appeals(self,guild_id,*,limit=25):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            rows=await conn.fetch('''SELECT * FROM moderation_appeals WHERE guild_id=$1 AND status='open' ORDER BY id ASC LIMIT $2''',guild_id,min(100,limit))
            return [dict(r) for r in rows]

    async def appeal_action(self,guild_id,appeal_id,actor_id,accepted,note):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                row=await conn.fetchrow('''UPDATE moderation_appeals SET status=$4,reviewer_id=$3,resolution=$5,updated_at=now()
                    WHERE guild_id=$1 AND id=$2 AND status='open' RETURNING *''',guild_id,appeal_id,actor_id,'accepted' if accepted else 'rejected',note)
                if row:
                    await conn.execute('''INSERT INTO moderation_events(guild_id,case_id,appeal_id,actor_id,action,detail)
                        VALUES($1,$2,$3,$4,$5,$6::jsonb)''',guild_id,row['case_id'],appeal_id,actor_id,'appeal.accepted' if accepted else 'appeal.rejected',json.dumps({'note':note},ensure_ascii=False))
                return _dict(row)

    async def events(self,guild_id,*,case_id=None,report_id=None,limit=30):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            if case_id:
                rows=await conn.fetch('SELECT * FROM moderation_events WHERE guild_id=$1 AND case_id=$2 ORDER BY id DESC LIMIT $3',guild_id,case_id,limit)
            elif report_id:
                rows=await conn.fetch('SELECT * FROM moderation_events WHERE guild_id=$1 AND report_id=$2 ORDER BY id DESC LIMIT $3',guild_id,report_id,limit)
            else:
                rows=await conn.fetch('SELECT * FROM moderation_events WHERE guild_id=$1 ORDER BY id DESC LIMIT $2',guild_id,limit)
            return [dict(r) for r in rows]

    async def voice_state(self,guild_id,target_id,field):
        """Последнее подтверждённое состояние голосового флага
        ('mute'/'deafen') по журналу модерации.

        Возвращает dict: {'status': 'active'|'inactive'|'unknown'|'none',
        'at': datetime|None}. '.unknown'-события трактуются консервативно
        как 'active' — неподтверждённый результат не доказывает,
        что модерация флаг НЕ ставила."""
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            rows=await conn.fetch(
                """SELECT action, detail, created_at FROM moderation_events
                   WHERE guild_id=$1 AND action LIKE $2
                   ORDER BY id DESC LIMIT 100""",
                guild_id, f'voice.{field}.%')
        for row in rows:
            detail=row.get('detail') or {}
            if isinstance(detail,str):
                try:detail=json.loads(detail)
                except ValueError:continue
            if str(detail.get('target_id'))!=str(target_id):
                continue
            action=row['action']
            if action.endswith('.requested'):
                continue  # запрос без подтверждения — не доказательство
            value=detail.get('value')
            if action.endswith('.unknown'):
                return {'status':'unknown','at':row['created_at']}
            if action.endswith('.applied') and value is not None:
                return {'status':'active' if value else 'inactive',
                        'at':row['created_at']}
        return {'status':'none','at':None}

    async def reconcile_candidates(self,guild_id,limit=100):
        """Actions that may have completed in Discord despite an API timeout."""
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            rows=await conn.fetch('''SELECT * FROM moderation_cases
                WHERE guild_id=$1 AND (
                   status IN ('pending','unknown') OR
                   (status='active' AND expires_at IS NOT NULL AND expires_at<=now()))
                ORDER BY id ASC LIMIT $2''',guild_id,limit)
            return [dict(r) for r in rows]

    async def stats(self,guild_id):
        await self.ensure()
        pool=await get_pool()
        async with pool.acquire() as conn:
            return _dict(await conn.fetchrow('''SELECT
                (SELECT count(*) FROM moderation_reports WHERE guild_id=$1 AND status IN ('open','claimed','escalated')) AS open_reports,
                (SELECT count(*) FROM moderation_appeals WHERE guild_id=$1 AND status='open') AS open_appeals,
                (SELECT count(*) FROM moderation_cases WHERE guild_id=$1 AND kind IN ('timeout','gif_timeout')
                    AND status='active' AND expires_at>now()) AS active_timeouts,
                (SELECT count(*) FROM moderation_events WHERE guild_id=$1 AND created_at>now()-interval '7 days') AS events_week''',guild_id))


moderation_store=ModerationStore()
