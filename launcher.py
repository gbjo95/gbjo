from __future__ import annotations

import asyncio
import threading
import uvicorn

from app.core.config import APP_HOST, APP_PORT
from app.main import app
from bot.main import run_bot


def run_api() -> None:
    uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level='info')


def main() -> None:
    t = threading.Thread(target=run_api, daemon=True)
    t.start()
    run_bot()


if __name__ == '__main__':
    main()
