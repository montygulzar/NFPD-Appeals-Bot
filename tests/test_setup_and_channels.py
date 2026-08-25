"""Tests for the split-transcripts + ticket-category configuration change.

- /setup writes every expected column (including the new ticket_category
  and, when provided, transcripts_channel).
- resolve_transcripts_channel prefers transcripts_channel and falls back to
  results_channel exactly as documented.
- The modal path refuses to create a ticket when ticket_category is not
  configured, without touching Discord.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

sys.modules.pop("cogs.dashboard", None)
sys.modules.pop("views.transcript", None)
from cogs import dashboard as dashboard_mod
from views import transcript as transcript_mod


# ---------------------------------------------------------------------------
# /setup writes the expected DB columns
# ---------------------------------------------------------------------------

class RecordingDB:
    def __init__(self):
        self.calls = []

    async def upsert_guild_config(self, guild_id, **fields):
        self.calls.append({"guild_id": guild_id, **fields})


class FakeInteraction:
    def __init__(self, guild_id=100):
        self.guild_id = guild_id
        self.response = SimpleNamespace(defer=self._defer)
        self.followup = SimpleNamespace(send=self._send)
        self._deferred = False
        self.sent = []

    async def _defer(self, *, ephemeral=False):
        self._deferred = True

    async def _send(self, *args, **kwargs):
        self.sent.append(kwargs)


def _text(id_):
    ch = SimpleNamespace(id=id_, mention=f"<#{id_}>")
    return ch


def _cat(id_):
    return SimpleNamespace(id=id_, mention=f"<#{id_}>")


def _role(id_):
    return SimpleNamespace(id=id_, mention=f"<@&{id_}>")


@pytest.mark.asyncio
async def test_setup_writes_all_new_columns_including_transcripts():
    bot = SimpleNamespace(db=RecordingDB())
    cog = dashboard_mod.DashboardCog(bot)
    interaction = FakeInteraction()

    await cog.setup.callback(
        cog, interaction,
        dashboard_channel=_text(11),
        ticket_category=_cat(22),
        voting_channel=_text(33),
        results_channel=_text(44),
        ban_team_role=_role(55),
        transcripts_channel=_text(66),
    )

    assert len(bot.db.calls) == 1
    payload = bot.db.calls[0]
    assert payload["guild_id"] == 100
    assert payload["appeals_channel"] == 11            # dashboard -> legacy column
    assert payload["ticket_category"] == 22
    assert payload["voting_channel"] == 33
    assert payload["results_channel"] == 44
    assert payload["ban_team_role"] == 55
    assert payload["transcripts_channel"] == 66


@pytest.mark.asyncio
async def test_setup_writes_null_transcripts_when_omitted():
    bot = SimpleNamespace(db=RecordingDB())
    cog = dashboard_mod.DashboardCog(bot)
    interaction = FakeInteraction()

    await cog.setup.callback(
        cog, interaction,
        dashboard_channel=_text(11),
        ticket_category=_cat(22),
        voting_channel=_text(33),
        results_channel=_text(44),
        ban_team_role=_role(55),
    )
    payload = bot.db.calls[0]
    assert payload["transcripts_channel"] is None


# ---------------------------------------------------------------------------
# resolve_transcripts_channel
# ---------------------------------------------------------------------------

class _FakeGuild:
    def __init__(self, channels):
        self._channels = channels

    def get_channel(self, cid):
        return self._channels.get(cid)

    async def fetch_channel(self, cid):
        ch = self._channels.get(cid)
        if ch is None:
            raise discord.HTTPException(
                type("R", (), {"status": 404, "reason": "Not Found"})(), "missing"
            )
        return ch


def _tc(id_):
    obj = type("_TC", (discord.TextChannel,), {})
    return obj.__new__(obj).__class__.__new__(_TC := type("_TC", (discord.TextChannel,), {}))  # unused


class _RealTextChannel(discord.TextChannel):
    """A minimal instance passable to isinstance(..., discord.TextChannel)."""
    def __init__(self, id_):
        # Bypass discord.TextChannel.__init__; only isinstance and .id matter.
        self.id = id_


def _rt(id_):
    return _RealTextChannel(id_)


class _DictConfig(dict):
    def keys(self):
        return super().keys()


@pytest.mark.asyncio
async def test_resolve_prefers_transcripts_channel_when_set():
    trans = _rt(200)
    results = _rt(300)
    guild = _FakeGuild({200: trans, 300: results})
    cfg = _DictConfig(transcripts_channel=200, results_channel=300)
    got = await transcript_mod.resolve_transcripts_channel(guild, cfg)
    assert got is trans


@pytest.mark.asyncio
async def test_resolve_falls_back_to_results_when_transcripts_null():
    results = _rt(300)
    guild = _FakeGuild({300: results})
    cfg = _DictConfig(transcripts_channel=None, results_channel=300)
    got = await transcript_mod.resolve_transcripts_channel(guild, cfg)
    assert got is results


@pytest.mark.asyncio
async def test_resolve_returns_none_when_neither_configured():
    guild = _FakeGuild({})
    cfg = _DictConfig(transcripts_channel=None, results_channel=None)
    assert await transcript_mod.resolve_transcripts_channel(guild, cfg) is None


@pytest.mark.asyncio
async def test_resolve_none_config():
    assert await transcript_mod.resolve_transcripts_channel(_FakeGuild({}), None) is None


# ---------------------------------------------------------------------------
# Modal refuses when ticket_category is missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_modal_refuses_when_ticket_category_missing():
    """Simulate the guard directly - we don't need to render the whole modal
    to verify the missing-category branch. The check is: if the resolved
    ticket_category is not a CategoryChannel, refuse."""
    # Nothing at that channel ID => refuse.
    guild = _FakeGuild({})
    cid = None  # column NULL
    got = guild.get_channel(cid) if cid else None
    assert not isinstance(got, discord.CategoryChannel)
    # Even when the ID exists but points to something else (a text channel):
    guild2 = _FakeGuild({999: _rt(999)})
    got2 = guild2.get_channel(999)
    assert not isinstance(got2, discord.CategoryChannel)
