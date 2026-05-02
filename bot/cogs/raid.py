import contextlib
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import OptionChoice, option
from discord.ext import commands

from bot.commands.party_manage import permission_check
from bot.commands.raid_commands import RaidCommands
from bot.core.http_client import http_client
from raidlist import raid_difficulty_map, raid_list
from bot.handler.party import CharacterNicknameModal

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]
RAIDS = raid_list[:25]

_seen = set()
DIFFICULTIES = []
for v in raid_difficulty_map.values():
    for d in v:
        if d not in _seen:
            _seen.add(d)
            DIFFICULTIES.append(d)
DIFFICULTIES = DIFFICULTIES[:25]

_PARTY_LIST_CACHE: dict[int, tuple[float, list[dict]]] = {}
_PARTY_LIST_TTL = 12.0


def get_date_options():
    now = datetime.now()
    out = []
    for i in range(22):
        d = now + timedelta(days=i)
        weekday_str = WEEKDAYS[d.weekday()]
        formatted_date_base = d.strftime("%y.%m.%d")
        out.append(f"{formatted_date_base}({weekday_str})")
    return out


async def fetch_party_list(guild_id: int) -> list[dict]:
    now = time.monotonic()
    cached = _PARTY_LIST_CACHE.get(guild_id)
    if cached and (now - cached[0]) < _PARTY_LIST_TTL:
        return cached[1]

    resp = await http_client.get(f"/party/list?guild_id={guild_id}")
    if resp is None or resp.status_code != 200:
        _PARTY_LIST_CACHE[guild_id] = (now, [])
        return []

    try:
        data = (resp.json() or {}).get("data") or []
        if not isinstance(data, list):
            data = []
    except Exception:
        data = []

    _PARTY_LIST_CACHE[guild_id] = (now, data)
    return data


async def announcement_autocomplete(ctx: discord.AutocompleteContext):
    parties = await fetch_party_list(ctx.interaction.guild_id)
    out = []
    for p in parties[:25]:
        title = str(p.get("title") or "")
        pid = str(p.get("id") or "")
        if title and pid:
            out.append(OptionChoice(name=title, value=pid))
    return out


class RaidCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.raid_commands = RaidCommands()

    async def _ephemeral(self, ctx_or_inter, content: str):
        try:
            if hasattr(ctx_or_inter, "respond"):
                if hasattr(ctx_or_inter, "response") and ctx_or_inter.response.is_done():
                    return await ctx_or_inter.followup.send(content, ephemeral=True)
                return await ctx_or_inter.respond(content, ephemeral=True)
            inter = ctx_or_inter
            if inter.response.is_done():
                return await inter.followup.send(content, ephemeral=True)
            return await inter.response.send_message(content, ephemeral=True)
        except Exception:
            with contextlib.suppress(Exception):
                if hasattr(ctx_or_inter, "followup"):
                    return await ctx_or_inter.followup.send(content, ephemeral=True)

    @commands.Cog.listener()
    async def on_application_command_error(self, ctx: discord.ApplicationContext, error):
        err = getattr(error, "original", error)

        if isinstance(err, commands.MissingPermissions):
            await self._ephemeral(ctx, "권한이 없어 해당 명령어 사용이 불가능해요.")
            return

        if isinstance(err, commands.BotMissingPermissions):
            await self._ephemeral(ctx, "봇 권한이 부족해요. 서버 권한을 확인해 주세요.")
            return

        if isinstance(err, discord.Forbidden):
            await self._ephemeral(ctx, "권한 문제로 작업을 완료할 수 없어요.")
            return

        if isinstance(err, commands.CheckFailure):
            await self._ephemeral(ctx, "권한이 없어 해당 명령어 사용이 불가능해요.")
            return

        await self._ephemeral(ctx, f"오류가 발생했습니다: {err}")

    @discord.slash_command(name="setups", description="레이드 일정 기능을 사용하기 위하여 기초 세팅을 진행해요. (관리자 전용)")
    @commands.has_permissions(administrator=True)
    async def setups(self, ctx: discord.ApplicationContext):
        await self.raid_commands.setup_raid_system(ctx, raid_list)

    @discord.slash_command(name="레이드", description="로스트아크 레이드 일정을 등록해요.")
    @option("메세지", description="일정 제목에 반영될 메세지를 입력해주세요.", required=False)
    @option("닉네임", description="입력 시 일정 생성과 동시에 해당 캐릭터로 자동 참가합니다.", required=False)
    async def 레이드(self, ctx: discord.ApplicationContext, 메세지: str = None, 닉네임: str = None):
        date_values = get_date_options()
        hours = [f"{i:02d}" for i in range(24)]
        minutes = [f"{i:02d}" for i in range(0, 60, 5)]

        _cog, _msg = self, 메세지
        _nick = 닉네임.strip() if 닉네임 else None

        class RaidCreateModal(discord.ui.DesignerModal):
            def __init__(self):
                super().__init__(title="레이드 일정 등록", custom_id="raid_modal_v3")
                self.date = discord.ui.Select(
                    placeholder="날짜 선택",
                    custom_id="date",
                    options=[discord.SelectOption(label=v, value=v) for v in date_values],
                    min_values=1,
                    max_values=1,
                )
                self.add_item(discord.ui.Label("날짜", self.date))
                self.hour = discord.ui.Select(
                    placeholder="시 선택 (00~23)",
                    custom_id="hour",
                    options=[discord.SelectOption(label=v, value=v) for v in hours],
                    min_values=1,
                    max_values=1,
                )
                self.add_item(discord.ui.Label("시", self.hour))
                self.minute = discord.ui.Select(
                    placeholder="분 선택 (00~59, 5분 단위)",
                    custom_id="minute",
                    options=[discord.SelectOption(label=v, value=v) for v in minutes],
                    min_values=1,
                    max_values=1,
                )
                self.add_item(discord.ui.Label("분", self.minute))
                self.raid = discord.ui.Select(
                    placeholder="레이드 선택",
                    custom_id="raid",
                    options=[discord.SelectOption(label=v, value=v) for v in RAIDS],
                    min_values=1,
                    max_values=1,
                )
                self.add_item(discord.ui.Label("레이드", self.raid))
                self.difficulty = discord.ui.Select(
                    placeholder="난이도 선택",
                    custom_id="difficulty",
                    options=[discord.SelectOption(label=v, value=v) for v in DIFFICULTIES],
                    min_values=1,
                    max_values=1,
                )
                self.add_item(discord.ui.Label("난이도", self.difficulty))

            async def callback(self, interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                날짜 = self.date.values[0]
                시 = self.hour.values[0]
                분 = self.minute.values[0]
                레이드 = self.raid.values[0]
                난이도 = self.difficulty.values[0]
                await _cog.raid_commands.party_create(
                    interaction,
                    날짜,
                    시,
                    분,
                    레이드,
                    난이도,
                    _msg,
                    auto_join_nickname=_nick,
                )

        await ctx.send_modal(RaidCreateModal())

    @discord.slash_command(name="모집", description="로스트아크 레이드 모집을 등록해요.")
    @option("닉네임", description="입력 시 일정 생성과 동시에 해당 캐릭터로 자동 참가합니다.", required=False)
    async def 모집(self, ctx: discord.ApplicationContext, 닉네임: str = None):
        _cog = self
        _nick = 닉네임.strip() if 닉네임 else None

        class RecruitModal(discord.ui.DesignerModal):
            def __init__(self):
                super().__init__(title="레이드 모집 등록", custom_id="recruit_modal_v1")
                self.raid = discord.ui.Select(
                    placeholder="레이드 선택",
                    custom_id="raid",
                    options=[discord.SelectOption(label=v, value=v) for v in RAIDS],
                    min_values=1,
                    max_values=1,
                )
                self.add_item(discord.ui.Label("레이드", self.raid))
                self.difficulty = discord.ui.Select(
                    placeholder="난이도 선택",
                    custom_id="difficulty",
                    options=[discord.SelectOption(label=v, value=v) for v in DIFFICULTIES],
                    min_values=1,
                    max_values=1,
                )
                self.add_item(discord.ui.Label("난이도", self.difficulty))
                self.message = discord.ui.InputText(
                    style=discord.InputTextStyle.long,
                    required=False,
                    placeholder="일정 제목에 반영될 메세지",
                )
                self.add_item(discord.ui.Label("메세지", self.message))

            async def callback(self, interaction: discord.Interaction):
                await interaction.response.defer(ephemeral=True)
                레이드 = self.raid.values[0]
                난이도 = self.difficulty.values[0]
                메세지 = self.message.value
                await _cog.raid_commands.party_create(
                    interaction,
                    None,
                    None,
                    None,
                    레이드,
                    난이도,
                    메세지,
                    auto_join_nickname=_nick,
                )

        await ctx.send_modal(RecruitModal())

    @discord.slash_command(name="강제참가", description="유저를 특정 레이드 일정에 강제로 참가시킵니다.")
    @option("공지", description="강제 참가시킬 레이드 공지를 선택하세요.", autocomplete=announcement_autocomplete)
    @option("유저", description="강제 참가시킬 디스코드 유저를 선택하세요.", type=discord.User, required=False)
    async def 강제참가(self, ctx: discord.ApplicationContext, 공지: str, 유저: Optional[discord.User] = None):
        data = await permission_check(ctx.interaction, int(공지))
        if not data:
            return

        if 유저:
            target_user_id = str(유저.id)
        else:
            base_user = getattr(ctx, "author", None) or getattr(ctx, "user", None) or ctx.interaction.user
            epoch = int(time.time())
            rand = secrets.token_hex(2)
            target_user_id = f"TEMP-{base_user.id}-{epoch}-{rand}"

        modal = CharacterNicknameModal(int(공지), target_user_id)
        await ctx.send_modal(modal)

    @discord.slash_command(name="일정관리", description="레이드 일정을 관리해요.")
    @option("공지", description="관리할 레이드 공지를 선택하세요.", autocomplete=announcement_autocomplete)
    async def 일정관리(self, ctx: discord.ApplicationContext, 공지: str):
        await ctx.defer(ephemeral=True)

        data = await permission_check(ctx.interaction, int(공지))
        if not data:
            return

        await ctx.followup.send(f"{공지} 일정 관리 메뉴입니다.", ephemeral=True)


def setup(bot):
    bot.add_cog(RaidCog(bot))