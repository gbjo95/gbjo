from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / '.env')

APP_HOST = os.getenv('APP_HOST', '0.0.0.0')
APP_PORT = int(os.getenv('APP_PORT', '8000'))
API_KEY = os.getenv('API_KEY', '')
DB_PATH = os.getenv('DB_PATH', str(ROOT_DIR / 'data' / 'mococo.sqlite3'))
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
API_BASE_URL = os.getenv('API_BASE_URL', f'http://127.0.0.1:{APP_PORT}')
DEFAULT_GUILD_ID = os.getenv('DEFAULT_GUILD_ID', '')
CHANNEL_ID = os.getenv('CHANNEL_ID', '')
CHAT_CHANNEL_ID = os.getenv('CHAT_CHANNEL_ID', '')
CANCEL_JOIN_CHANNEL_ID = os.getenv('CANCEL_JOIN_CHANNEL_ID', '')
MENTION_ROLE_ID = os.getenv('MENTION_ROLE_ID', '')
WAITLIST_USE = int(os.getenv('WAITLIST_USE', '1'))
ALERT_TIMER = int(os.getenv('ALERT_TIMER', '3'))
ALERT_START = int(os.getenv('ALERT_START', '1'))
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

LOSTARK_API_KEY = os.getenv('LOSTARK_API_KEY', '')
LOSTARK_API_BASE = os.getenv('LOSTARK_API_BASE', 'https://developer-lostark.game.onstove.com')
