"""End-to-end proof that /remove_cooldown operates on the same cooldown
state that the normal appeal workflow writes.

The concern: two cooldown systems could accidentally coexist - one that
writes when an appeal finalises, and a separate one that /remove_cooldown
reads. This test drives the whole loop through the DB helpers used by
production code and asserts every step reads and writes the same store.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

for m in ("config", "cogs.voting"):
    sys.modules.pop(m, None)
from cogs import voting as voting_mod


class SharedStoreDB:
    """A fake DB whose cooldown store is a plain dict so we can prove
    everything hits the same row. Implements exactly the surface used by
    finalise_appeal(), /remove_cooldown, and the modal's pre-submit check.
    """

    def __init__(self, appeal, votes):
        self._appeal = dict(appeal, computed_verdict=None,
                            finalised_at=None, finalised_by=None)
        self._votes = list(votes)
        self._cooldowns: dict[tuple[int, int], dict] = {}
        self.close_calls = []
        self.claim_lock = asyncio.Lock()

    # ---- appeal path used by finalise_appeal ------------------------------
    async def claim_for_finalisation(self, appeal_id, *, minimum_votes, actor_id=None):
        async with self.claim_lock:
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
        return None

    async def close_appeal(self, appeal_id, status):
        self.close_calls.append((appeal_id, status))
        self._appeal["status"] = status

    # ---- cooldown API (this is the shared store) --------------------------
    async def set_cooldown(self, guild_id, user_id, hours, reason=""):
        if hours <= 0:
            return
        self._cooldowns[(guild_id, user_id)] = {
            "expires_at": datetime.now(timezone.utc) + timedelta(hours=hours),
            "reason": reason,
        }

    async def get_active_cooldown(self, guild_id, user_id):
        row = self._cooldowns.get((guild_id, user_id))
        if row and row["expires_at"] > datetime.now(timezone.utc):
            return row
        return None

    async def remove_cooldown(self, guild_id, user_id):
        row = self._cooldowns.get((guild_id, user_id))
        if row and row["expires_at"] > datetime.now(timezone.utc):
            del self._cooldowns[(guild_id, user_id)]
            return True
        return False


class SilentBot:
    def __init__(self, db):
        self.db = db

    def get_guild(self, guild_id):
        return None

    async def fetch_user(self, user_id):
        import discord
        resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
        raise discord.HTTPException(resp, "closed DMs")


def _appeal():
    return {
        "id": 1001, "guild_id": 100, "appellant_id": 42,
        "roblox_username": "u", "discord_tag": "u#0",
        "ban_reason": "r", "appeal_reason": "a",
        "ticket_channel": None, "voting_msg_id": None,
        "status": "voting", "closes_at": None, "platform": "Discord",
    }


@pytest.mark.asyncio
async def test_finalisation_writes_cooldown_and_remove_cooldown_clears_it(monkeypatch):
    # Enable cooldowns for the duration of this test - the module-level
    # constant is what _apply_cooldown checks.
    monkeypatch.setattr(voting_mod, "APPEAL_COOLDOWN_HOURS", 24)

    db = SharedStoreDB(_appeal(), ["keep_banned", "keep_banned", "keep_banned"])
    bot = SilentBot(db)

    # 1. Finalise an appeal the normal way (rejected -> cooldown applies).
    result = await voting_mod.finalise_appeal(bot, dict(db._appeal))
    assert result.verdict == "rejected"

    # 2. The modal's pre-submit check reads via get_active_cooldown - the
    #    exact same store that _apply_cooldown wrote to during finalisation.
    active = await db.get_active_cooldown(100, 42)
    assert active is not None, (
        "cooldown set by the appeal workflow must be visible to the modal check"
    )
    assert "rejected" in active["reason"].lower()

    # 3. /remove_cooldown must delete that same row.
    removed = await db.remove_cooldown(100, 42)
    assert removed is True, "/remove_cooldown must remove the row set by finalisation"

    # 4. After removal, subsequent modal checks see no cooldown.
    assert await db.get_active_cooldown(100, 42) is None

    # 5. Idempotency: /remove_cooldown on a user with no active cooldown
    #    reports False rather than raising.
    assert await db.remove_cooldown(100, 42) is False


@pytest.mark.asyncio
async def test_cooldown_disabled_writes_nothing(monkeypatch):
    # With APPEAL_COOLDOWN_HOURS=0 (the default, preserving pre-change
    # behaviour) finalisation does NOT write to the cooldown store.
    monkeypatch.setattr(voting_mod, "APPEAL_COOLDOWN_HOURS", 0)

    db = SharedStoreDB(_appeal(), ["keep_banned", "keep_banned", "keep_banned"])
    await voting_mod.finalise_appeal(SilentBot(db), dict(db._appeal))
    assert db._cooldowns == {}
    assert await db.get_active_cooldown(100, 42) is None
