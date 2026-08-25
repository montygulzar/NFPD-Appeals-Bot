import asyncio
from datetime import datetime, timezone, timedelta

import discord

from views.transcript import build_transcript_embed, generate_transcript
from config import (
    BOT_AVATAR_URL,
    COLOUR_ACCEPTED,
    COLOUR_CLOSED,
    COLOUR_INFO,
    COLOUR_REJECTED,
    COLOUR_VOTING,
    MINIMUM_VOTES,
    VOTING_HOURS,
)


class AppealActionsView(discord.ui.View):
    """Staff-facing buttons inside the private ticket channel."""

    def __init__(self, bot, appeal_id: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self.appeal_id = appeal_id

    def _resolve_appeal_id(self, channel: discord.TextChannel) -> int | None:
        if self.appeal_id:
            return self.appeal_id
        if channel.topic and "Appeal ID:" in channel.topic:
            try:
                return int(channel.topic.split("Appeal ID:")[1].strip())
            except (ValueError, IndexError):
                pass
        return None

    @discord.ui.button(
        label="Forward to Ban Team",
        style=discord.ButtonStyle.success,
        custom_id="appeals:forward",
    )
    async def forward_to_ban_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)

        appeal_id = self._resolve_appeal_id(interaction.channel)
        if not appeal_id:
            await interaction.followup.send("Could not determine the appeal ID for this ticket.", ephemeral=True)
            return

        db = self.bot.db
        config = await db.get_guild_config(interaction.guild_id)

        if not config or not config["voting_channel"]:
            await interaction.followup.send("No voting channel configured. Run `/setup` first.", ephemeral=True)
            return

        appeal = await db.get_appeal(appeal_id)
        if not appeal:
            await interaction.followup.send("This appeal could not be found.", ephemeral=True)
            return

        if appeal["status"] != "open":
            await interaction.followup.send(
                f"This appeal is already **{appeal['status'].replace('_', ' ').title()}** and cannot be forwarded.",
                ephemeral=True,
            )
            return

        voting_channel = interaction.guild.get_channel(config["voting_channel"])
        if not voting_channel:
            await interaction.followup.send("The voting channel could not be found. Check `/setup`.", ephemeral=True)
            return

        closes_at = datetime.now(timezone.utc) + timedelta(hours=VOTING_HOURS)
        ban_team_mention = f"<@&{config['ban_team_role']}>" if config["ban_team_role"] else None

        from views.voting_panel import VotingPanelView
        voting_msg = await voting_channel.send(
            content=ban_team_mention,
            embed=_build_voting_embed(appeal, closes_at),
            view=VotingPanelView(self.bot, appeal_id),
        )

        # Atomic claim - loses gracefully if another staff double-clicks or
        # runs /forwardtobanteam at the same time.
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

        button.disabled = True
        button.label = "Forwarded to Ban Team ✔"
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        await interaction.followup.send(
            f"Forwarded to {voting_channel.mention}. Voting closes {discord.utils.format_dt(closes_at, 'R')}.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="appeals:close_ticket",
    )
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        appeal_id = self._resolve_appeal_id(interaction.channel)
        db = self.bot.db

        import logging
        _logger = logging.getLogger("appeals-bot.transcript")

        if appeal_id:
            appeal = await db.get_appeal(appeal_id)
            if appeal and appeal["status"] not in ("actioned_unban", "actioned_close"):
                await db.close_appeal(appeal_id, "closed")

                config = await db.get_guild_config(interaction.guild_id)
                if config and config.get("results_channel"):
                    results_channel = interaction.guild.get_channel(config["results_channel"])
                    if results_channel is None:
                        try:
                            results_channel = await interaction.guild.fetch_channel(config["results_channel"])
                        except discord.HTTPException:
                            _logger.warning("Could not find results channel %s", config["results_channel"])
                    if results_channel:
                        try:
                            transcript_file = await generate_transcript(interaction.channel, appeal)
                            embed = build_transcript_embed(appeal, interaction.user, "Closed by staff")
                            await results_channel.send(embed=embed, file=transcript_file)
                        except Exception:
                            _logger.exception("Failed to post transcript for appeal %s", appeal_id)

        await interaction.followup.send("Closing ticket in 5 seconds...", ephemeral=True)
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Appeal {appeal_id} closed by {interaction.user}")
        except discord.HTTPException:
            pass




def _build_voting_embed(appeal, closes_at: datetime) -> discord.Embed:
    from views.voting_panel import _build_vote_bar

    embed = discord.Embed(
        title=f"Appeal #{appeal['id']} - Vote Required",
        description=(
            "A ban appeal has been escalated for team review.\n"
            "Read the case carefully and cast your vote below.\n\n"
            f"> Minimum **{MINIMUM_VOTES} votes** required for a valid result.\n"
            f"> Voting closes {discord.utils.format_dt(closes_at, 'R')}."
        ),
        color=COLOUR_VOTING,
    )
    embed.set_author(name="North Florida Police Department  |  Ban Team Vote", icon_url=BOT_AVATAR_URL)
    embed.add_field(name="Banned on", value=appeal.get("platform", "Unknown"), inline=True)
    embed.add_field(name="Roblox Username", value=f"`{appeal['roblox_username']}`", inline=True)
    embed.add_field(name="Discord", value=appeal["discord_tag"], inline=True)
    embed.add_field(name="Appellant", value=f"<@{appeal['appellant_id']}>", inline=True)

    embed.add_field(name="Reason for Ban", value=appeal["ban_reason"], inline=False)
    embed.add_field(name="Appeal Statement", value=appeal["appeal_reason"], inline=False)

    embed.add_field(
        name="Voting Window",
        value=(
            f"Opened: {discord.utils.format_dt(datetime.now(timezone.utc), 'F')}\n"
            f"Closes: {discord.utils.format_dt(closes_at, 'F')}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Vote Progress",
        value=f"🟢 {_build_vote_bar(0, 0)} 🔴\n**0** vote(s) cast",
        inline=False,
    )
    embed.add_field(name="🟢 Unban", value="**0** vote(s)", inline=True)
    embed.add_field(name="🔴 Keep Banned", value="**0** vote(s)", inline=True)

    embed.set_footer(text=f"Appeal ID: {appeal['id']}  •  Minimum {MINIMUM_VOTES} votes required")
    embed.timestamp = discord.utils.utcnow()
    return embed
