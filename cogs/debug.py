import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_AVATAR_URL, COLOUR_PRIMARY

logger = logging.getLogger("appeals-bot.debug")


class DebugCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="debug", description="Run a full health check on the appeals bot.")
    @app_commands.default_permissions(administrator=True)
    async def debug(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        results: list[str] = []

        # 1. Database connectivity
        try:
            start = time.perf_counter()
            ok = await self.bot.db.ping()
            latency_ms = (time.perf_counter() - start) * 1000
            results.append(f"Database: {'OK' if ok else 'FAIL'} ({latency_ms:.0f}ms)")
        except Exception as exc:
            results.append(f"Database: FAIL - {type(exc).__name__}")

        # 2. Guild config
        config = await self.bot.db.get_guild_config(interaction.guild_id)
        if not config:
            results.append("Guild config: not set - run /setup")
        else:
            results.append("Guild config: found")

            # 3. Channel permission checks
            channel_checks = {
                "Appeals channel": (config["appeals_channel"], ["view_channel", "send_messages", "manage_channels"]),
                "Voting channel":  (config["voting_channel"],  ["view_channel", "send_messages", "embed_links"]),
                "Results channel": (config["results_channel"], ["view_channel", "send_messages", "embed_links", "attach_files"]),
            }
            for label, (channel_id, required_perms) in channel_checks.items():
                if not channel_id:
                    results.append(f"{label}: not configured")
                    continue
                channel = interaction.guild.get_channel(channel_id)
                if channel is None:
                    results.append(f"{label}: configured but channel not found (ID {channel_id})")
                    continue
                me = interaction.guild.me
                perms = channel.permissions_for(me)
                missing = [p for p in required_perms if not getattr(perms, p, False)]
                if missing:
                    results.append(f"{label}: {channel.mention} - missing permissions: {', '.join(missing)}")
                else:
                    results.append(f"{label}: {channel.mention} - OK")

            # 4. Test that the bot can actually edit a message in the voting channel
            # This is the most likely cause of vote embed updates failing.
            if config["voting_channel"]:
                voting_channel = interaction.guild.get_channel(config["voting_channel"])
                if voting_channel:
                    try:
                        test_msg = await voting_channel.send("Debug check - deleting in 3 seconds.")
                        await test_msg.edit(content="Debug check - edit successful.")
                        await test_msg.delete()
                        results.append("Voting channel edit test: OK")
                    except discord.Forbidden as exc:
                        results.append(f"Voting channel edit test: FAIL (Forbidden) - {exc}")
                    except discord.HTTPException as exc:
                        results.append(f"Voting channel edit test: FAIL - {exc}")

            # 5. Ban team role
            if not config["ban_team_role"]:
                results.append("Ban team role: not configured")
            else:
                role = interaction.guild.get_role(config["ban_team_role"])
                if role:
                    results.append(f"Ban team role: {role.mention} ({role.id})")
                else:
                    results.append(f"Ban team role: configured but role not found (ID {config['ban_team_role']})")

        # 6. Transcript test - verify read_message_history in the current channel
        perms = interaction.channel.permissions_for(interaction.guild.me)
        if perms.read_message_history:
            results.append("Transcript read access (this channel): OK")
        else:
            results.append("Transcript read access (this channel): FAIL - missing read_message_history")

        # 7. Recent appeals
        try:
            recent = await self.bot.db.get_recent_appeals(interaction.guild_id, limit=5)
            if recent:
                lines = [f"Recent appeals ({len(recent)} shown):"]
                for appeal in recent:
                    lines.append(f"  #{appeal['id']} {appeal['status']:15} {appeal['roblox_username']}")
                results.append("\n".join(lines))
            else:
                results.append("Recent appeals: none found")
        except Exception as exc:
            results.append(f"Recent appeals: FAIL - {exc}")

        embed = discord.Embed(
            title="Appeals Bot - Health Check",
            description="```\n" + "\n".join(results) + "\n```",
            color=COLOUR_PRIMARY,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="North Florida Police Department  |  Debug", icon_url=BOT_AVATAR_URL)
        await interaction.followup.send(embed=embed, ephemeral=True)
