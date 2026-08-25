"""Staff-only commands: /force_end_voting, /user_appeal_history, /remove_cooldown."""
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    APPEAL_COOLDOWN_HOURS,
    BOT_AVATAR_URL,
    COLOUR_ACCEPTED,
    COLOUR_CLOSED,
    COLOUR_INFO,
    COLOUR_PRIMARY,
    COLOUR_REJECTED,
    COLOUR_VOTING,
    STAFF_ROLE_IDS,
)
from cogs.voting import finalise_appeal

logger = logging.getLogger("appeals-bot.staff")


def _is_appeals_staff(interaction: discord.Interaction, guild_config) -> bool:
    """Truthy if the invoker counts as Appeals staff for this guild.

    Order of precedence:
      1. STAFF_ROLE_IDS env - a role in that set.
      2. Per-guild ban_team_role configured via /setup.
      3. Fallback: Discord manage_guild permission.
    """
    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    role_ids = {r.id for r in member.roles}
    if STAFF_ROLE_IDS and (role_ids & STAFF_ROLE_IDS):
        return True
    if guild_config and guild_config["ban_team_role"] in role_ids:
        return True
    perms = member.guild_permissions
    return perms.manage_guild or perms.administrator


def _status_display(status: str) -> str:
    return status.replace("_", " ").title()


def _status_colour(status: str) -> int:
    return {
        "open": COLOUR_INFO,
        "voting": COLOUR_VOTING,
        "accepted": COLOUR_ACCEPTED,
        "rejected": COLOUR_REJECTED,
        "closed": COLOUR_CLOSED,
        "actioned_unban": COLOUR_ACCEPTED,
        "actioned_close": COLOUR_CLOSED,
    }.get(status, COLOUR_PRIMARY)


class StaffCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ─────────────────────────────────────────────────────────────────
    # /force_end_voting
    # ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="force_end_voting",
        description="End an active appeal vote early and finalise the result.",
    )
    @app_commands.describe(appeal_id="The numeric appeal ID currently in voting.")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def force_end_voting(self, interaction: discord.Interaction, appeal_id: int):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db

        config = await db.get_guild_config(interaction.guild_id)
        if not _is_appeals_staff(interaction, config):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return

        appeal = await db.get_appeal(appeal_id)
        if not appeal or appeal["guild_id"] != interaction.guild_id:
            await interaction.followup.send("No appeal found with that ID in this server.", ephemeral=True)
            return

        if appeal["status"] != "voting":
            await interaction.followup.send(
                f"Appeal #{appeal_id} is not currently in voting "
                f"(status: **{_status_display(appeal['status'])}**). Cannot force-end.",
                ephemeral=True,
            )
            return

        try:
            result = await finalise_appeal(
                self.bot, appeal,
                actor_id=interaction.user.id,
                force=True,
            )
        except Exception:
            logger.exception("force_end_voting failed for appeal %s", appeal_id)
            await interaction.followup.send(
                "The database call failed while finalising this appeal. Please try again.",
                ephemeral=True,
            )
            return

        if not result.won_claim:
            await interaction.followup.send(
                f"Appeal #{appeal_id} was already finalised (now **{_status_display(result.verdict or 'unknown')}**). "
                "No action taken.",
                ephemeral=True,
            )
            return

        outcome_label = {
            "accepted": "UNBAN APPROVED",
            "rejected": "KEEP BANNED",
            "closed": "INCONCLUSIVE (below minimum votes)",
        }.get(result.verdict, result.verdict or "unknown")

        embed = discord.Embed(
            title="Voting Force-Ended",
            description=(
                f"Appeal **#{appeal_id}** has been closed early by {interaction.user.mention}.\n\n"
                f"**Outcome:** {outcome_label}\n"
                f"**Final tally:** {result.tally['unban']} unban  /  "
                f"{result.tally['keep_banned']} keep banned  "
                f"({result.total_votes} total)"
            ),
            color=_status_colour(result.verdict or "closed"),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Appeal ID: {appeal_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────
    # /user_appeal_history
    # ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="user_appeal_history",
        description="View a user's previous appeals in this server.",
    )
    @app_commands.describe(
        user="The Discord user to look up (mention or pick from list).",
        user_id="Alternative: a Discord user ID, in case the user is no longer in the server.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def user_appeal_history(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        user_id: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db

        config = await db.get_guild_config(interaction.guild_id)
        if not _is_appeals_staff(interaction, config):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return

        target_id: int | None = None
        if user is not None:
            target_id = user.id
        elif user_id:
            try:
                target_id = int(user_id.strip())
            except ValueError:
                await interaction.followup.send("`user_id` must be a numeric Discord ID.", ephemeral=True)
                return
        if target_id is None:
            await interaction.followup.send("Provide a `user` or a `user_id`.", ephemeral=True)
            return

        try:
            stats = await db.get_user_appeal_stats(interaction.guild_id, target_id)
            recent = await db.get_user_appeals(interaction.guild_id, target_id, limit=10)
        except Exception:
            logger.exception("user_appeal_history DB query failed for user %s", target_id)
            await interaction.followup.send(
                "The database lookup failed. Please try again.", ephemeral=True
            )
            return

        # Try to resolve a display name; falls back to raw ID for users who left.
        display = f"<@{target_id}>"
        if user is not None:
            display = f"{user.mention}  (`{user}`)"

        if stats["total"] == 0:
            embed = discord.Embed(
                title="No Appeals Found",
                description=f"{display} has never submitted an appeal in this server.",
                color=COLOUR_PRIMARY,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text=f"User ID: {target_id}")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Summary line
        summary_bits = [f"**Total:** {stats['total']}"]
        for key in ("accepted", "rejected", "closed", "actioned_unban", "actioned_close", "open", "voting"):
            if stats.get(key):
                summary_bits.append(f"{_status_display(key)}: **{stats[key]}**")

        embed = discord.Embed(
            title="Appeal History",
            description=f"For {display}\n\n" + "  •  ".join(summary_bits),
            color=COLOUR_PRIMARY,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(
            name="North Florida Police Department  |  Staff Lookup",
            icon_url=BOT_AVATAR_URL,
        )

        # Recent appeals list - guard against Discord embed field limits.
        max_field_chars = 1000
        lines: list[str] = []
        for row in recent:
            ts = int(row["created_at"].timestamp())
            reason = (row["appeal_reason"] or "").strip().replace("\n", " ")
            if len(reason) > 80:
                reason = reason[:77] + "..."
            platform = row.get("platform") or "Unknown"
            lines.append(
                f"**#{row['id']}**  •  <t:{ts}:d>  •  {_status_display(row['status'])}  "
                f"•  `{row['roblox_username']}`  •  {platform}\n"
                f"> {reason if reason else '_(no statement)_'}"
            )

        # Fit as many recent rows as Discord allows in one field.
        block = ""
        included = 0
        for line in lines:
            candidate = (block + "\n\n" + line) if block else line
            if len(candidate) > max_field_chars:
                break
            block = candidate
            included += 1
        if not block:
            block = "_(recent entries could not be rendered - use `/appealinfo <id>` for details)_"

        header = f"Most recent {included} of {len(recent)}"
        if len(recent) < stats["total"]:
            header += f" (out of {stats['total']} total)"
        embed.add_field(name=header, value=block, inline=False)
        embed.set_footer(text=f"User ID: {target_id}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ─────────────────────────────────────────────────────────────────
    # /remove_cooldown
    # ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="remove_cooldown",
        description="End a user's appeal cooldown early.",
    )
    @app_commands.describe(
        user="The Discord user whose cooldown should be removed.",
        user_id="Alternative: a Discord user ID (for users no longer in the server).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def remove_cooldown(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        user_id: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        db = self.bot.db

        config = await db.get_guild_config(interaction.guild_id)
        if not _is_appeals_staff(interaction, config):
            await interaction.followup.send("You do not have permission to use this command.", ephemeral=True)
            return

        target_id: int | None = None
        if user is not None:
            target_id = user.id
        elif user_id:
            try:
                target_id = int(user_id.strip())
            except ValueError:
                await interaction.followup.send("`user_id` must be a numeric Discord ID.", ephemeral=True)
                return
        if target_id is None:
            await interaction.followup.send("Provide a `user` or a `user_id`.", ephemeral=True)
            return

        if APPEAL_COOLDOWN_HOURS <= 0:
            await interaction.followup.send(
                "Cooldowns are disabled on this bot (APPEAL_COOLDOWN_HOURS=0), "
                "so there is nothing to remove.",
                ephemeral=True,
            )
            return

        try:
            removed = await db.remove_cooldown(interaction.guild_id, target_id)
        except Exception:
            logger.exception("remove_cooldown DB delete failed for user %s", target_id)
            await interaction.followup.send(
                "The database call failed. Please try again.", ephemeral=True
            )
            return

        display = user.mention if user is not None else f"<@{target_id}>"
        if not removed:
            embed = discord.Embed(
                title="No Active Cooldown",
                description=f"{display} does not currently have an active appeal cooldown.",
                color=COLOUR_CLOSED,
                timestamp=discord.utils.utcnow(),
            )
        else:
            embed = discord.Embed(
                title="Cooldown Removed",
                description=(
                    f"Appeal cooldown removed for {display} by {interaction.user.mention}.\n"
                    "They may now submit a new appeal (subject to the usual "
                    "'one active appeal at a time' rule)."
                ),
                color=COLOUR_ACCEPTED,
                timestamp=discord.utils.utcnow(),
            )
        embed.set_footer(text=f"User ID: {target_id}")
        await interaction.followup.send(embed=embed, ephemeral=True)
