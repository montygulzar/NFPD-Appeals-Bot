import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands, tasks

from config import (
    APPEAL_COOLDOWN_HOURS,
    BOT_AVATAR_URL,
    MINIMUM_VOTES,
    COLOUR_ACCEPTED,
    COLOUR_REJECTED,
    COLOUR_CLOSED,
)

logger = logging.getLogger("appeals-bot.voting")


class FinaliseResult:
    """Outcome of a finalisation attempt."""

    __slots__ = ("won_claim", "verdict", "tally", "total_votes")

    def __init__(self, won_claim: bool, verdict: Optional[str], tally: dict, total_votes: int):
        self.won_claim = won_claim
        self.verdict = verdict            # 'accepted' | 'rejected' | 'inconclusive' | None
        self.tally = tally
        self.total_votes = total_votes


class VotingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        # Start the scheduled sweep as part of cog load, not __init__.
        # Prevents accidental double-starts if the cog is reloaded, and defers
        # starting until the bot is actually ready (via before_loop).
        if not self.check_expired_votes.is_running():
            self.check_expired_votes.start()

    def cog_unload(self):
        self.check_expired_votes.cancel()

    @tasks.loop(minutes=5)
    async def check_expired_votes(self):
        try:
            await self._process_expired_appeals()
        except Exception:
            logger.exception("Error in check_expired_votes loop")

    @check_expired_votes.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def _process_expired_appeals(self):
        expired = await self.bot.db.get_appeals_due_for_verdict()
        for appeal in expired:
            try:
                await finalise_appeal(self.bot, appeal, actor_id=None, force=False)
            except Exception:
                logger.exception("Failed to finalise appeal %s", appeal["id"])


# ---------------------------------------------------------------------------
# Public finalisation entry point
# ---------------------------------------------------------------------------

async def finalise_appeal(
    bot,
    appeal,
    *,
    actor_id: Optional[int] = None,
    force: bool = False,
) -> FinaliseResult:
    """The single source of truth for closing out a vote.

    Called both by the scheduled sweep (when closes_at has passed) and by
    /force_end_voting. Uses a conditional UPDATE to atomically claim the
    appeal so two simultaneous callers cannot both finalise it and cannot
    both post results.

    ``force`` only affects how the "inconclusive" vs "verdict" decision is
    described in the DM and result embed - the actual vote arithmetic is
    exactly the same in both paths so a staff force-end can never change the
    outcome relative to letting the timer run out.
    """
    db = bot.db
    appeal_id = appeal["id"]

    claimed = await db.claim_for_finalisation(appeal_id, actor_id=actor_id)
    if not claimed:
        # Someone else got here first - refetch so the caller can render a
        # useful message. verdict==None signals "did not perform the finalise".
        fresh = await db.get_appeal(appeal_id)
        tally = await db.get_vote_tally(appeal_id)
        return FinaliseResult(
            won_claim=False,
            verdict=(fresh["status"] if fresh else None),
            tally=tally,
            total_votes=tally["unban"] + tally["keep_banned"],
        )

    # We own the finalisation from here.
    tally = await db.get_vote_tally(appeal_id)
    total_votes = tally["unban"] + tally["keep_banned"]

    config = await db.get_guild_config(appeal["guild_id"])
    guild = bot.get_guild(appeal["guild_id"])

    results_channel = None
    ticket_channel = None
    if guild and config:
        if config["results_channel"]:
            results_channel = guild.get_channel(config["results_channel"])
        if appeal["ticket_channel"]:
            ticket_channel = guild.get_channel(appeal["ticket_channel"])
        await _disable_voting_message(guild, config, appeal)

    if total_votes < MINIMUM_VOTES:
        embed = _build_inconclusive_embed(appeal, tally, total_votes, forced=force)
        for ch in (results_channel, ticket_channel):
            if ch is not None:
                try:
                    await ch.send(embed=embed)
                except discord.HTTPException:
                    logger.warning("Could not post inconclusive result for appeal %s", appeal_id)
        await db.close_appeal(appeal_id, "closed")
        await _apply_cooldown(db, appeal, "Appeal closed inconclusively")
        await _dm_appellant(bot, appeal, verdict="inconclusive")
        return FinaliseResult(True, "closed", tally, total_votes)

    unban_wins = tally["unban"] > tally["keep_banned"]
    verdict = "accepted" if unban_wins else "rejected"
    embed = _build_verdict_embed(appeal, tally, total_votes, unban_wins, forced=force)

    from views.verdict_panel import VerdictPanelView
    ban_team_mention = (
        f"<@&{config['ban_team_role']}>" if config and config["ban_team_role"] else None
    )

    for ch in (results_channel, ticket_channel):
        if ch is None:
            continue
        try:
            await ch.send(
                content=ban_team_mention if ch is results_channel else None,
                embed=embed,
                view=VerdictPanelView(bot, appeal_id),
            )
        except discord.HTTPException:
            logger.warning("Could not post verdict for appeal %s in %s", appeal_id, ch.id)

    await db.close_appeal(appeal_id, verdict)
    await _apply_cooldown(
        db, appeal,
        "Appeal accepted" if unban_wins else "Appeal rejected",
    )
    await _dm_appellant(bot, appeal, verdict=verdict)
    return FinaliseResult(True, verdict, tally, total_votes)


