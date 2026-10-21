import logging

import discord

from config import APPEAL_COOLDOWN_HOURS, VOTING_HOURS, bot_avatar_url, COLOUR_INFO, COLOUR_PRIMARY

logger = logging.getLogger("appeals-bot.modal")


class AppealModal(discord.ui.Modal, title="NFPD Ban Appeal"):
    platform = discord.ui.TextInput(
        label="Where were you banned?",
        placeholder="Discord, Roblox, or both?",
        min_length=2,
        max_length=50,
    )
    roblox_username = discord.ui.TextInput(
        label="Roblox Username",
        placeholder="Your exact Roblox username",
        min_length=3,
        max_length=20,
    )
    ban_reason = discord.ui.TextInput(
        label="Reason You Were Banned",
        placeholder="What reason were you given for your ban?",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )
    appeal_reason = discord.ui.TextInput(
        label="Why Should You Be Unbanned?",
        placeholder="Explain your case honestly and in detail.",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        db = self.bot.db
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        existing = await db.get_active_appeal_for_user(guild_id, user_id)
        if existing:
            await interaction.followup.send(
                "You already have an open appeal. Please wait for it to be resolved before submitting another.",
                ephemeral=True,
            )
            return

        if APPEAL_COOLDOWN_HOURS > 0:
            cooldown = await db.get_active_cooldown(guild_id, user_id)
            if cooldown:
                await interaction.followup.send(
                    "You are currently on an appeal cooldown. You may submit a new "
                    f"appeal <t:{int(cooldown['expires_at'].timestamp())}:R>.",
                    ephemeral=True,
                )
                return

        config = await db.get_guild_config(guild_id)
        if not config or not config["appeals_channel"]:
            await interaction.followup.send(
                "Appeals are not configured on this server. Please contact an administrator.",
                ephemeral=True,
            )
            return

        # ticket_category is required as of the split-setup change.
        # Refuse rather than fall back so tickets never land in the wrong
        # place - staff must re-run /setup after the upgrade.
        ticket_category_id = config["ticket_category"]
        ticket_category = (
            interaction.guild.get_channel(ticket_category_id) if ticket_category_id else None
        )
        if not isinstance(ticket_category, discord.CategoryChannel):
            await interaction.followup.send(
                "This server has not been fully set up. Please ask an administrator "
                "to re-run `/setup` and specify a `ticket_category`.",
                ephemeral=True,
            )
            return

        platform = self.platform.value.strip()
        roblox_username = self.roblox_username.value.strip()
        # Identity comes from Discord, not a typed field, so it can't be faked.
        discord_tag = str(interaction.user)
        ban_reason = self.ban_reason.value.strip()
        appeal_reason = self.appeal_reason.value.strip()

        appeal_id = await db.create_appeal(
            guild_id=guild_id,
            appellant_id=user_id,
            roblox_username=roblox_username,
            discord_tag=discord_tag,
            ban_reason=ban_reason,
            appeal_reason=appeal_reason,
            platform=platform,
        )
        if appeal_id is None:
            # Lost a race with a second submit: the active-appeal unique index
            # rejected this one.
            await interaction.followup.send(
                "You already have an open appeal. Please wait for it to be resolved before submitting another.",
                ephemeral=True,
            )
            return

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True,
            ),
            interaction.guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True, manage_messages=True,
            ),
        }
        ban_team_role = None
        if config.get("ban_team_role"):
            ban_team_role = interaction.guild.get_role(config["ban_team_role"])
            if ban_team_role:
                overwrites[ban_team_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True,
                )

        try:
            ticket_channel = await interaction.guild.create_text_channel(
                name=f"appeal-{interaction.user.name}",
                category=ticket_category,
                topic=f"Ban appeal submitted by {interaction.user} | Appeal ID: {appeal_id}",
                overwrites=overwrites,
            )
        except discord.HTTPException as exc:
            # Without a ticket the appeal can never be reviewed or closed, and
            # the 'open' row would block the user forever. Remove it so they
            # can retry once staff fix the cause (permissions, or the category
            # hitting Discord's 50-channel limit).
            logger.error("Could not create ticket channel for appeal %s: %s", appeal_id, exc)
            await db.delete_unstarted_appeal(appeal_id)
            await interaction.followup.send(
                "Your appeal could not be submitted because the ticket channel couldn't be "
                "created. Nothing was saved - please try again later, or contact a staff member.",
                ephemeral=True,
            )
            return

        await db.set_appeal_ticket_channel(appeal_id, ticket_channel.id)

        notice_embed = discord.Embed(
            title="Appeal Submitted",
            description=(
                f"Your appeal has been received and is pending review by NFPD staff.\n\n"
                "**What happens next:**\n"
                "> A staff member will review your case and forward it to the Ban Team.\n"
                f"> The Ban Team votes for {VOTING_HOURS} hours once the appeal is forwarded.\n"
                "> You will be notified in this channel when a verdict is reached.\n\n"
                "Please be patient. Do not submit duplicate appeals."
            ),
            color=COLOUR_INFO,
        )
        notice_embed.set_author(name="North Florida Police Department  |  Ban Appeals", icon_url=bot_avatar_url())
        notice_embed.set_thumbnail(url=interaction.user.display_avatar.url)
        notice_embed.set_footer(text=f"Appeal ID: {appeal_id}  |  NFPD Ban Appeals")
        notice_embed.timestamp = discord.utils.utcnow()

        case_embed = _build_case_embed(
            appeal_id=appeal_id,
            appellant=interaction.user,
            platform=platform,
            roblox_username=roblox_username,
            discord_tag=discord_tag,
            ban_reason=ban_reason,
            appeal_reason=appeal_reason,
        )

        from views.appeal_actions import AppealActionsView
        await ticket_channel.send(
            content=interaction.user.mention,
            embeds=[notice_embed, case_embed],
            view=AppealActionsView(self.bot, appeal_id),
        )

        if ban_team_role:
            await ticket_channel.send(
                f"{ban_team_role.mention} - A new ban appeal requires review.",
                allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False),
            )

        await interaction.followup.send(
            f"Your appeal has been submitted. You can track it in {ticket_channel.mention}.",
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        from errors import report_interaction_error
        await report_interaction_error(interaction, error, where="appeal modal")


def _build_case_embed(
    appeal_id: int,
    appellant: discord.Member,
    platform: str,
    roblox_username: str,
    discord_tag: str,
    ban_reason: str,
    appeal_reason: str,
) -> discord.Embed:
    embed = discord.Embed(title=f"Case File - Appeal #{appeal_id}", color=0x1E3A5F)
    embed.set_author(name="North Florida Police Department  |  Staff Review", icon_url=bot_avatar_url())
    embed.set_thumbnail(url=appellant.display_avatar.url)
    embed.add_field(name="Banned on", value=platform, inline=True)
    embed.add_field(name="Roblox Username", value=f"`{roblox_username}`", inline=True)
    embed.add_field(name="Discord", value=discord_tag, inline=True)
    embed.add_field(name="Submitted By", value=appellant.mention, inline=True)
    embed.add_field(name="Reason for Ban", value=ban_reason, inline=False)
    embed.add_field(name="Appeal Statement", value=appeal_reason, inline=False)
    embed.set_footer(text=f"Appeal ID: {appeal_id}  |  Awaiting staff review")
    embed.timestamp = discord.utils.utcnow()
    return embed
