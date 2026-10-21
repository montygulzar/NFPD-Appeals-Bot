import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import asyncpg

logger = logging.getLogger("appeals-bot.db")

# Arbitrary constant key for pg_advisory_xact_lock around schema changes.
SCHEMA_LOCK_KEY = 0x4E465044  # "NFPD"


# Terminal statuses - an appeal in one of these has been fully resolved.
TERMINAL_STATUSES = frozenset({
    "accepted", "rejected", "closed", "actioned_unban", "actioned_close",
})

# Non-final statuses. An appeal in any of these is "still in progress" from
# the user's perspective and blocks them from opening another appeal.
# 'finalising' is a short-lived transitional state; it appears here so a
# crash during finalisation does not free the slot before recovery runs.
ACTIVE_STATUSES = frozenset({"open", "voting", "finalising"})


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
                    # Recycle idle connections so a PostgreSQL restart or a
                    # NAT/firewall idle drop doesn't leave dead sockets in the pool.
                    max_inactive_connection_lifetime=300,
                    server_settings={"application_name": "nfpd-appeals-bot"},
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

    async def close(self, *, timeout: float = 10.0):
        if self._pool is not None:
            pool, self._pool = self._pool, None
            try:
                await asyncio.wait_for(pool.close(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("Pool close timed out after %.0fs; terminating connections.", timeout)
                pool.terminate()

    async def ping(self) -> bool:
        if self._pool is None:
            return False
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
        """Apply the idempotent schema in ONE transaction under an advisory
        lock: a failure (e.g. the pre-migration conflict guard) rolls back
        every statement instead of leaving a half-migrated schema, and two
        containers starting at once cannot interleave DDL."""
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK_KEY)
                await self._apply_schema(conn)

    async def _apply_schema(self, conn):
        # All statements are idempotent - safe against the live production DB.
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
        # ticket_category: category ID where new appeal ticket channels
        # are created. Split from appeals_channel (which stays as the
        # public dashboard channel) so staff can control ticket placement
        # explicitly instead of relying on appeals_channel.category.
        await conn.execute(
            "ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS ticket_category BIGINT"
        )
        # transcripts_channel: where ticket transcripts are posted on
        # close. Nullable - NULL means "fall back to results_channel"
        # so existing servers keep working without an immediate
        # re-/setup after this deploy.
        await conn.execute(
            "ALTER TABLE guild_config ADD COLUMN IF NOT EXISTS transcripts_channel BIGINT"
        )

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
        # computed_verdict: the verdict decided at claim time, before any
        # Discord side effect runs. Persisting it means that if the bot
        # crashes mid-finalisation the recovery pass can drive the row to
        # the correct terminal status without re-running any side effects
        # or re-computing the tally.
        await conn.execute(
            "ALTER TABLE appeals ADD COLUMN IF NOT EXISTS computed_verdict TEXT"
        )

        # Pre-migration integrity check. We are about to widen the
        # active-appeal unique index to cover status IN
        # ('open','voting','finalising'). If any user already has more
        # than one row in that set (impossible under normal operation
        # but possible after an unclean earlier deployment), creating
        # the index would fail and leave the bot crash-looping.
        #
        # We refuse to start with a clear, actionable error rather than
        # partially migrating or silently changing production data.
        conflicts = await find_active_appeal_conflicts(conn)
        if conflicts:
            raise RuntimeError(_format_conflict_error(conflicts))

        # Partial unique index: at most one *active* appeal per user per
        # guild. Blocks the modal-side double-submit race and cleanly
        # permits historical rows in closed states.
        #
        # An older deployment had this restricted to status='open' only;
        # migrate by dropping it if present, then recreating with the
        # wider predicate. Both operations are idempotent.
        await conn.execute("DROP INDEX IF EXISTS appeals_one_open_per_user")
        await conn.execute("DROP INDEX IF EXISTS appeals_one_active_per_user")
        await conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS appeals_one_active_per_user_v2
            ON appeals (guild_id, appellant_id)
            WHERE status IN ('open', 'voting', 'finalising')
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
        """Returns an appeal in any non-terminal status (open/voting/finalising)."""
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM appeals "
                "WHERE guild_id = $1 AND appellant_id = $2 "
                "  AND status IN ('open', 'voting', 'finalising')",
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
    ) -> Optional[int]:
        """Insert a new appeal. Returns None if the user already has an
        active appeal (the partial unique index rejected it) - e.g. two
        submits landing at once."""
        async with self._pool.acquire() as conn:
            try:
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
            except asyncpg.UniqueViolationError:
                return None
            return row["id"]

    async def delete_unstarted_appeal(self, appeal_id: int) -> bool:
        """Remove an appeal whose ticket channel could never be created.

        Only touches a row that is still 'open' with no ticket channel - i.e.
        one that was inserted moments ago in the same request and never
        reached staff - so it can't block the user from retrying.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "DELETE FROM appeals "
                "WHERE id = $1 AND status = 'open' AND ticket_channel IS NULL "
                "RETURNING id",
                appeal_id,
            )
            return row is not None

    async def find_orphan_appeals(self, guild_id: int):
        """'open' appeals with no ticket channel older than 10 minutes.

        These predate the delete_unstarted_appeal fix (or survived a crash
        mid-submit) and block the user from appealing again. Reported by
        /debug; never modified automatically.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, appellant_id, created_at FROM appeals "
                "WHERE guild_id = $1 AND status = 'open' AND ticket_channel IS NULL "
                "  AND created_at < NOW() - INTERVAL '10 minutes' "
                "ORDER BY id",
                guild_id,
            )

    async def get_appeal(self, appeal_id: int) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM appeals WHERE id = $1", appeal_id
            )

    async def get_appeal_by_ticket_channel(self, channel_id: int) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM appeals WHERE ticket_channel = $1 ORDER BY id DESC LIMIT 1",
                channel_id,
            )

    async def get_appeal_by_voting_msg(self, message_id: int) -> Optional[asyncpg.Record]:
        async with self._pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM appeals WHERE voting_msg_id = $1 ORDER BY id DESC LIMIT 1",
                message_id,
            )

    async def transition_status(self, appeal_id: int, from_statuses, to_status: str) -> bool:
        """Conditional status change: only succeeds if the row is currently
        in one of ``from_statuses``. Exactly one of several concurrent
        callers (double-clicks, two staff members) wins."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE appeals SET status = $3 "
                "WHERE id = $1 AND status = ANY($2::text[]) "
                "RETURNING id",
                appeal_id, list(from_statuses), to_status,
            )
            return row is not None

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

    async def claim_for_finalisation(
        self,
        appeal_id: int,
        *,
        minimum_votes: int,
        actor_id: Optional[int] = None,
    ) -> Optional[dict]:
        """Atomically claim a 'voting' appeal and record its computed verdict.

        Runs entirely inside a serialisable transaction with SELECT FOR UPDATE
        so exactly one of many concurrent finalisers wins. Losers get None.

        The verdict is decided from the tally *before* any Discord side effect
        runs and is persisted alongside the 'finalising' status transition, so
        a crash between the claim and close_appeal() cannot lose or change the
        decision - the recovery pass has everything it needs to drive the row
        to its final status without re-computing anything and without
        re-running any non-idempotent side effect.

        Returns None if the claim did not win. Otherwise returns:
            {'verdict': 'accepted'|'rejected'|'closed',
             'tally':   {'unban': N, 'keep_banned': N},
             'total':   int}
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status FROM appeals WHERE id = $1 FOR UPDATE",
                    appeal_id,
                )
                if row is None or row["status"] != "voting":
                    return None

                vote_rows = await conn.fetch(
                    "SELECT vote, COUNT(*) AS c FROM votes "
                    "WHERE appeal_id = $1 GROUP BY vote",
                    appeal_id,
                )
                tally = {"unban": 0, "keep_banned": 0}
                for r in vote_rows:
                    tally[r["vote"]] = r["c"]
                total = tally["unban"] + tally["keep_banned"]

                if total < minimum_votes:
                    verdict = "closed"
                elif tally["unban"] > tally["keep_banned"]:
                    verdict = "accepted"
                else:
                    verdict = "rejected"

                await conn.execute(
                    "UPDATE appeals SET status = 'finalising', "
                    "                   computed_verdict = $1, "
                    "                   finalised_at = NOW(), "
                    "                   finalised_by = COALESCE($2, finalised_by) "
                    "WHERE id = $3",
                    verdict, actor_id, appeal_id,
                )
                return {"verdict": verdict, "tally": tally, "total": total}

    async def find_abandoned_finalisations(self, *, stale_after_seconds: int = 300):
        """Return appeals stuck in 'finalising' for longer than the threshold.

        Under normal operation the transition finalising -> terminal takes
        well under a second. Anything older than a few minutes is an abandoned
        finalisation (bot crashed, container restarted, network died) that
        must be recovered by the startup / periodic recovery pass rather than
        left permanently stranded.
        """
        async with self._pool.acquire() as conn:
            return await conn.fetch(
                "SELECT id, guild_id, appellant_id, computed_verdict, "
                "       finalised_at, finalised_by "
                "FROM appeals "
                "WHERE status = 'finalising' "
                "  AND finalised_at IS NOT NULL "
                "  AND finalised_at < NOW() - ($1 || ' seconds')::interval "
                "ORDER BY id",
                str(int(stale_after_seconds)),
            )

    async def recover_finalisation(self, appeal_id: int) -> Optional[str]:
        """Force an abandoned 'finalising' row to its recorded terminal status.

        Uses the verdict that was computed and stored atomically at claim
        time, so this involves no re-running of Discord side effects (no
        duplicate embeds, no duplicate DMs) and no re-computation of the
        tally. If computed_verdict is somehow NULL (should not happen with
        the new claim path), we fall back to 'closed' to avoid a permanent
        stuck row and log a warning at the caller.

        Returns the terminal status the row was moved to, or None if the row
        was no longer in 'finalising' when we tried (someone else fixed it).
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE appeals "
                "SET status = COALESCE(computed_verdict, 'closed') "
                "WHERE id = $1 AND status = 'finalising' "
                "RETURNING status, computed_verdict",
                appeal_id,
            )
            if row is None:
                return None
            return row["status"]

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
                ON CONFLICT (appeal_id, voter_id)
                DO UPDATE SET vote = EXCLUDED.vote, voted_at = NOW()
                """,
                appeal_id, voter_id, vote,
            )

    async def record_vote(self, appeal_id: int, voter_id: int, vote: str):
        """Record a vote only if the appeal is still open for voting.

        Takes FOR SHARE on the appeal row, which conflicts with the FOR UPDATE
        lock claim_for_finalisation holds while it tallies. So a vote either
        commits before the tally (and is counted) or waits, sees the status
        is no longer 'voting', and is refused - it can never be stored after
        the tally and silently ignored.

        Returns (accepted: bool, previous_vote: str | None, status: str | None).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                appeal = await conn.fetchrow(
                    "SELECT status, closes_at FROM appeals WHERE id = $1 FOR SHARE",
                    appeal_id,
                )
                if appeal is None:
                    return False, None, None
                if appeal["status"] != "voting" or (
                    appeal["closes_at"] is not None
                    and appeal["closes_at"] <= datetime.now(timezone.utc)
                ):
                    return False, None, appeal["status"]
                previous = await conn.fetchval(
                    "SELECT vote FROM votes WHERE appeal_id = $1 AND voter_id = $2",
                    appeal_id, voter_id,
                )
                await conn.execute(
                    """
                    INSERT INTO votes (appeal_id, voter_id, vote)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (appeal_id, voter_id)
                    DO UPDATE SET vote = EXCLUDED.vote, voted_at = NOW()
                    """,
                    appeal_id, voter_id, vote,
                )
                return True, previous, "voting"

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


# ---------------------------------------------------------------------------
# Module-level migration helpers (used by _create_tables and by tests)
# ---------------------------------------------------------------------------

async def find_active_appeal_conflicts(conn) -> list:
    """Return groups where the widened unique index would be violated.

    Result rows: (guild_id, appellant_id, ids[list of ints]).
    """
    rows = await conn.fetch(
        "SELECT guild_id, appellant_id, "
        "       array_agg(id ORDER BY id) AS ids, "
        "       array_agg(status ORDER BY id) AS statuses "
        "FROM appeals "
        "WHERE status IN ('open', 'voting', 'finalising') "
        "GROUP BY guild_id, appellant_id "
        "HAVING COUNT(*) > 1"
    )
    return [dict(r) for r in rows]


def _format_conflict_error(conflicts: list) -> str:
    lines = [
        "Refusing to create appeals_one_active_per_user_v2 unique index: the "
        "existing production data already violates it. This means one or more "
        "users currently have more than one non-terminal appeal on record, "
        "which should not happen under normal operation.",
        "",
        "Conflicting groups (guild_id, appellant_id, appeal ids, statuses):",
    ]
    for c in conflicts[:20]:
        lines.append(
            f"  guild={c['guild_id']} user={c['appellant_id']} "
            f"ids={c['ids']} statuses={c['statuses']}"
        )
    if len(conflicts) > 20:
        lines.append(f"  ... and {len(conflicts) - 20} more.")
    lines += [
        "",
        "The bot has NOT modified any appeals. Resolve the conflicts by "
        "manually closing the stale row(s) via psql, e.g.:",
        "  UPDATE appeals SET status='closed' WHERE id=<older-id>;",
        "then restart the container.",
    ]
    return "\n".join(lines)
