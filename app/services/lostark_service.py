from __future__ import annotations

import asyncio
import time
import urllib.parse
from typing import Any

import httpx

from app.core.config import LOSTARK_API_BASE, LOSTARK_API_KEY

_CACHE_TTL_SEC = 15.0
_CACHE_MAX_SIZE = 1024
_char_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_inflight: dict[str, asyncio.Task[dict[str, Any] | None]] = {}
_cache_lock = asyncio.Lock()


def parse_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(',', '').strip())
    except (ValueError, TypeError):
        return default


async def _fetch_lostark_character(char_name: str) -> dict[str, Any] | None:
    if not LOSTARK_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(
            f'{LOSTARK_API_BASE}/armories/characters/{urllib.parse.quote(char_name)}/profiles',
            headers={'Authorization': f'Bearer {LOSTARK_API_KEY}'},
        )
    if response.status_code != 200:
        return None
    data = response.json() or {}
    return {
        'char_name': data.get('CharacterName', char_name),
        'class_name': data.get('CharacterClassName', ''),
        'server_name': data.get('ServerName', ''),
        'guild_name': data.get('GuildName', ''),
        'char_image': data.get('CharacterImage', ''),
        'item_lvl': parse_float(data.get('ItemAvgLevel')),
        'combat_power': parse_float(data.get('CombatPower')),
    }


def _prune_cache(now_mono: float) -> None:
    expired = [k for k, (exp, _) in _char_cache.items() if exp <= now_mono]
    for key in expired:
        _char_cache.pop(key, None)
    if len(_char_cache) <= _CACHE_MAX_SIZE:
        return
    overflow = len(_char_cache) - _CACHE_MAX_SIZE
    for key, _ in sorted(_char_cache.items(), key=lambda item: item[1][0])[:overflow]:
        _char_cache.pop(key, None)


async def search_lostark_character(char_name: str) -> dict[str, Any] | None:
    key = (char_name or '').strip().lower()
    if not key:
        return None
    now_mono = time.monotonic()
    async with _cache_lock:
        _prune_cache(now_mono)
        cached = _char_cache.get(key)
        if cached and cached[0] > now_mono:
            return dict(cached[1]) if isinstance(cached[1], dict) else cached[1]
        inflight = _inflight.get(key)
        if inflight is None:
            inflight = asyncio.create_task(_fetch_lostark_character(char_name))
            _inflight[key] = inflight
    try:
        result = await inflight
    finally:
        async with _cache_lock:
            if _inflight.get(key) is inflight:
                _inflight.pop(key, None)
    async with _cache_lock:
        _char_cache[key] = (time.monotonic() + _CACHE_TTL_SEC, result)
        _prune_cache(time.monotonic())
    return dict(result) if isinstance(result, dict) else result
