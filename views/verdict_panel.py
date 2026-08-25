import asyncio
import logging

import discord

from config import BOT_AVATAR_URL, COLOUR_CLOSED

logger = logging.getLogger("appeals-bot.verdict")

_ACTIONED_STATUSES = {"actioned_unban", "actioned_close"}


class VerdictPanelView(discord.ui.View):
    def __init__(self, bot, appeal_id: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self._appeal_id_override = appeal_id

    @discord.ui.button(label="Execute Unban", style=discord.ButtonStyle.success, custom_id="verdict:execute_unban")
    async def execute_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        appeal_id = self._resolve_appeal_id(interaction.message)
        if not appeal_id:
            await interaction.followup.send("Could not resolve appeal ID.", ephemeral=True)
            return

        db = self.bot.db
        appeal = await db.get_appeal(appeal_id)
        if not appeal:
            await interaction.followup.send("Appeal not found.", ephemeral=True)
            return

        if appeal["status"] == "actioned_unban":
            await interaction.followup.send("This appeal has already been unbanned.", ephemeral=True)
            return
        if appeal["status"] in _ACTIONED_STATUSES or appeal["status"] not in ("accepted", "rejected"):
            await interaction.followup.send("This verdict has already been actioned or is not actionable.", ephemeral=True)
            return
        if appeal["status"] == "rejected":
            await interaction.followup.send("The vote result was Keep Banned. Unban is not available.", ephemeral=True)
            return

        try:
            await interaction.guild.unban(
                discord.Object(id=appeal["appellant_id"]),
                reason=f"Appeal {appeal_id} accepted - actioned by {interaction.user}",
            )
            result_text = (
                f"<@{appeal['appellant_id']}> (`{appeal['roblox_username']}`) has been unbanned. "
                f"Actioned by {interaction.user.mention}."
            )
        except discord.NotFound:
            result_text = (
                f"`{appeal['roblox_username']}` was not found in the ban list. "
                "They may have already been unbanned."
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to unban members. Check my role and permissions.", ephemeral=True
            )
            return

        await db.close_appeal(appeal_id, "actioned_unban")
        self._disable_all_buttons()
        await interaction.message.edit(view=self)
        await interaction.followup.send(result_text, ephemeral=False)
        await _close_ticket_channel(self.bot, appeal, closed_by=interaction.user, reason="Appeal accepted - unban executed")

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.secondary, custom_id="verdict:close_ticket")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        appeal_id = self._resolve_appeal_id(interaction.message)
        if not appeal_id:
            await interaction.followup.send("Could not resolve appeal ID.", ephemeral=True)
            return

        db = self.bot.db
        appeal = await db.get_appeal(appeal_id)
        if not appeal:
            await interaction.followup.send("Appeal not found.", ephemeral=True)
            return

        if appeal["status"] in _ACTIONED_STATUSES:
            await interaction.followup.send("This verdict has already been actioned.", ephemeral=True)
            return

        await db.close_appeal(appeal_id, "actioned_close")
        self._disable_all_buttons()
        await interaction.message.edit(view=self)
        await interaction.followup.send(f"Appeal {appeal_id} closed by {interaction.user.mention}.", ephemeral=False)
        await _close_ticket_channel(self.bot, appeal, closed_by=interaction.user, reason="Verdict actioned")

    def _resolve_appeal_id(self, message: discord.Message) -> int | None:
        if self._appeal_id_override:
            return self._appeal_id_override
        try:
            footer = message.embeds[0].footer.text
            part = footer.split("Appeal ID:")[1].strip().split()[0].rstrip("|").strip()
            return int(part)
        except (IndexError, ValueError, AttributeError):
            return None

    def _disable_all_buttons(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


async def _close_ticket_channel(
    bot,
    appeal: dict,
    *,
    closed_by: discord.Member | discord.User,
    reason: str,
) -> None:
    """Generate a transcript, post it to the results channel, then delete the ticket channel."""
    if not appeal["ticket_channel"]:
        return

    channel = None
    for guild in bot.guilds:
        channel = guild.get_channel(appeal["ticket_channel"])
        if channel:
            break

    if channel is None:
        return

    # Generate and post the transcript before the channel is deleted.
    try:
        config = await bot.db.get_guild_config(appeal["guild_id"])
        from views.transcript import (
            build_transcript_embed, generate_transcript, resolve_transcripts_channel,
        )
        target = await resolve_transcripts_channel(channel.guild, config)
        if target is None:
            logger.warning(
                "No transcripts destination for appeal %s (neither transcripts_channel "
                "nor results_channel resolves).", appeal["id"],
            )
        else:
            transcript_file = await generate_transcript(channel, appeal)
            transcript_embed = build_transcript_embed(appeal, closed_by, reason)
            await target.send(embed=transcript_embed, file=transcript_file)
    except Exception:
        logger.exception("Failed to generate or post transcript for appeal %s", appeal["id"])

    try:
        close_embed = discord.Embed(
            title="Ticket Closing",
            description="This appeal has been resolved. The channel will be deleted in 10 seconds.",
            color=COLOUR_CLOSED,
        )
        close_embed.set_author(name="North Florida Police Department  |  Ban Appeals", icon_url=BOT_AVATAR_URL)
        close_embed.set_footer(text=f"Appeal ID: {appeal['id']}")
        close_embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=close_embed)
        await asyncio.sleep(10)
        await channel.delete(reason=f"Appeal {appeal['id']} resolved.")
    except discord.HTTPException:
        pass
