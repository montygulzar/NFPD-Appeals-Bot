import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import modbot_client
from config import MAIN_GUILD_ID, bot_avatar_url, COLOUR_PRIMARY

logger = logging.getLogger("appeals-bot.debug")


class DebugCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="debug", description="Run a full health check on the appeals bot.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
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
                "Dashboard channel": (config["appeals_channel"], ["view_channel", "send_messages"]),
                "Voting channel":    (config["voting_channel"],  ["view_channel", "send_messages", "embed_links"]),
                "Results channel":   (config["results_channel"], ["view_channel", "send_messages", "embed_links", "attach_files"]),
            }
            if config["transcripts_channel"]:
                channel_checks["Transcripts channel"] = (
                    config["transcripts_channel"],
                    ["view_channel", "send_messages", "embed_links", "attach_files"],
                )
            else:
                results.append("Transcripts channel: not configured (falls back to results channel)")
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

            # Ticket category
            tc_id = config["ticket_category"]
            if not tc_id:
                results.append("Ticket category: NOT configured - staff must re-run /setup")
            else:
                cat = interaction.guild.get_channel(tc_id)
                if not isinstance(cat, discord.CategoryChannel):
                    results.append(f"Ticket category: configured (ID {tc_id}) but not found or not a category")
                else:
                    me = interaction.guild.me
                    perms = cat.permissions_for(me)
                    if not perms.manage_channels:
                        results.append(f"Ticket category: {cat.name} - missing manage_channels")
                    else:
                        results.append(f"Ticket category: {cat.name} - OK")

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
            results.append(f"Recent appeals: FAIL - {type(exc).__name__}")

        # 8. Orphaned appeals: 'open' with no ticket channel. These block the
        # user from appealing again and can't be closed from Discord.
        try:
            orphans = await self.bot.db.find_orphan_appeals(interaction.guild_id)
            if orphans:
                ids = ", ".join(f"#{o['id']}" for o in orphans[:15])
                results.append(
                    f"Orphaned appeals (open, no ticket): {len(orphans)} - {ids}\n"
                    "  Fix: UPDATE appeals SET status='closed' WHERE id IN (...);"
                )
            else:
                results.append("Orphaned appeals: none")
        except Exception as exc:
            results.append(f"Orphaned appeals: FAIL - {type(exc).__name__}")

        # 9. Mod Bot link used by Execute Unban
        if modbot_client.is_configured():
            results.append(f"Mod Bot unban link: configured (main guild {MAIN_GUILD_ID})")
        else:
            results.append("Mod Bot unban link: NOT configured - Execute Unban will refuse")

        embed = discord.Embed(
            title="Appeals Bot - Health Check",
            description="```\n" + "\n".join(results) + "\n```",
            color=COLOUR_PRIMARY,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="North Florida Police Department  |  Debug", icon_url=bot_avatar_url())
        await interaction.followup.send(embed=embed, ephemeral=True)
