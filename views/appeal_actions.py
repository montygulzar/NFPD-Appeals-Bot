import asyncio
import logging
from datetime import datetime, timezone, timedelta

import discord

from channels import resolve_channel
from errors import report_interaction_error
from permissions import staff_button_check
from views.transcript import build_transcript_embed, generate_transcript, resolve_transcripts_channel
from config import (
    bot_avatar_url,
    COLOUR_ACCEPTED,
    COLOUR_CLOSED,
    COLOUR_INFO,
    COLOUR_REJECTED,
    COLOUR_VOTING,
    MINIMUM_VOTES,
    VOTING_HOURS,
)

logger = logging.getLogger("appeals-bot.tickets")


class AppealActionsView(discord.ui.View):
    """Staff-facing buttons inside the private ticket channel.

    The appellant can see this channel, so every button is staff-gated in
    interaction_check - otherwise the appellant could forward or close their
    own appeal.
    """

    def __init__(self, bot, appeal_id: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self.appeal_id = appeal_id

    async def _resolve_appeal(self, channel: discord.abc.GuildChannel):
        """Find the appeal for this ticket by its stored ticket_channel id;
        the channel topic (which staff can edit) is only a legacy fallback."""
        db = self.bot.db
        if self.appeal_id:
            return await db.get_appeal(self.appeal_id)
        appeal = await db.get_appeal_by_ticket_channel(channel.id)
        if appeal is not None:
            return appeal
        topic = getattr(channel, "topic", None)
        if topic and "Appeal ID:" in topic:
            try:
                return await db.get_appeal(int(topic.split("Appeal ID:")[1].strip()))
            except (ValueError, IndexError):
                pass
        return None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        appeal = None
        try:
            appeal = await self._resolve_appeal(interaction.channel)
        except Exception:
            logger.exception("Could not resolve appeal for ticket permission check")
        return await staff_button_check(
            self.bot, interaction,
            appellant_id=appeal["appellant_id"] if appeal else None,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_interaction_error(interaction, error, where="ticket action")

    @discord.ui.button(
        label="Forward to Ban Team",
        style=discord.ButtonStyle.success,
        custom_id="appeals:forward",
    )
    async def forward_to_ban_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = self.bot.db
        appeal = await self._resolve_appeal(interaction.channel)
        if not appeal:
            await interaction.followup.send("Could not determine the appeal for this ticket.", ephemeral=True)
            return
        appeal_id = appeal["id"]

        config = await db.get_guild_config(interaction.guild_id)
        if not config or not config["voting_channel"]:
            await interaction.followup.send("No voting channel configured. Run `/setup` first.", ephemeral=True)
            return

        if appeal["status"] != "open":
            await interaction.followup.send(
                f"This appeal is already **{appeal['status'].replace('_', ' ').title()}** and cannot be forwarded.",
                ephemeral=True,
            )
            return

        voting_channel = await resolve_channel(interaction.guild, config["voting_channel"])
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
            allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
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

        db = self.bot.db
        appeal = await self._resolve_appeal(interaction.channel)
        appeal_id = appeal["id"] if appeal else None

        if appeal:
            status = appeal["status"]
            # A live vote must be ended through the voting system, not by
            # deleting the ticket - otherwise the verdict is lost and the
            # voting message keeps live-looking buttons.
            if status in ("voting", "finalising"):
                await interaction.followup.send(
                    "This appeal is currently being voted on. Wait for voting to end, "
                    "or use `/force_end_voting` first.",
                    ephemeral=True,
                )
                return

            # open -> closed (closed before voting); accepted/rejected ->
            # actioned_close (verdict dismissed). Conditional, so a verdict
            # can't be overwritten and a double click can't post twice.
            if status == "open":
                changed = await db.transition_status(appeal_id, ["open"], "closed")
            elif status in ("accepted", "rejected"):
                changed = await db.transition_status(appeal_id, ["accepted", "rejected"], "actioned_close")
            else:
                changed = False  # already closed/actioned: just remove the channel

            if changed:
                config = await db.get_guild_config(interaction.guild_id)
                target = await resolve_transcripts_channel(interaction.guild, config)
                if target is None:
                    logger.warning(
                        "No transcripts destination for appeal %s (neither transcripts_channel "
                        "nor results_channel resolves).", appeal_id,
                    )
                else:
                    try:
                        transcript_file = await generate_transcript(interaction.channel, appeal)
                        embed = build_transcript_embed(appeal, interaction.user, "Closed by staff")
                        await target.send(embed=embed, file=transcript_file)
                    except Exception:
                        logger.exception("Failed to post transcript for appeal %s", appeal_id)

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
    embed.set_author(name="North Florida Police Department  |  Ban Team Vote", icon_url=bot_avatar_url())
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
