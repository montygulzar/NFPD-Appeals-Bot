"""Shared Appeals-staff permission checks.

Used by the staff slash commands and by the staff-only buttons in ticket
channels and verdict panels, so a button can never be pressed by someone who
couldn't run the equivalent command.
"""
import logging

import discord

from config import STAFF_ROLE_IDS

logger = logging.getLogger("appeals-bot.permissions")

STAFF_ONLY_MESSAGE = "Only Appeals staff can use this."


def is_appeals_staff(interaction: discord.Interaction, guild_config) -> bool:
    """Truthy if the invoker counts as Appeals staff for this guild.

    Order of precedence:
      1. STAFF_ROLE_IDS env - a role in that set.
      2. Per-guild ban_team_role configured via /setup.
      3. Fallback: Discord manage_guild permission (same permission the
         existing /forwardtobanteam and /appealinfo commands already
         require - this is not a new privilege).

    Commands using this helper are also declared with
    @app_commands.default_permissions(manage_guild=True) and
    @app_commands.guild_only(), so Discord itself hides the command from
    users without Manage Server unless a server admin has explicitly
    granted a role via Server Settings -> Integrations.
    """
    member = interaction.user
    roles = getattr(member, "roles", None)
    perms = getattr(member, "guild_permissions", None)
    if roles is None or perms is None:
        return False
    role_ids = {r.id for r in roles}
    if STAFF_ROLE_IDS and (role_ids & STAFF_ROLE_IDS):
        return True
    if guild_config and guild_config["ban_team_role"] in role_ids:
        return True
    return bool(perms.manage_guild or perms.administrator)


async def staff_button_check(bot, interaction: discord.Interaction, *, appellant_id: int | None = None) -> bool:
    """interaction_check body for staff-only buttons.

    Fails closed: a DB error, a DM context, or the appellant themselves (who
    can see their own ticket channel) are all refused. Sends the refusal
    ephemerally so the caller can just `return False`.
    """
    allowed = False
    if appellant_id is None or interaction.user.id != appellant_id:
        try:
            config = await bot.db.get_guild_config(interaction.guild_id)
        except Exception:
            logger.exception("Permission check could not read guild config")
            config = None
            # Without config we can still honour STAFF_ROLE_IDS / Manage Server.
        allowed = is_appeals_staff(interaction, config)

    if not allowed:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(STAFF_ONLY_MESSAGE, ephemeral=True)
            else:
                await interaction.response.send_message(STAFF_ONLY_MESSAGE, ephemeral=True)
        except discord.HTTPException:
            pass
    return allowed
