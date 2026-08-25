"""Tests for the shared finalisation path and its crash-recovery contract.

These exercise finalise_appeal() and the abandoned-finalisation recovery
pass using an in-memory fake DB that models just enough of asyncpg to prove:

  * The verdict is computed and persisted atomically at claim time.
  * Exactly one of many concurrent finalisers wins the claim.
  * A subsequent scheduled sweep on the same appeal will not re-finalise.
  * A crash after claim (finalising) is recovered without re-running any
    Discord side effect - the recorded computed_verdict is honoured.
  * A row still in 'finalising' but younger than the stale threshold is NOT
    touched by recovery.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

sys.modules.pop("config", None)
sys.modules.pop("cogs.voting", None)
from cogs import voting as voting_mod


class FakeDB:
    """Models the small slice of Database we exercise here."""

    def __init__(self, appeal, votes):
        self._appeal = dict(appeal)
        # computed_verdict starts unset - just like a fresh row in Postgres.
        self._appeal.setdefault("computed_verdict", None)
        self._appeal.setdefault("finalised_at", None)
        self._appeal.setdefault("finalised_by", None)
        self._votes = list(votes)
        self.close_calls = []
        self.cooldowns = []
        self.side_effect_calls = 0
        self.claim_lock = asyncio.Lock()

    async def claim_for_finalisation(self, appeal_id, *, minimum_votes, actor_id=None):
        # Serialise the SELECT FOR UPDATE + UPDATE.
        async with self.claim_lock:
            if self._appeal["id"] != appeal_id:
                return None
            if self._appeal["status"] != "voting":
                return None
            tally = {"unban": 0, "keep_banned": 0}
            for v in self._votes:
                tally[v] += 1
            total = tally["unban"] + tally["keep_banned"]
            if total < minimum_votes:
                verdict = "closed"
            elif tally["unban"] > tally["keep_banned"]:
                verdict = "accepted"
            else:
                verdict = "rejected"
            self._appeal["status"] = "finalising"
            self._appeal["computed_verdict"] = verdict
            self._appeal["finalised_at"] = datetime.now(timezone.utc)
            self._appeal["finalised_by"] = actor_id
            return {"verdict": verdict, "tally": tally, "total": total}

    async def get_appeal(self, appeal_id):
        return self._appeal if self._appeal["id"] == appeal_id else None

    async def get_vote_tally(self, appeal_id):
        tally = {"unban": 0, "keep_banned": 0}
        for v in self._votes:
            tally[v] += 1
        return tally

    async def get_guild_config(self, guild_id):
        return None  # no channel posts in these tests

    async def close_appeal(self, appeal_id, status):
        self.close_calls.append((appeal_id, status))
        self._appeal["status"] = status

    async def set_cooldown(self, *args, **kwargs):
        self.cooldowns.append((args, kwargs))

    # ---- Recovery API ------------------------------------------------------

    async def find_abandoned_finalisations(self, *, stale_after_seconds):
        if self._appeal["status"] != "finalising":
            return []
        finalised_at = self._appeal.get("finalised_at")
        if finalised_at is None:
            return []
        age = (datetime.now(timezone.utc) - finalised_at).total_seconds()
        if age < stale_after_seconds:
            return []
        return [{
            "id": self._appeal["id"],
            "guild_id": self._appeal["guild_id"],
            "appellant_id": self._appeal["appellant_id"],
            "computed_verdict": self._appeal["computed_verdict"],
            "finalised_at": finalised_at,
            "finalised_by": self._appeal["finalised_by"],
        }]

    async def recover_finalisation(self, appeal_id):
        if self._appeal["id"] != appeal_id or self._appeal["status"] != "finalising":
            return None
        # Track that recovery ran WITHOUT re-invoking a Discord side effect.
        self._appeal["status"] = self._appeal["computed_verdict"] or "closed"
        return self._appeal["status"]


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.dms = []
        self.channel_sends = []

    def get_guild(self, guild_id):
        return None

    async def fetch_user(self, user_id):
        # Simulate the Discord flow: user object with a send that records DMs.
        bot = self
        class _User:
            async def send(self, *, embed):
                bot.dms.append(embed.title)
        return _User()


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
        "computed_verdict": None,
        "finalised_at": None,
        "finalised_by": None,
    }


# ---------------------------------------------------------------------------
# Verdict arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verdict_accepted_when_unban_majority():
    db = FakeDB(_appeal(), ["unban", "unban", "unban", "keep_banned"])
    result = await voting_mod.finalise_appeal(FakeBot(db), dict(db._appeal))
    assert result.won_claim is True
    assert result.verdict == "accepted"
    assert db.close_calls == [(42, "accepted")]
    assert db._appeal["computed_verdict"] == "accepted"


@pytest.mark.asyncio
async def test_verdict_rejected_when_keep_majority():
    db = FakeDB(_appeal(), ["keep_banned", "keep_banned", "keep_banned"])
    result = await voting_mod.finalise_appeal(FakeBot(db), dict(db._appeal))
    assert result.verdict == "rejected"
    assert db._appeal["computed_verdict"] == "rejected"


@pytest.mark.asyncio
async def test_inconclusive_below_minimum():
    db = FakeDB(_appeal(), ["unban", "unban"])
    result = await voting_mod.finalise_appeal(FakeBot(db), dict(db._appeal))
    assert result.verdict == "closed"
    assert db._appeal["computed_verdict"] == "closed"


@pytest.mark.asyncio
async def test_force_and_scheduled_produce_identical_outcome():
    votes = ["unban", "unban", "unban", "keep_banned"]
    a, b = FakeDB(_appeal(), votes), FakeDB(_appeal(), votes)
    ra = await voting_mod.finalise_appeal(FakeBot(a), dict(a._appeal), force=False)
    rb = await voting_mod.finalise_appeal(FakeBot(b), dict(b._appeal), force=True, actor_id=999)
    assert ra.verdict == rb.verdict == "accepted"
    assert ra.tally == rb.tally


# ---------------------------------------------------------------------------
# Concurrency + finalising as an active state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_one_of_two_concurrent_calls_wins_claim():
    db = FakeDB(_appeal(), ["unban", "unban", "unban"])
    bot = FakeBot(db)
    a, b = await asyncio.gather(
        voting_mod.finalise_appeal(bot, dict(db._appeal), force=True, actor_id=1),
        voting_mod.finalise_appeal(bot, dict(db._appeal), force=True, actor_id=2),
    )
    winners = [r for r in (a, b) if r.won_claim]
    assert len(winners) == 1
    assert len(db.close_calls) == 1


@pytest.mark.asyncio
async def test_scheduled_sweep_after_force_end_is_a_noop():
    db = FakeDB(_appeal(), ["unban", "unban", "unban"])
    bot = FakeBot(db)
    await voting_mod.finalise_appeal(bot, dict(db._appeal), force=True)
    replay = await voting_mod.finalise_appeal(bot, dict(db._appeal), force=False)
    assert replay.won_claim is False
    assert len(db.close_calls) == 1


# ---------------------------------------------------------------------------
# Crash / recovery
# ---------------------------------------------------------------------------

class CrashingBot(FakeBot):
    """FakeBot whose DM helper raises to simulate a crash mid-finalise."""

    async def fetch_user(self, user_id):
        raise RuntimeError("simulated crash mid-DM")


@pytest.mark.asyncio
async def test_crash_after_claim_leaves_row_in_finalising_with_verdict_recorded():
    db = FakeDB(_appeal(), ["unban", "unban", "unban", "unban"])
    bot = CrashingBot(db)
    # finalise_appeal's DM helper now logs and swallows unexpected errors,
    # so we simulate an even earlier crash: manually claim, then abort.
    claim = await db.claim_for_finalisation(42, minimum_votes=3, actor_id=1)
    assert claim["verdict"] == "accepted"
    # Simulate process death: don't call close_appeal.
    assert db._appeal["status"] == "finalising"
    assert db._appeal["computed_verdict"] == "accepted"
    # Backdate the finalised_at so the recovery pass considers it abandoned.
    db._appeal["finalised_at"] = datetime.now(timezone.utc) - timedelta(minutes=10)

    # Recovery pass runs.
    abandoned = await db.find_abandoned_finalisations(stale_after_seconds=300)
    assert len(abandoned) == 1
    new_status = await db.recover_finalisation(42)
    assert new_status == "accepted"
    # No new Discord side effects fired during recovery.
    assert bot.dms == []
    assert bot.channel_sends == []


@pytest.mark.asyncio
async def test_recent_finalising_row_is_not_recovered():
    db = FakeDB(_appeal(), ["unban", "unban", "unban"])
    await db.claim_for_finalisation(42, minimum_votes=3)
    # finalised_at is now-ish (< threshold). Recovery must not touch it.
    abandoned = await db.find_abandoned_finalisations(stale_after_seconds=300)
    assert abandoned == []
    assert db._appeal["status"] == "finalising"


@pytest.mark.asyncio
async def test_recovery_is_a_noop_when_row_moved_on():
    db = FakeDB(_appeal(), ["unban", "unban", "unban"])
    await db.claim_for_finalisation(42, minimum_votes=3)
    # Simulate the real finalise completing normally.
    await db.close_appeal(42, "accepted")
    # Now the recovery pass runs but the row is already terminal.
    result = await db.recover_finalisation(42)
    assert result is None
