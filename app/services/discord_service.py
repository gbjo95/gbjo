from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, List

import httpx

from app.core.config import BOT_TOKEN
from app.database.connection import get_db

logger = logging.getLogger(__name__)


class DiscordService:
    def __init__(self) -> None:
        self.bot_token = BOT_TOKEN
        self.base_url = 'https://discord.com/api/v10'
        self._client: httpx.AsyncClient | None = None

    async def _client_get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=20.0)
        return self._client

    async def _api(self, method: str, endpoint: str, *, json_payload: Dict[str, Any] | None = None, params: Dict[str, Any] | None = None, multipart: Dict[str, Any] | None = None) -> httpx.Response | None:
        if not self.bot_token:
            return None
        client = await self._client_get()
        headers = {'Authorization': f'Bot {self.bot_token}'}
        if multipart is None:
            headers['Content-Type'] = 'application/json'
        try:
            if multipart is not None:
                return await client.request(method, f'{self.base_url}{endpoint}', headers=headers, files=multipart)
            return await client.request(method, f'{self.base_url}{endpoint}', headers=headers, json=json_payload, params=params)
        except Exception:
            logger.exception('discord api call failed: %s %s', method, endpoint)
            return None

    async def get_guild_roles(self, guild_id: str) -> List[Dict[str, Any]]:
        resp = await self._api('GET', f'/guilds/{guild_id}/roles')
        if resp and resp.status_code == 200:
            try:
                return resp.json()
            except Exception:
                return []
        return []

    async def _create_mention_content(self, party_data: Dict[str, Any], guild_id: str | None = None) -> str:
        mentions: list[str] = []
        if guild_id:
            async with get_db() as db:
                row = await db.execute('SELECT mention_role_id FROM server WHERE guild_id = ? LIMIT 1', (str(guild_id),))
            if row and row[0].get('mention_role_id'):
                mentions.append(f"<@&{row[0]['mention_role_id']}>")
        return ' '.join(mentions)

    async def _get_user_emojis(self, user_ids: List[str]) -> Dict[str, str]:
        if not user_ids:
            return {}
        marks = ','.join(['?'] * len(user_ids))
        async with get_db() as db:
            rows = await db.execute(f'SELECT user_id, emoji FROM user WHERE user_id IN ({marks})', tuple(user_ids))
        return {str(r['user_id']): r.get('emoji') or '' for r in rows or []}

    async def _create_party_embed(self, party_data: Dict[str, Any], thumb_url: str | None = None) -> Dict[str, Any]:
        dealers = (party_data.get('participants') or {}).get('dealers') or []
        supporters = (party_data.get('participants') or {}).get('supporters') or []
        dealer_max = int(party_data.get('dealer') or 0)
        supporter_max = int(party_data.get('supporter') or 0)
        owner_display = party_data.get('owner') or party_data.get('owner_id') or '알수없음'
        user_ids = [str(p.get('user_id')) for p in dealers + supporters if p.get('user_id')]
        emojis = await self._get_user_emojis(user_ids)

        def format_member(p: Dict[str, Any], tag: str) -> str:
            uid = str(p.get('user_id') or '')
            mention = f'<@{uid}>' if uid.isdigit() else '**[용병]**'
            e = emojis.get(uid, '')
            return f"{mention} {tag} {p.get('name') or '-'}\n-# Lv. {p.get('item_level') or '-'} | {p.get('class_name') or '-'} | {p.get('combat_power') or '-'} {e}".strip()

        fields = [
            {'name': f"딜러 **`({len(dealers)}/{dealer_max})`**", 'value': '\n'.join(format_member(p, '⚔️') for p in dealers) or '== 없음 ==', 'inline': False},
            {'name': f"서포터 **`({len(supporters)}/{supporter_max})`**", 'value': '\n'.join(format_member(p, '🛡️') for p in supporters) or '== 없음 ==', 'inline': False},
        ]
        embed = {
            'title': f"**{party_data.get('title', '공격대')}**",
            'description': f"-# 공격대 생성자 : <@{owner_display}>\nㅤ",
            'color': 0xFFFFFF,
            'fields': fields,
            'footer': {'text': f"생성시각 {(datetime.now() + timedelta(hours=9)).strftime('%Y-%m-%d %H:%M')}"},
        }
        if thumb_url:
            embed['thumbnail'] = {'url': thumb_url}
        return embed

    def _create_party_components(self, party_id: int, *, disabled_join: bool = False, join_label: str = '참가신청', show_waitlist_check: bool = False) -> List[Dict[str, Any]]:
        components = [
            {'type': 2, 'style': 3, 'label': join_label, 'custom_id': f'party_join_{party_id}', 'disabled': disabled_join},
            {'type': 2, 'style': 4, 'label': '참가취소', 'custom_id': f'party_leave_{party_id}'},
        ]
        if show_waitlist_check:
            components.insert(1, {'type': 2, 'style': 2, 'label': '대기열 확인', 'custom_id': f'party_waitlist_{party_id}'})
        return [{'type': 1, 'components': components}]

    def _create_manage_components(self, party_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        party_id = party_data.get('id') or party_data.get('party_id')
        return [{
            'type': 1,
            'components': [
                {'type': 2, 'style': 2, 'label': '인원 관리', 'custom_id': f'party_force_cancel_{party_id}'},
                {'type': 2, 'style': 2, 'label': '인원 멘션', 'custom_id': f'party_mention_{party_id}'},
                {'type': 2, 'style': 2, 'label': '일정 공개', 'custom_id': f'party_public_{party_id}'},
                {'type': 2, 'style': 4, 'label': '일정 삭제', 'custom_id': f'party_delete_{party_id}'},
            ],
        }]

    async def _resolve_waitlist_enabled(self, party_data: Dict[str, Any]) -> bool:
        try:
            v = party_data.get('waitlist_use')
            if v is not None:
                return int(v or 0) == 1
        except Exception:
            pass
        gid = party_data.get('guild_id')
        if not gid:
            return False
        async with get_db() as db:
            row = await db.execute('SELECT waitlist_use FROM server WHERE guild_id = ? LIMIT 1', (str(gid),))
        return bool(row and int(row[0].get('waitlist_use') or 0) == 1)

    async def create_forum_post(self, forum_channel_id: str, party_data: Dict[str, Any], guild_id: str | None = None) -> Dict[str, Any]:
        embed = await self._create_party_embed(party_data)
        content = await self._create_mention_content(party_data, guild_id)
        payload = {
            'name': party_data['title'],
            'message': {'content': content, 'embeds': [embed], 'components': self._create_party_components(int(party_data['id']))},
            'auto_archive_duration': 10080,
        }
        resp = await self._api('POST', f'/channels/{forum_channel_id}/threads', json_payload=payload)
        if not resp:
            return {'_error': {'detail': 'no response'}}
        try:
            data = resp.json()
        except Exception:
            return {'_error': {'detail': f'status={resp.status_code}'}}
        if resp.status_code not in (200, 201) or 'id' not in data:
            return {'_error': {'status': resp.status_code, 'detail': data}}
        thread_id = str(data['id'])
        await self._api('POST', f'/channels/{thread_id}/messages', json_payload={'components': self._create_manage_components(party_data)})
        return {'id': thread_id}

    async def update_forum_post(self, thread_id: str, party_data: Dict[str, Any]) -> bool:
        embed = await self._create_party_embed(party_data)
        dealers = (party_data.get('participants') or {}).get('dealers') or []
        supporters = (party_data.get('participants') or {}).get('supporters') or []
        total_slots = int(party_data.get('dealer') or 0) + int(party_data.get('supporter') or 0)
        total_now = len(dealers) + len(supporters)
        is_full = total_slots > 0 and total_now >= total_slots
        waitlist_enabled = await self._resolve_waitlist_enabled(party_data)
        both_closed = int(party_data.get('is_dealer_closed') or 0) == 1 and int(party_data.get('is_supporter_closed') or 0) == 1
        join_label = '대기열 신청' if waitlist_enabled and is_full and not both_closed else '참가신청'
        disabled_join = bool(both_closed or (is_full and not waitlist_enabled))
        show_waitlist_check = bool(waitlist_enabled and is_full and not both_closed)
        content = f"<@{party_data.get('owner') or party_data.get('owner_id')}>"
        resp = await self._api('PATCH', f'/channels/{thread_id}/messages/{thread_id}', json_payload={
            'content': content,
            'embeds': [embed],
            'components': self._create_party_components(int(party_data['id']), disabled_join=disabled_join, join_label=join_label, show_waitlist_check=show_waitlist_check),
        })
        return bool(resp and resp.status_code == 200)

    async def delete_forum_post(self, thread_id: str) -> bool:
        resp = await self._api('DELETE', f'/channels/{thread_id}')
        return bool(resp and resp.status_code in (200, 202, 204))

    async def safe_delete_forum_post(self, thread_id: str, reason: str = '') -> bool:
        return await self.delete_forum_post(thread_id)

    async def send_to_thread(self, thread_id: str, content: str | None = None, embed: Dict[str, Any] | None = None) -> bool:
        payload: Dict[str, Any] = {}
        if content:
            payload['content'] = content
        if embed:
            payload['embeds'] = [embed]
        resp = await self._api('POST', f'/channels/{thread_id}/messages', json_payload=payload)
        return bool(resp and resp.status_code in (200, 201))

    async def send_to_channel(self, channel_id: str, content: str | None = None, embed: Dict[str, Any] | None = None, *, allowed_mentions: Dict[str, Any] | None = None) -> bool:
        payload: Dict[str, Any] = {}
        if content:
            payload['content'] = content
        if embed:
            payload['embeds'] = [embed]
        if allowed_mentions is not None:
            payload['allowed_mentions'] = allowed_mentions
        resp = await self._api('POST', f'/channels/{channel_id}/messages', json_payload=payload)
        return bool(resp and resp.status_code in (200, 201))

    async def send_dm(self, user_id: str, content: str | None = None, embed: Dict[str, Any] | None = None) -> bool:
        resp = await self._api('POST', '/users/@me/channels', json_payload={'recipient_id': str(user_id)})
        if not resp or resp.status_code not in (200, 201):
            return False
        try:
            channel_id = resp.json().get('id')
        except Exception:
            return False
        return await self.send_to_channel(str(channel_id), content=content, embed=embed)

    async def fetch_channel_messages(self, channel_id: str, *, limit_total: int = 5000, batch_size: int = 100) -> tuple[bool, list[dict], dict | None]:
        out: list[dict] = []
        before: str | None = None
        while len(out) < limit_total:
            params = {'limit': min(batch_size, limit_total - len(out))}
            if before:
                params['before'] = before
            resp = await self._api('GET', f'/channels/{channel_id}/messages', params=params)
            if not resp or resp.status_code != 200:
                return False, out, {'status': resp.status_code if resp else None}
            try:
                data = resp.json() or []
            except Exception:
                data = []
            if not data:
                break
            out.extend(data)
            before = str(data[-1].get('id') or '')
            if len(data) < params['limit']:
                break
        return True, out, None


discord_service = DiscordService()
