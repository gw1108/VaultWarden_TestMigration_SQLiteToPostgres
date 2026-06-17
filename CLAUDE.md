# Vaultwarden SQLite → PostgreSQL Migration Test

## Project goal
A throwaway local test environment to:
1. **Run Vaultwarden locally backed by SQLite** (current state — done).
2. **Migrate the data from SQLite to PostgreSQL.**
3. **Verify the migration — automated only.** Prove all data transferred with no difference using
   per-table row-count parity across every table, plus a log-error scan and the `/alive` health
   check. No manual login or vault spot-check.

This is a test/learning setup, not production.

> ## ⚠️ HTTPS IS REQUIRED — even on `localhost`. Do NOT assume plain HTTP works.
>
> The web vault bundled in **`vaultwarden/server:1.36.0`** (and any comparably recent build)
> contains an **application-level guard in the client JavaScript** that **rejects every non-HTTPS
> API URL**, throwing:
>
> ```
> Error: Insecure URL not allowed. All URLs must use HTTPS.
> ```
>
> Confirmed against this image on 2026-06-16: registering/logging in at `http://localhost` fails
> with exactly this error (thrown from `api.service.ts` / `account-api.service.ts`).
>
> **This is NOT the old browser secure-context rule.** Two separate checks are easy to confuse:
> 1. **Browser Web Crypto / secure-context** — `http://localhost` *is* exempt from this one. This is
>    the only layer where "localhost is fine over HTTP" was ever true.
> 2. **The Bitwarden web-vault's own hardcoded URL guard** — added in recent web-vault builds, it
>    does **NOT** honor any localhost exception. This is what blocks you. See
>    bitwarden/clients #17409 ("Blocks Localhost HTTP Access") and vaultwarden discussions #6883/#6581.
>
> **What still works over plain HTTP:** only server-level/API endpoints like `GET /alive` (handy for
> health checks). **What does NOT work over plain HTTP:** the web vault UI, registration, login,
> browser extensions, and mobile apps — anything that drives the client app. To actually *use*
> Vaultwarden you must serve it over HTTPS (reverse proxy such as Caddy, optionally with `mkcert`
> for a locally-trusted cert; or Rocket's built-in `ROCKET_TLS` with an RSA cert).
>
> **Future agents: never tell the user `http://localhost` is usable for the web vault/extensions/apps.
> It is not. HTTPS is mandatory.**

## Environment
- **Host:** Windows 10, Docker Desktop, PowerShell.
- **Data directory (this folder):** `C:\GameDev\vaultwarden_testmigration` — bind-mounted into the
  container at `/data`. Contains the live SQLite DB and keys:
  - `db.sqlite3` (+ `-wal`, `-shm`) — the SQLite database.
  - `rsa_key.pem` — JWT signing key (auto-generated; keep it, or all sessions invalidate).
- **Image:** `vaultwarden/server:1.36.0` (pinned; `latest` resolved to this at setup time).
- **Container name:** `vaultwarden`.
- **Port:** host `80` → container `80`. The server answers on `http://localhost` (e.g. `/alive`),
  but the **web vault UI is unusable over plain HTTP** — see the HTTPS warning above. You must put
  it behind HTTPS to log in / register / use extensions.

## Current state (verified working)
- Container `vaultwarden` is running, healthy, and **published** on port 80.
- `GET http://localhost/alive` → 200. The web vault page *loads* but is **not usable over HTTP**
  (client app rejects HTTP API calls — see the ⚠️ HTTPS warning above).
- Vaultwarden listens on `0.0.0.0:80` inside the container.

### Run command currently in use
```powershell
docker run -d --name vaultwarden -v C:/GameDev/vaultwarden_testmigration:/data -p 80:80 vaultwarden/server:1.36.0
```

## Setup history / lessons learned
1. On Windows `docker run`, use **forward slashes** in the host path (`C:/GameDev/...:/data`) to
   avoid path-parsing issues with the `:` separator.

## Handy commands
```powershell
# Status / ports / logs
docker ps -a --filter "name=vaultwarden" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
docker port vaultwarden          # should show: 80/tcp -> 0.0.0.0:80
docker logs vaultwarden --tail 60

# HTTP health check
Invoke-WebRequest http://localhost/alive -UseBasicParsing   # expect HTTP 200

# Recreate (data preserved in this folder)
docker rm -f vaultwarden
docker run -d --name vaultwarden -v C:/GameDev/vaultwarden_testmigration:/data -p 80:80 vaultwarden/server:1.36.0
```

## Next: SQLite → PostgreSQL migration (planned)
Vaultwarden selects its backend via the `DATABASE_URL` env var. With no `DATABASE_URL` it defaults
to SQLite at `/data/db.sqlite3` (current state). For Postgres, point it at a Postgres instance:
`DATABASE_URL=postgresql://user:password@host:5432/vaultwarden`.

**Important:** Vaultwarden has **no built-in SQLite→Postgres data migration**. Switching
`DATABASE_URL` to a fresh Postgres DB gives you an *empty* vault — it does not copy existing data.
Migrating the actual data requires a one-time export/transform/import (e.g. dump SQLite, convert to
Postgres-compatible SQL, load it), since SQLite and Postgres dialects differ (types, sequences,
auto-increment, booleans, blobs). The exact approach is TBD and is the core of this project.

High-level plan to work through:
1. Stand up a PostgreSQL container (e.g. `postgres:16`) and an empty `vaultwarden` database, on a
   shared Docker network with the Vaultwarden container.
2. Choose/validate a migration method (data-copy tool such as `pgloader`, or a dump→transform→load
   pipeline). Confirm whether the running Vaultwarden version's schema matches between backends.
3. Stop Vaultwarden, migrate the data, then restart Vaultwarden with `DATABASE_URL` pointing at
   Postgres (still using this folder for `rsa_key.pem` / attachments / config).
4. **Verify (fully automated — no human check):** compare per-table row counts across **every**
   table between SQLite and Postgres (an empty diff is the pass gate); assert zero orphaned rows
   across **every** foreign key (SQLite's FK enforcement is historically off and pgloader's
   `disable triggers` can carry orphans through undetected — counts still match, logs stay clean);
   scan `docker logs` for database errors; confirm `GET /alive` is 200. Do **not** rely on logging
   in or spot-checking vault items by hand. See README §3 for the exact PowerShell.

## Security / HTTPS note
- **HTTPS is required even on `localhost`** for the web vault, browser extensions, and mobile apps
  — see the ⚠️ warning near the top. `http://localhost` is **not** a usable shortcut here.
- After creating your account, set `-e SIGNUPS_ALLOWED=false` to block further registrations
  (do this *after* registering).
- Any non-local exposure additionally needs a real domain + trusted TLS cert (a reverse proxy with
  Let's Encrypt/Caddy), and OCSP stapling for the mobile app.
