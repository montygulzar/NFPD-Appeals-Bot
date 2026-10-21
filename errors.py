"""Shared error reporting for slash commands, buttons and modals.

Without this, an exception raised after `interaction.response.defer()` leaves
the user staring at "thinking..." forever. Here every unhandled error is
logged with a traceback and the user gets a short reply instead.

Only the exception class name is ever shown to users - never its message,
which could contain connection details.
"""
import logging

import asyncpg
import discord
from discord import app_commands

logger = logging.getLogger("appeals-bot.errors")


def describe_error(error: BaseException) -> tuple[str, bool]:
    """Map an exception to (user-facing text, should_log_traceback)."""
    original = getattr(error, "original", None) or error.__cause__ or error

    if isinstance(error, app_commands.CheckFailure) and not isinstance(error, app_commands.CommandInvokeError):
        if isinstance(error, app_commands.NoPrivateMessage):
            return "This command can only be used in a server.", False
        if isinstance(error, app_commands.MissingPermissions):
            return "You don't have permission to use this command.", False
        return "You can't use this command here.", False
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"Slow down - try again in {error.retry_after:.0f}s.", False
    if isinstance(original, (asyncpg.PostgresConnectionError, asyncpg.CannotConnectNowError,
                             asyncpg.InterfaceError, ConnectionError, OSError, TimeoutError)):
        return "The bot can't reach its database right now. Please try again in a minute.", True
    if isinstance(original, discord.Forbidden):
        return "I'm missing a Discord permission needed for that. Ask an admin to run `/debug`.", True
    return f"Something went wrong ({type(original).__name__}). The developers have been notified in the logs.", True


async def report_interaction_error(interaction: discord.Interaction, error: BaseException, *, where: str) -> None:
    text, should_log = describe_error(error)
    if should_log:
        logger.error(
            "Unhandled error in %s (guild=%s user=%s)",
            where, interaction.guild_id, getattr(interaction.user, "id", None),
            exc_info=error,
        )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass


class AppealsCommandTree(app_commands.CommandTree):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        name = interaction.command.qualified_name if interaction.command else "unknown command"
        await report_interaction_error(interaction, error, where=f"/{name}")
