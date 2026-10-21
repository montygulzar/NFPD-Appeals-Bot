"""Client for the NFPD Mod Bot's internal unban endpoint.

Appellants are banned in the main NFPD server, which the Appeals bot doesn't
moderate. Execute Unban asks the Mod Bot (in the main server, already
logging cases) to perform the unban over the private nodey-monitoring Docker
network:

    POST {MODBOT_API_URL}/internal/unban
    Authorization: Bearer {INTERNAL_API_TOKEN}
    {"guild_id", "user_id", "moderator_id", "reason", "appeal_id"}

The call is idempotent on the Mod Bot side: unbanning someone who isn't
banned returns "not_banned" rather than an error, so a retry after a timeout
is safe.
"""
import logging
from dataclasses import dataclass

import aiohttp

import config

logger = logging.getLogger("appeals-bot.modbot")

# status values the Mod Bot returns
UNBANNED = "unbanned"
NOT_BANNED = "not_banned"
FORBIDDEN = "forbidden"
ERROR = "error"
NOT_CONFIGURED = "not_configured"
UNREACHABLE = "unreachable"


@dataclass
class UnbanResult:
    status: str
    detail: str = ""
    case_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.status in (UNBANNED, NOT_BANNED)


def is_configured() -> bool:
    return bool(config.MODBOT_API_URL and config.INTERNAL_API_TOKEN and config.MAIN_GUILD_ID)


async def request_unban(*, user_id: int, moderator_id: int, appeal_id: int, reason: str) -> UnbanResult:
    if not is_configured():
        return UnbanResult(
            NOT_CONFIGURED,
            "MODBOT_API_URL, INTERNAL_API_TOKEN and MAIN_GUILD_ID must all be set in .env.",
        )

    payload = {
        "guild_id": config.MAIN_GUILD_ID,
        "user_id": user_id,
        "moderator_id": moderator_id,
        "appeal_id": appeal_id,
        "reason": reason,
    }
    headers = {"Authorization": f"Bearer {config.INTERNAL_API_TOKEN}"}
    timeout = aiohttp.ClientTimeout(total=config.MODBOT_TIMEOUT_SECONDS)
    url = f"{config.MODBOT_API_URL}/internal/unban"

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = {}
                if resp.status == 401:
                    return UnbanResult(ERROR, "Mod Bot rejected the shared secret (INTERNAL_API_TOKEN mismatch).")
                if resp.status == 404 and not body:
                    return UnbanResult(ERROR, "Mod Bot has no /internal/unban endpoint - update the Mod Bot.")
                if resp.status == 503 and body.get("status") == "disabled":
                    return UnbanResult(ERROR, "Mod Bot's internal API is disabled (INTERNAL_API_TOKEN not set there).")
                status = body.get("status") or ERROR
                return UnbanResult(status, str(body.get("detail", "")), body.get("case_id"))
    except (aiohttp.ClientError, TimeoutError) as exc:
        # Never log the token; the URL is internal and safe to show.
        logger.warning("Mod Bot unban request to %s failed: %s", url, type(exc).__name__)
        return UnbanResult(UNREACHABLE, f"Could not reach the Mod Bot ({type(exc).__name__}).")


async def check_ready() -> tuple[bool, str]:
    """GET the Mod Bot's /ready. Read-only; used by /test_modbot_link."""
    if not config.MODBOT_API_URL:
        return False, "MODBOT_API_URL is not set."
    timeout = aiohttp.ClientTimeout(total=config.MODBOT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{config.MODBOT_API_URL}/ready") as resp:
                try:
                    body = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    body = {}
                if resp.status == 200:
                    return True, "ready"
                discord_detail = (body.get("discord") or {}).get("detail", "?")
                db_ok = (body.get("database") or {}).get("ok")
                return False, f"HTTP {resp.status} (discord: {discord_detail}, database ok: {db_ok})"
    except (aiohttp.ClientError, TimeoutError) as exc:
        return False, f"unreachable ({type(exc).__name__})"
