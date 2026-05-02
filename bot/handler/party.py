import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import discord

from bot.commands.party_manage import MemberDropdown, MentionTypeView, permission_check
from bot.core.config import SUPPORTER_CLASSES
from bot.core.http_client import http_client

_SUCCESS = {200, 201, 204}


def _format_character_summary(ch: Dict[str, Any]) -> str:
    return f"클래스: {ch.get('class_name', 'Unknown')} | 아이템 레벨: {ch.get('item_lvl', 'N/A')} | 전투력: {ch.get('combat_power', 'N/A')}"


def _flatten_participants(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    participants = []
    raw = data.get('participants') or {}
    for role, plist in raw.items():
        for p in plist:
            item = dict(p)
            item['role'] = 0 if role == 'dealers' else 1
            participants.append(item)
    return participants


async def _send_ephemeral(interaction: discord.Interaction, content: str, **kwargs):
    if not interaction.response.is_done():
        await interaction.response.send_message(content, ephemeral=True, **kwargs)
    else:
        await interaction.followup.send(content, ephemeral=True, **kwargs)


def _character_payload(data: Dict[str, Any], character_id: Optional[int] = None) -> Dict[str, Any]:
    payload = {
        'char_name': data.get('char_name') or '',
        'class_name': data.get('class_name') or '',
        'class_emoji': data.get('class_emoji') or '',
        'item_lvl': data.get('item_lvl') or 0,
        'combat_power': data.get('combat_power') or 0,
    }
    if character_id:
        payload['character_id'] = int(character_id)
    return payload


async def auto_join_party_by_nickname(party_id: int, user_id: Any, nickname: str) -> tuple[bool, str]:
    nickname = (nickname or '').strip()
    if not nickname:
        return False, '닉네임이 비어 있습니다.'
    encoded = urllib.parse.quote(nickname, safe='')
    resp = await http_client.get(f'/character/search/{encoded}')
    if resp.status_code != 200:
        return False, f"'{nickname}' 캐릭터를 찾을 수 없습니다."
    payload = resp.json() or {}
    data = payload.get('data')
    if not data:
        return False, f"'{nickname}' 캐릭터 정보를 가져올 수 없습니다."
    class_name = (data.get('class_name') or '').strip()
    role = 1 if class_name in SUPPORTER_CLASSES else 0
    code, message = await _post_join(int(party_id), user_id, _character_payload(data), role)
    if code in (200, 201, 204):
        return True, message or '파티에 성공적으로 참가했습니다.'
    return False, message or '파티 참가에 실패했습니다.'


async def _post_join(party_id: int, user_id: Any, character: Dict[str, Any], role: int) -> tuple[Optional[int], Optional[str]]:
    resp = await http_client.post(f'/party/{party_id}/join', json={'user_id': str(user_id), 'role': role, 'character': character, 'character_id': character.get('character_id')})
    try:
        response_data = resp.json()
        message = response_data.get('message', '')
    except Exception:
        message = '파티에 성공적으로 참가했습니다.' if resp.status_code in (200, 201, 204) else '파티 참가에 실패하였어요.'
    return resp.status_code, message


class RoleSelectView(discord.ui.View):
    def __init__(self, party_id: int, character_data: Dict[str, Any], user_id: Any):
        super().__init__(timeout=60)
        self.party_id = int(party_id)
        self.character_data = character_data
        self.user_id = str(user_id)

    async def _join_party(self, interaction: discord.Interaction, role: int):
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            pass
        code, message = await _post_join(self.party_id, self.user_id, self.character_data, role)
        if code in (200, 201, 204):
            role_name = '서포터' if role == 1 else '딜러'
            await interaction.edit_original_response(content=f"**{self.character_data['char_name']}** ({role_name})로 파티에 참가했습니다!\n" + _format_character_summary(self.character_data), view=None)
        else:
            await interaction.edit_original_response(content=message, view=None)

    @discord.ui.button(label='딜러', style=discord.ButtonStyle.primary)
    async def dealer_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._join_party(interaction, role=0)

    @discord.ui.button(label='서포터', style=discord.ButtonStyle.success)
    async def supporter_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._join_party(interaction, role=1)


class RegisteredCharacterSelect(discord.ui.Select):
    def __init__(self, party_id: int, characters: List[Dict[str, Any]], user_id: Any):
        options = []
        self._char_map: Dict[str, Dict[str, Any]] = {}
        for i, ch in enumerate(characters[:25]):
            char_id = ch.get('char_id')
            value = str(char_id) if char_id else f'unknown_{i}'
            options.append(discord.SelectOption(label=str(f"{ch.get('char_name', '이름없음')} (레벨 {ch.get('item_lvl', 'N/A')})")[:100], description=str(f"{ch.get('class_name', '직업정보없음')} | 전투력 {ch.get('combat_power', 'N/A')}")[:100], value=value))
            self._char_map[value] = ch
        super().__init__(placeholder='등록된 캐릭터를 선택하세요...', options=options, min_values=1, max_values=1)
        self.party_id = int(party_id)
        self.user_id = str(user_id)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        ch = self._char_map.get(self.values[0])
        if not ch:
            await interaction.followup.send('선택한 캐릭터 정보를 찾을 수 없습니다.', ephemeral=True)
            return
        char_id = ch.get('char_id')
        if not char_id:
            await interaction.followup.send('캐릭터 ID를 찾을 수 없습니다.', ephemeral=True)
            return
        payload = _character_payload(ch, character_id=int(char_id))
        if (ch.get('class_name') or '') in SUPPORTER_CLASSES:
            await interaction.followup.send(f"**{ch.get('char_name', '이름없음')}** ({ch.get('class_name', '')})은(는) 딜러/서포터 모두 가능해요.\n원하는 역할을 선택하세요.", view=RoleSelectView(self.party_id, payload, self.user_id), ephemeral=True)
            return
        code, message = await _post_join(self.party_id, self.user_id, payload, role=0)
        if code in (200, 201, 204):
            await interaction.followup.send(f"**{ch.get('char_name', '이름없음')}** 캐릭터로 파티에 참가했습니다!\n" + _format_character_summary(ch) + (f"\n✅ {message}" if message else ''), ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {message or '파티 참가 중 오류가 발생했습니다.'}", ephemeral=True)


class CharacterNicknameModal(discord.ui.Modal):
    def __init__(self, party_id: int, user_id: Any):
        super().__init__(title='캐릭터 닉네임 입력')
        self.party_id = int(party_id)
        self.user_id = str(user_id)
        self.add_item(discord.ui.InputText(label='캐릭터 닉네임', placeholder='참가할 캐릭터의 닉네임을 입력하세요...', required=True, max_length=100))

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        nickname = self.children[0].value.strip()
        encoded = urllib.parse.quote(nickname, safe='')
        resp = await http_client.get(f'/character/search/{encoded}')
        if resp.status_code != 200:
            await interaction.followup.send(f"'{nickname}' 캐릭터를 찾을 수 없습니다.", ephemeral=True)
            return
        data = (resp.json() or {}).get('data')
        if not data:
            await interaction.followup.send(f"'{nickname}' 캐릭터 정보를 가져올 수 없습니다.", ephemeral=True)
            return
        payload = _character_payload(data)
        if (data.get('class_name') or '') in SUPPORTER_CLASSES:
            await interaction.followup.send(f"**{data['char_name']}** ({data.get('class_name', '')})은(는) 딜러/서포터 모두 가능해요.\n원하는 역할을 선택하세요.", view=RoleSelectView(self.party_id, payload, self.user_id), ephemeral=True)
            return
        code, message = await _post_join(self.party_id, self.user_id, payload, role=0)
        if code in (200, 201, 204):
            await interaction.followup.send(f"**{data['char_name']}** 캐릭터로 파티에 참가했습니다!\n" + _format_character_summary(data) + (f"\n✅ {message}" if message else ''), ephemeral=True)
        else:
            await interaction.followup.send(f"❌ {message or '파티 참가 중 오류가 발생했습니다.'}", ephemeral=True)


class ForceCancelView(discord.ui.View):
    def __init__(self, party_id: int, participants: List[Dict[str, Any]], is_dealer_closed: int, is_supporter_closed: int):
        super().__init__(timeout=180)
        self.party_id = int(party_id)
        self.is_dealer_closed = int(is_dealer_closed or 0)
        self.is_supporter_closed = int(is_supporter_closed or 0)
        if participants:
            self.add_item(MemberDropdown(participants, party_id))
        self.dealer_button = discord.ui.Button(custom_id=f'party_dealer_toggle_{party_id}', label='딜러 마감', style=discord.ButtonStyle.danger)
        self.dealer_button.callback = self._on_dealer_toggle
        self.add_item(self.dealer_button)
        self.supporter_button = discord.ui.Button(custom_id=f'party_supporter_toggle_{party_id}', label='서포터 마감', style=discord.ButtonStyle.danger)
        self.supporter_button.callback = self._on_supporter_toggle
        self.add_item(self.supporter_button)
        self._sync_buttons()

    def _sync_buttons(self):
        self.dealer_button.label = '딜러 마감 해제' if self.is_dealer_closed else '딜러 마감'
        self.dealer_button.style = discord.ButtonStyle.success if self.is_dealer_closed else discord.ButtonStyle.danger
        self.supporter_button.label = '서포터 마감 해제' if self.is_supporter_closed else '서포터 마감'
        self.supporter_button.style = discord.ButtonStyle.success if self.is_supporter_closed else discord.ButtonStyle.danger

    async def _toggle(self, interaction: discord.Interaction, role: int):
        resp = await http_client.post(f'/party/{self.party_id}/toggle/{role}')
        if resp.status_code != 200:
            await _send_ephemeral(interaction, '상태 변경 실패')
            return
        payload = resp.json() or {}
        self.is_dealer_closed = int(payload.get('is_dealer_closed', self.is_dealer_closed))
        self.is_supporter_closed = int(payload.get('is_supporter_closed', self.is_supporter_closed))
        self._sync_buttons()
        if not interaction.response.is_done():
            await interaction.response.edit_message(view=self)
        else:
            await interaction.edit_original_response(view=self)

    async def _on_dealer_toggle(self, interaction: discord.Interaction):
        await self._toggle(interaction, role=0)

    async def _on_supporter_toggle(self, interaction: discord.Interaction):
        await self._toggle(interaction, role=1)


class PartyJoinSelectView(discord.ui.View):
    def __init__(self, party_id: int, user_id: Any, characters: Optional[List[Dict[str, Any]]] = None):
        super().__init__(timeout=300)
        self.party_id = int(party_id)
        self.user_id = str(user_id)
        if characters:
            self.add_item(RegisteredCharacterSelect(party_id, characters, user_id))

    @discord.ui.button(label='직접 입력', style=discord.ButtonStyle.secondary, emoji='✏️')
    async def manual_input(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.send_modal(CharacterNicknameModal(self.party_id, self.user_id))


async def handle_party_join(interaction: discord.Interaction, party_id: int):
    resp = await http_client.get(f'/siblings/{interaction.user.id}')
    characters: List[Dict[str, Any]] = []
    if resp.status_code == 200:
        characters = (resp.json() or {}).get('characters', []) or []
    if characters:
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title='🏴‍☠️ 파티 참가', description=f'등록된 캐릭터 **{len(characters)}개**가 있어요!\n아래에서 선택하거나 직접 입력하세요.', color=discord.Color.blue())
        view = PartyJoinSelectView(party_id, interaction.user.id, characters)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_modal(CharacterNicknameModal(party_id, interaction.user.id))


async def handle_party_leave(interaction: discord.Interaction, party_id: int):
    await interaction.response.defer(ephemeral=True)
    resp = await http_client.delete(f'/party/{party_id}/participants/{interaction.user.id}')
    if resp.status_code in (200, 204):
        await interaction.followup.send('파티에서 탈퇴했습니다.', ephemeral=True)
    elif resp.status_code == 404:
        await interaction.followup.send('참가하지 않은 파티입니다.', ephemeral=True)
    else:
        await interaction.followup.send(f'탈퇴 처리 중 오류가 발생했습니다. (상태 코드: {resp.status_code})', ephemeral=True)


async def handle_party_force_cancel(interaction: discord.Interaction, party_id: int):
    data = await permission_check(interaction, party_id)
    if not data:
        return
    participants = _flatten_participants(data)
    view = ForceCancelView(party_id=party_id, participants=participants, is_dealer_closed=int(data.get('is_dealer_closed') or 0), is_supporter_closed=int(data.get('is_supporter_closed') or 0))
    await _send_ephemeral(interaction, '강제 참가 취소할 인원을 선택하세요.' if participants else '참가 중인 인원이 없어 딜러/서포터 마감 상태만 변경할 수 있어요.', view=view)


async def handle_party_mention(interaction: discord.Interaction, party_id: int):
    data = await permission_check(interaction, party_id)
    if not data:
        return
    participants = _flatten_participants(data)
    if not participants:
        await _send_ephemeral(interaction, '참가 중인 인원이 없습니다.')
        return
    chat_channel_id = data.get('thread_manage_id') or data.get('thread_id')
    if not chat_channel_id:
        await _send_ephemeral(interaction, '멘션을 보낼 스레드 정보를 찾을 수 없습니다.')
        return
    view = MentionTypeView(party_id, chat_channel_id, participants)
    await _send_ephemeral(interaction, '멘션 방식을 선택하세요.', view=view)



async def handle_party_delete(interaction: discord.Interaction, party_id: int):
    data = await permission_check(interaction, party_id)
    if not data:
        return
    resp = await http_client.delete(f'/party/{party_id}/delete')
    msg = '일정이 삭제되었습니다.' if resp.status_code == 200 else '일정 삭제 실패!'
    await _send_ephemeral(interaction, msg)


async def handle_party_public(interaction: discord.Interaction, party_id: int):
    data = await permission_check(interaction, party_id)
    if not data:
        return
    try:
        resp = await http_client.post(f'/party/public/{party_id}')
    except Exception as e:
        await _send_ephemeral(interaction, f'상태 변경 중 오류가 발생했습니다.\n{e}')
        return
    if resp.status_code != 200:
        await _send_ephemeral(interaction, f'상태 변경 실패 (코드: {resp.status_code})')
        return
    payload = resp.json() or {}
    is_active = int(payload.get('is_active', 0))
    msg = f'✅ 일정이 공개 상태로 변경되었습니다.\n🔗 https://mococobot.kr/party/{party_id}' if is_active == 1 else '✅ 일정이 비공개 상태로 변경되었습니다.'
    await _send_ephemeral(interaction, msg)
    try:
        view = discord.ui.View.from_message(interaction.message)
        target_custom_id = f'party_public_{party_id}'
        for child in view.children:
            if isinstance(child, discord.ui.Button) and child.custom_id == target_custom_id:
                child.label = '일정 공개 🟢' if is_active == 1 else '일정 공개 🔴'
        await interaction.message.edit(view=view)
    except Exception:
        pass


async def handle_party_waitlist(interaction: discord.Interaction, party_id: int):
    try:
        await interaction.response.defer(ephemeral=True)
        resp = await http_client.get(f'/party/{party_id}/waitlist')
        if resp.status_code != 200:
            await interaction.followup.send('대기열 정보를 불러오지 못했습니다.', ephemeral=True)
            return
        data = (resp.json() or {}).get('data') or {}
        items = data.get('dealers') or []
        embed = discord.Embed(title='📋 대기열 현황 (딜러)', color=discord.Color.green())
        if not items:
            embed.description = '```\n현재 대기 인원이 없습니다.\n```'
        else:
            lines = []
            for idx, item in enumerate(items[:30], start=1):
                name = item.get('name') or item.get('char_name') or '이름없음'
                cls = item.get('class_name') or '직업정보없음'
                emo = item.get('class_emoji') or ''
                ilvl = item.get('item_level') or item.get('item_lvl') or '-'
                cp = item.get('combat_power') or '-'
                pos = item.get('position') or idx
                lines.append(f'`{pos}` {emo} **{name}** | {cls} | Lv {ilvl} | CP {cp}')
            embed.description = '\n'.join(lines)
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await _send_ephemeral(interaction, f'오류가 발생했습니다: {e}')
