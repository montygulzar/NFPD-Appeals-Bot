"""HTTP health endpoints, same contract as the NFPD Mod Bot / FJ bots.

    /health   liveness  - the process is up and its event loop answers.
    /ready    readiness - logged into Discord AND PostgreSQL answers.
                          503 with JSON details otherwise.

Not published to the host by docker-compose: reachable only inside the
Docker network, so it needs no authentication. A failing Docker HEALTHCHECK
marks the container unhealthy but does NOT restart it on its own -
`restart: unless-stopped` covers crashes.
"""
import logging
import time

from aiohttp import web

logger = logging.getLogger("appeals-bot.health")

# discord.py resumes dropped sessions within seconds; don't flap readiness
# during a normal resume.
DISCORD_GRACE_SECONDS = 90.0


class HealthServer:
    def __init__(self, bot, *, port: int) -> None:
        self._bot = bot
        self._port = port
        self._runner: web.AppRunner | None = None
        self._disconnected_since: float | None = None
        self._started = time.monotonic()

    def note_disconnected(self) -> None:
        if self._disconnected_since is None:
            self._disconnected_since = time.monotonic()

    def note_connected(self) -> None:
        self._disconnected_since = None

    def discord_state(self) -> tuple[bool, str]:
        if self._bot.is_closed():
            return False, "client closed"
        if not self._bot.is_ready():
            return False, "connecting"
        if self._disconnected_since is not None:
            outage = time.monotonic() - self._disconnected_since
            if outage > DISCORD_GRACE_SECONDS:
                return False, f"disconnected for {outage:.0f}s"
            return True, f"reconnecting ({outage:.0f}s)"
        return True, "connected"

    async def _health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "uptime_seconds": round(time.monotonic() - self._started)})

    async def _ready(self, request: web.Request) -> web.Response:
        discord_ok, discord_detail = self.discord_state()
        db = getattr(self._bot, "db", None)
        db_ok = bool(db is not None and await db.ping())
        ready = discord_ok and db_ok
        return web.json_response(
            {
                "status": "ready" if ready else "not_ready",
                "discord": {"ok": discord_ok, "detail": discord_detail},
                "database": {"ok": db_ok},
            },
            status=200 if ready else 503,
        )

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._health)
        app.router.add_get("/ready", self._ready)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, "0.0.0.0", self._port).start()
            logger.info("Health server listening on :%d (/health, /ready).", self._port)
        except OSError as exc:
            # A taken port must not stop the bot itself.
            logger.error("Health server could not bind :%d (%s); continuing without it.", self._port, exc)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
