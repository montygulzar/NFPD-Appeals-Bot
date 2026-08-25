import asyncio
import logging
import logging.handlers
import os
import signal

import discord
import config as _config
from discord.ext import commands

from config import (
    APPROVED_GUILD_IDS,
    DATABASE_URL,
    DEVELOPER_IDS,
    DISCORD_TOKEN,
    LEAVE_UNAPPROVED_GUILDS,
    LOG_LEVEL,
    UNAUTHORISED_GUILD_MESSAGE,
    DB_CONNECT_MAX_WAIT,
    DB_POOL_MIN,
    DB_POOL_MAX,
)
from database import Database
from cogs.appeals import AppealsCog
from cogs.dashboard import DashboardCog
from cogs.debug import DebugCog
from cogs.voting import VotingCog
from cogs.staff import StaffCog


def _setup_logging() -> logging.Logger:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    # Replace any pre-existing handlers so Docker gets exactly one stdout stream.
    root.handlers = [handler]
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    return logging.getLogger("appeals-bot")


logger = _setup_logging()


class AppealsBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.db: Database = None
        self._persistent_views_added = False

    async def setup_hook(self):
        # DB startup - retries transient connection failures so a slow-starting
        # appeals-postgres container does not turn into an immediate crash loop.
        self.db = Database(DATABASE_URL, min_size=DB_POOL_MIN, max_size=DB_POOL_MAX)
        try:
            await self.db.initialise(connect_max_wait_seconds=DB_CONNECT_MAX_WAIT)
            logger.info("Database connected.")
        except Exception as exc:
            # Log the type and message but never the raw DSN.
            logger.error("Database initialisation failed: %s", exc)
            raise

        await self.add_cog(DashboardCog(self))
        await self.add_cog(AppealsCog(self))
        await self.add_cog(VotingCog(self))
        await self.add_cog(StaffCog(self))
        await self.add_cog(DebugCog(self))

        try:
            synced = await self.tree.sync()
            logger.info("Synced %d slash command(s).", len(synced))
        except Exception as exc:
            logger.error("Slash command sync failed: %s", exc)

    async def close(self):
        """Graceful shutdown: close Discord first, then release the DB pool."""
        try:
            await super().close()
        finally:
            if self.db is not None:
                try:
                    await self.db.close()
                    logger.info("Database pool closed cleanly.")
                except Exception:
                    logger.exception("Error closing database pool.")

    async def on_ready(self):
        logger.info("Logged in as %s (%s)", self.user, self.user.id)
        _config.BOT_AVATAR_URL = self.user.display_avatar.url
        logger.info("In %d guild(s).", len(self.guilds))

        if not APPROVED_GUILD_IDS:
            logger.error(
                "APPROVED_GUILD_IDS is empty - bot will leave every server it joins. "
                "Set APPROVED_GUILD_IDS in .env to a comma-separated list of guild IDs."
            )

        # on_ready fires on every Discord reconnect. Persistent views must be
        # registered exactly once for the process lifetime, otherwise duplicate
        # handlers accumulate and vote buttons fire N times per click.
        if not self._persistent_views_added:
            from views.appeal_panel import AppealPanelView
            from views.voting_panel import VotingPanelView
            from views.verdict_panel import VerdictPanelView
            from views.appeal_actions import AppealActionsView

            self.add_view(AppealPanelView(self))
            self.add_view(VotingPanelView(self))
            self.add_view(VerdictPanelView(self))
            self.add_view(AppealActionsView(self))
            self._persistent_views_added = True

        for guild in self.guilds:
            if _is_approved(guild):
                logger.info("Approved guild: %s (%s)", guild.name, guild.id)
            else:
                logger.warning("Unapproved guild on startup: %s (%s)", guild.name, guild.id)
                await _handle_unapproved_guild(self, guild)

    async def on_guild_join(self, guild: discord.Guild):
        if _is_approved(guild):
            logger.info("Joined approved guild: %s (%s)", guild.name, guild.id)
            await _alert_developers(self, f"Joined approved server **{guild.name}** (`{guild.id}`).")
            return

        logger.warning("Joined unapproved guild: %s (%s)", guild.name, guild.id)
        await _handle_unapproved_guild(self, guild)


# ---------------------------------------------------------------------------
# Guild guard helpers
# ---------------------------------------------------------------------------

def _is_approved(guild: discord.Guild) -> bool:
    return bool(APPROVED_GUILD_IDS) and guild.id in APPROVED_GUILD_IDS


async def _alert_developers(bot: AppealsBot, message: str) -> None:
    for dev_id in DEVELOPER_IDS:
        try:
            dev = bot.get_user(dev_id) or await bot.fetch_user(dev_id)
            await dev.send(message)
        except discord.HTTPException:
            pass


async def _handle_unapproved_guild(bot: AppealsBot, guild: discord.Guild) -> None:
    inviter_name, inviter_id = "Unknown", "Unknown"
    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
            if entry.target and entry.target.id == bot.user.id:
                inviter_name = str(entry.user)
                inviter_id = str(entry.user.id)
                break
    except discord.Forbidden:
        pass

    invite_url = "Unable to generate invite"
    for channel in guild.text_channels:
        try:
            invite = await channel.create_invite(
                max_age=86400, max_uses=1, reason="Unapproved guild report"
            )
            invite_url = invite.url
            break
        except discord.HTTPException:
            continue

    action_note = (
        "Leaving automatically."
        if LEAVE_UNAPPROVED_GUILDS
        else "LEAVE_UNAPPROVED_GUILDS=false - bot is staying but is not approved."
    )

    embed = discord.Embed(
        title="Unapproved Server Join",
        color=0xDC2626,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Server", value=f"{guild.name}\n`{guild.id}`", inline=True)
    embed.add_field(name="Members", value=str(guild.member_count), inline=True)
    embed.add_field(name="Added by", value=f"{inviter_name}\n`{inviter_id}`", inline=True)
    embed.add_field(name="Invite (24h)", value=invite_url, inline=False)
    embed.set_footer(text=action_note)

    for dev_id in DEVELOPER_IDS:
        try:
            dev = bot.get_user(dev_id) or await bot.fetch_user(dev_id)
            await dev.send(embed=embed)
        except discord.HTTPException:
            pass

    if LEAVE_UNAPPROVED_GUILDS:
        try:
            channel = guild.system_channel or next(
                (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages),
                None,
            )
            if channel:
                await channel.send(UNAUTHORISED_GUILD_MESSAGE)
        except discord.HTTPException:
            pass

        try:
            await guild.leave()
            logger.info("Left unapproved guild %s (%s)", guild.name, guild.id)
        except discord.HTTPException as exc:
            logger.warning("Could not leave %s (%s): %s", guild.name, guild.id, exc)


async def _run() -> None:
    """Runs the bot with graceful SIGTERM/SIGINT handling for Docker.

    docker stop sends SIGTERM. We translate it into a bot.close() call so the
    asyncpg pool and Discord gateway shut down cleanly instead of being torn
    down mid-write when the 10s grace expires.
    """
    bot = AppealsBot()
    loop = asyncio.get_running_loop()

    def _handle_shutdown_signal(signame: str) -> None:
        logger.info("Received %s - shutting down gracefully.", signame)
        loop.create_task(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_shutdown_signal, sig.name)
        except NotImplementedError:
            # Windows / restricted environments.
            pass

    async with bot:
        await bot.start(DISCORD_TOKEN)


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
