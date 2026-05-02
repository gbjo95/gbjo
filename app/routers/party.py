from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Path, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.database.connection import DatabaseManager, get_db
from app.services.party_service import party_service
from app.utils.datetime_utils import format_datetime_fields
from app.utils.task_utils import fire_and_forget

router = APIRouter(prefix='/party', tags=['party'])


class PartyCreateRequest(BaseModel):
    title: str
    guild_id: str
    raid_name: str
    difficulty: str
    start_date: Optional[str] = None
    owner_id: Optional[int] = None
    message: Optional[str] = None


class PartyEditRequest(BaseModel):
    title: Optional[str] = None
    guild_id: Optional[str] = None
    raid_name: Optional[str] = None
    difficulty: Optional[str] = None
    start_date: Optional[str] = None
    owner_id: Optional[int] = None
    message: Optional[str] = None


class PartyJoinCharacterRequest(BaseModel):
    char_name: str
    class_name: str = ''
    class_emoji: str = ''
    item_lvl: float = 0
    combat_power: float = 0
    character_id: Optional[int] = None


class PartyJoinRequest(BaseModel):
    user_id: str
    role: Optional[int] = Field(0)
    character_id: Optional[int] = None
    character: Optional[PartyJoinCharacterRequest] = None


class PartyStatusUpdateRequest(BaseModel):
    is_dealer_closed: bool
    is_supporter_closed: bool


class WaitlistCancelRequest(BaseModel):
    user_id: str
    role: Optional[int] = Field(0)


async def get_party_with_raid_info(db: DatabaseManager, party_id: int) -> Dict[str, Any]:
    party = await db.execute(
        '''SELECT p.id, p.title, p.guild_id, p.raid_id, p.start_date, p.owner, p.message,
                  p.thread_manage_id, p.is_dealer_closed, p.is_supporter_closed, p.is_active, p.created_at, p.updated_at,
                  r.name AS raid_name, r.difficulty, r.min_lvl, r.dealer, r.supporter
           FROM party p LEFT JOIN raid r ON p.raid_id = r.id WHERE p.id = ? LIMIT 1''',
        (party_id,),
    )
    if not party:
        raise HTTPException(status_code=404, detail='파티를 찾을 수 없습니다.')
    return party[0]


