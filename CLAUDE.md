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
- **Data directory:** the repo's `data/` subfolder (`./data`, relative to the project root) —
  bind-mounted into the container at `/data`. Contains the live SQLite DB and keys:
  - `db.sqlite3` (+ `-wal`, `-shm`) — the SQLite database.
  - `rsa_key.pem` — JWT signing key (auto-generated; keep it, or all sessions invalidate).
- **Image:** `vaultwarden/server:1.36.0` (pinned; `latest` resolved to this at setup time).
- **Containers (Docker Compose):** `vaultwarden` (the app, SQLite-backed) and `caddy` (HTTPS
  reverse proxy). Defined in `docker-compose.yml`; Caddy config in `Caddyfile`.
- **Ports / access:** **Caddy** publishes host `80` (auto-redirects to HTTPS) and `443` (the HTTPS
  web vault). **Vaultwarden publishes no host port** — Caddy reaches it as `http://vaultwarden:80`
  on the internal compose network. Browse the vault at **`https://localhost`**. This satisfies the
  HTTPS requirement above; plain `http://localhost` only 308-redirects to HTTPS.

## Current state
- The stack runs via **`docker compose up -d`**: `vaultwarden` (SQLite at `/data/db.sqlite3`,
  internal-only) sits behind `caddy`, which terminates TLS using its built-in **local CA**.
- The web vault is served at **`https://localhost`** and is *usable* — TLS satisfies the web-vault
  HTTPS guard. `GET https://localhost/alive` → 200. Caddy's local CA + issued certs live in the
  `caddy_data` named volume; trust the root once (see `Caddyfile`) to silence the browser warning.
- Vaultwarden listens on `0.0.0.0:80` *inside* its container; only Caddy is reachable from the host.

### Run command currently in use
```powershell
# From the repo root (where docker-compose.yml lives). Brings up vaultwarden + caddy.
docker compose up -d        # then browse https://localhost
```

## Setup history / lessons learned
1. Everyday use is **Docker Compose** (`docker compose up -d`), which mounts `./data:/data` for you
   — no absolute path needed. For the standalone `docker run` commands in the migration recipe
   (README §2), prefer `${PWD}/data:/data` (run from the repo root) over a hardcoded path; Docker
   Desktop handles the drive-letter colon. If you do spell a path out, use **forward slashes**
   (`C:/.../data:/data`) to avoid path-parsing issues with the `:` separator.
2. HTTPS is served by Caddy with its **local CA** (see the ⚠️ warning) — the web-vault guard rejects
   plain HTTP even on localhost, so the everyday stack must stay behind Caddy.

## Handy commands
```powershell
# Status / logs (run from the repo root, where docker-compose.yml lives)
docker compose ps                       # both services + Caddy's 80/443 port mappings
docker compose logs -f                  # all services; or: docker compose logs vaultwarden --tail 60

# Health check over HTTPS (Caddy's local CA). Trust the CA once so the cert validates:
#   docker cp caddy:/data/caddy/pki/authorities/local/root.crt .
#   certutil -addstore -user Root root.crt        # then restart the shell/browser
Invoke-WebRequest https://localhost/alive -UseBasicParsing   # expect HTTP 200

# Stop / restart (data preserved in ./data and the caddy_* named volumes)
docker compose down
docker compose up -d
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
   Postgres (still using the `./data` folder for `rsa_key.pem` / attachments / config).
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
