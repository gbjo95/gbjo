import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / '.env')

API_BASE_URL = os.getenv('API_BASE_URL', 'http://127.0.0.1:8000')
API_KEY = os.getenv('API_KEY', '')
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
SUPPORTER_CLASSES = ['바드', '도화가', '홀리나이트', '발키리']
