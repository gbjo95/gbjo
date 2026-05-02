from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from app.database.connection import DatabaseManager, get_db
from app.services.discord_service import discord_service
from app.utils.datetime_utils import parse_start_date
from app.utils.task_utils import fire_and_forget

logger = logging.getLogger(__name__)

def _parse_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace(',', '').strip())
    except (TypeError, ValueError):
        return default


def _character_snapshot(character: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    c = character or {}
    return {
        'character_id': int(c.get('character_id') or 0) or None,
        'char_name': str(c.get('char_name') or '').strip(),
        'class_name': str(c.get('class_name') or '').strip(),
        'class_emoji': str(c.get('class_emoji') or '').strip(),
        'item_lvl': _parse_float(c.get('item_lvl')),
        'combat_power': _parse_float(c.get('combat_power')),
    }


class PartyService:
    def __init__(self) -> None:
        self._discord_update_locks: Dict[int, asyncio.Lock] = {}
        self._discord_payload_fingerprint: Dict[int, Tuple[str, float]] = {}
        self._discord_fingerprint_cache_max = max(100, int(os.getenv('PARTY_DISCORD_FINGERPRINT_CACHE_MAX', '5000')))
        self._discord_fingerprint_ttl_sec = max(5.0, float(os.getenv('PARTY_DISCORD_FINGERPRINT_TTL_SEC', '20')))

    def _discord_update_coalesce_key(self, party_id: int) -> str:
        return f'party:discord_update:{int(party_id)}'

    def _waitlist_reconcile_coalesce_key(self, party_id: int) -> str:
        return f'party:waitlist_reconcile:{int(party_id)}'

    def _get_discord_update_lock(self, party_id: int) -> asyncio.Lock:
        pid = int(party_id)
        lock = self._discord_update_locks.get(pid)
        if lock is None:
            lock = asyncio.Lock()
            self._discord_update_locks[pid] = lock
        return lock

    def _build_discord_payload_fingerprint(self, party_info: Dict[str, Any]) -> str:
        payload = {
            'id': party_info.get('id'),
            'title': party_info.get('title'),
            'guild_id': party_info.get('guild_id'),
            'raid_id': party_info.get('raid_id'),
            'raid_name': party_info.get('raid_name'),
            'difficulty': party_info.get('difficulty'),
            'start_date': party_info.get('start_date'),
            'owner': party_info.get('owner'),
            'message': party_info.get('message'),
            'thread_manage_id': party_info.get('thread_manage_id'),
            'is_dealer_closed': party_info.get('is_dealer_closed'),
            'is_supporter_closed': party_info.get('is_supporter_closed'),
            'participants': party_info.get('participants') or {},
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    def _remember_discord_payload_fingerprint(self, party_id: int, fingerprint: str) -> None:
        pid = int(party_id)
        now = time.monotonic()
        if len(self._discord_payload_fingerprint) >= self._discord_fingerprint_cache_max and pid not in self._discord_payload_fingerprint:
            oldest = next(iter(self._discord_payload_fingerprint), None)
            if oldest is not None:
                self._discord_payload_fingerprint.pop(oldest, None)
                self._discord_update_locks.pop(int(oldest), None)
        self._discord_payload_fingerprint[pid] = (fingerprint, now)

    def _is_duplicate_discord_payload(self, party_id: int, fingerprint: str) -> bool:
        cached = self._discord_payload_fingerprint.get(int(party_id))
        if not cached:
            return False
        cached_fp, cached_ts = cached
        return cached_fp == fingerprint and (time.monotonic() - float(cached_ts or 0.0)) <= self._discord_fingerprint_ttl_sec

    @staticmethod
    def _is_temp_user(user_id: str) -> bool:
        return str(user_id).startswith('TEMP-')

    async def get_participants_data(self, party_id: int) -> Dict[str, List]:
        async with get_db() as db:
            return await self.get_participants_data_with_db(db, party_id)

    async def get_participants_data_with_db(self, db: DatabaseManager, party_id: int) -> Dict[str, List]:
        rows = await db.execute(
            '''SELECT p.id AS participant_id, p.role, p.user_id, p.character_id,
                      COALESCE(p.char_name, c.char_name) AS name,
                      COALESCE(p.item_lvl, c.item_lvl) AS item_level,
                      COALESCE(p.combat_power, c.combat_power) AS combat_power,
                      COALESCE(p.class_name, cl.name) AS class_name,
                      COALESCE(p.class_emoji, cl.emoji) AS class_emoji,
                      c.class_id AS class_id, p.joined_at
               FROM participants p
               LEFT JOIN character c ON p.character_id = c.id
               LEFT JOIN class cl ON c.class_id = cl.id
               WHERE p.party_id = ? ORDER BY p.joined_at ASC''',
            (party_id,),
        )
        dealers: List[Dict[str, Any]] = []
        supporters: List[Dict[str, Any]] = []
        for p in rows or []:
            data = {
                'participant_id': p['participant_id'],
                'user_id': p['user_id'],
                'character_id': p.get('character_id'),
                'class_id': p.get('class_id'),
                'name': p.get('name'),
                'item_level': p.get('item_level'),
                'combat_power': p.get('combat_power'),
                'class_name': p.get('class_name'),
                'class_emoji': p.get('class_emoji'),
                'joined_at': p.get('joined_at'),
            }
            if int(p.get('role') or 0) == 0:
                dealers.append(data)
            else:
                supporters.append(data)
        return {'dealers': dealers, 'supporters': supporters}

    async def create_party(self, guild_id: int, party_data: Dict) -> Dict:
        message_text = (party_data.get('message') or '')
        if len(message_text) > 200:
            return {'message': 'message는 200자를 초과할 수 없습니다.', 'status_code': 400}
        async with get_db() as db:
            raid_result = await db.execute('SELECT id, dealer, supporter FROM raid WHERE name = ? AND difficulty = ? LIMIT 1', (party_data['raid_name'], party_data['difficulty']))
            if not raid_result:
                return {'message': '해당 레이드를 찾을 수 없습니다.', 'status_code': 404}
            raid_info = raid_result[0]
            parsed_start_date = parse_start_date(party_data.get('start_date', ''))
            try:
                await db.execute('INSERT INTO party (title, guild_id, raid_id, start_date, owner, message) VALUES (?, ?, ?, ?, ?, ?)', (
                    party_data['title'], str(guild_id), raid_info['id'], parsed_start_date, str(party_data.get('owner_id') or ''), party_data.get('message', ''),
                ))
                party_id = db.lastrowid
                ok, thread_id, err = await self._create_discord_post(db, party_id, guild_id, party_data, raid_info)
                if not ok or not thread_id:
                    await db.rollback()
                    return {'message': 'Discord 포럼글 생성 실패', 'status_code': 404, 'error': err or {'detail': '알 수 없는 오류'}, 'party_id': party_id}
                await db.commit()
                return {'title': party_data['title'], 'start_date': parsed_start_date, 'party_id': party_id, 'thread_id': thread_id, 'discord_posted': True, 'message': '파티가 성공적으로 생성되었습니다.'}
            except Exception as e:
                await db.rollback()
                logger.exception('create party failed')
                return {'message': '파티 생성 실패', 'status_code': 400, 'error': {'type': e.__class__.__name__, 'detail': str(e)}}

    async def join_party(self, party_id: int, user_id: str, role: int = 0, character_id: Optional[int] = None, character: Optional[Dict[str, Any]] = None) -> Dict:
        async with get_db() as db:
            party_result = await db.execute(
                '''SELECT p.raid_id, p.guild_id, p.is_dealer_closed, p.is_supporter_closed, p.title, p.start_date,
                          p.thread_manage_id, r.min_lvl, r.dealer, r.supporter, r.name AS raid_name, r.difficulty
                   FROM party p JOIN raid r ON p.raid_id = r.id WHERE p.id = ? LIMIT 1''',
                (party_id,),
            )
            if not party_result:
                return {'party_id': party_id, 'message': '파티를 찾을 수 없습니다.', 'status_code': 404}
            party_info = party_result[0]
            guild_id = str(party_info.get('guild_id') or '')
            snapshot: Dict[str, Any]
            if character:
                snapshot = _character_snapshot(character)
                if not snapshot['char_name']:
                    return {'party_id': party_id, 'message': '캐릭터를 찾을 수 없습니다.', 'status_code': 404}
            else:
                cid = int(character_id or 0)
                character_result = await db.execute(
                    '''SELECT c.id AS character_id, c.char_name, c.item_lvl, c.combat_power, cl.name AS class_name, cl.emoji AS class_emoji, c.class_id
                       FROM character c
                       LEFT JOIN class cl ON c.class_id = cl.id
                       WHERE c.id = ? LIMIT 1''',
                    (cid,),
                )
                if not character_result:
                    return {'party_id': party_id, 'character_id': cid, 'message': '캐릭터를 찾을 수 없습니다.', 'status_code': 404}
                row = character_result[0]
                snapshot = {
                    'character_id': int(row.get('character_id') or 0) or None,
                    'char_name': str(row.get('char_name') or '').strip(),
                    'class_name': str(row.get('class_name') or '').strip(),
                    'class_emoji': str(row.get('class_emoji') or '').strip(),
                    'item_lvl': _parse_float(row.get('item_lvl')),
                    'combat_power': _parse_float(row.get('combat_power')),
                    'class_id': row.get('class_id'),
                }
            min_lvl = float(party_info.get('min_lvl') or 0)
            char_item_lvl = float(snapshot.get('item_lvl') or 0)
            if min_lvl > 0 and char_item_lvl < min_lvl:
                return {'party_id': party_id, 'message': f'아이템 레벨이 충족되지 않습니다. (필요: {min_lvl}, 보유: {char_item_lvl})', 'status_code': 404}
            role = int(role or 0)
            if role not in (0, 1):
                return {'party_id': party_id, 'message': 'role 값은 0(딜러) 또는 1(서포터)만 허용됩니다.', 'status_code': 400}
            if not self._is_temp_user(user_id):
                exists = await db.execute('SELECT 1 FROM participants WHERE party_id = ? AND user_id = ? LIMIT 1', (party_id, str(user_id)))
                if exists:
                    return {'party_id': party_id, 'user_id': str(user_id), 'message': '이미 파티에 참가한 유저입니다.', 'status_code': 409}
            cap = int(party_info.get('dealer') if role == 0 else party_info.get('supporter') or 0)
            closed = int(party_info.get('is_dealer_closed') if role == 0 else party_info.get('is_supporter_closed') or 0)
            if closed:
                return {'party_id': party_id, 'message': '마감된 역할입니다.', 'status_code': 403}
            cnt = await db.execute('SELECT COUNT(*) AS cnt FROM participants WHERE party_id = ? AND role = ?', (party_id, role))
            current_count = int(cnt[0]['cnt']) if cnt else 0
            waitlist_enabled = await self._is_waitlist_enabled(db, guild_id) if guild_id else False
            if current_count < cap:
                await db.execute(
                    'INSERT INTO participants (party_id, character_id, user_id, role, char_name, class_name, class_emoji, item_lvl, combat_power, joined_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                    (party_id, snapshot.get('character_id'), str(user_id), role, snapshot.get('char_name'), snapshot.get('class_name'), snapshot.get('class_emoji'), snapshot.get('item_lvl'), snapshot.get('combat_power')),
                )
                new_id = int(db.lastrowid or 0)
                await db.commit()
                fire_and_forget(self.update_discord_after_change(party_id), name='party:update_discord_after_join', timeout_sec=20, coalesce_key=self._discord_update_coalesce_key(party_id))
                return {'status_code': 201, 'joined': True, 'waitlisted': False, 'id': new_id, 'party_id': party_id, 'character_id': snapshot.get('character_id'), 'user_id': str(user_id), 'role': role, 'message': '파티에 성공적으로 참가했습니다.'}
            if not waitlist_enabled:
                return {'party_id': party_id, 'message': '딜러 자리가 모두 찼습니다.' if role == 0 else '서포터 자리가 모두 찼습니다.', 'status_code': 404}
            wl_row = await db.execute('SELECT id FROM party_waitlist WHERE party_id = ? AND user_id = ? AND role = ? LIMIT 1', (party_id, str(user_id), role))
            if wl_row:
                wl_id = int(wl_row[0]['id'])
                pos = await self._get_waitlist_position(db, party_id, role, wl_id)
                await db.commit()
                fire_and_forget(self._send_waitlist_registered_dm(party_id, str(user_id), pos, role), name='party:waitlist_registered_dm', timeout_sec=10)
                return {'status_code': 200, 'joined': False, 'waitlisted': True, 'party_id': party_id, 'user_id': str(user_id), 'role': role, 'waitlist_position': pos, 'message': '이미 대기열에 등록되어 있습니다.'}
            await db.execute(
                'INSERT INTO party_waitlist (party_id, character_id, user_id, role, char_name, class_name, class_emoji, item_lvl, combat_power, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)',
                (party_id, snapshot.get('character_id'), str(user_id), role, snapshot.get('char_name'), snapshot.get('class_name'), snapshot.get('class_emoji'), snapshot.get('item_lvl'), snapshot.get('combat_power')),
            )
            wl_id = int(db.lastrowid or 0)
            pos = await self._get_waitlist_position(db, party_id, role, wl_id)
            await db.commit()
        fire_and_forget(self._send_waitlist_registered_dm(party_id, str(user_id), pos, role), name='party:waitlist_registered_dm', timeout_sec=10)
        return {'status_code': 202, 'joined': False, 'waitlisted': True, 'party_id': party_id, 'user_id': str(user_id), 'role': role, 'waitlist_position': pos, 'message': '정원이 가득 차 대기열로 등록되었습니다.'}

    async def purge_user_participations_in_guild(self, guild_id: str, user_id: str) -> Dict | None:
        async with get_db() as db:
            guild_exists = await db.execute('SELECT 1 FROM party WHERE guild_id = ? LIMIT 1', (str(guild_id),))
            if not guild_exists:
                return None
            cancel_rows = await db.execute(
                '''SELECT p.id AS participant_id, p.party_id, p.user_id, p.char_name AS char_name, party.title, party.guild_id, party.thread_manage_id
                   FROM participants p LEFT JOIN party ON p.party_id = party.id
                   WHERE party.guild_id = ? AND p.user_id = ?''',
                (str(guild_id), str(user_id)),
            ) or []
            party_ids = sorted({row['party_id'] for row in cancel_rows}) if cancel_rows else []
            await db.execute('DELETE FROM participants WHERE user_id = ? AND party_id IN (SELECT id FROM party WHERE guild_id = ?)', (str(user_id), str(guild_id)))
            affected_rows = int(getattr(db, 'rowcount', 0) or 0)
            await db.commit()
            if cancel_rows and affected_rows > 0:
                for row in cancel_rows:
                    fire_and_forget(self._send_cancel_notification(row), name='party:cancel_notification', timeout_sec=10)
            for pid in party_ids:
                fire_and_forget(self.update_discord_after_change(pid), name='party:update_discord_after_purge', timeout_sec=20, coalesce_key=self._discord_update_coalesce_key(int(pid)))
                fire_and_forget(self.reconcile_waitlists_for_party(int(pid)), name='party:reconcile_waitlist_after_purge', timeout_sec=20, coalesce_key=self._waitlist_reconcile_coalesce_key(int(pid)))
            return {'guild_id': str(guild_id), 'user_id': str(user_id), 'deleted': True, 'affected_rows': affected_rows, 'affected_parties': party_ids, 'message': '해당 서버에서 유저의 모든 참가정보를 삭제했습니다.'}

    async def leave_party(self, party_id: int, user_id: str = None, participant_id: int = None) -> Dict:
        async with get_db() as db:
            party_row = await db.execute('SELECT id, guild_id FROM party WHERE id = ? LIMIT 1', (party_id,))
            if not party_row:
                raise Exception('파티를 찾을 수 없습니다.')
            guild_id = str(party_row[0].get('guild_id') or '')
            cancel_user_info = None
            left_role = None
            if participant_id:
                rows = await db.execute(
                    '''SELECT p.user_id, p.role, p.char_name AS char_name, party.title, party.guild_id, party.thread_manage_id
                       FROM participants p LEFT JOIN party ON p.party_id = party.id
                       WHERE p.party_id = ? AND p.id = ? LIMIT 1''',
                    (party_id, participant_id),
                )
                cancel_user_info = rows[0] if rows else None
            elif user_id:
                rows = await db.execute(
                    '''SELECT p.user_id, p.role, p.char_name AS char_name, party.title, party.guild_id, party.thread_manage_id
                       FROM participants p LEFT JOIN party ON p.party_id = party.id
                       WHERE p.party_id = ? AND p.user_id = ? LIMIT 1''',
                    (party_id, str(user_id)),
                )
                cancel_user_info = rows[0] if rows else None
            else:
                raise Exception('participant_id 또는 user_id가 필요합니다.')
            if cancel_user_info:
                left_role = int(cancel_user_info.get('role') or 0)
            if participant_id:
                await db.execute('DELETE FROM participants WHERE party_id = ? AND id = ?', (party_id, participant_id))
                identifier = participant_id
            else:
                await db.execute('DELETE FROM participants WHERE party_id = ? AND user_id = ?', (party_id, str(user_id)))
                identifier = str(user_id)
            affected_rows = int(getattr(db, 'rowcount', 0) or 0)
            if affected_rows <= 0:
                waitlisted = False
                if user_id:
                    wl = await db.execute('SELECT 1 FROM party_waitlist WHERE party_id = ? AND user_id = ? LIMIT 1', (party_id, str(user_id)))
                    waitlisted = bool(wl)
                await db.rollback()
                if waitlisted:
                    return {'party_id': party_id, 'identifier': identifier, 'deleted': False, 'affected_rows': 0, 'message': '대기열 참가 상태입니다. 대기열 취소를 이용해주세요.', 'status_code': 404}
                return {'party_id': party_id, 'identifier': identifier, 'deleted': False, 'affected_rows': 0, 'message': '파티 참가자가 아닙니다.', 'status_code': 404}
            promoted = None
            if affected_rows > 0 and left_role in (0, 1) and guild_id and await self._is_waitlist_enabled(db, guild_id):
                promoted = await self._promote_next_waiter_locked(db, party_id, left_role)
            await db.commit()
            if cancel_user_info and affected_rows > 0:
                fire_and_forget(self._send_cancel_notification(cancel_user_info), name='party:cancel_notification', timeout_sec=10)
            fire_and_forget(self.update_discord_after_change(party_id), name='party:update_discord_after_leave', timeout_sec=20, coalesce_key=self._discord_update_coalesce_key(party_id))
            if promoted:
                fire_and_forget(self._send_waitlist_promoted_dm(party_id, promoted['user_id'], promoted['role']), name='party:waitlist_promoted_dm', timeout_sec=10)
            return {'party_id': party_id, 'identifier': identifier, 'deleted': True, 'affected_rows': affected_rows, 'promoted': bool(promoted), 'promoted_user_id': promoted['user_id'] if promoted else None, 'message': '파티에서 성공적으로 나갔습니다.'}

    async def delete_party(self, party_id: int) -> Dict:
        async with get_db() as db:
            party_info = await db.execute('SELECT thread_manage_id FROM party WHERE id = ?', (party_id,))
            if not party_info:
                raise Exception('파티를 찾을 수 없습니다.')
            thread_id = party_info[0].get('thread_manage_id')
            await db.execute('DELETE FROM participants WHERE party_id = ?', (party_id,))
            await db.execute('DELETE FROM party_waitlist WHERE party_id = ?', (party_id,))
            await db.execute('DELETE FROM party WHERE id = ?', (party_id,))
            await db.commit()
            self._discord_payload_fingerprint.pop(int(party_id), None)
            self._discord_update_locks.pop(int(party_id), None)
        if thread_id:
            fire_and_forget(discord_service.delete_forum_post(str(thread_id)), name='party:delete_forum_post_on_delete', timeout_sec=20)
        return {'party_id': party_id, 'message': '파티가 성공적으로 삭제되었습니다.'}

    async def update_party(self, party_id: int, party_data: Dict) -> Dict:
        async with get_db() as db:
            party_result = await db.execute('SELECT id, thread_manage_id, guild_id FROM party WHERE id = ? LIMIT 1', (party_id,))
            if not party_result:
                return {'party_id': party_id, 'message': '파티를 찾을 수 없습니다.', 'status_code': 404}
            old_thread_id = party_result[0].get('thread_manage_id')
            current_guild_id = party_result[0].get('guild_id')
            raid_result = await db.execute('SELECT id, dealer, supporter FROM raid WHERE name = ? AND difficulty = ? LIMIT 1', (party_data['raid_name'], party_data['difficulty']))
            if not raid_result:
                raise Exception('해당 레이드를 찾을 수 없습니다.')
            raid_info = raid_result[0]
            parsed_start_date = parse_start_date(party_data.get('start_date', ''))
            await db.execute('UPDATE party SET title = ?, raid_id = ?, start_date = ?, owner = ?, message = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (
                party_data['title'], raid_info['id'], parsed_start_date, str(party_data.get('owner_id') or ''), party_data.get('message', ''), party_id,
            ))
            await db.commit()
            target_guild_id = party_data.get('guild_id') or current_guild_id
            discord_ok, new_thread_id, _ = await self._create_discord_post(db, party_id, target_guild_id, party_data, raid_info)
            if discord_ok and new_thread_id:
                await db.execute('UPDATE party SET thread_manage_id = ?, guild_id = ? WHERE id = ?', (str(new_thread_id), str(target_guild_id), party_id))
                await db.commit()
                if old_thread_id and str(old_thread_id) != str(new_thread_id):
                    fire_and_forget(discord_service.delete_forum_post(str(old_thread_id)), name='party:delete_old_forum_post_on_update', timeout_sec=20)
                return {'party_id': party_id, 'updated': True, 'discord_created': True, 'thread_id': new_thread_id, 'message': '파티 정보가 성공적으로 수정되었습니다.'}
            return {'party_id': party_id, 'updated': True, 'discord_created': False, 'thread_id': old_thread_id, 'message': '파티 기본 정보만 수정되었으며, 새 스레드 생성은 실패했습니다.'}

    async def update_discord_after_change(self, party_id: int):
        try:
            pid = int(party_id)
            lock = self._get_discord_update_lock(pid)
            async with lock:
                party_info = None
                async with get_db() as db:
                    party_result = await db.execute(
                        '''SELECT p.id, p.title, p.guild_id, p.raid_id, p.start_date, p.owner, p.message,
                                  p.thread_manage_id, p.is_dealer_closed, p.is_supporter_closed,
                                  r.name AS raid_name, r.difficulty, r.dealer, r.supporter
                           FROM party p LEFT JOIN raid r ON p.raid_id = r.id WHERE p.id = ?''',
                        (pid,),
                    )
                    if party_result:
                        party_info = party_result[0]
                        party_info['participants'] = await self.get_participants_data_with_db(db, pid)
                if not party_info:
                    self._discord_payload_fingerprint.pop(pid, None)
                    self._discord_update_locks.pop(pid, None)
                    return
                thread_id = party_info.get('thread_manage_id')
                if not thread_id:
                    return
                fingerprint = self._build_discord_payload_fingerprint(party_info)
                if self._is_duplicate_discord_payload(pid, fingerprint):
                    return
                await discord_service.update_forum_post(str(thread_id), party_info)
                self._remember_discord_payload_fingerprint(pid, fingerprint)
        except Exception:
            logger.exception('discord update after party change failed')

    async def _create_discord_post(self, db: DatabaseManager, party_id: int, guild_id: int, party_data: Dict, raid_info: Dict) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        try:
            server_result = await db.execute('SELECT forum_channel_id FROM server WHERE guild_id = ? LIMIT 1', (str(guild_id),))
            if not server_result or not server_result[0].get('forum_channel_id'):
                return False, None, {'stage': 'lookup_forum_channel', 'detail': '서버에 forum_channel_id가 설정되어 있지 않음', 'guild_id': str(guild_id)}
            forum_channel_id = server_result[0]['forum_channel_id']
            discord_party_data = {
                'id': party_id,
                'title': party_data['title'],
                'raid_name': party_data['raid_name'],
                'difficulty': party_data['difficulty'],
                'message': party_data.get('message', ''),
                'dealer': raid_info['dealer'],
                'supporter': raid_info['supporter'],
                'owner': party_data.get('owner_id', ''),
                'participants': await self.get_participants_data_with_db(db, party_id),
            }
            result = await discord_service.create_forum_post(str(forum_channel_id), discord_party_data, str(guild_id))
            if not isinstance(result, dict) or 'id' not in result:
                return False, None, {'stage': 'discord_create_forum_post', 'detail': '응답에 thread id가 없음', 'result_preview': json.dumps(result, ensure_ascii=False, default=str)[:500]}
            thread_id = str(result['id'])
            await db.execute('UPDATE party SET thread_manage_id = ?, guild_id = ? WHERE id = ?', (thread_id, str(guild_id), party_id))
            return True, thread_id, None
        except Exception as e:
            logger.exception('discord post creation failed')
            return False, None, {'stage': 'exception', 'detail': str(e), 'type': e.__class__.__name__}

    async def _is_waitlist_enabled(self, db: DatabaseManager, guild_id: str) -> bool:
        row = await db.execute('SELECT waitlist_use FROM server WHERE guild_id = ? LIMIT 1', (str(guild_id),))
        return bool(row and int(row[0].get('waitlist_use') or 0) == 1)

    async def _get_waitlist_position(self, db: DatabaseManager, party_id: int, role: int, wait_id: int) -> int:
        row = await db.execute('SELECT COUNT(*) AS c FROM party_waitlist WHERE party_id = ? AND role = ? AND id <= ?', (party_id, int(role), int(wait_id)))
        return int(row[0]['c'] or 0) if row else 0

    async def _get_party_dm_info(self, db: DatabaseManager, party_id: int) -> Optional[Dict[str, Any]]:
        row = await db.execute(
            '''SELECT p.id, p.title, p.start_date, p.guild_id, p.thread_manage_id, r.name AS raid_name, r.difficulty
               FROM party p LEFT JOIN raid r ON p.raid_id = r.id WHERE p.id = ? LIMIT 1''',
            (party_id,),
        )
        return row[0] if row else None

    def _format_start_date(self, dt: Any) -> str:
        if dt is None:
            return '-'
        try:
            import datetime as _dt
            if isinstance(dt, str):
                return _dt.datetime.fromisoformat(dt.replace(' ', 'T')).strftime('%Y-%m-%d %H:%M')
            return dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            return str(dt)

    def _build_party_link(self, party: Dict[str, Any]) -> Optional[str]:
        gid = str(party.get('guild_id') or '')
        tid = str(party.get('thread_manage_id') or '')
        if gid.isdigit() and tid.isdigit():
            return f'https://discord.com/channels/{gid}/{tid}'
        return None

    async def _send_waitlist_registered_dm(self, party_id: int, user_id: str, position: int, role: int) -> None:
        if not str(user_id).isdigit():
            return
        async with get_db() as db:
            party = await self._get_party_dm_info(db, party_id)
        if not party:
            return
        role_name = '딜러' if int(role) == 0 else '서포터'
        start_text = self._format_start_date(party.get('start_date'))
        link = self._build_party_link(party)
        title_text = party.get('title') or '파티'
        desc = f"**{title_text}**\n{role_name} 자리가 현재 정원 초과라서 **{int(position)}번째 대기열**로 등록되었습니다.\n\n자리가 생기면 자동으로 참가가 확정되고, DM으로 알려드릴게요."
        if link:
            desc += f'\n\n파티 링크: {link}'
        embed = {'title': '대기열 등록 완료', 'description': desc, 'color': 0x5865F2, 'fields': [{'name': '일정', 'value': start_text, 'inline': True}, {'name': '레이드', 'value': f"{party.get('raid_name') or '-'} {party.get('difficulty') or ''}".strip(), 'inline': True}, {'name': '대기 순번', 'value': f'{int(position)}번', 'inline': True}]}
        await discord_service.send_dm(str(user_id), embed=embed)

    async def _send_waitlist_promoted_dm(self, party_id: int, user_id: str, role: int) -> None:
        if not str(user_id).isdigit():
            return
        async with get_db() as db:
            party = await self._get_party_dm_info(db, party_id)
        if not party:
            return
        role_name = '딜러' if int(role) == 0 else '서포터'
        start_text = self._format_start_date(party.get('start_date'))
        link = self._build_party_link(party)
        title_text = party.get('title') or '파티'
        desc = f"대기열에서 순번이 돌아와 **참가가 확정**되었습니다.\n\n**{title_text}**\n역할: **{role_name}**\n일정: **{start_text}**"
        if link:
            desc += f'\n\n파티 링크: {link}'
        await discord_service.send_dm(str(user_id), embed={'title': '참가 확정', 'description': desc, 'color': 0x57F287})

    async def _promote_next_waiter_locked(self, db: DatabaseManager, party_id: int, role: int) -> Optional[Dict[str, Any]]:
        w = await db.execute('SELECT id, user_id, character_id, role, char_name, class_name, class_emoji, item_lvl, combat_power FROM party_waitlist WHERE party_id = ? AND role = ? ORDER BY id ASC LIMIT 1', (party_id, int(role)))
        if not w:
            return None
        w0 = w[0]
        uid = str(w0.get('user_id') or '')
        cid = int(w0.get('character_id') or 0) or None
        r = int(w0.get('role') or 0)
        if not self._is_temp_user(uid):
            dup = await db.execute('SELECT 1 FROM participants WHERE party_id = ? AND user_id = ? LIMIT 1', (party_id, uid))
            if dup:
                await db.execute('DELETE FROM party_waitlist WHERE id = ?', (int(w0['id']),))
                return None
        await db.execute('INSERT INTO participants (party_id, character_id, user_id, role, char_name, class_name, class_emoji, item_lvl, combat_power, joined_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)', (party_id, cid, uid, r, w0.get('char_name'), w0.get('class_name'), w0.get('class_emoji'), w0.get('item_lvl'), w0.get('combat_power')))
        await db.execute('DELETE FROM party_waitlist WHERE id = ?', (int(w0['id']),))
        return {'user_id': uid, 'role': r}

    async def get_waitlist(self, party_id: int, role: Optional[int] = None) -> Dict[str, List[Dict[str, Any]]]:
        async with get_db() as db:
            query = '''SELECT w.id AS wait_id, w.role, w.user_id, w.created_at, w.character_id,
                              COALESCE(w.char_name, c.char_name) AS char_name,
                              COALESCE(w.item_lvl, c.item_lvl) AS item_lvl,
                              COALESCE(w.combat_power, c.combat_power) AS combat_power,
                              COALESCE(w.class_name, cl.name) AS class_name,
                              COALESCE(w.class_emoji, cl.emoji) AS class_emoji
                       FROM party_waitlist w LEFT JOIN character c ON w.character_id = c.id LEFT JOIN class cl ON c.class_id = cl.id WHERE w.party_id = ?'''
            params: list[Any] = [party_id]
            if role in (0, 1):
                query += ' AND w.role = ?'
                params.append(int(role))
            query += ' ORDER BY w.role ASC, w.id ASC'
            rows = await db.execute(query, tuple(params)) or []
        dealers: List[Dict[str, Any]] = []
        supporters: List[Dict[str, Any]] = []
        d_pos = 0
        s_pos = 0
        for r in rows:
            rr = int(r.get('role') or 0)
            base = {'wait_id': r.get('wait_id'), 'user_id': r.get('user_id'), 'character_id': r.get('character_id'), 'name': r.get('char_name'), 'item_level': r.get('item_lvl'), 'combat_power': r.get('combat_power'), 'class_name': r.get('class_name'), 'class_emoji': r.get('class_emoji')}
            if rr == 0:
                d_pos += 1
                dealers.append({'position': d_pos, **base})
            else:
                s_pos += 1
                supporters.append({'position': s_pos, **base})
        return {'dealers': dealers, 'supporters': supporters}

    async def get_waitlist_my_position(self, party_id: int, user_id: str, role: int) -> Dict[str, Any]:
        role = int(role or 0)
        async with get_db() as db:
            row = await db.execute('SELECT id FROM party_waitlist WHERE party_id = ? AND user_id = ? AND role = ? LIMIT 1', (party_id, str(user_id), role))
            if not row:
                return {'party_id': party_id, 'user_id': str(user_id), 'role': role, 'in_waitlist': False}
            wid = int(row[0]['id'])
            pos = await self._get_waitlist_position(db, party_id, role, wid)
            return {'party_id': party_id, 'user_id': str(user_id), 'role': role, 'in_waitlist': True, 'wait_id': wid, 'position': pos}

    async def cancel_waitlist(self, party_id: int, user_id: str, role: int = 0) -> Dict[str, Any]:
        role = int(role or 0)
        async with get_db() as db:
            await db.execute('DELETE FROM party_waitlist WHERE party_id = ? AND user_id = ? AND role = ?', (party_id, str(user_id), role))
            affected = int(getattr(db, 'rowcount', 0) or 0)
            await db.commit()
        return {'status_code': 200, 'party_id': party_id, 'user_id': str(user_id), 'role': role, 'deleted': affected > 0, 'affected_rows': affected}

    async def reconcile_waitlists_for_party(self, party_id: int) -> Dict[str, Any]:
        promotions: List[Dict[str, Any]] = []
        async with get_db() as db:
            pr = await db.execute('SELECT p.id, p.guild_id, r.dealer, r.supporter FROM party p JOIN raid r ON p.raid_id = r.id WHERE p.id = ? LIMIT 1', (party_id,))
            if not pr:
                return {'party_id': party_id, 'promoted': 0}
            guild_id = str(pr[0].get('guild_id') or '')
            if not guild_id or not await self._is_waitlist_enabled(db, guild_id):
                return {'party_id': party_id, 'promoted': 0}
            max_dealer = int(pr[0].get('dealer') or 0)
            max_supporter = int(pr[0].get('supporter') or 0)
            for r in (0, 1):
                max_count = max_dealer if r == 0 else max_supporter
                cnt_row = await db.execute('SELECT COUNT(*) AS cnt FROM participants WHERE party_id = ? AND role = ?', (party_id, r))
                cur = int(cnt_row[0]['cnt']) if cnt_row else 0
                while cur < max_count:
                    promoted = await self._promote_next_waiter_locked(db, party_id, r)
                    if not promoted:
                        break
                    promotions.append(promoted)
                    cur += 1
            await db.commit()
        if promotions:
            fire_and_forget(self.update_discord_after_change(party_id), name='party:update_discord_after_waitlist_reconcile', timeout_sec=20, coalesce_key=self._discord_update_coalesce_key(party_id))
            for p in promotions:
                fire_and_forget(self._send_waitlist_promoted_dm(party_id, p['user_id'], p['role']), name='party:waitlist_promoted_dm', timeout_sec=10)
        return {'party_id': party_id, 'promoted': len(promotions), 'promotions': promotions}

    async def reconcile_waitlists_tick(self, max_parties: int = 50) -> Dict[str, Any]:
        async with get_db() as db:
            rows = await db.execute('SELECT DISTINCT party_id FROM party_waitlist ORDER BY party_id DESC LIMIT ?', (int(max_parties or 50),)) or []
        promoted_total = 0
        parties = 0
        for r in rows:
            pid = int(r.get('party_id'))
            parties += 1
            res = await self.reconcile_waitlists_for_party(pid)
            promoted_total += int(res.get('promoted') or 0)
        return {'checked_parties': parties, 'promoted_total': promoted_total}

    async def _send_cancel_notification(self, cancel_info: Dict):
        try:
            guild_id = cancel_info.get('guild_id')
            if not guild_id:
                return
            async with get_db() as db:
                server_result = await db.execute('SELECT cancel_join_channel_id FROM server WHERE guild_id = ? LIMIT 1', (str(guild_id),))
            if not server_result or not server_result[0].get('cancel_join_channel_id'):
                return
            cancel_channel_id = server_result[0]['cancel_join_channel_id']
            user_id = cancel_info['user_id']
            char_name = cancel_info.get('char_name') or cancel_info.get('name') or '알 수 없음'
            party_title = cancel_info.get('title', '알 수 없는 파티')
            thread_id = cancel_info.get('thread_manage_id')
            party_title_display = f'**{party_title}**'
            if thread_id:
                party_title_display = f'[{party_title}](https://discord.com/channels/{guild_id}/{thread_id})'
            embed_data = {'title': '🚪 파티 참가 취소', 'description': f'{party_title_display} 파티에서 참가자가 취소하였어요.', 'color': 0xFF6B6B, 'fields': [{'name': '유저', 'value': f'<@{user_id}>', 'inline': True}, {'name': '캐릭터', 'value': f'**{char_name}**', 'inline': True}]}
            await discord_service.send_to_channel(str(cancel_channel_id), embed=embed_data)
        except Exception:
            logger.exception('cancel notification send failed')


party_service = PartyService()
