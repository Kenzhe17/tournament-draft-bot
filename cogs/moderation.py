"""Compatibility bridge for the original r!z3 GIF moderation.

GIF triggers remain available to configured trusted staff, but no longer run
asyncio.sleep-based role mutes. Both GIF mute and /mod timeout now use the
same native Discord timeout + PostgreSQL case tracking and audit pipeline.
"""
from __future__ import annotations

import logging
import os
from datetime import timedelta

import discord
from discord.ext import commands

from config import ADMIN_USER_IDS, WARN_GIF_URL, UNWARN_GIF_URL
from services import moderation_service as ms

logger=logging.getLogger(__name__)
AUTHORIZED_GIF_USERS = ADMIN_USER_IDS | {
    int(x) for x in os.getenv('GIF_TRIGGER_USER_IDS','').split(',') if x.strip().isdigit()
}


def _is_trigger(message,configured,alias):
    links=[message.content]+[a.url for a in message.attachments]
    return any((configured and configured in s) or alias in s for s in links)


class ModerationCog(commands.Cog):
    def __init__(self,bot):self.bot=bot

    @commands.Cog.listener()
    async def on_message(self,message:discord.Message):
        if not message.guild or message.author.bot or not message.reference or not message.reference.message_id:
            return
        if message.author.id not in AUTHORIZED_GIF_USERS:
            return
        is_mute=_is_trigger(message,WARN_GIF_URL,'h2di-the-dictator')
        is_unmute=_is_trigger(message,UNWARN_GIF_URL,'slice-execute-4')
        if not (is_mute or is_unmute):return
        try:
            replied=await message.channel.fetch_message(message.reference.message_id)
            target=replied.author
            if not isinstance(target,discord.Member):
                target=await message.guild.fetch_member(target.id)
            actor=message.author
            if not isinstance(actor,discord.Member):
                actor=await message.guild.fetch_member(actor.id)
            if is_mute:
                result=await ms.apply_timeout(actor,target,timedelta(minutes=10),
                    'GIF-мьют (r!z3)',self.bot,trusted_gif=True,url=replied.jump_url)
                msg=await message.channel.send(f'{target.mention} получил тайм-аут на 10 минут · {ms.ref(result)}',
                    allowed_mentions=discord.AllowedMentions.none(),delete_after=10)
            else:
                removed=await ms.remove_timeout(actor,target,self.bot,'GIF-размьют (r!z3)',trusted_gif=True)
                if removed:
                    await message.channel.send(f'Тайм-аут {target.mention} снят',
                        allowed_mentions=discord.AllowedMentions.none(),delete_after=10)
        except (discord.HTTPException,ValueError,PermissionError,RuntimeError):
            logger.exception('GIF moderation action failed')
        except Exception:
            logger.exception('Unexpected GIF moderation exception')


async def setup(bot):
    await bot.add_cog(ModerationCog(bot))
