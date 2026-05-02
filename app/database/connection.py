from __future__ import annotations

import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Sequence

from app.core.config import DB_PATH, DEFAULT_GUILD_ID, CHANNEL_ID, CHAT_CHANNEL_ID, CANCEL_JOIN_CHANNEL_ID, MENTION_ROLE_ID, WAITLIST_USE, ALERT_TIMER, ALERT_START
from raidlist import raid_meta


class DatabaseManager:
    def __init__(self) -> None:
        self.conn: aiosqlite.Connection | None = None
        self.lastrowid = 0
        self.rowcount = 0

    async def connect(self) -> bool:
        Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(DB_PATH)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute('PRAGMA foreign_keys = ON')
        return True

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def commit(self) -> None:
        if self.conn is not None:
            await self.conn.commit()

    async def rollback(self) -> None:
        if self.conn is not None:
            await self.conn.rollback()

    async def execute(self, query: str, params: Sequence[Any] | None = None):
        if self.conn is None:
            await self.connect()
        assert self.conn is not None
        cur = await self.conn.execute(query, tuple(params or []))
        self.lastrowid = int(cur.lastrowid or 0)
        self.rowcount = int(cur.rowcount or 0)
        head = query.lstrip().split(None, 1)[0].upper() if query.strip() else ''
        if head in {'SELECT', 'PRAGMA', 'WITH'}:
            rows = await cur.fetchall()
            await cur.close()
            return [dict(r) for r in rows]
        await cur.close()
        return []


@asynccontextmanager
async def get_db():
    db = DatabaseManager()
    await db.connect()
    try:
        yield db
    finally:
        await db.close()


async def init_db() -> None:
    async with get_db() as db:
        await db.execute('''
        CREATE TABLE IF NOT EXISTS class (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            emoji TEXT
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS raid (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            min_lvl REAL DEFAULT 0,
            dealer INTEGER DEFAULT 0,
            supporter INTEGER DEFAULT 0,
            urls TEXT,
            UNIQUE(name, difficulty)
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS server (
            guild_id TEXT PRIMARY KEY,
            forum_channel_id TEXT,
            chat_channel_id TEXT,
            cancel_join_channel_id TEXT,
            mention_role_id TEXT,
            waitlist_use INTEGER DEFAULT 1,
            alert_timer INTEGER DEFAULT 3,
            alert_start INTEGER DEFAULT 1,
            admin_roles TEXT
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS user (
            user_id TEXT PRIMARY KEY,
            emoji TEXT
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS character (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            char_name TEXT UNIQUE NOT NULL,
            class_id INTEGER,
            item_lvl REAL DEFAULT 0,
            combat_power REAL DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(class_id) REFERENCES class(id)
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS siblings (
            user_id TEXT NOT NULL,
            character_id INTEGER,
            PRIMARY KEY(user_id, character_id),
            FOREIGN KEY(character_id) REFERENCES character(id) ON DELETE CASCADE
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS party (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            guild_id TEXT,
            raid_id INTEGER NOT NULL,
            start_date TEXT,
            owner TEXT,
            message TEXT,
            thread_manage_id TEXT,
            is_dealer_closed INTEGER DEFAULT 0,
            is_supporter_closed INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(raid_id) REFERENCES raid(id)
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            party_id INTEGER NOT NULL,
            character_id INTEGER,
            user_id TEXT NOT NULL,
            role INTEGER DEFAULT 0,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(party_id) REFERENCES party(id) ON DELETE CASCADE,
            FOREIGN KEY(character_id) REFERENCES character(id) ON DELETE CASCADE
        )''')
        await db.execute('''
        CREATE TABLE IF NOT EXISTS party_waitlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            party_id INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            user_id TEXT NOT NULL,
            role INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(party_id) REFERENCES party(id) ON DELETE CASCADE,
            FOREIGN KEY(character_id) REFERENCES character(id) ON DELETE CASCADE
        )''')
        await ensure_party_snapshot_columns(db)
        await seed_defaults(db)
        await db.commit()


async def seed_defaults(db: DatabaseManager) -> None:
    classes = [
        (1, '워로드', '🛡️'),
        (2, '버서커', '🗡️'),
        (3, '디스트로이어', '🔨'),
        (4, '홀리나이트', '✨'),
        (5, '슬레이어', '🔥'),
        (6, '발키리', '⚔️'),
        (7, '배틀마스터', '🥋'),
        (8, '인파이터', '🥊'),
        (9, '기공사', '💥'),
        (10, '창술사', '🗡️'),
        (11, '스트라이커', '🐯'),
        (12, '브레이커', '👊'),
        (13, '데빌헌터', '🔫'),
        (14, '블래스터', '💣'),
        (15, '호크아이', '🦅'),
        (16, '스카우터', '🤖'),
        (17, '건슬링어', '🎯'),
        (18, '바드', '🎵'),
        (19, '서머너', '🌿'),
        (20, '아르카나', '🃏'),
        (21, '소서리스', '❄️'),
        (22, '블레이드', '🌙'),
        (23, '데모닉', '😈'),
        (24, '리퍼', '🗡️'),
        (25, '소울이터', '💀'),
        (26, '도화가', '🖌️'),
        (27, '기상술사', '☂️'),
        (28, '환수사', '🐾'),
    ]
    for row in classes:
        await db.execute('INSERT OR IGNORE INTO class (id, name, emoji) VALUES (?, ?, ?)', row)
    raids = []
    for raid_name, difficulties in raid_meta.items():
        for difficulty, meta in difficulties.items():
            raids.append((raid_name, difficulty, meta['min_lvl'], meta['dealer'], meta['supporter']))
    for row in raids:
        await db.execute('INSERT OR IGNORE INTO raid (name, difficulty, min_lvl, dealer, supporter) VALUES (?, ?, ?, ?, ?)', row)
    if DEFAULT_GUILD_ID:
        await db.execute(
            '''INSERT OR IGNORE INTO server (guild_id, forum_channel_id, chat_channel_id, cancel_join_channel_id, mention_role_id, waitlist_use, alert_timer, alert_start)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (DEFAULT_GUILD_ID, CHANNEL_ID, CHAT_CHANNEL_ID, CANCEL_JOIN_CHANNEL_ID, MENTION_ROLE_ID, WAITLIST_USE, ALERT_TIMER, ALERT_START),
        )



async def ensure_party_snapshot_columns(db: DatabaseManager) -> None:
    async def ensure_column(table: str, column: str, ddl: str) -> None:
        cols = await db.execute(f"PRAGMA table_info({table})")
        names = {c.get('name') for c in cols or []}
        if column not in names:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    for table in ('participants', 'party_waitlist'):
        await ensure_column(table, 'char_name', 'char_name TEXT')
        await ensure_column(table, 'class_name', 'class_name TEXT')
        await ensure_column(table, 'class_emoji', 'class_emoji TEXT')
        await ensure_column(table, 'item_lvl', 'item_lvl REAL DEFAULT 0')
        await ensure_column(table, 'combat_power', 'combat_power REAL DEFAULT 0')
