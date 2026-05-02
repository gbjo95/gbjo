import logging

import discord
from discord.ext import commands

from bot.core.config import BOT_TOKEN
from bot.core.http_client import http_client
from bot.handler.party import handle_party_delete, handle_party_force_cancel, handle_party_join, handle_party_leave, handle_party_mention, handle_party_public, handle_party_waitlist

logging.basicConfig(level=logging.INFO)
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
bot = commands.Bot(intents=intents)


@bot.event
async def on_ready():
    logging.info('Bot ready: %s', bot.user)


@bot.event
async def on_interaction(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = getattr(interaction.data, 'get', lambda _k, _d=None: None)('custom_id') if isinstance(interaction.data, dict) else None
        if custom_id:
            try:
                if custom_id.startswith('party_join_'):
                    await handle_party_join(interaction, int(custom_id.removeprefix('party_join_')))
                    return
                elif custom_id.startswith('party_leave_'):
                    await handle_party_leave(interaction, int(custom_id.removeprefix('party_leave_')))
                    return
                elif custom_id.startswith('party_force_cancel_'):
                    await handle_party_force_cancel(interaction, int(custom_id.removeprefix('party_force_cancel_')))
                    return
                elif custom_id.startswith('party_mention_'):
                    await handle_party_mention(interaction, int(custom_id.removeprefix('party_mention_')))
                    return
                elif custom_id.startswith('party_public_'):
                    await handle_party_public(interaction, int(custom_id.removeprefix('party_public_')))
                    return
                elif custom_id.startswith('party_waitlist_'):
                    await handle_party_waitlist(interaction, int(custom_id.removeprefix('party_waitlist_')))
                    return
                elif custom_id.startswith('party_delete_'):
                    await handle_party_delete(interaction, int(custom_id.removeprefix('party_delete_')))
                    return
            except Exception:
                logging.exception('interaction handler failed')
                return

    await bot.process_application_commands(interaction)


@bot.event
async def on_member_remove(member: discord.Member):
    await http_client.delete(f'/party/guilds/{member.guild.id}/participants/{member.id}')


def run_bot() -> None:
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN is empty')
    bot.load_extension('bot.cogs.raid')
    bot.run(BOT_TOKEN)
