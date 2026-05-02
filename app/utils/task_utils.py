from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine

logger = logging.getLogger(__name__)
_tasks: dict[str, asyncio.Task] = {}


def fire_and_forget(coro: Coroutine, *, name: str = 'task', timeout_sec: float | None = None, coalesce_key: str | None = None) -> None:
    async def runner() -> None:
        try:
            if timeout_sec:
                await asyncio.wait_for(coro, timeout=timeout_sec)
            else:
                await coro
        except Exception:
            logger.exception('background task failed: %s', name)
        finally:
            if coalesce_key:
                _tasks.pop(coalesce_key, None)

    if coalesce_key:
        task = _tasks.get(coalesce_key)
        if task and not task.done():
            return
    task = asyncio.create_task(runner(), name=name)
    if coalesce_key:
        _tasks[coalesce_key] = task