@router.post('/{guild_id}/create')
async def create_party(guild_id: int = Path(...), request: PartyCreateRequest = ...):
    try:
        result = await party_service.create_party(guild_id, request.model_dump())
        return JSONResponse(status_code=201 if int(result.get('status_code') or 201) < 400 else int(result.get('status_code') or 400), content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch('/{party_id}/edit')
async def edit_party(party_id: int = Path(...), request: PartyEditRequest = ...):
    try:
        async with get_db() as db:
            existing_party = await db.execute(
                '''SELECT p.title, p.guild_id, r.name AS raid_name, r.difficulty, p.start_date, p.owner AS owner_id, p.message
                   FROM party p JOIN raid r ON p.raid_id = r.id WHERE p.id = ? LIMIT 1''',
                (party_id,),
            )
            if not existing_party:
                raise HTTPException(status_code=404, detail='파티를 찾을 수 없습니다.')
            existing = existing_party[0]
        party_data = {
            'title': request.title if request.title is not None else existing['title'],
            'guild_id': request.guild_id if request.guild_id is not None else existing['guild_id'],
            'raid_name': request.raid_name if request.raid_name is not None else existing['raid_name'],
            'difficulty': request.difficulty if request.difficulty is not None else existing['difficulty'],
            'start_date': request.start_date if request.start_date is not None else existing['start_date'],
            'owner_id': request.owner_id if request.owner_id is not None else existing['owner_id'],
            'message': request.message if request.message is not None else existing['message'],
        }
        result = await party_service.update_party(party_id, party_data)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.patch('/{party_id}/status')
async def update_party_status(party_id: int, request: PartyStatusUpdateRequest):
    try:
        async with get_db() as db:
            await db.execute('UPDATE party SET is_dealer_closed = ?, is_supporter_closed = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (int(request.is_dealer_closed), int(request.is_supporter_closed), party_id))
            if int(getattr(db, 'rowcount', 0) or 0) == 0:
                raise HTTPException(status_code=404, detail='파티를 찾을 수 없습니다.')
            await db.commit()
        fire_and_forget(party_service.update_discord_after_change(party_id), name='party:update_discord_after_status', timeout_sec=20, coalesce_key=f'party:discord_update:{party_id}')
        return JSONResponse(content={'party_id': party_id, 'is_dealer_closed': request.is_dealer_closed, 'is_supporter_closed': request.is_supporter_closed, 'updated': True})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'상태 업데이트 오류: {str(e)}')


@router.get('/list')
async def get_party_list(guild_id: Optional[int] = Query(None)):
    try:
        async with get_db() as db:
            query = '''SELECT p.id, p.title, p.start_date, p.guild_id, p.owner, p.message, p.thread_manage_id,
                              p.is_dealer_closed, p.is_supporter_closed, p.is_active,
                              r.name AS raid_name, r.difficulty, r.min_lvl, r.dealer, r.supporter
                       FROM party p LEFT JOIN raid r ON p.raid_id = r.id'''
            params: list[Any] = []
            if guild_id:
                query += ' WHERE p.guild_id = ?'
                params.append(str(guild_id))
            query += ' ORDER BY p.start_date ASC'
            parties = await db.execute(query, tuple(params) if params else None)
            result_data = []
            for p in parties or []:
                pid = p['id']
                item = dict(p)
                item['participants'] = await party_service.get_participants_data(pid)
                result_data.append(format_datetime_fields(item))
            return JSONResponse(content={'data': result_data})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post('/{party_id}/join')
async def join_party(party_id: int, request: PartyJoinRequest):
    try:
        result = await party_service.join_party(
            party_id=party_id,
            user_id=request.user_id,
            role=request.role or 0,
            character_id=request.character_id,
            character=(request.character.model_dump() if request.character else None),
        )
        return JSONResponse(status_code=int(result.get('status_code') or 201), content=result)
    except HTTPException:
        raise
    except Exception as e:
        detail = str(e)
        if detail in {'파티를 찾을 수 없습니다.', '캐릭터를 찾을 수 없습니다.'}:
            raise HTTPException(status_code=404, detail=detail)
        raise HTTPException(status_code=500, detail=detail)


@router.get('/{party_id}/participants')
async def get_party_participants(party_id: int):
    try:
        async with get_db() as db:
            exists = await db.execute('SELECT 1 FROM party WHERE id = ? LIMIT 1', (party_id,))
            if not exists:
                raise HTTPException(status_code=404, detail='파티를 찾을 수 없습니다.')
        return JSONResponse(content={'data': await party_service.get_participants_data(party_id)})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete('/{party_id}/delete')
async def delete_party(party_id: int):
    try:
        return JSONResponse(content=await party_service.delete_party(party_id))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete('/{party_id}/participants/{participant_id}/kick')
async def kick_participant(party_id: int, participant_id: int):
    try:
        result = await party_service.leave_party(party_id, participant_id=participant_id)
        return JSONResponse(status_code=int(result.get('status_code') or 200), content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete('/{party_id}/participants/{user_id}')
async def leave_party(party_id: int, user_id: str):
    try:
        result = await party_service.leave_party(party_id, user_id=user_id)
        return JSONResponse(status_code=int(result.get('status_code') or 200), content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete('/guilds/{guild_id}/participants/{user_id}')
async def purge_user_participations_in_guild(guild_id: str, user_id: str):
    try:
        result = await party_service.purge_user_participations_in_guild(guild_id=guild_id, user_id=user_id)
        if result is None:
            return Response(status_code=204)
        return JSONResponse(content=result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/{thread_id}/thread')
async def get_party_by_thread_id(thread_id: int):
    try:
        async with get_db() as db:
            party_result = await db.execute('SELECT id, title, thread_manage_id, guild_id, owner FROM party WHERE thread_manage_id = ? LIMIT 1', (str(thread_id),))
            if not party_result:
                raise HTTPException(status_code=404, detail='해당 스레드 ID로 파티를 찾을 수 없습니다.')
            return JSONResponse(content=party_result[0])
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/{party_id}')
async def get_party_detail(party_id: int):
    try:
        async with get_db() as db:
            party = await get_party_with_raid_info(db, party_id)
        party['participants'] = await party_service.get_participants_data(party_id)
        return JSONResponse(content={'data': format_datetime_fields(party)})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'파티 조회 오류: {str(e)}')


@router.post('/{party_id}/toggle/{role}')
async def toggle_party_status(party_id: int, role: int):
    try:
        if int(role) not in (0, 1):
            raise HTTPException(status_code=400, detail='role 값은 0(딜러) 또는 1(서포터)만 허용됩니다.')
        result = await party_service.toggle_party_status(party_id, role)
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))




@router.post('/public/{party_id}')
async def toggle_party_public(party_id: int):
    try:
        async with get_db() as db:
            rows = await db.execute('SELECT is_active FROM party WHERE id = ? LIMIT 1', (party_id,))
            if not rows:
                raise HTTPException(status_code=404, detail='파티를 찾을 수 없습니다.')
            current = int(rows[0].get('is_active') or 0)
            new_value = 0 if current == 1 else 1
            await db.execute('UPDATE party SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (new_value, party_id))
            await db.commit()
        return JSONResponse(content={'party_id': party_id, 'is_active': new_value, 'updated': True})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get('/{party_id}/waitlist')
async def get_waitlist(party_id: int, role: Optional[int] = Query(None)):
    try:
        data = await party_service.get_waitlist(party_id, role)
        return JSONResponse(content={'data': data})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get('/{party_id}/waitlist/me')
async def get_my_waitlist_position(party_id: int, user_id: str = Query(...), role: int = Query(0)):
    try:
        data = await party_service.get_waitlist_my_position(party_id, user_id, role)
        return JSONResponse(content={'data': data})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete('/{party_id}/waitlist')
async def cancel_waitlist(party_id: int, request: WaitlistCancelRequest):
    try:
        data = await party_service.cancel_waitlist(party_id, request.user_id, request.role or 0)
        return JSONResponse(content=data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
