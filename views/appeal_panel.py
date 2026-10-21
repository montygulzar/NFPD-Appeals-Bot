import discord

from config import APPEAL_COOLDOWN_HOURS
from errors import report_interaction_error
from views.appeal_modal import AppealModal


class AppealPanelView(discord.ui.View):
    """Persistent view attached to the dashboard message."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Submit a Ban Appeal",
        style=discord.ButtonStyle.primary,
        custom_id="appeals:open_modal",
    )
    async def open_appeal(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await self.bot.db.get_active_appeal_for_user(interaction.guild_id, interaction.user.id)
        if existing:
            await interaction.response.send_message(
                "You already have an open appeal. Please wait for it to be resolved before submitting another.",
                ephemeral=True,
            )
            return
        if APPEAL_COOLDOWN_HOURS > 0:
            # Tell them before they fill in the form, not after.
            cooldown = await self.bot.db.get_active_cooldown(interaction.guild_id, interaction.user.id)
            if cooldown:
                await interaction.response.send_message(
                    "You are currently on an appeal cooldown. You may submit a new "
                    f"appeal <t:{int(cooldown['expires_at'].timestamp())}:R>.",
                    ephemeral=True,
                )
                return
        await interaction.response.send_modal(AppealModal(self.bot))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_interaction_error(interaction, error, where="appeal panel")
