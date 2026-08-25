import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg

logger = logging.getLogger("appeals-bot.db")


# Terminal statuses - an appeal in one of these has been fully resolved.
TERMINAL_STATUSES = frozenset({
    "accepted", "rejected", "closed", "actioned_unban", "actioned_close",
})


class Database:
    """Thin wrapper around an asyncpg connection pool.

    Handles startup retry for a still-booting PostgreSQL container and exposes
    atomic finalisation primitives so nothing can double-resolve an appeal.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 5):
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._pool: Optional[asyncpg.Pool] = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def initialise(self, *, connect_max_wait_seconds: int = 120):
        """Create the pool, retrying transient network errors.

        Distinguishes recoverable connection errors (server not up yet) from
        fatal authentication/configuration errors (bad password, wrong DB) so
        the container does not crash-loop on misconfiguration.
        """
        deadline = asyncio.get_event_loop().time() + connect_max_wait_seconds
        delay = 1.0
        attempt = 0
        while True:
            attempt += 1
            try:
                self._pool = await asyncpg.create_pool(
                    self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    command_timeout=30,
                )
                # Verify the pool actually works.
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
                break
            except (asyncpg.InvalidPasswordError,
                    asyncpg.InvalidAuthorizationSpecificationError,
                    asyncpg.InvalidCatalogNameError) as exc:
                # Non-recoverable: config is wrong. Do NOT retry forever.
                raise RuntimeError(
                    f"Fatal PostgreSQL configuration error: {type(exc).__name__}. "
                    "Check DATABASE_URL credentials and database name."
                ) from exc
            except (OSError, asyncpg.CannotConnectNowError,
                    asyncpg.PostgresConnectionError) as exc:
                if asyncio.get_event_loop().time() >= deadline:
                    raise RuntimeError(
                        f"PostgreSQL not reachable after {connect_max_wait_seconds}s: {exc}"
                    ) from exc
                logger.warning(
                    "PostgreSQL not ready (attempt %d): %s. Retrying in %.1fs",
                    attempt, exc, delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15.0)
        await self._create_tables()

    async def close(self):
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> bool:
        try:
            async with self._pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Schema                                                             #
    # ------------------------------------------------------------------ #

    async def _create_tables(self):
        # All statements are idempotent - safe against the live production DB.
        async with self._pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id         BIGINT PRIMARY KEY,
                    appeals_channel  BIGINT,
                    voting_channel   BIGINT,
                    results_channel  BIGINT,
                    ban_team_role    BIGINT,
                    dashboard_msg_id BIGINT
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS appeals (
                    id               SERIAL PRIMARY KEY,
                    guild_id         BIGINT NOT NULL,
                    appellant_id     BIGINT NOT NULL,
                    roblox_username  TEXT NOT NULL,
                    discord_tag      TEXT NOT NULL,
                    ban_reason       TEXT NOT NULL,
                    appeal_reason    TEXT NOT NULL,
                    ticket_channel   BIGINT,
                    voting_msg_id    BIGINT,
                    status           TEXT NOT NULL DEFAULT 'open',
                    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    closes_at        TIMESTAMPTZ
                )
            """)

            # Additive migrations - safe to run on the existing production DB.
            await conn.execute(
                "ALTER TABLE appeals ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'Unknown'"
            )
            await conn.execute(
                "ALTER TABLE appeals ADD COLUMN IF NOT EXISTS finalised_at TIMESTAMPTZ"
            )
            await conn.execute(
                "ALTER TABLE appeals ADD COLUMN IF NOT EXISTS finalised_by BIGINT"
            )

            # Partial unique index: at most one *active* appeal (open or voting)
            # per user per guild. Blocks the modal-side double-submit race and
            # cleanly permits historical rows in closed states.
            #
            # An older deployment had this restricted to status='open' only;
            # migrate by dropping it if present, then recreating with the wider
            # predicate. Both operations are idempotent.
            await conn.execute("DROP INDEX IF EXISTS appeals_one_open_per_user")
            await conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS appeals_one_active_per_user
                ON appeals (guild_id, appellant_id)
                WHERE status IN ('open', 'voting')
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS votes (
                    appeal_id  INT NOT NULL REFERENCES appeals(id) ON DELETE CASCADE,
                    voter_id   BIGINT NOT NULL,
                    vote       TEXT NOT NULL CHECK (vote IN ('unban', 'keep_banned')),
                    voted_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (appeal_id, voter_id)
                )
            """)

            # New: cooldowns. Additive; default deployment has cooldown hours=0
            # so no rows are ever inserted and behaviour is unchanged.
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS appeal_cooldowns (
                    guild_id    BIGINT NOT NULL,
                    user_id     BIGINT NOT NULL,
                    expires_at  TIMESTAMPTZ NOT NULL,
                    reason      TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)

            await conn.execute("""
                CREATE INDEX IF NOT EXISTS appeals_history_lookup
                ON appeals (guild_id, appellant_id, id DESC)
            """)

    # ------------------------------------------------------------------ #
    # Guild config                                                       #
    # ------------------------------------------------------------------ #

    async def get_guild_config(self, guild_id: int) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM guild_config WHERE guild_id = $1", guild_id
            )

    async def upsert_guild_config(self, guild_id: int, **fields):
        if not fields:
            return
        columns = ", ".join(fields.keys())
        placeholders = ", ".join(f"${i + 2}" for i in range(len(fields)))
        updates = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
        values = list(fields.values())

        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO guild_config (guild_id, {columns})
                VALUES ($1, {placeholders})
                ON CONFLICT (guild_id) DO UPDATE SET {updates}
                """,
                guild_id,
                *values,
            )

    # ------------------------------------------------------------------ #
    # Appeals                                                            #
    # ------------------------------------------------------------------ #

    async def get_active_appeal_for_user(self, guild_id: int, user_id: int) -> Optional[asyncpg.Record]:
        """Returns an appeal in 'open' or 'voting' status, if any."""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM appeals "
                "WHERE guild_id = $1 AND appellant_id = $2 "
                "  AND status IN ('open', 'voting')",
                guild_id,
                user_id,
            )

    # Kept for backwards compatibility with any external caller.
    async def get_open_appeal_for_user(self, guild_id: int, user_id: int):
        return await self.get_active_appeal_for_user(guild_id, user_id)

    async def create_appeal(
        self,
        guild_id: int,
        appellant_id: int,
        roblox_username: str,
        discord_tag: str,
        ban_reason: str,
        appeal_reason: str,
        platform: str,
    ) -> int:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO appeals
                    (guild_id, appellant_id, roblox_username, discord_tag,
                     ban_reason, appeal_reason, platform)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING id
                """,
                guild_id, appellant_id, roblox_username, discord_tag,
                ban_reason, appeal_reason, platform,
            )
            return row["id"]

    async def get_appeal(self, appeal_id: int) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM appeals WHERE id = $1", appeal_id
            )

    async def set_appeal_ticket_channel(self, appeal_id: int, channel_id: int):
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE appeals SET ticket_channel = $1 WHERE id = $2",
                channel_id, appeal_id,
            )

    async def claim_open_for_voting(self, appeal_id: int, voting_msg_id: int, closes_at) -> bool:
        """Atomically move an appeal from 'open' to 'voting'.

        Returns True if this call was the one that made the transition; False
        if another concurrent forward already claimed it.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE appeals "
                "SET voting_msg_id = $1, closes_at = $2, status = 'voting' "
                "WHERE id = $3 AND status = 'open' "
                "RETURNING id",
                voting_msg_id, closes_at, appeal_id,
            )
            return row is not None

    # Kept for backwards compatibility with any external caller.
    async def set_appeal_voting(self, appeal_id: int, voting_msg_id: int, closes_at):
        await self.claim_open_for_voting(appeal_id, voting_msg_id, closes_at)

    async def claim_for_finalisation(self, appeal_id: int, actor_id: Optional[int] = None) -> bool:
        """Atomically mark a 'voting' appeal as being finalised right now.

        Uses a conditional UPDATE so exactly one of many concurrent finalisers
        (the scheduled task, /force_end_voting) wins. The winning call proceeds
        to compute and post the verdict; losers get False and back off.

        The status is set to 'finalising' (a short-lived transitional state)
        and the caller is expected to call close_appeal() with the terminal
        status once the verdict computation is done.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE appeals "
                "SET status = 'finalising', finalised_at = NOW(), "
                "    finalised_by = COALESCE($2, finalised_by) "
                "WHERE id = $1 AND status = 'voting' "
                "RETURNING id",
                appeal_id, actor_id,
            )
            return row is not None

    async def close_appeal(self, appeal_id: int, status: str):
        """
        Valid statuses:
          'accepted'       - vote result: unban won
          'rejected'       - vote result: keep banned won
          'closed'         - inconclusive vote, or staff closed before voting
          'actioned_unban' - staff pressed Execute Unban
          'actioned_close' - staff pressed Close Ticket after verdict
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE appeals SET status = $1 WHERE id = $2",
                status, appeal_id,
            )

    async def get_appeals_due_for_verdict(self):
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM appeals WHERE status = 'voting' AND closes_at <= NOW()"
            )

    async def get_recent_appeals(self, guild_id: int, limit: int = 5):
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, status, roblox_username, appellant_id, created_at "
                "FROM appeals WHERE guild_id = $1 ORDER BY id DESC LIMIT $2",
                guild_id, limit,
            )

    async def get_user_appeals(self, guild_id: int, user_id: int, *, limit: int = 10):
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, status, roblox_username, ban_reason, appeal_reason, "
                "       platform, created_at "
                "FROM appeals "
                "WHERE guild_id = $1 AND appellant_id = $2 "
                "ORDER BY id DESC LIMIT $3",
                guild_id, user_id, limit,
            )

    async def get_user_appeal_stats(self, guild_id: int, user_id: int) -> dict:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT status, COUNT(*) AS n FROM appeals "
                "WHERE guild_id = $1 AND appellant_id = $2 GROUP BY status",
                guild_id, user_id,
            )
        stats = {"total": 0}
        for row in rows:
            stats[row["status"]] = row["n"]
            stats["total"] += row["n"]
        return stats

    # ------------------------------------------------------------------ #
    # Votes                                                              #
    # ------------------------------------------------------------------ #

    async def get_vote(self, appeal_id: int, voter_id: int) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM votes WHERE appeal_id = $1 AND voter_id = $2",
                appeal_id, voter_id,
            )

    async def upsert_vote(self, appeal_id: int, voter_id: int, vote: str):
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO votes (appeal_id, voter_id, vote)
                VALUES ($1, $2, $3)
                ON CONFLICT (appeal_id, voter_id) DO UPDATE SET vote = EXCLUDED.vote
                """,
                appeal_id, voter_id, vote,
            )

    async def get_vote_tally(self, appeal_id: int) -> dict:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT vote, COUNT(*) AS count FROM votes WHERE appeal_id = $1 GROUP BY vote",
                appeal_id,
            )
        tally = {"unban": 0, "keep_banned": 0}
        for row in rows:
            tally[row["vote"]] = row["count"]
        return tally

    # ------------------------------------------------------------------ #
    # Cooldowns                                                          #
    # ------------------------------------------------------------------ #

    async def get_active_cooldown(self, guild_id: int, user_id: int) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM appeal_cooldowns "
                "WHERE guild_id = $1 AND user_id = $2 AND expires_at > NOW()",
                guild_id, user_id,
            )

    async def set_cooldown(self, guild_id: int, user_id: int, hours: int, reason: str = ""):
        if hours <= 0:
            return
        expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO appeal_cooldowns (guild_id, user_id, expires_at, reason)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET expires_at = EXCLUDED.expires_at, reason = EXCLUDED.reason
                """,
                guild_id, user_id, expires_at, reason,
            )

    async def remove_cooldown(self, guild_id: int, user_id: int) -> bool:
        """Delete any active cooldown for the user. Returns True if one was
        actually removed (the user had an active cooldown), False if there was
        nothing to remove."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM appeal_cooldowns "
                "WHERE guild_id = $1 AND user_id = $2 AND expires_at > NOW() "
                "RETURNING user_id",
                guild_id, user_id,
            )
            return row is not None
