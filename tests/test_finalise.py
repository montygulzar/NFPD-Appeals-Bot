"""Tests for the shared finalisation path used by both the scheduled sweep
and /force_end_voting.

These tests exercise finalise_appeal() with an in-memory fake DB that models
just enough of asyncpg to prove the atomic claim contract:

  * Exactly one of many concurrent callers wins the claim.
  * Losers report won_claim=False and do NOT re-finalise.
  * A subsequent scheduled sweep on the same appeal will not re-finalise it
    (because the atomic claim now finds status != 'voting').
  * Force-end and scheduled-end apply identical vote arithmetic.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

# Stub the config env before anything imports it.
os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

# We only need the finalise_appeal function - avoid importing bot.py.
sys.modules.pop("config", None)
sys.modules.pop("cogs.voting", None)
from cogs import voting as voting_mod


class FakeDB:
    def __init__(self, appeal, votes):
        self._appeal = dict(appeal)
        self._votes = list(votes)
        self.close_calls = []
        self.cooldowns = []
        self.claim_lock = asyncio.Lock()

    async def claim_for_finalisation(self, appeal_id, actor_id=None):
        # Serialise the atomic UPDATE ... WHERE status='voting'.
        async with self.claim_lock:
            if self._appeal["id"] != appeal_id:
                return False
            if self._appeal["status"] != "voting":
                return False
            self._appeal["status"] = "finalising"
            self._appeal["finalised_by"] = actor_id
            return True

    async def get_appeal(self, appeal_id):
        return self._appeal if self._appeal["id"] == appeal_id else None

    async def get_vote_tally(self, appeal_id):
        tally = {"unban": 0, "keep_banned": 0}
        for v in self._votes:
            tally[v] += 1
        return tally

    async def get_guild_config(self, guild_id):
        return None  # skip channel posting entirely

    async def close_appeal(self, appeal_id, status):
        self.close_calls.append((appeal_id, status))
        self._appeal["status"] = status

    async def set_cooldown(self, *args, **kwargs):
        self.cooldowns.append((args, kwargs))


class FakeBot:
    def __init__(self, db):
        self.db = db

    def get_guild(self, guild_id):
        return None

    async def fetch_user(self, user_id):
        raise Exception("no discord in tests")


def _appeal(status="voting"):
    return {
        "id": 42,
        "guild_id": 1,
        "appellant_id": 7,
        "roblox_username": "user",
        "discord_tag": "user#0",
        "ban_reason": "r",
        "appeal_reason": "a",
        "ticket_channel": None,
        "voting_msg_id": None,
        "status": status,
        "closes_at": None,
        "platform": "Discord",
    }


@pytest.mark.asyncio
async def test_verdict_accepted_when_unban_majority():
    db = FakeDB(_appeal(), ["unban", "unban", "unban", "keep_banned"])
    bot = FakeBot(db)
    result = await voting_mod.finalise_appeal(bot, dict(db._appeal), force=False)
    assert result.won_claim is True
    assert result.verdict == "accepted"
    assert db.close_calls == [(42, "accepted")]


@pytest.mark.asyncio
async def test_verdict_rejected_when_keep_majority():
    db = FakeDB(_appeal(), ["keep_banned", "keep_banned", "keep_banned"])
    result = await voting_mod.finalise_appeal(FakeBot(db), dict(db._appeal))
    assert result.won_claim is True
    assert result.verdict == "rejected"
    assert db.close_calls == [(42, "rejected")]


@pytest.mark.asyncio
async def test_inconclusive_below_minimum():
    # MINIMUM_VOTES defaults to 3
    db = FakeDB(_appeal(), ["unban", "unban"])
    result = await voting_mod.finalise_appeal(FakeBot(db), dict(db._appeal))
    assert result.won_claim is True
    assert result.verdict == "closed"


@pytest.mark.asyncio
async def test_force_and_scheduled_produce_identical_outcome():
    votes = ["unban", "unban", "unban", "keep_banned"]
    a = FakeDB(_appeal(), votes)
    b = FakeDB(_appeal(), votes)
    ra = await voting_mod.finalise_appeal(FakeBot(a), dict(a._appeal), force=False)
    rb = await voting_mod.finalise_appeal(FakeBot(b), dict(b._appeal), force=True, actor_id=999)
    assert ra.verdict == rb.verdict == "accepted"
    assert ra.tally == rb.tally


@pytest.mark.asyncio
async def test_only_one_of_two_concurrent_calls_wins_claim():
    db = FakeDB(_appeal(), ["unban", "unban", "unban"])
    bot = FakeBot(db)
    a, b = await asyncio.gather(
        voting_mod.finalise_appeal(bot, dict(db._appeal), force=True, actor_id=1),
        voting_mod.finalise_appeal(bot, dict(db._appeal), force=True, actor_id=2),
    )
    winners = [r for r in (a, b) if r.won_claim]
    losers = [r for r in (a, b) if not r.won_claim]
    assert len(winners) == 1
    assert len(losers) == 1
    # Only the winner triggered a close_appeal write.
    assert len(db.close_calls) == 1


@pytest.mark.asyncio
async def test_scheduled_sweep_after_force_end_is_a_noop():
    db = FakeDB(_appeal(), ["unban", "unban", "unban"])
    bot = FakeBot(db)
    forced = await voting_mod.finalise_appeal(bot, dict(db._appeal), force=True)
    assert forced.won_claim is True
    # Simulate the scheduled task picking up the appeal a moment later.
    replay = await voting_mod.finalise_appeal(bot, dict(db._appeal), force=False)
    assert replay.won_claim is False
    assert len(db.close_calls) == 1  # no duplicate finalisation
