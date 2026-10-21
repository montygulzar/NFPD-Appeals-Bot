"""
Runtime configuration loaded entirely from environment variables.

All values come from the container's environment (populated from .env in the
self-hosted docker-compose setup). No code changes needed to change vote
thresholds, add developers, or approve servers.

  DISCORD_TOKEN            Bot token (required).
  DATABASE_URL             PostgreSQL DSN (required). In the self-hosted
                           deployment this points at the existing
                           appeals-postgres container on the nodey-monitoring
                           network, e.g.
                           postgresql://appeals:PASSWORD@appeals-postgres:5432/appeals

  APPROVED_GUILD_IDS       Comma-separated guild IDs the bot is allowed to
                           stay in. Empty = leaves every server it joins.

  LEAVE_UNAPPROVED_GUILDS  true/false. Default true.

  DEVELOPER_IDS            Comma-separated Discord user IDs to DM on
                           unapproved-guild joins. Default: 1285998518213017663.

  STAFF_ROLE_IDS           Comma-separated role IDs that count as Appeals
                           staff for /force_end_voting, /user_appeal_history,
                           and /remove_cooldown. Empty = fall back to the
                           per-guild ban_team_role configured via /setup, and
                           finally to Discord's manage_guild permission.

  MINIMUM_VOTES            Votes needed for a valid result (default 3).
  VOTING_HOURS             Voting window length in hours (default 48).
  BAN_TEAM_ROLE_IDS        Extra role IDs allowed to vote.

  APPEAL_COOLDOWN_HOURS    Hours a user must wait between appeals after their
                           previous appeal is finalised. Default 0 (disabled)
                           - preserves the existing production behaviour of
                           blocking only while an appeal is currently active.

  DB_CONNECT_MAX_WAIT      Seconds the bot will wait at startup for PostgreSQL
                           to become reachable before giving up. Default 120.
  DB_POOL_MIN              asyncpg pool min_size. Default 1.
  DB_POOL_MAX              asyncpg pool max_size. Default 5.

  LOG_LEVEL                DEBUG/INFO/WARNING/ERROR. Default INFO.
"""
import os
import sys


def _fail(message: str) -> None:
    print(f"Configuration error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _parse_id_list(name: str, *, default: str = "") -> set[int]:
    raw = os.environ.get(name, default).strip()
    if not raw:
        return set()
    try:
        return {int(piece.strip()) for piece in raw.split(",") if piece.strip()}
    except ValueError:
        _fail(f"{name} must be a comma-separated list of numeric Discord IDs, got: {raw!r}")


def _parse_bool(name: str, *, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _parse_int(name: str, *, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _fail(f"{name} must be an integer, got: {raw!r}")


# ── Required ─────────────────────────────────────────────────────────────────

DISCORD_TOKEN: str = os.environ.get("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    _fail("DISCORD_TOKEN is required. Set it in .env.")

DATABASE_URL: str = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    _fail(
        "DATABASE_URL is required. In the self-hosted deployment this should "
        "point at the appeals-postgres container over the nodey-monitoring "
        "network, e.g. postgresql://appeals:PASSWORD@appeals-postgres:5432/appeals"
    )

# ── Guild allowlist ───────────────────────────────────────────────────────────

APPROVED_GUILD_IDS: set[int] = _parse_id_list("APPROVED_GUILD_IDS")
LEAVE_UNAPPROVED_GUILDS: bool = _parse_bool("LEAVE_UNAPPROVED_GUILDS", default=True)

# ── Developer alerts ──────────────────────────────────────────────────────────

DEVELOPER_IDS: set[int] = _parse_id_list("DEVELOPER_IDS", default="1285998518213017663")

# ── Appeal settings ───────────────────────────────────────────────────────────

MINIMUM_VOTES: int = _parse_int("MINIMUM_VOTES", default=3)
VOTING_HOURS: int  = _parse_int("VOTING_HOURS", default=48)
BAN_TEAM_ROLE_IDS: set[int] = _parse_id_list("BAN_TEAM_ROLE_IDS")
STAFF_ROLE_IDS: set[int] = _parse_id_list("STAFF_ROLE_IDS")
APPEAL_COOLDOWN_HOURS: int = _parse_int("APPEAL_COOLDOWN_HOURS", default=0)

# ── Runtime / DB ──────────────────────────────────────────────────────────────

DB_CONNECT_MAX_WAIT: int = _parse_int("DB_CONNECT_MAX_WAIT", default=120)
DB_POOL_MIN: int = _parse_int("DB_POOL_MIN", default=1)
DB_POOL_MAX: int = _parse_int("DB_POOL_MAX", default=5)
LOG_LEVEL: str = (os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO").upper()

# HTTP health server (/health liveness, /ready readiness). Not published to
# the host by docker-compose; reachable only inside the Docker network.
HEALTH_PORT: int = _parse_int("HEALTH_PORT", default=8080)

# ── Mod Bot integration (Execute Unban) ───────────────────────────────────────
#
# Appellants are banned in the main NFPD server, not the appeals server, so
# Execute Unban asks the NFPD Mod Bot to perform the unban there (which also
# records a mod-log case). All three must be set for the button to work.
#
#   MODBOT_API_URL      e.g. http://nfpd-mod-bot:8080  (over nodey-monitoring)
#   INTERNAL_API_TOKEN  shared secret; must match the Mod Bot's value
#   MAIN_GUILD_ID       the main NFPD server ID where the bans live

MODBOT_API_URL: str = os.environ.get("MODBOT_API_URL", "").strip().rstrip("/")
INTERNAL_API_TOKEN: str = os.environ.get("INTERNAL_API_TOKEN", "").strip()
MAIN_GUILD_ID: int = _parse_int("MAIN_GUILD_ID", default=0)
MODBOT_TIMEOUT_SECONDS: int = _parse_int("MODBOT_TIMEOUT_SECONDS", default=15)

# ── Brand ─────────────────────────────────────────────────────────────────────

COLOUR_PRIMARY  = 0x0D1117
COLOUR_INFO     = 0x2563EB
COLOUR_VOTING   = 0xD97706
COLOUR_ACCEPTED = 0x16A34A
COLOUR_REJECTED = 0xDC2626
COLOUR_CLOSED   = 0x6B7280

UNAUTHORISED_GUILD_MESSAGE = (
    "This server is not an approved NFPD server. "
    "If you believe this is a mistake, please DM the developer.\n\n"
    "**Developer:** omb\n"
    "**Username:** ombdeveloping\n"
    "**Discord ID:** `1285998518213017663`"
)

# Bot avatar URL - set at startup in on_ready.
BOT_AVATAR_URL: str | None = None


def bot_avatar_url() -> str | None:
    """Read at embed-build time. Modules must call this rather than doing
    `from config import BOT_AVATAR_URL`, which copies the import-time None
    and never sees the value on_ready sets."""
    return BOT_AVATAR_URL
