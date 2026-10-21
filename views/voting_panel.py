import logging

import discord

from config import BAN_TEAM_ROLE_IDS, bot_avatar_url, COLOUR_ACCEPTED, COLOUR_REJECTED
from errors import report_interaction_error
from permissions import is_appeals_staff

logger = logging.getLogger("appeals-bot.voting")


def _build_vote_bar(unban: int, keep: int) -> str:
    total = unban + keep
    if total == 0:
        return "`▱▱▱▱▱▱▱▱▱▱`"
    filled = round((unban / total) * 10)
    return f"`{'▰' * filled}{'▱' * (10 - filled)}`"


class VotingPanelView(discord.ui.View):
    def __init__(self, bot, appeal_id: int = 0):
        super().__init__(timeout=None)
        self.bot = bot
        self.appeal_id = appeal_id

    async def _resolve_appeal(self, message: discord.Message):
        """Find the appeal behind this voting message.

        Looks it up by the stored voting_msg_id first, so it doesn't depend on
        embed text. The footer parse is kept for messages from before
        voting_msg_id was reliably stored.
        """
        db = self.bot.db
        if self.appeal_id:
            return await db.get_appeal(self.appeal_id)
        appeal = await db.get_appeal_by_voting_msg(message.id)
        if appeal is not None:
            return appeal
        try:
            footer = message.embeds[0].footer.text
            part = footer.split("Appeal ID:")[1].strip().split()[0].rstrip("|").strip()
            return await db.get_appeal(int(part))
        except (IndexError, ValueError, AttributeError):
            return None

    async def _can_vote(self, interaction: discord.Interaction) -> bool:
        """Ban team only. Fails closed: if no voter role is configured, or the
        guild config can't be read, only Appeals staff may vote."""
        member_role_ids = {role.id for role in getattr(interaction.user, "roles", [])}
        if BAN_TEAM_ROLE_IDS and (member_role_ids & BAN_TEAM_ROLE_IDS):
            return True
        try:
            config = await self.bot.db.get_guild_config(interaction.guild_id)
        except Exception:
            logger.exception("Vote permission check could not read guild config")
            return is_appeals_staff(interaction, None)
        db_role_id = config["ban_team_role"] if config else None
        if db_role_id and db_role_id in member_role_ids:
            return True
        if not BAN_TEAM_ROLE_IDS and not db_role_id:
            return is_appeals_staff(interaction, config)
        return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_interaction_error(interaction, error, where="vote")

    @discord.ui.button(label="Unban", style=discord.ButtonStyle.success, custom_id="votes:unban", emoji="🟢")
    async def vote_unban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, "unban")

    @discord.ui.button(label="Keep Banned", style=discord.ButtonStyle.danger, custom_id="votes:keep_banned", emoji="🔴")
    async def vote_keep_banned(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_vote(interaction, "keep_banned")

    async def _handle_vote(self, interaction: discord.Interaction, vote: str):
        await interaction.response.defer(ephemeral=True)

        if not await self._can_vote(interaction):
            await interaction.followup.send(
                "You do not have permission to vote on appeals.", ephemeral=True
            )
            return

        appeal = await self._resolve_appeal(interaction.message)
        if not appeal:
            await interaction.followup.send(
                "Could not determine which appeal this belongs to. Contact an admin.", ephemeral=True
            )
            return
        appeal_id = appeal["id"]

        if appeal["appellant_id"] == interaction.user.id:
            await interaction.followup.send("You cannot vote on your own appeal.", ephemeral=True)
            return

        db = self.bot.db
        accepted, previous_vote, status = await db.record_vote(appeal_id, interaction.user.id, vote)
        if not accepted:
            if status == "voting":
                text = "The voting window for this appeal has closed."
            elif status:
                text = f"This appeal is {status.replace('_', ' ')} and is no longer accepting votes."
            else:
                text = "This appeal could not be found."
            await interaction.followup.send(text, ephemeral=True)
            return

        tally = await db.get_vote_tally(appeal_id)

        try:
            fresh = await interaction.channel.fetch_message(interaction.message.id)
            await _update_vote_embed(fresh, tally)
        except Exception as exc:
            logger.warning("Failed to update voting embed for appeal %s: %s", appeal_id, exc)

        label = "Unban" if vote == "unban" else "Keep Banned"
        total = tally["unban"] + tally["keep_banned"]
        bar = _build_vote_bar(tally["unban"], tally["keep_banned"])

        if previous_vote == vote:
            msg = f"You have already voted **{label}**."
        elif previous_vote:
            old_label = "Unban" if previous_vote == "unban" else "Keep Banned"
            msg = f"Vote changed from **{old_label}** to **{label}**."
        else:
            msg = f"**{label}** vote recorded."

        color = COLOUR_ACCEPTED if vote == "unban" else COLOUR_REJECTED
        reply = discord.Embed(color=color)
        reply.set_author(name="North Florida Police Department  |  Ban Team Vote", icon_url=bot_avatar_url())
        reply.description = msg
        reply.add_field(
            name="Current Tally",
            value=(
                f"Unban: **{tally['unban']}**  -  Keep Banned: **{tally['keep_banned']}**\n"
                f"🟢 {bar} 🔴  -  **{total}** total"
            ),
            inline=False,
        )
        await interaction.followup.send(embed=reply, ephemeral=True)


async def _update_vote_embed(message: discord.Message, tally: dict) -> None:
    """Update the vote count fields on the voting embed.

    Uses set_field_at by position rather than matching field names, which avoids
    any fragility around spacing or emoji variants in field names.

    The voting embed always ends with three fields in order:
    Vote Progress, Unban, Keep Banned. We find their indices by checking whether
    "Progress", "Unban", or "Keep" appears in the field name.
    """
    if not message.embeds:
        return

    embed = message.embeds[0]
    fields = embed.fields
    if not fields:
        return

    bar = _build_vote_bar(tally["unban"], tally["keep_banned"])
    total = tally["unban"] + tally["keep_banned"]

    for index, field in enumerate(fields):
        name = field.name
        if "Progress" in name:
            embed.set_field_at(
                index,
                name=name,
                value=f"🟢 {bar} 🔴\n**{total}** vote(s) cast",
                inline=False,
            )
        elif "Unban" in name:
            embed.set_field_at(
                index,
                name=name,
                value=f"**{tally['unban']}** vote(s)",
                inline=True,
            )
        elif "Keep" in name:
            embed.set_field_at(
                index,
                name=name,
                value=f"**{tally['keep_banned']}** vote(s)",
                inline=True,
            )

    await message.edit(embed=embed)
