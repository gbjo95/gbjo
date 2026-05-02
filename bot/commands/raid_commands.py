import discord
import pytz
from datetime import datetime
from typing import Optional

from bot.core.http_client import http_client
from raidlist import raid_difficulty_map
from bot.handler.party import auto_join_party_by_nickname


class RaidCommands:
    def __init__(self):
        self.client = http_client

    async def api_post(self, endpoint: str, data: dict):
        resp = await self.client.post(endpoint, json=data)
        resp.raise_for_status()
        return resp.json()

    async def api_get(self, endpoint: str):
        resp = await self.client.get(endpoint)
        resp.raise_for_status()
        return resp.json()

    async def api_patch(self, endpoint: str, data: dict):
        resp = await self.client.patch(endpoint, json=data)
        resp.raise_for_status()
        return resp.json()

    async def setup_raid_system(self, ctx: discord.ApplicationContext, raid_list):
        await ctx.defer(ephemeral=True)
        guild_id = ctx.guild.id
        forum_channel = await ctx.guild.create_forum_channel(name='│🎮┊레이드공지', reason='레이드 일정 관리를 위한 포럼 채널')
        raid_chat_channel = await ctx.guild.create_text_channel('│🎮┊레이드채팅방')
        await self.api_post(f'/discord/server/{guild_id}', {'forum_channel_id': str(forum_channel.id), 'chat_channel_id': str(raid_chat_channel.id)})
        embed = discord.Embed(title='✅ 레이드일정 시스템 설정 완료!', description=f'**일정 채널:** {forum_channel.mention}\n**채팅 채널:** {raid_chat_channel.mention}', color=discord.Color.green())
        await ctx.followup.send(embed=embed, ephemeral=True)

    async def party_create(self, interaction: discord.Interaction, raid_date: Optional[str] = None, hour: Optional[str] = None, minute: Optional[str] = None, raid_name: Optional[str] = None, difficulty: Optional[str] = None, messages: Optional[str] = None, auto_join_nickname: Optional[str] = None):
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        progress = await interaction.followup.send(content='서버 설정을 확인하고 있습니다...', ephemeral=True)
        start_date_str = None if not (raid_date and hour and minute) else self._validate_datetime(raid_date, hour, minute)
        if start_date_str is False:
            await interaction.followup.edit_message(progress.id, content='선택하신 날짜와 시간으로는 공지 등록이 불가능합니다.')
            return
        if not self._validate_boss_diff_local(raid_name, difficulty):
            await interaction.followup.edit_message(progress.id, content=f"선택한 보스 '{raid_name}'에 난이도 '{difficulty}'는 존재하지 않아요. 다시 선택하세요.")
            return
        server_config = await self.api_get(f'/discord/server/{guild_id}')
        if not server_config:
            await interaction.followup.edit_message(progress.id, content='당신의 서버는 아직 기초 설정이 되지 않았습니다. 관리자에게 문의하세요.')
            return
        title = self._generate_party_title(raid_name, start_date_str, difficulty, messages)
        await interaction.followup.edit_message(progress.id, content='파티를 생성하고 있습니다...')
        party_result = await self.api_post(f'/party/{guild_id}/create', {'guild_id': str(guild_id), 'title': title, 'raid_name': raid_name, 'difficulty': difficulty, 'start_date': start_date_str, 'owner_id': user_id, 'message': messages or ''})
        join_ok = None
        join_msg = None
        auto_nick = (auto_join_nickname or '').strip() if auto_join_nickname else ''
        if auto_nick and party_result.get('party_id'):
            join_ok, join_msg = await auto_join_party_by_nickname(int(party_result['party_id']), user_id, auto_nick)
        await self._send_completion_message(interaction, progress.id, party_result, raid_name, difficulty, user_id)
        if join_ok is not None:
            await interaction.followup.send((f"✅ '{auto_nick}' 캐릭터로 방금 만든 일정에 자동 참가했어요." if join_ok else '⚠️ 일정은 생성됐지만 자동 참가에는 실패했어요.') + (f"\n{join_msg}" if join_msg else ''), ephemeral=True)

    async def party_edit(self, interaction: discord.Interaction, raid_data: dict, raid_date: Optional[str] = None, hour: Optional[str] = None, minute: Optional[str] = None, messages: Optional[str] = None):
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        progress = await interaction.followup.send(content='서버 설정을 확인하고 있습니다...', ephemeral=True, wait=True)
        start_date_str = None if not (raid_date and hour and minute) else self._validate_datetime(raid_date, hour, minute)
        if start_date_str is False:
            await interaction.followup.edit_message(progress.id, content='선택하신 날짜와 시간으로는 공지 수정이 불가능합니다.')
            return
        await self.api_get(f'/discord/server/{guild_id}')
        raid_name = raid_data.get('raid_name')
        difficulty = raid_data.get('difficulty')
        party_id = raid_data.get('id')
        title = self._generate_party_title(raid_name, start_date_str, difficulty, messages)
        await interaction.followup.edit_message(progress.id, content='파티를 수정하고 있습니다...')
        await self.api_patch(f'/party/{party_id}/edit', {'title': title, 'guild_id': str(guild_id), 'raid_name': raid_name, 'difficulty': difficulty, 'start_date': start_date_str, 'owner_id': user_id, 'message': messages or ''})
        await interaction.followup.edit_message(progress.id, content='파티 수정이 완료되었어요!')

    def _validate_boss_diff_local(self, raid_name: Optional[str], difficulty: Optional[str]) -> bool:
        if not raid_name or not difficulty:
            return True
        diffs = raid_difficulty_map.get(raid_name)
        return bool(diffs and difficulty in diffs)

    def _validate_datetime(self, raid_date: str, hour: str, minute: str):
        try:
            base_date = raid_date.split('(')[0].strip()
            dt = datetime.strptime(base_date, '%y.%m.%d')
            dt = dt.replace(hour=int(hour), minute=int(minute))
            seoul = pytz.timezone('Asia/Seoul')
            dt = seoul.localize(dt)
            if dt < datetime.now(seoul):
                return False
            return f'{raid_date} {hour}:{minute}'
        except Exception:
            return False

    def _generate_party_title(self, raid_name: Optional[str], start_date_str: Optional[str], difficulty: Optional[str], messages: Optional[str]) -> str:
        raid = (raid_name or '').strip()
        diff = (difficulty or '').strip()
        msg = (messages or '').strip()
        head = f'[{raid} : {diff}]' if raid and diff else f'[{raid or diff or "레이드"}]'
        title = head
        if start_date_str:
            title += f' {start_date_str}'
        if msg:
            title += f' : {msg}'
        return title

    async def _send_completion_message(self, interaction: discord.Interaction, progress_message_id: int, party_result: dict, raid_name: Optional[str], difficulty: Optional[str], user_id: int):
        embed = discord.Embed(title='✅ 레이드 일정 생성 완료!', description=f"**제목:** {party_result.get('title', '')}", color=discord.Color.green())
        if party_result.get('start_date'):
            embed.add_field(name='시작 날짜', value=str(party_result['start_date']), inline=True)
        if raid_name or difficulty:
            embed.add_field(name='레이드', value=f"{raid_name or ''} {difficulty or ''}".strip(), inline=True)
        embed.add_field(name='공대장', value=f'<@{user_id}>', inline=True)
        await interaction.followup.edit_message(progress_message_id, content='', embed=embed)
