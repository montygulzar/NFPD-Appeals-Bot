# NFPD Ban Appeals Bot

A Discord bot for managing ban appeals: private ticket channels, a ban-team
voting workflow, automatic verdicts, DM notifications, and a guild allowlist
with automatic developer alerts on unapproved joins.

Deployed self-hosted on Ubuntu with Docker Compose, connecting to an existing
PostgreSQL 16 container over a shared Docker network.

---

## Environment

- Ubuntu 26.04 LTS (x86_64)
- Docker + Docker Compose plugin
- PostgreSQL 16 in an existing `appeals-postgres` container
- Shared external Docker network: `nodey-monitoring`
- Bot container: `nfpd-appeals-bot`, Python 3.12

The `docker-compose.yml` defines only the bot. It does **not** create the
database; it connects to the existing `appeals-postgres` container through
`nodey-monitoring`.

---

## Configuration (.env)

Copy `.env.example` to `.env` and fill in the values. Every setting is loaded
from environment variables; nothing lives in code.

Key variables:

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `DATABASE_URL` | e.g. `postgresql://appeals:PASSWORD@appeals-postgres:5432/appeals` |
| `APPROVED_GUILD_IDS` | Comma-separated guild IDs the bot may stay in |
| `LEAVE_UNAPPROVED_GUILDS` | `true` (default) / `false` |
| `DEVELOPER_IDS` | DM'd on unapproved joins |
| `STAFF_ROLE_IDS` | Role IDs treated as Appeals staff for the new commands |
| `BAN_TEAM_ROLE_IDS` | Extra role IDs allowed to vote |
| `MINIMUM_VOTES` | Minimum votes for a valid result (default 3) |
| `VOTING_HOURS` | Voting window length (default 48) |
| `APPEAL_COOLDOWN_HOURS` | Post-verdict cooldown per user (default 0 = off) |
| `DB_CONNECT_MAX_WAIT` | Startup wait for PostgreSQL (default 120s) |
| `DB_POOL_MIN` / `DB_POOL_MAX` | asyncpg pool sizing |
| `LOG_LEVEL` | INFO / DEBUG / WARNING / ERROR |
| `HEALTH_PORT` | Internal health server port (default 8080, not published) |
| `MODBOT_API_URL` | Mod Bot internal API, e.g. `http://nfpd-mod-bot:8080` |
| `INTERNAL_API_TOKEN` | Shared secret; same value in the Mod Bot's `.env` |
| `MAIN_GUILD_ID` | Main NFPD server where bans live (Execute Unban target) |
| `MODBOT_TIMEOUT_SECONDS` | Timeout for the Mod Bot unban call (default 15) |

---

## Commands

| Command | Permission | Description |
|---|---|---|
| `/setup` | Administrator | Set channels and ban-team role |
| `/createdashboard` | Administrator | Post the public appeal panel |
| `/forwardtobanteam` | Manage Guild | Manually forward an appeal to voting |
| `/appealinfo <id>` | Manage Guild | View a case file and live vote tally |
| `/force_end_voting <id>` | Appeals staff | End a live vote early using the same finalisation logic as the scheduled sweep |
| `/user_appeal_history <user>` | Appeals staff | Show a user's appeal history and recent cases |
| `/remove_cooldown <user>` | Appeals staff | End a user's appeal cooldown early |
| `/debug` | Administrator | Full health check |

Appeals staff = role in `STAFF_ROLE_IDS`, or the ban-team role configured via
`/setup`, or Discord's `Manage Server`.

---

## Deployment

First run:

```bash
cp .env.example .env
$EDITOR .env
docker compose build
docker compose up -d
docker compose logs -f nfpd-appeals-bot
```

Updates:

```bash
git pull
docker compose build
docker compose up -d
```

Rollback:

```bash
git checkout <previous-commit>
docker compose build
docker compose up -d
```

The database schema uses idempotent `ADD COLUMN IF NOT EXISTS` /
`CREATE TABLE IF NOT EXISTS` migrations, so rolling back the image with the
schema in place is safe.

---

## Execute Unban and the Mod Bot

Appellants are banned in the **main NFPD server**, not the appeals server, so
the Appeals bot doesn't unban anyone itself. Pressing **Execute Unban** sends
an authenticated request to the NFPD Mod Bot over the private
`nodey-monitoring` network (`POST /internal/unban`). The Mod Bot lifts the ban
in `MAIN_GUILD_ID` and records an `unban` case in its mod log.

- If the Mod Bot is unreachable or refuses, the appeal **stays Accepted** and
  staff are told why, so they can press the button again once it's fixed.
- If the user wasn't banned any more, the appeal is marked actioned anyway.
- Only Appeals staff can press any ticket or verdict button. The appellant
  can see their ticket, but can't forward, close or action it.

## Health

`/health` (liveness) and `/ready` (Discord logged in + PostgreSQL answering)
are served on `HEALTH_PORT` inside the container. The Docker HEALTHCHECK probes
`/ready`. An unhealthy container is flagged in `docker ps`; Docker does not
restart it for that alone.

## Appeal flow

1. Member clicks **Submit a Ban Appeal** on the dashboard panel
2. Modal captures the appeal
3. A private ticket channel is created; the member and Ban Team can see it
4. Staff click **Forward to Ban Team** (or run `/forwardtobanteam`)
5. Appeal posts in the voting channel; live tally updates
6. After `VOTING_HOURS` the scheduled task finalises, **or** an Appeals staff
   member runs `/force_end_voting <id>` to close early. Both paths go through
   the same `finalise_appeal()` function, so the outcome is identical.
7. Staff action the verdict; the ticket channel is transcribed and deleted.

---

## `/setup` — channels and roles

Run once per server (Administrator):

- `dashboard_channel` — text channel where `/createdashboard` posts the public "Submit a Ban Appeal" panel.
- `ticket_category` — Discord category where new private ticket channels are created. **Required.**
- `voting_channel` — where the ban team votes.
- `results_channel` — where the verdict embed lands. Also the fallback destination for transcripts.
- `ban_team_role` — role pinged on new appeals and used as the default staff role.
- `transcripts_channel` — optional. If set, ticket transcripts on close land here instead of `results_channel`.

Existing servers upgrading from an earlier version **must re-run `/setup`** to specify `ticket_category`. Until they do, appeal submissions will ephemerally refuse with an actionable message rather than creating tickets in the wrong place.
