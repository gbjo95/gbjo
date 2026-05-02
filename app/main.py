from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import API_KEY, LOG_LEVEL
from app.database.connection import init_db
from app.routers.character import router as character_router
from app.routers.discord_server import router as discord_router
from app.routers.party import router as party_router
from app.routers.siblings import router as siblings_router
from app.services.scheduler import party_scheduler

logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO))

app = FastAPI(title='Mococo Party Lite')
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])


@app.middleware('http')
async def api_key_middleware(request, call_next):
    if API_KEY and request.url.path not in ('/docs', '/openapi.json', '/redoc'):
        header_key = request.headers.get('X-API-Key', '')
        if header_key != API_KEY:
            raise HTTPException(status_code=401, detail='Invalid API key')
    return await call_next(request)


app.include_router(party_router)
app.include_router(discord_router)
app.include_router(character_router)
app.include_router(siblings_router)


@app.on_event('startup')
async def startup() -> None:
    await init_db()
    party_scheduler.start()


@app.on_event('shutdown')
async def shutdown() -> None:
    party_scheduler.stop()
