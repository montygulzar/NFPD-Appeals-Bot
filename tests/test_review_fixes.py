"""Tests for the fixes from the FJ / Mod Bot comparison review.

All Discord and PostgreSQL objects are faked; the Mod Bot client is tested
against a real local aiohttp server.
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import discord
import pytest
import pytest_asyncio
from aiohttp import web

os.environ.setdefault("DISCORD_TOKEN", "test")
os.environ.setdefault("DATABASE_URL", "postgresql://test/test")

import config  # noqa: E402
import errors  # noqa: E402
import modbot_client  # noqa: E402
import permissions  # noqa: E402
from health import HealthServer  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self):
        self._done = False
        self.sent = []

    def is_done(self):
        return self._done

    async def defer(self, **kwargs):
        self._done = True

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.sent.append(content)


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content if content is not None else kwargs.get("embed"))


def member(user_id=1, role_ids=(), manage_guild=False, admin=False):
    return SimpleNamespace(
        id=user_id,
        roles=[SimpleNamespace(id=r) for r in role_ids],
        guild_permissions=SimpleNamespace(manage_guild=manage_guild, administrator=admin),
        mention=f"<@{user_id}>",
    )


def interaction(user, guild_id=100):
    return SimpleNamespace(
        user=user, guild_id=guild_id, guild=None, command=None,
        response=FakeResponse(), followup=FakeFollowup(),
    )


class ConfigDB:
    def __init__(self, config=None, raise_exc=None):
        self._config = config
        self._raise = raise_exc

    async def get_guild_config(self, guild_id):
        if self._raise:
            raise self._raise
        return self._config


# ---------------------------------------------------------------------------
# Embed avatar: read at call time, not captured at import
# ---------------------------------------------------------------------------

def test_bot_avatar_url_reflects_value_set_after_import():
    original = config.BOT_AVATAR_URL
    try:
        config.BOT_AVATAR_URL = "https://cdn.example/avatar.png"
        assert config.bot_avatar_url() == "https://cdn.example/avatar.png"
    finally:
        config.BOT_AVATAR_URL = original


def test_no_module_imports_avatar_by_value():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    for folder in ("cogs", "views"):
        for name in os.listdir(os.path.join(root, folder)):
            if name.endswith(".py"):
                text = open(os.path.join(root, folder, name), encoding="utf-8").read()
                if "BOT_AVATAR_URL" in text:
                    offenders.append(f"{folder}/{name}")
    assert offenders == []


# ---------------------------------------------------------------------------
# Staff-only buttons
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_staff_button_refuses_the_appellant_even_with_manage_server():
    bot = SimpleNamespace(db=ConfigDB({"ban_team_role": 9}))
    inter = interaction(member(user_id=42, manage_guild=True))
    assert await permissions.staff_button_check(bot, inter, appellant_id=42) is False
    assert inter.response.sent == [permissions.STAFF_ONLY_MESSAGE]


@pytest.mark.asyncio
async def test_staff_button_refuses_ordinary_member():
    bot = SimpleNamespace(db=ConfigDB({"ban_team_role": 9}))
    inter = interaction(member(user_id=5, role_ids=[123]))
    assert await permissions.staff_button_check(bot, inter, appellant_id=42) is False


@pytest.mark.asyncio
async def test_staff_button_allows_ban_team_role():
    bot = SimpleNamespace(db=ConfigDB({"ban_team_role": 9}))
    inter = interaction(member(user_id=5, role_ids=[9]))
    assert await permissions.staff_button_check(bot, inter, appellant_id=42) is True
    assert inter.response.sent == []


@pytest.mark.asyncio
async def test_staff_button_fails_closed_on_db_error_for_role_based_staff():
    # A ban-team member can't be recognised without the config, so a DB error
    # refuses them instead of letting anyone through.
    bot = SimpleNamespace(db=ConfigDB(raise_exc=OSError("db down")))
    inter = interaction(member(user_id=5, role_ids=[9]))
    assert await permissions.staff_button_check(bot, inter, appellant_id=42) is False


@pytest.mark.asyncio
async def test_staff_button_refusal_uses_followup_after_defer():
    bot = SimpleNamespace(db=ConfigDB({"ban_team_role": 9}))
    inter = interaction(member(user_id=5))
    await inter.response.defer()
    assert await permissions.staff_button_check(bot, inter) is False
    assert inter.followup.sent == [permissions.STAFF_ONLY_MESSAGE]


# ---------------------------------------------------------------------------
# Voting: fail closed, no self-votes, race-safe refusal
# ---------------------------------------------------------------------------

class VoteDB:
    def __init__(self, appeal, config=None, record=(True, None, "voting"), config_exc=None):
        self.appeal = appeal
        self.config = config
        self.record = record
        self.config_exc = config_exc
        self.record_calls = []

    async def get_guild_config(self, guild_id):
        if self.config_exc:
            raise self.config_exc
        return self.config

    async def get_appeal(self, appeal_id):
        return self.appeal

    async def get_appeal_by_voting_msg(self, message_id):
        return self.appeal

    async def record_vote(self, appeal_id, voter_id, vote):
        self.record_calls.append((appeal_id, voter_id, vote))
        return self.record

    async def get_vote_tally(self, appeal_id):
        return {"unban": 1, "keep_banned": 0}


def _vote_interaction(user):
    inter = interaction(user)
    inter.message = SimpleNamespace(id=777, embeds=[])
    inter.channel = SimpleNamespace(fetch_message=None)  # embed update is best-effort
    return inter


def _view(db):
    from views.voting_panel import VotingPanelView
    return VotingPanelView(SimpleNamespace(db=db))


@pytest.mark.asyncio
async def test_can_vote_fails_closed_when_config_unreadable():
    db = VoteDB(appeal=None, config_exc=OSError("db down"))
    view = _view(db)
    assert await view._can_vote(_vote_interaction(member(user_id=5))) is False


@pytest.mark.asyncio
async def test_can_vote_without_any_voter_role_only_allows_staff():
    db = VoteDB(appeal=None, config={"ban_team_role": None})
    view = _view(db)
    assert await view._can_vote(_vote_interaction(member(user_id=5))) is False
    assert await view._can_vote(_vote_interaction(member(user_id=6, manage_guild=True))) is True


@pytest.mark.asyncio
async def test_appellant_cannot_vote_on_own_appeal():
    appeal = {"id": 1, "appellant_id": 42, "status": "voting"}
    db = VoteDB(appeal, config={"ban_team_role": 9})
    view = _view(db)
    inter = _vote_interaction(member(user_id=42, role_ids=[9]))
    await view._handle_vote(inter, "unban")
    assert db.record_calls == []
    assert inter.followup.sent == ["You cannot vote on your own appeal."]


@pytest.mark.asyncio
async def test_vote_refused_when_db_says_appeal_no_longer_voting():
    # record_vote's FOR SHARE check found the appeal already finalising.
    appeal = {"id": 1, "appellant_id": 42, "status": "voting"}
    db = VoteDB(appeal, config={"ban_team_role": 9}, record=(False, None, "finalising"))
    view = _view(db)
    inter = _vote_interaction(member(user_id=5, role_ids=[9]))
    await view._handle_vote(inter, "keep_banned")
    assert inter.followup.sent == ["This appeal is finalising and is no longer accepting votes."]


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

def test_describe_error_hides_exception_message():
    err = RuntimeError("postgresql://user:SECRET@host/db exploded")
    text, should_log = errors.describe_error(err)
    assert "SECRET" not in text and "RuntimeError" in text
    assert should_log is True


def test_describe_error_database_unreachable():
    text, _ = errors.describe_error(ConnectionRefusedError())
    assert "database" in text


@pytest.mark.asyncio
async def test_report_error_replies_after_defer_instead_of_hanging(caplog):
    inter = interaction(member())
    await inter.response.defer()
    with caplog.at_level(logging.ERROR, logger="appeals-bot.errors"):
        await errors.report_interaction_error(inter, KeyError("x"), where="/test")
    assert len(inter.followup.sent) == 1
    assert any(r.exc_info for r in caplog.records)


# ---------------------------------------------------------------------------
# Health server
# ---------------------------------------------------------------------------

class HealthBot:
    def __init__(self, ready=True, closed=False, db_ok=True):
        self._ready, self._closed = ready, closed
        self.db = SimpleNamespace(ping=self._ping)
        self._db_ok = db_ok

    async def _ping(self):
        return self._db_ok

    def is_ready(self):
        return self._ready

    def is_closed(self):
        return self._closed


async def _get(handler):
    resp = await handler(None)
    import json
    return resp.status, json.loads(resp.body)


@pytest.mark.asyncio
async def test_ready_200_when_discord_and_db_up():
    hs = HealthServer(HealthBot(), port=0)
    status, body = await _get(hs._ready)
    assert status == 200 and body["status"] == "ready"


@pytest.mark.asyncio
async def test_ready_503_when_db_down_but_health_still_ok():
    hs = HealthServer(HealthBot(db_ok=False), port=0)
    status, body = await _get(hs._ready)
    assert status == 503 and body["database"]["ok"] is False
    status, _ = await _get(hs._health)
    assert status == 200


@pytest.mark.asyncio
async def test_ready_tolerates_brief_disconnect_then_fails():
    hs = HealthServer(HealthBot(), port=0)
    hs.note_disconnected()
    assert hs.discord_state()[0] is True          # within grace
    hs._disconnected_since -= 1000                # long outage
    assert hs.discord_state()[0] is False
    hs.note_connected()
    assert hs.discord_state() == (True, "connected")


# ---------------------------------------------------------------------------
# Mod Bot client (against a real local HTTP server)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def modbot_server(monkeypatch):
    seen = []

    async def unban(request):
        seen.append({"auth": request.headers.get("Authorization"), "body": await request.json()})
        if request.headers.get("Authorization") != "Bearer good-token":
            return web.json_response({"status": "unauthorized"}, status=401)
        return web.json_response({"status": "unbanned", "case_id": 17})

    app = web.Application()
    app.router.add_post("/internal/unban", unban)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    monkeypatch.setattr(config, "MODBOT_API_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(config, "INTERNAL_API_TOKEN", "good-token")
    monkeypatch.setattr(config, "MAIN_GUILD_ID", 555)
    yield seen
    await runner.cleanup()


@pytest.mark.asyncio
async def test_request_unban_success_sends_main_guild_and_token(modbot_server):
    result = await modbot_client.request_unban(user_id=42, moderator_id=7, appeal_id=3, reason="r")
    assert result.ok and result.status == "unbanned" and result.case_id == 17
    body = modbot_server[0]["body"]
    assert body["guild_id"] == 555 and body["user_id"] == 42 and body["moderator_id"] == 7


@pytest.mark.asyncio
async def test_request_unban_bad_token_is_reported(modbot_server, monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_API_TOKEN", "wrong")
    result = await modbot_client.request_unban(user_id=42, moderator_id=7, appeal_id=3, reason="r")
    assert not result.ok and "INTERNAL_API_TOKEN" in result.detail


@pytest.mark.asyncio
async def test_request_unban_not_configured(monkeypatch):
    monkeypatch.setattr(config, "MODBOT_API_URL", "")
    result = await modbot_client.request_unban(user_id=1, moderator_id=2, appeal_id=3, reason="r")
    assert result.status == modbot_client.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_request_unban_unreachable_does_not_leak_token(monkeypatch, caplog):
    monkeypatch.setattr(config, "MODBOT_API_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(config, "INTERNAL_API_TOKEN", "super-secret-token")
    monkeypatch.setattr(config, "MAIN_GUILD_ID", 555)
    with caplog.at_level(logging.DEBUG):
        result = await modbot_client.request_unban(user_id=1, moderator_id=2, appeal_id=3, reason="r")
    assert result.status == modbot_client.UNREACHABLE
    assert "super-secret-token" not in caplog.text and "super-secret-token" not in result.detail


# ---------------------------------------------------------------------------
# Execute Unban flow
# ---------------------------------------------------------------------------

class VerdictDB:
    def __init__(self, status="accepted"):
        self.appeal = {"id": 3, "appellant_id": 42, "roblox_username": "u",
                       "guild_id": 100, "ticket_channel": None, "status": status}
        self.transitions = []

    async def get_appeal(self, appeal_id):
        return dict(self.appeal)

    async def transition_status(self, appeal_id, from_statuses, to_status):
        if self.appeal["status"] in from_statuses:
            self.appeal["status"] = to_status
            self.transitions.append(to_status)
            return True
        return False


def _verdict_interaction():
    inter = interaction(member(user_id=7, manage_guild=True))

    async def _edit(**kwargs):
        return None
    inter.message = SimpleNamespace(embeds=[], edit=_edit)
    return inter


def _verdict_view(db):
    from views.verdict_panel import VerdictPanelView

    async def _fetch_user(uid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "no")
    return VerdictPanelView(SimpleNamespace(db=db, fetch_user=_fetch_user, get_guild=lambda g: None), appeal_id=3)


@pytest.mark.asyncio
async def test_execute_unban_failure_keeps_appeal_accepted(monkeypatch):
    async def fail(**kw):
        return modbot_client.UnbanResult(modbot_client.FORBIDDEN, "no perms")
    monkeypatch.setattr(modbot_client, "request_unban", fail)
    db = VerdictDB()
    inter = _verdict_interaction()
    await _verdict_view(db)._execute_unban(inter, 3)
    assert db.appeal["status"] == "accepted"
    assert "not** carried out" in inter.followup.sent[0]


@pytest.mark.asyncio
async def test_execute_unban_success_marks_actioned_once(monkeypatch):
    calls = []

    async def ok(**kw):
        calls.append(kw)
        return modbot_client.UnbanResult(modbot_client.UNBANNED, case_id=9)
    monkeypatch.setattr(modbot_client, "request_unban", ok)
    db = VerdictDB()
    view = _verdict_view(db)
    await view._execute_unban(_verdict_interaction(), 3)
    assert db.appeal["status"] == "actioned_unban"
    # Second press: already actioned -> no second Mod Bot call.
    inter2 = _verdict_interaction()
    await view._execute_unban(inter2, 3)
    assert len(calls) == 1
    assert inter2.followup.sent == ["This appeal has already been unbanned."]


@pytest.mark.asyncio
async def test_execute_unban_refused_for_rejected_verdict(monkeypatch):
    async def never(**kw):
        raise AssertionError("must not call the Mod Bot")
    monkeypatch.setattr(modbot_client, "request_unban", never)
    db = VerdictDB(status="rejected")
    inter = _verdict_interaction()
    await _verdict_view(db)._execute_unban(inter, 3)
    assert "Keep Banned" in inter.followup.sent[0]


# ---------------------------------------------------------------------------
# Cooldown only after denial / inconclusive
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_accepted_appeal_sets_no_cooldown(monkeypatch):
    sys.modules.pop("tests.test_cooldown_roundtrip", None)
    from tests.test_cooldown_roundtrip import SharedStoreDB, SilentBot, _appeal
    from cogs import voting as voting_mod
    monkeypatch.setattr(voting_mod, "APPEAL_COOLDOWN_HOURS", 24)
    db = SharedStoreDB(_appeal(), ["unban", "unban", "unban"])
    result = await voting_mod.finalise_appeal(SilentBot(db), dict(db._appeal))
    assert result.verdict == "accepted"
    assert db._cooldowns == {}


# ---------------------------------------------------------------------------
# Ticket Close Ticket: never overwrites a live vote or a verdict
# ---------------------------------------------------------------------------

class TicketDB:
    def __init__(self, status):
        self.appeal = {"id": 5, "appellant_id": 42, "status": status, "guild_id": 100}
        self.transitions = []

    async def get_appeal_by_ticket_channel(self, channel_id):
        return dict(self.appeal)

    async def transition_status(self, appeal_id, from_statuses, to_status):
        self.transitions.append((tuple(from_statuses), to_status))
        if self.appeal["status"] in from_statuses:
            self.appeal["status"] = to_status
            return True
        return False

    async def get_guild_config(self, guild_id):
        return None


def _ticket_interaction():
    inter = interaction(member(user_id=7, manage_guild=True))
    deleted = []

    async def _delete(reason=None):
        deleted.append(reason)
    inter.channel = SimpleNamespace(id=900, topic=None, delete=_delete)
    inter.guild = SimpleNamespace(get_channel=lambda cid: None)
    inter._deleted = deleted
    return inter


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["voting", "finalising"])
async def test_ticket_close_refused_while_vote_is_live(status):
    from views.appeal_actions import AppealActionsView
    db = TicketDB(status)
    view = AppealActionsView(SimpleNamespace(db=db))
    inter = _ticket_interaction()
    await view.close_ticket.callback(inter)
    assert db.appeal["status"] == status        # untouched
    assert db.transitions == []
    assert inter._deleted == []                 # channel kept
    assert "/force_end_voting" in inter.followup.sent[0]


@pytest.mark.asyncio
async def test_ticket_close_after_verdict_marks_actioned_close_not_closed(monkeypatch):
    import views.appeal_actions as aa
    real_sleep = asyncio.sleep
    monkeypatch.setattr(aa.asyncio, "sleep", lambda s: real_sleep(0))
    db = TicketDB("accepted")
    view = aa.AppealActionsView(SimpleNamespace(db=db))
    inter = _ticket_interaction()
    await view.close_ticket.callback(inter)
    assert db.appeal["status"] == "actioned_close"


# ---------------------------------------------------------------------------
# Modal: duplicate submit + orphan cleanup
# ---------------------------------------------------------------------------

class ModalDB:
    def __init__(self, create_result=11):
        self.create_result = create_result
        self.deleted = []
        self.ticket_set = []

    async def get_active_appeal_for_user(self, guild_id, user_id):
        return None

    async def get_active_cooldown(self, guild_id, user_id):
        return None

    async def get_guild_config(self, guild_id):
        return {"appeals_channel": 1, "ticket_category": 2, "ban_team_role": None}

    async def create_appeal(self, **kwargs):
        self.created = kwargs
        return self.create_result

    async def delete_unstarted_appeal(self, appeal_id):
        self.deleted.append(appeal_id)
        return True

    async def set_appeal_ticket_channel(self, appeal_id, channel_id):
        self.ticket_set.append((appeal_id, channel_id))


class _Category(discord.CategoryChannel):
    def __init__(self):
        pass


class _User:
    id = 42
    name = "appellant"
    mention = "<@42>"
    display_avatar = SimpleNamespace(url="https://x/a.png")

    def __str__(self):
        return "appellant"


def _modal_interaction(create_channel):
    inter = interaction(_User())
    category = _Category()
    inter.guild = SimpleNamespace(
        get_channel=lambda cid: category,
        get_role=lambda rid: None,
        default_role=object(), me=object(),
        create_text_channel=create_channel,
    )
    return inter


def _modal(db):
    from views.appeal_modal import AppealModal
    m = AppealModal(SimpleNamespace(db=db))
    for field, value in (("platform", "Discord"), ("roblox_username", "robloxer"),
                         ("ban_reason", "x"), ("appeal_reason", "y")):
        getattr(m, field)._value = value
    return m


@pytest.mark.asyncio
async def test_modal_duplicate_submit_gets_friendly_message():
    async def never(**kw):
        raise AssertionError("no channel should be created")
    db = ModalDB(create_result=None)
    inter = _modal_interaction(never)
    await _modal(db).on_submit(inter)
    assert "already have an open appeal" in inter.followup.sent[0]


@pytest.mark.asyncio
async def test_modal_removes_appeal_when_ticket_channel_cannot_be_created():
    async def forbidden(**kw):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
    db = ModalDB(create_result=11)
    inter = _modal_interaction(forbidden)
    await _modal(db).on_submit(inter)
    assert db.deleted == [11]
    assert db.ticket_set == []
    assert "could not be submitted" in inter.followup.sent[0]


@pytest.mark.asyncio
async def test_modal_uses_real_discord_identity_not_typed_text():
    async def forbidden(**kw):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "x")
    db = ModalDB()
    inter = _modal_interaction(forbidden)
    await _modal(db).on_submit(inter)
    from views.appeal_modal import AppealModal
    assert not hasattr(AppealModal, "discord_tag")
    assert db.created["discord_tag"] == str(inter.user)


# ---------------------------------------------------------------------------
# Unapproved guild: reported once per process, not on every reconnect
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unapproved_guild_reported_once_across_reconnects(monkeypatch):
    import bot as bot_mod
    handled = []

    async def fake_handle(b, guild):
        handled.append(guild.id)
    monkeypatch.setattr(bot_mod, "_handle_unapproved_guild", fake_handle)
    monkeypatch.setattr(bot_mod, "APPROVED_GUILD_IDS", {1})

    b = bot_mod.AppealsBot()
    guilds = [SimpleNamespace(id=1, name="ok"), SimpleNamespace(id=2, name="rogue")]
    fake_user = SimpleNamespace(id=99, display_avatar=SimpleNamespace(url="u"))
    monkeypatch.setattr(bot_mod.AppealsBot, "guilds", property(lambda self: guilds))
    monkeypatch.setattr(bot_mod.AppealsBot, "user", property(lambda self: fake_user))
    await b.on_ready()
    await b.on_ready()   # gateway reconnect
    assert handled == [2]
