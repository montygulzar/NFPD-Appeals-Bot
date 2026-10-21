"""Channel lookup that survives an empty cache.

guild.get_channel only checks discord.py's in-memory cache, which can be
incomplete right after a restart or reconnect. A miss there used to silently
skip verdict posts and voting-message updates; this falls back to an API
fetch before giving up.
"""
import logging
from typing import Optional

import discord

logger = logging.getLogger("appeals-bot.channels")


async def resolve_channel(guild: Optional[discord.Guild], channel_id: Optional[int]):
    if guild is None or not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is not None:
        return channel
    try:
        return await guild.fetch_channel(channel_id)
    except discord.NotFound:
        return None
    except discord.HTTPException as exc:
        logger.warning("Could not fetch channel %s in guild %s: %s", channel_id, guild.id, exc)
        return None
