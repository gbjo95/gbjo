from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.database.connection import get_db

router = APIRouter(tags=['siblings'])


class SiblingLinkRequest(BaseModel):
    user_id: str
    character_id: int


@router.get('/siblings/{user_id}')
async def get_siblings(user_id: str):
    async with get_db() as db:
        rows = await db.execute(
            '''SELECT c.id AS char_id, c.char_name, c.item_lvl, c.combat_power, cl.name AS class_name
               FROM siblings s JOIN character c ON s.character_id = c.id LEFT JOIN class cl ON c.class_id = cl.id
               WHERE s.user_id = ? ORDER BY c.item_lvl DESC, c.char_name ASC''',
            (str(user_id),),
        )
    return JSONResponse(content={'characters': rows or []})


@router.post('/siblings/link')
async def link_sibling(request: SiblingLinkRequest):
    async with get_db() as db:
        await db.execute('INSERT OR IGNORE INTO siblings (user_id, character_id) VALUES (?, ?)', (str(request.user_id), int(request.character_id)))
        await db.commit()
    return JSONResponse(content={'ok': True})
