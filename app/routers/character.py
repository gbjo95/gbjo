from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import JSONResponse

from app.services.character_sync import search_lostark_character

router = APIRouter(prefix='/character', tags=['character'])


@router.get('/search/{nickname}')
async def search_character(nickname: str = Path(...)):
    decoded_nickname = urllib.parse.unquote(nickname)
    lostark_data = await search_lostark_character(decoded_nickname)
    if not lostark_data:
        raise HTTPException(status_code=404, detail='캐릭터를 찾을 수 없습니다.')
    return JSONResponse(content={'data': lostark_data, 'source': 'lostark_api'})