async def _apply_cooldown(db, appeal, reason: str) -> None:
    if APPEAL_COOLDOWN_HOURS <= 0:
        return
    try:
        await db.set_cooldown(appeal["guild_id"], appeal["appellant_id"], APPEAL_COOLDOWN_HOURS, reason)
    except Exception:
        logger.exception("Failed to set cooldown for user %s", appeal["appellant_id"])


# ---------------------------------------------------------------------------
# Embed / DM helpers
# ---------------------------------------------------------------------------

async def _dm_appellant(bot, appeal, *, verdict: str) -> None:
    # Courtesy DM only - must never take down the finalisation path.
    try:
        user = await bot.fetch_user(appeal["appellant_id"])
    except Exception:
        logger.debug("Could not fetch user %s to DM verdict", appeal["appellant_id"])
        return

    if verdict == "accepted":
        embed = discord.Embed(
            title="Ban Appeal - Accepted",
            description=(
                "Your ban appeal has been reviewed by the NFPD Ban Team.\n\n"
                "**The vote was in your favour.** A staff member will action your unban shortly.\n\n"
                "You will regain access to North Florida City Police Department once the unban has been executed."
            ),
            color=COLOUR_ACCEPTED,
        )
    elif verdict == "rejected":
        embed = discord.Embed(
            title="Ban Appeal - Rejected",
            description=(
                "Your ban appeal has been reviewed by the NFPD Ban Team.\n\n"
                "**The vote did not go in your favour.** Your ban will remain in place.\n\n"
                "If you believe this outcome is incorrect, you may contact a senior staff member. "
                "Please be respectful in any further communication."
            ),
            color=COLOUR_REJECTED,
        )
    else:  # inconclusive
        embed = discord.Embed(
            title="Ban Appeal - Inconclusive",
            description=(
                "Your ban appeal did not receive enough votes within the voting window to reach a valid result.\n\n"
                f"A minimum of **{MINIMUM_VOTES} votes** is required. Your appeal has been closed without a verdict.\n\n"
                "Staff may reopen your appeal manually. If you have questions, please contact a staff member."
            ),
            color=COLOUR_CLOSED,
        )

    embed.set_footer(text=f"Appeal #{appeal['id']}  |  NFPD Ban Appeals")
    embed.add_field(name="Roblox Username", value=f"`{appeal['roblox_username']}`", inline=True)
    embed.add_field(name="Appeal ID", value=str(appeal["id"]), inline=True)
    embed.timestamp = discord.utils.utcnow()

    try:
        await user.send(embed=embed)
    except Exception:
        logger.debug("Could not DM appellant %s for appeal %s", appeal["appellant_id"], appeal["id"])


