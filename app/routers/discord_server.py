from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.database.connection import get_db

router = APIRouter(prefix='/discord', tags=['discord'])


class ServerConfigRequest(BaseModel):
    forum_channel_id: str | None = None
    chat_channel_id: str | None = None
    cancel_join_channel_id: str | None = None
    mention_role_id: str | None = None
    waitlist_use: int | None = None
    alert_timer: int | None = None
    alert_start: int | None = None
    admin_roles: str | None = None


@router.get('/server/{guild_id}')
async def get_server(guild_id: str):
    async with get_db() as db:
        row = await db.execute('SELECT * FROM server WHERE guild_id = ? LIMIT 1', (str(guild_id),))
    return JSONResponse(content={'data': row[0] if row else {}})


@router.post('/server/{guild_id}')
async def upsert_server(guild_id: str, request: ServerConfigRequest):
    try:
        data = request.model_dump()
        async with get_db() as db:
            row = await db.execute('SELECT guild_id FROM server WHERE guild_id = ? LIMIT 1', (str(guild_id),))
            if row:
                current = (await db.execute('SELECT * FROM server WHERE guild_id = ? LIMIT 1', (str(guild_id),)))[0]
                merged = {**current, **{k: v for k, v in data.items() if v is not None}, 'guild_id': str(guild_id)}
                await db.execute(
                    '''UPDATE server SET forum_channel_id=?, chat_channel_id=?, cancel_join_channel_id=?, mention_role_id=?, waitlist_use=?, alert_timer=?, alert_start=?, admin_roles=? WHERE guild_id=?''',
                    (merged.get('forum_channel_id'), merged.get('chat_channel_id'), merged.get('cancel_join_channel_id'), merged.get('mention_role_id'), merged.get('waitlist_use') or 0, merged.get('alert_timer') or 0, merged.get('alert_start') or 0, merged.get('admin_roles'), str(guild_id)),
                )
            else:
                await db.execute(
                    '''INSERT INTO server (guild_id, forum_channel_id, chat_channel_id, cancel_join_channel_id, mention_role_id, waitlist_use, alert_timer, alert_start, admin_roles) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (str(guild_id), data.get('forum_channel_id'), data.get('chat_channel_id'), data.get('cancel_join_channel_id'), data.get('mention_role_id'), data.get('waitlist_use') or 0, data.get('alert_timer') or 0, data.get('alert_start') or 0, data.get('admin_roles')),
                )
            await db.commit()
        return JSONResponse(content={'ok': True, 'data': {'guild_id': str(guild_id), **{k: v for k, v in data.items() if v is not None}}})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
