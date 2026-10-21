"""DM error handling and pre-migration conflict formatter."""
import logging
import os
import sys

import discord
import pytest

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

sys.modules.pop("cogs.voting", None)
from cogs import voting as voting_mod
from database import _format_conflict_error, find_active_appeal_conflicts


class _RaisingUser:
    def __init__(self, exc):
        self._exc = exc

    async def send(self, *, embed):
        raise self._exc


class BotWithUser:
    """FakeBot that returns a user whose .send raises the given exception."""

    def __init__(self, exc):
        self._user = _RaisingUser(exc)

    async def fetch_user(self, user_id):
        return self._user


class BotFailingFetch:
    """FakeBot whose fetch_user itself raises."""

    def __init__(self, exc):
        self._exc = exc

    async def fetch_user(self, user_id):
        raise self._exc


def _appeal():
    return {
        "id": 7, "guild_id": 1, "appellant_id": 42,
        "roblox_username": "u", "discord_tag": "u#0",
        "ban_reason": "r", "appeal_reason": "a",
        "ticket_channel": None, "voting_msg_id": None,
        "status": "voting", "closes_at": None, "platform": "Discord",
    }


def _http_exception():
    # discord.HTTPException needs a fake Response.
    resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
    return discord.HTTPException(resp, "Cannot send messages to this user")


@pytest.mark.asyncio
async def test_dm_expected_failure_logs_at_debug(caplog):
    caplog.set_level(logging.DEBUG, logger="appeals-bot.voting")
    bot = BotWithUser(_http_exception())
    # Must not raise.
    await voting_mod._dm_appellant(bot, _appeal(), verdict="accepted")
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("Could not DM appellant" in r.getMessage() for r in debug_msgs)
    # Nothing at ERROR.
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


@pytest.mark.asyncio
async def test_dm_unexpected_failure_logs_at_error_with_traceback(caplog):
    caplog.set_level(logging.DEBUG, logger="appeals-bot.voting")
    bot = BotWithUser(TypeError("boom"))
    await voting_mod._dm_appellant(bot, _appeal(), verdict="accepted")
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errs, "unexpected DM failure should log at ERROR"
    r = errs[0]
    assert "Unexpected error DMing appellant" in r.getMessage()
    assert r.exc_info is not None, "logger.exception should attach a traceback"


@pytest.mark.asyncio
async def test_dm_fetch_expected_failure_logs_at_debug(caplog):
    caplog.set_level(logging.DEBUG, logger="appeals-bot.voting")
    bot = BotFailingFetch(_http_exception())
    await voting_mod._dm_appellant(bot, _appeal(), verdict="accepted")
    debug_msgs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("Could not fetch appellant" in r.getMessage() for r in debug_msgs)


@pytest.mark.asyncio
async def test_dm_fetch_unexpected_failure_logs_at_error(caplog):
    caplog.set_level(logging.DEBUG, logger="appeals-bot.voting")
    bot = BotFailingFetch(AttributeError("no .id"))
    await voting_mod._dm_appellant(bot, _appeal(), verdict="accepted")
    errs = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errs
    assert "Unexpected error fetching appellant" in errs[0].getMessage()


def test_conflict_error_formatter_lists_groups_and_actionable_fix():
    text = _format_conflict_error([
        {"guild_id": 1, "appellant_id": 10, "ids": [1, 5], "statuses": ["open", "voting"]},
        {"guild_id": 1, "appellant_id": 11, "ids": [7, 9], "statuses": ["voting", "finalising"]},
    ])
    assert "Refusing to create" in text
    assert "has NOT modified any appeals" in text
    assert "guild=1 user=10" in text
    assert "guild=1 user=11" in text
    assert "UPDATE appeals SET status='closed'" in text


class _FakeConn:
    """Minimal fetch() implementation, enough to drive find_active_appeal_conflicts."""

    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, *args):
        return self._rows


@pytest.mark.asyncio
async def test_find_active_appeal_conflicts_returns_rows_from_conn():
    conn = _FakeConn(rows=[
        {"guild_id": 1, "appellant_id": 42, "ids": [3, 4], "statuses": ["open", "voting"]},
    ])
    conflicts = await find_active_appeal_conflicts(conn)
    assert conflicts == [
        {"guild_id": 1, "appellant_id": 42, "ids": [3, 4], "statuses": ["open", "voting"]},
    ]


@pytest.mark.asyncio
async def test_find_active_appeal_conflicts_none():
    conn = _FakeConn(rows=[])
    assert await find_active_appeal_conflicts(conn) == []