def _build_verdict_embed(appeal, tally: dict, total_votes: int, unban_wins: bool, *, forced: bool = False) -> discord.Embed:
    from views.voting_panel import _build_vote_bar
    colour = COLOUR_ACCEPTED if unban_wins else COLOUR_REJECTED
    outcome = "UNBAN APPROVED" if unban_wins else "KEEP BANNED"
    icon = "🟢" if unban_wins else "🔴"
    bar = _build_vote_bar(tally["unban"], tally["keep_banned"])
    action = (
        "Use **Execute Unban** to lift the ban, or **Close Ticket** to dismiss."
        if unban_wins
        else "Use **Close Ticket** to formally close this appeal."
    )
    title_prefix = "Voting Force-Ended" if forced else "Voting Closed"

    embed = discord.Embed(
        title=f"{icon}  {title_prefix}  —  {outcome}",
        description=(
            f"The voting window for Appeal #{appeal['id']} has closed.\n\n"
            f"{action}"
        ),
        color=colour,
    )
    embed.set_author(name="North Florida Police Department  |  Verdict", icon_url=BOT_AVATAR_URL)
    embed.add_field(name="Roblox Username", value=f"`{appeal['roblox_username']}`", inline=True)
    embed.add_field(name="Discord", value=appeal["discord_tag"], inline=True)
    embed.add_field(name="Appellant", value=f"<@{appeal['appellant_id']}>", inline=True)
    embed.add_field(
        name=f"📊 Final Vote  ({total_votes} cast)",
        value=(
            f"🟢 {bar} 🔴\n"
            f"**{tally['unban']}** unban  •  **{tally['keep_banned']}** keep banned"
        ),
        inline=False,
    )
    footer = f"Appeal ID: {appeal['id']}  •  "
    footer += "Voting force-ended by staff" if forced else "Voting closed"
    embed.set_footer(text=footer)
    embed.timestamp = discord.utils.utcnow()
    return embed


def _build_inconclusive_embed(appeal, tally: dict, total_votes: int, *, forced: bool = False) -> discord.Embed:
    title = "⚫  Voting Force-Ended  -  INCONCLUSIVE" if forced else "⚫  Voting Closed  -  INCONCLUSIVE"
    embed = discord.Embed(
        title=title,
        description=(
            f"Appeal #{appeal['id']} did not reach the minimum **{MINIMUM_VOTES} votes** required for a valid result.\n\n"
            f"Only **{total_votes}** vote(s) were cast. "
            "This appeal has been closed without a verdict. Staff may reopen it manually if required."
        ),
        color=COLOUR_CLOSED,
    )
    embed.set_author(
        name="North Florida Police Department  |  Inconclusive",
        icon_url=BOT_AVATAR_URL,
    )
    embed.add_field(name="Roblox Username", value=f"`{appeal['roblox_username']}`", inline=True)
    embed.add_field(name="Discord", value=appeal["discord_tag"], inline=True)
    embed.add_field(name="Unban Votes", value=str(tally["unban"]), inline=True)
    embed.add_field(name="Keep Banned Votes", value=str(tally["keep_banned"]), inline=True)
    embed.set_footer(text=f"Appeal ID: {appeal['id']}")
    embed.timestamp = discord.utils.utcnow()
    return embed


async def _disable_voting_message(guild: discord.Guild, config, appeal):
    if not config["voting_channel"] or not appeal["voting_msg_id"]:
        return
    voting_channel = guild.get_channel(config["voting_channel"])
    if not voting_channel:
        return
    try:
        msg = await voting_channel.fetch_message(appeal["voting_msg_id"])
        embed = msg.embeds[0] if msg.embeds else None
        if embed is not None:
            embed.color = discord.Color(COLOUR_CLOSED)
        await msg.edit(embed=embed, view=None)
    except (discord.NotFound, discord.HTTPException):
        pass
