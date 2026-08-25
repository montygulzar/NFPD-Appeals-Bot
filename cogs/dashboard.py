import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_AVATAR_URL, COLOUR_PRIMARY


class DashboardCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Configure the appeals bot for this server.")
    @app_commands.describe(
        dashboard_channel="Channel where /createdashboard posts the public appeal panel",
        ticket_category="Category where new appeal ticket channels are created",
        voting_channel="Channel where the ban team votes on appeals",
        results_channel="Channel where verdict results are posted",
        ban_team_role="Role to ping when a new appeal is forwarded for voting",
        transcripts_channel="Optional. Channel where transcripts are posted on ticket close. Falls back to results channel if omitted.",
    )
    @app_commands.default_permissions(administrator=True)
    async def setup(
        self,
        interaction: discord.Interaction,
        dashboard_channel: discord.TextChannel,
        ticket_category: discord.CategoryChannel,
        voting_channel: discord.TextChannel,
        results_channel: discord.TextChannel,
        ban_team_role: discord.Role,
        transcripts_channel: discord.TextChannel | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        # dashboard_channel is stored in the legacy appeals_channel column
        # so no destructive DB migration is needed. The user-facing name is
        # what matters.
        await self.bot.db.upsert_guild_config(
            interaction.guild_id,
            appeals_channel=dashboard_channel.id,
            ticket_category=ticket_category.id,
            voting_channel=voting_channel.id,
            results_channel=results_channel.id,
            ban_team_role=ban_team_role.id,
            transcripts_channel=(transcripts_channel.id if transcripts_channel else None),
        )

        embed = discord.Embed(
            title="Configuration Saved",
            description="The appeals bot is ready to use. Run `/createdashboard` in your dashboard channel to post the submission panel.",
            color=COLOUR_PRIMARY,
        )
        embed.add_field(name="Dashboard Channel", value=dashboard_channel.mention, inline=True)
        embed.add_field(name="Ticket Category", value=ticket_category.mention, inline=True)
        embed.add_field(name="Voting Channel", value=voting_channel.mention, inline=True)
        embed.add_field(name="Results Channel", value=results_channel.mention, inline=True)
        embed.add_field(
            name="Transcripts Channel",
            value=(
                transcripts_channel.mention if transcripts_channel
                else f"(falls back to {results_channel.mention})"
            ),
            inline=True,
        )
        embed.add_field(name="Ban Team Role", value=ban_team_role.mention, inline=True)
        embed.set_footer(text="North Florida Police Department  |  Ban Appeals System")
        embed.timestamp = discord.utils.utcnow()

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(
        name="createdashboard",
        description="Post the ban appeal dashboard panel in this channel.",
    )
    @app_commands.default_permissions(administrator=True)
    async def create_dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        config = await self.bot.db.get_guild_config(interaction.guild_id)
        if not config or not config["appeals_channel"]:
            await interaction.followup.send(
                "Please run `/setup` first to configure the appeals bot.",
                ephemeral=True,
            )
            return

        if config["dashboard_msg_id"]:
            try:
                old_msg = await interaction.channel.fetch_message(config["dashboard_msg_id"])
                await old_msg.delete()
            except (discord.NotFound, discord.HTTPException):
                pass

        from views.appeal_panel import AppealPanelView
        msg = await interaction.channel.send(
            embed=_build_dashboard_embed(),
            view=AppealPanelView(self.bot),
        )

        await self.bot.db.upsert_guild_config(interaction.guild_id, dashboard_msg_id=msg.id)
        await interaction.followup.send("Dashboard posted.", ephemeral=True)


def _build_dashboard_embed() -> discord.Embed:
    embed = discord.Embed(
        title="NFPD Ban Appeals",
        description=(
            "If you believe your ban was issued in error, or you have since reflected on your "
            "conduct and wish to request reconsideration, you may submit a formal appeal below.\n\n"
            "**Before you submit:**\n"
            "> Appeals are reviewed by the NFPD Ban Team.\n"
            "> You may only hold one open appeal at a time.\n"
            "> Reviews take up to 48 hours once forwarded to the team.\n"
            "> Dishonest or frivolous appeals may result in a permanent ban.\n"
            "> Be clear, honest, and respectful in your statement."
        ),
        color=COLOUR_PRIMARY,
    )
    embed.set_author(
        name="North Florida Police Department  |  Ban Appeals",
        icon_url=BOT_AVATAR_URL,
    )
    embed.set_footer(text="Press the button below to begin your appeal  •  NFPD Ban Appeals")
    return embed
