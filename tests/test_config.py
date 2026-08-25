"""Config parsing tests. Runs without Discord or PostgreSQL."""
import importlib
import os
import sys

import pytest


def _reload_config(env: dict):
    for k in list(os.environ):
        if k in {"DISCORD_TOKEN", "DATABASE_URL", "APPROVED_GUILD_IDS",
                 "LEAVE_UNAPPROVED_GUILDS", "DEVELOPER_IDS", "MINIMUM_VOTES",
                 "VOTING_HOURS", "BAN_TEAM_ROLE_IDS", "STAFF_ROLE_IDS",
                 "APPEAL_COOLDOWN_HOURS", "DB_CONNECT_MAX_WAIT",
                 "DB_POOL_MIN", "DB_POOL_MAX", "LOG_LEVEL"}:
            del os.environ[k]
    os.environ.update(env)
    sys.modules.pop("config", None)
    return importlib.import_module("config")


def test_missing_token_fails():
    with pytest.raises(SystemExit):
        _reload_config({"DATABASE_URL": "postgresql://x/y"})


def test_missing_dsn_fails():
    with pytest.raises(SystemExit):
        _reload_config({"DISCORD_TOKEN": "x"})


def test_id_list_parse_and_defaults():
    cfg = _reload_config({
        "DISCORD_TOKEN": "x",
        "DATABASE_URL": "postgresql://x/y",
        "APPROVED_GUILD_IDS": "111, 222, 333",
        "STAFF_ROLE_IDS": "",
        "APPEAL_COOLDOWN_HOURS": "48",
    })
    assert cfg.APPROVED_GUILD_IDS == {111, 222, 333}
    assert cfg.STAFF_ROLE_IDS == set()
    assert cfg.APPEAL_COOLDOWN_HOURS == 48
    assert cfg.DEVELOPER_IDS == {1285998518213017663}
    assert cfg.MINIMUM_VOTES == 3
    assert cfg.VOTING_HOURS == 48


def test_bad_id_list_fails():
    with pytest.raises(SystemExit):
        _reload_config({
            "DISCORD_TOKEN": "x",
            "DATABASE_URL": "postgresql://x/y",
            "APPROVED_GUILD_IDS": "111,not_a_number",
        })


def test_bool_parse():
    cfg = _reload_config({
        "DISCORD_TOKEN": "x",
        "DATABASE_URL": "postgresql://x/y",
        "LEAVE_UNAPPROVED_GUILDS": "false",
    })
    assert cfg.LEAVE_UNAPPROVED_GUILDS is False
