import asyncio
import logging

import discord

import modbot_client
from channels import resolve_channel
from errors import report_interaction_error
from permissions import staff_button_check
from config import bot_avatar_url, COLOUR_ACCEPTED, COLOUR_CLOSED

logger = logging.getLogger("appeals-bot.verdict")

# One in-flight Execute Unban per appeal within this process, so a double
# click can't fire two Mod Bot calls and two "unbanned" announcements.
_unban_locks: dict[int, asyncio.Lock] = {}


class VerdictPanelView(discord.ui.View):
    """Execute Unban / Close Ticket after a verdict.

    Posted in both the results channel and the appellant's ticket channel, so
    every button is staff-gated (the appellant must not be able to action
    their own verdict).
    """

    def __init__(self, bot, appeal_id: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self._appeal_id_override = appeal_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        appellant_id = None
        appeal_id = self._resolve_appeal_id(interaction.message)
        if appeal_id:
            try:
                appeal = await self.bot.db.get_appeal(appeal_id)
                appellant_id = appeal["appellant_id"] if appeal else None
            except Exception:
                logger.exception("Could not load appeal %s for verdict permission check", appeal_id)
        return await staff_button_check(self.bot, interaction, appellant_id=appellant_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_interaction_error(interaction, error, where="verdict panel")

    @discord.ui.button(label="Execute Unban", style=discord.ButtonStyle.success, custom_id="verdict:execute_unban")
    async def execute_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        appeal_id = self._resolve_appeal_id(interaction.message)
        if not appeal_id:
            await interaction.followup.send("Could not resolve appeal ID.", ephemeral=True)
            return

        lock = _unban_locks.setdefault(appeal_id, asyncio.Lock())
        if lock.locked():
            await interaction.followup.send("An unban for this appeal is already in progress.", ephemeral=True)
            return
        async with lock:
            await self._execute_unban(interaction, appeal_id)

    async def _execute_unban(self, interaction: discord.Interaction, appeal_id: int):
        db = self.bot.db
        appeal = await db.get_appeal(appeal_id)
        if not appeal:
            await interaction.followup.send("Appeal not found.", ephemeral=True)
            return

        status = appeal["status"]
        if status == "actioned_unban":
            await interaction.followup.send("This appeal has already been unbanned.", ephemeral=True)
            return
        if status == "rejected":
            await interaction.followup.send("The vote result was Keep Banned. Unban is not available.", ephemeral=True)
            return
        if status != "accepted":
            await interaction.followup.send("This verdict has already been actioned or is not actionable.", ephemeral=True)
            return

        # The ban lives in the main NFPD server, which the Mod Bot moderates.
        # The call is idempotent ("not_banned" if already lifted), so it is
        # safe to retry after a failure.
        result = await modbot_client.request_unban(
            user_id=appeal["appellant_id"],
            moderator_id=interaction.user.id,
            appeal_id=appeal_id,
            reason=f"Ban appeal #{appeal_id} accepted - actioned by {interaction.user} ({interaction.user.id})",
        )
        if not result.ok:
            await interaction.followup.send(
                f"Unban was **not** carried out: {_describe_failure(result)}\n"
                "The appeal is still marked Accepted, so you can press Execute Unban again once fixed.",
                ephemeral=True,
            )
            return

        if not await db.transition_status(appeal_id, ["accepted"], "actioned_unban"):
            await interaction.followup.send("This appeal was actioned by someone else a moment ago.", ephemeral=True)
            return

        if result.status == modbot_client.UNBANNED:
            result_text = (
                f"<@{appeal['appellant_id']}> (`{appeal['roblox_username']}`) has been unbanned "
                f"from the main server. Actioned by {interaction.user.mention}."
            )
            if result.case_id:
                result_text += f" Mod-log case #{result.case_id}."
        else:
            result_text = (
                f"`{appeal['roblox_username']}` was not banned in the main server "
                f"(they may already have been unbanned). Marked as actioned by {interaction.user.mention}."
            )

        self._disable_all_buttons()
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(
            result_text, ephemeral=False,
            allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False),
        )
        await _dm_unbanned(self.bot, appeal)
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

        # Conditional: only a pending verdict can be closed, and only once.
        if not await db.transition_status(appeal_id, ["accepted", "rejected", "closed"], "actioned_close"):
            await interaction.followup.send("This verdict has already been actioned.", ephemeral=True)
            return

        self._disable_all_buttons()
        try:
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
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


def _describe_failure(result: "modbot_client.UnbanResult") -> str:
    if result.status == modbot_client.FORBIDDEN:
        return "the Mod Bot lacks Ban Members permission in the main server."
    if result.status == modbot_client.NOT_CONFIGURED:
        return "the Mod Bot link isn't configured. " + result.detail
    return result.detail or f"Mod Bot returned `{result.status}`."


async def _dm_unbanned(bot, appeal) -> None:
    """Courtesy DM once the unban has actually happened."""
    try:
        user = await bot.fetch_user(appeal["appellant_id"])
        embed = discord.Embed(
            title="Ban Appeal - Unban Executed",
            description=(
                "Your unban has now been carried out. You can rejoin the "
                "North Florida City Police Department server."
            ),
            color=COLOUR_ACCEPTED,
        )
        embed.set_footer(text=f"Appeal #{appeal['id']}  |  NFPD Ban Appeals")
        await user.send(embed=embed)
    except discord.HTTPException:
        logger.debug("Could not DM appellant %s about executed unban", appeal["appellant_id"])
    except Exception:
        logger.exception("Unexpected error DMing appellant %s about executed unban", appeal["appellant_id"])


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

    channel = await resolve_channel(bot.get_guild(appeal["guild_id"]), appeal["ticket_channel"])
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
        close_embed.set_author(name="North Florida Police Department  |  Ban Appeals", icon_url=bot_avatar_url())
        close_embed.set_footer(text=f"Appeal ID: {appeal['id']}")
        close_embed.timestamp = discord.utils.utcnow()
        await channel.send(embed=close_embed)
        await asyncio.sleep(10)
        await channel.delete(reason=f"Appeal {appeal['id']} resolved.")
    except discord.HTTPException:
        pass
