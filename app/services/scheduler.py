from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.database.connection import get_db
from app.services.discord_service import discord_service

logger = logging.getLogger(__name__)
HOUR_RANGE = (3540, 3660)
MIN10_RANGE = (540, 660)
START_RANGE = (-60, 60)
EXPIRE_SEC = -300


def _is_real_user_id(uid: Any) -> bool:
    try:
        return str(uid).isdigit()
    except Exception:
        return False


class PartyScheduler:
    def __init__(self) -> None:
        self.scheduler = AsyncIOScheduler()
        self.sent_notifications: Dict[int, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def start(self) -> None:
        self.scheduler.add_job(self.check_parties, IntervalTrigger(minutes=1), id='party_check', coalesce=True, max_instances=1, replace_existing=True)
        self.scheduler.start()
        logger.info('Party scheduler started')

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown()

    async def _fetch_db_info(self):
        async with get_db() as db:
            parties: List[Dict[str, Any]] = await db.execute(
                '''SELECT p.id,p.title,p.message,p.start_date,p.owner,p.thread_manage_id,p.guild_id,
                          r.dealer AS dealer_max,r.supporter AS supporter_max,r.name AS raid_name,r.difficulty,
                          CAST((julianday(p.start_date) - julianday('now','localtime')) * 86400 AS INTEGER) AS tdiff
                   FROM party p LEFT JOIN raid r ON p.raid_id = r.id
                   WHERE p.start_date IS NOT NULL AND datetime(p.start_date) BETWEEN datetime('now','localtime','-5 minutes') AND datetime('now','localtime','+2 hours')'''
            ) or []
            if not parties:
                return [], {}, {}, set()
            party_ids = [p['id'] for p in parties]
            live_party_ids = set(party_ids)
            guild_ids = list({str(p['guild_id']) for p in parties if p.get('guild_id')})
            server_cfg_map: Dict[str, Dict[str, Any]] = {}
            if guild_ids:
                marks = ','.join(['?'] * len(guild_ids))
                cfg_rows = await db.execute(f'SELECT guild_id, alert_timer, alert_start, chat_channel_id FROM server WHERE guild_id IN ({marks})', tuple(guild_ids)) or []
                server_cfg_map = {str(r['guild_id']): r for r in cfg_rows}
            participants_map: Dict[int, List[Dict[str, Any]]] = {pid: [] for pid in party_ids}
            if party_ids:
                marks = ','.join(['?'] * len(party_ids))
                part_rows = await db.execute(
                    f'''SELECT p.party_id,p.user_id,p.role,c.char_name,c.item_lvl,c.combat_power,cl.name AS class_name
                        FROM participants p LEFT JOIN character c ON p.character_id = c.id LEFT JOIN class cl ON c.class_id = cl.id
                        WHERE p.party_id IN ({marks})''',
                    tuple(party_ids),
                ) or []
                for row in part_rows:
                    participants_map[row['party_id']].append(row)
        return parties, server_cfg_map, participants_map, live_party_ids

    def _ensure_notification_entry(self, party_id: int, now_ts: float) -> Dict[str, Any]:
        flags = self.sent_notifications.get(party_id)
        if flags is None:
            flags = {'hour': False, 'min10': False, 'start': False, 'ts': now_ts}
            self.sent_notifications[party_id] = flags
        flags['ts'] = now_ts
        return flags

    def _cleanup_notifications(self, live_party_ids: set[int]) -> None:
        for pid in list(self.sent_notifications.keys()):
            if pid not in live_party_ids:
                self.sent_notifications.pop(pid, None)

    async def check_parties(self) -> None:
        async with self._lock:
            parties, server_cfg_map, participants_map, live_party_ids = await self._fetch_db_info()
            tasks: list[Any] = []
            now_ts = datetime.now().timestamp()
            for party in parties or []:
                party_id = int(party['id'])
                tdiff = int(party.get('tdiff') or 0)
                flags = self._ensure_notification_entry(party_id, now_ts)
                guild_id = str(party.get('guild_id') or '')
                cfg = server_cfg_map.get(guild_id) or {}
                alert_mode = int(cfg.get('alert_timer') or 0) & 0b11
                enable_10m = (alert_mode & 0b01) != 0
                enable_1h = (alert_mode & 0b10) != 0
                alert_start = bool(cfg.get('alert_start') or 0)
                chat_channel_id = cfg.get('chat_channel_id')
                parts = participants_map.get(party_id, [])
                if enable_1h and HOUR_RANGE[0] <= tdiff <= HOUR_RANGE[1] and not flags['hour']:
                    tasks.append(self._send_notification_prefetched(party, parts, '1시간', alert_start))
                    flags['hour'] = True
                elif enable_10m and MIN10_RANGE[0] <= tdiff <= MIN10_RANGE[1] and not flags['min10']:
                    tasks.append(self._send_notification_prefetched(party, parts, '10분', alert_start))
                    flags['min10'] = True
                elif START_RANGE[0] <= tdiff <= START_RANGE[1] and not flags['start']:
                    tasks.append(self._send_start_notification_prefetched(party, parts, chat_channel_id, alert_start))
                    flags['start'] = True
                elif tdiff <= EXPIRE_SEC:
                    tasks.append(self._delete_party_and_post(party_id, party.get('thread_manage_id')))
                    self.sent_notifications.pop(party_id, None)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._cleanup_notifications(live_party_ids or set())

    async def _send_notification_prefetched(self, party: Dict[str, Any], participants: List[Dict[str, Any]], time_before: str, alert_start: bool) -> None:
        if not participants:
            return
        embed = self._create_notification_embed(party, time_before)
        real_users = [p for p in participants if _is_real_user_id(p.get('user_id'))]
        mentions = ' '.join(f"<@{p['user_id']}>" for p in real_users)
        tasks: list[Any] = []
        thread_id = party.get('thread_manage_id')
        if thread_id:
            tasks.append(discord_service.send_to_thread(str(thread_id), content=(mentions or None), embed=embed))
        if alert_start:
            for p in real_users:
                tasks.append(discord_service.send_dm(str(p['user_id']), embed=embed))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_start_notification_prefetched(self, party: Dict[str, Any], participants: List[Dict[str, Any]], chat_channel_id: Optional[str], alert_start: bool) -> None:
        party_id = int(party['id'])
        thread_id = str(party.get('thread_manage_id') or '')
        if not participants:
            await self._delete_party_and_post(party_id, thread_id or None)
            return
        party_data = {'id': party.get('id'), 'title': party.get('title'), 'raid_name': party.get('raid_name'), 'difficulty': party.get('difficulty'), 'dealer': party.get('dealer_max'), 'supporter': party.get('supporter_max'), 'owner': party.get('owner'), 'participants': self._split_roles(participants)}
        embed = await discord_service._create_party_embed(party_data)
        real_users = [p for p in participants if _is_real_user_id(p.get('user_id'))]
        mentions = ' '.join(f"<@{p['user_id']}>" for p in real_users)
        start_message = '\n-# **레이드 일정 시간이 되었어요!**'
        tasks: list[Any] = []
        if chat_channel_id:
            tasks.append(discord_service.send_to_channel(str(chat_channel_id), content=((mentions + ' ') if mentions else '') + start_message, embed=embed))
        if alert_start:
            for p in real_users:
                tasks.append(discord_service.send_dm(str(p['user_id']), content=start_message, embed=embed))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._delete_party_and_post(party_id, thread_id or None)

    async def _delete_party_and_post(self, party_id: int, thread_id: Optional[str] = None) -> None:
        try:
            if thread_id:
                await discord_service.safe_delete_forum_post(str(thread_id))
        finally:
            async with get_db() as db:
                await db.execute('DELETE FROM participants WHERE party_id = ?', (int(party_id),))
                await db.execute('DELETE FROM party_waitlist WHERE party_id = ?', (int(party_id),))
                await db.execute('DELETE FROM party WHERE id = ?', (int(party_id),))
                await db.commit()
            self.sent_notifications.pop(int(party_id), None)

    def _create_notification_embed(self, party: Dict[str, Any], time_before: str) -> Dict[str, Any]:
        start_time = party.get('start_date')
        try:
            start_dt = datetime.fromisoformat(str(start_time).replace(' ', 'T'))
        except Exception:
            start_dt = datetime.now()
        time_str = start_dt.strftime('%Y-%m-%d %H:%M')
        embed = {'title': f'🔔 레이드 시작 {time_before} 전 알림', 'description': f"**{party.get('raid_name')}** ({party.get('difficulty')})", 'color': 0xFFD700, 'fields': [{'name': '📅 시작 시간', 'value': f'```{time_str}```', 'inline': True}, {'name': '⏰ 남은 시간', 'value': f'```{time_before} 전```', 'inline': True}], 'footer': {'text': '레이드 알림'}}
        if party.get('message'):
            embed['fields'].append({'name': '💬 메시지', 'value': party['message'], 'inline': False})
        return embed

    def _split_roles(self, participants: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        dealers: List[Dict[str, Any]] = []
        supporters: List[Dict[str, Any]] = []
        for row in participants:
            data = {'user_id': row['user_id'], 'name': row.get('char_name'), 'item_level': row.get('item_lvl'), 'class_name': row.get('class_name'), 'combat_power': row.get('combat_power')}
            if int(row.get('role') or 0) == 0:
                dealers.append(data)
            else:
                supporters.append(data)
        return {'dealers': dealers, 'supporters': supporters}


party_scheduler = PartyScheduler()
