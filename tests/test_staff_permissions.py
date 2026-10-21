"""Verify the staff-permission model matches the pre-existing bot: no user
who could not already invoke a Manage-Server-gated appeals command should
gain access through the new commands.
"""
import os
import sys
from types import SimpleNamespace

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

# Force re-import with a controlled env so STAFF_ROLE_IDS is empty by default.
for m in ("config", "permissions", "cogs.staff"):
    sys.modules.pop(m, None)


def _member(role_ids, *, manage_guild=False, admin=False):
    perms = SimpleNamespace(manage_guild=manage_guild, administrator=admin)
    roles = [SimpleNamespace(id=r) for r in role_ids]
    return SimpleNamespace(roles=roles, guild_permissions=perms)


def _interaction(member):
    return SimpleNamespace(user=member)


def test_manage_server_can_use_commands_matching_existing_bot():
    # Existing bot's /appealinfo and /forwardtobanteam already required
    # manage_guild - so a Manage Server user having access here is NOT a
    # privilege expansion.
    from cogs.staff import _is_appeals_staff
    inter = _interaction(_member(role_ids=[], manage_guild=True))
    assert _is_appeals_staff(inter, guild_config=None) is True


def test_admin_can_use_commands():
    from cogs.staff import _is_appeals_staff
    inter = _interaction(_member(role_ids=[], admin=True))
    assert _is_appeals_staff(inter, guild_config=None) is True


def test_random_role_cannot_use_commands():
    from cogs.staff import _is_appeals_staff
    inter = _interaction(_member(role_ids=[123456]))
    assert _is_appeals_staff(inter, guild_config=None) is False


def test_ban_team_role_can_use_commands_when_configured():
    from cogs.staff import _is_appeals_staff
    inter = _interaction(_member(role_ids=[9999]))
    cfg = {"ban_team_role": 9999}
    assert _is_appeals_staff(inter, guild_config=cfg) is True


def test_ban_team_role_configured_but_user_lacks_role():
    from cogs.staff import _is_appeals_staff
    inter = _interaction(_member(role_ids=[111]))
    cfg = {"ban_team_role": 9999}
    assert _is_appeals_staff(inter, guild_config=cfg) is False


def test_staff_role_ids_env_grants_access(monkeypatch):
    # Reload config with STAFF_ROLE_IDS set.
    monkeypatch.setenv("DISCORD_TOKEN", "x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("STAFF_ROLE_IDS", "555")
    for m in ("config", "permissions", "cogs.staff"):
        sys.modules.pop(m, None)
    from cogs.staff import _is_appeals_staff
    inter = _interaction(_member(role_ids=[555]))
    assert _is_appeals_staff(inter, guild_config=None) is True


def test_non_member_interactions_are_rejected():
    from cogs.staff import _is_appeals_staff
    # A non-Member (DM context) should never pass.
    inter = SimpleNamespace(user=object())
    assert _is_appeals_staff(inter, guild_config=None) is False
