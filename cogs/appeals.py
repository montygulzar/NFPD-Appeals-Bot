import discord
from discord import app_commands
from discord.ext import commands

from config import (
    BOT_AVATAR_URL,
    VOTING_HOURS,
    COLOUR_INFO,
    COLOUR_VOTING,
    COLOUR_ACCEPTED,
    COLOUR_REJECTED,
    COLOUR_CLOSED,
    COLOUR_PRIMARY,
)


class AppealsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="forwardtobanteam",
        description="Manually forward this ticket's appeal to the ban team for voting.",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def forward_to_ban_team(self, interaction: discord.Interaction):
        """
        Fallback for when the in-ticket button is unavailable (e.g. message deleted).
        Must be run inside an appeal ticket channel.
        """
        await interaction.response.defer(ephemeral=True)
        db = self.bot.db
        channel = interaction.channel

        if not channel.topic or "Appeal ID:" not in channel.topic:
            await interaction.followup.send(
                "This channel does not appear to be an appeal ticket. "
                "Run this command inside a ticket channel.",
                ephemeral=True,
            )
            return

        try:
            appeal_id = int(channel.topic.split("Appeal ID:")[1].strip())
        except (ValueError, IndexError):
            await interaction.followup.send(
                "Could not parse the appeal ID from this channel's topic.",
                ephemeral=True,
            )
            return

        appeal = await db.get_appeal(appeal_id)
        if not appeal:
            await interaction.followup.send("No appeal found with that ID.", ephemeral=True)
            return

        if appeal["status"] != "open":
            await interaction.followup.send(
                f"This appeal is currently **{appeal['status'].replace('_', ' ').title()}** and cannot be forwarded.",
                ephemeral=True,
            )
            return

        config = await db.get_guild_config(interaction.guild_id)
        if not config or not config["voting_channel"]:
            await interaction.followup.send(
                "No voting channel configured. Run `/setup` first.", ephemeral=True
            )
            return

        voting_channel = interaction.guild.get_channel(config["voting_channel"])
        if not voting_channel:
            await interaction.followup.send(
                "Cannot find the configured voting channel.", ephemeral=True
            )
            return

        from datetime import datetime, timezone, timedelta
        closes_at = datetime.now(timezone.utc) + timedelta(hours=VOTING_HOURS)

        from views.appeal_actions import _build_voting_embed
        from views.voting_panel import VotingPanelView

        ban_team_mention = f"<@&{config['ban_team_role']}>" if config["ban_team_role"] else None
        voting_msg = await voting_channel.send(
            content=ban_team_mention,
            embed=_build_voting_embed(appeal, closes_at),
            view=VotingPanelView(self.bot, appeal_id),
        )

        # Atomic claim: only succeeds if the appeal is still 'open'. If another
        # staff member forwarded it a moment ago, delete the duplicate voting
        # message so the channel does not carry two live votes.
        claimed = await db.claim_open_for_voting(appeal_id, voting_msg.id, closes_at)
        if not claimed:
            try:
                await voting_msg.delete()
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                "This appeal was already forwarded a moment ago. No duplicate created.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Appeal forwarded to {voting_channel.mention}. "
            f"Voting closes <t:{int(closes_at.timestamp())}:R>.",
            ephemeral=True,
        )

    @app_commands.command(
        name="appealinfo",
        description="View the status and details of an appeal by ID.",
    )
    @app_commands.describe(appeal_id="The numeric appeal ID")
    @app_commands.default_permissions(manage_guild=True)
    async def appeal_info(self, interaction: discord.Interaction, appeal_id: int):
        await interaction.response.defer(ephemeral=True)
        appeal = await self.bot.db.get_appeal(appeal_id)

        if not appeal or appeal["guild_id"] != interaction.guild_id:
            await interaction.followup.send("No appeal found with that ID.", ephemeral=True)
            return

        tally = await self.bot.db.get_vote_tally(appeal_id)
        status = appeal["status"]

        embed = discord.Embed(
            title=f"Appeal #{appeal_id}",
            color=_status_colour(status),
        )
        embed.set_author(
            name="North Florida Police Department  |  Case File",
            icon_url=BOT_AVATAR_URL,
        )

        status_display = status.replace("_", " ").title()
        status_indicator = _status_indicator(status)

        embed.add_field(name="Status", value=f"{status_indicator} {status_display}", inline=True)
        embed.add_field(name="Banned on", value=appeal.get("platform", "Unknown"), inline=True)
        embed.add_field(name="Roblox Username", value=f"`{appeal['roblox_username']}`", inline=True)
        embed.add_field(name="Discord", value=appeal["discord_tag"], inline=True)
        embed.add_field(name="Appellant", value=f"<@{appeal['appellant_id']}>", inline=True)

        embed.add_field(name="Reason for Ban", value=appeal["ban_reason"], inline=False)
        embed.add_field(name="Appeal Statement", value=appeal["appeal_reason"], inline=False)

        total = tally["unban"] + tally["keep_banned"]
        vote_bar = _build_vote_bar(tally["unban"], tally["keep_banned"])
        embed.add_field(
            name=f"Vote Tally ({total} total)",
            value=(
                f"{vote_bar}\n"
                f"Unban: **{tally['unban']}**  |  Keep Banned: **{tally['keep_banned']}**"
            ),
            inline=False,
        )

        if appeal["closes_at"]:
            embed.add_field(
                name="Voting Window",
                value=f"Closes <t:{int(appeal['closes_at'].timestamp())}:R>",
                inline=True,
            )

        embed.set_footer(text=f"Submitted  •  Appeal ID: {appeal_id}")
        embed.timestamp = appeal["created_at"]

        await interaction.followup.send(embed=embed, ephemeral=True)


def _status_colour(status: str) -> int:
    return {
        "open": COLOUR_INFO,
        "voting": COLOUR_VOTING,
        "accepted": COLOUR_ACCEPTED,
        "rejected": COLOUR_REJECTED,
        "closed": COLOUR_CLOSED,
    }.get(status, COLOUR_PRIMARY)


def _status_indicator(status: str) -> str:
    return {
        "open": "🔵",
        "voting": "🟡",
        "accepted": "🟢",
        "rejected": "🔴",
        "closed": "⚫",
    }.get(status, "⚪")


def _build_vote_bar(unban_count: int, keep_banned_count: int) -> str:
    total = unban_count + keep_banned_count
    if total == 0:
        return "▱▱▱▱▱▱▱▱▱▱  No votes yet"
    filled = round((unban_count / total) * 10)
    bar = "▰" * filled + "▱" * (10 - filled)
    return f"🟢 {bar} 🔴"
