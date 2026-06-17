# Migrating the test SQLite DB → PostgreSQL with pgloader

This is the working recipe for migrating the live SQLite
data (`db.sqlite3`) into PostgreSQL and confirm Vaultwarden runs identically
against it. Everything runs through Docker Desktop / PowerShell, like the rest
of this project. Commands use PowerShell line continuations (`` ` ``).

> **Run every command from the repo root.** The `docker run` mounts use
> `${PWD}/data`, which PowerShell resolves to this project's `data/` folder — so
> there's no hardcoded absolute path to edit.

---

## 0. (Optional) Seed bulk test data

To give the migration real volume to copy and verify, `seed_testdata.py` inserts
synthetic, schema-valid rows into the SQLite DB (every non-system table, with edge
values). It writes to `data/db.sqlite3` by default and backs it up to
`data/db.sqlite3.bak` first. **Stop Vaultwarden before seeding** — SQLite allows
only one writer.

Roughly **3 MB** of seed data (measured against this `1.36.0` schema, on top of the
~280 KB starter DB):

```powershell
docker compose stop vaultwarden
python seed_testdata.py --users 60 --orgs 8
docker compose start vaultwarden
```

Size scales ~linearly at ~0.045 MB/user (orgs scaled alongside): `--users 50` ≈
2.6 MB, `--users 60 --orgs 8` ≈ 2.9 MB, `--users 100` ≈ 4.9 MB. Tune `--users` up
or down for a different target. Remove all seeded rows later with
`python seed_testdata.py --clear`.

---

## 1. Install pgloader

**pgloader has no official Windows build.** building on Windows is "supported in theory"
but the maintainers don't keep it working, and there are no Windows binaries. The native packages
(`apt-get install pgloader`, `brew`, `yum`) are Linux/macOS only.

On Windows the practical, supported option is the **official Docker image**:

```powershell
docker pull dimitri/pgloader:latest
docker run --rm dimitri/pgloader:latest pgloader --version   # smoke test
```

Alternatives (not used below):
- GitHub Container Registry build of `main`: `ghcr.io/dimitri/pgloader:latest`
- Inside WSL2 Ubuntu: `sudo apt-get install pgloader` gives you a native CLI.

---

## 2. The migration, end to end

Why the multi-step dance instead of "just point pgloader at an empty DB"?
Vaultwarden's tables are created by **Diesel migrations**, and its PostgreSQL
migrations differ from its SQLite ones (different column types, different
version rows in `__diesel_schema_migrations`). If pgloader creates the tables
itself it picks generic types that Diesel later rejects, and Vaultwarden won't
start. So we let **Vaultwarden build the correct Postgres schema first**, then
have pgloader copy **data only** into it. This is the reliable path.

### Step 0 — quiesce SQLite and fold in the WAL

Stop the running server so the SQLite file isn't being written during the copy,
then **checkpoint the WAL into `db.sqlite3` and drop WAL mode** so pgloader reads
the whole database from a single file. Do **not** rely on the `-wal` / `-shm`
sidecars for a consistent snapshot here: pgloader opens the DB from inside the
`dimitri/pgloader` container over the Docker Desktop **Windows bind mount**, where
SQLite's WAL/SHM locking is historically unreliable — uncheckpointed rows can be
missed silently (the row-count gate in §3 still "matches" because *both* engines
miss the same rows, so the bug hides). Collapsing everything into the main file before the
copy sidesteps that entirely. The row *data* is unchanged — the WAL only held
already-committed rows — and Vaultwarden re-enables WAL on its next SQLite boot.

```powershell
docker compose stop vaultwarden     # or, if you used `docker run`:  docker rm -f vaultwarden

# Optional but cheap: snapshot the source DB before touching it (distinct name so
# it won't clobber the seed step's db.sqlite3.bak).
Copy-Item data/db.sqlite3 data/db.sqlite3.premigration.bak -Force

# Fold committed WAL contents into db.sqlite3 and switch journal mode to DELETE,
# which also removes the -wal/-shm files so pgloader opens a single, plain DB.
# Uses the sqlite3 CLI in a throwaway alpine container (no host install needed).
docker run --rm -v ${PWD}/data:/data alpine sh -c `
  'apk add -q --no-cache sqlite >/dev/null; sqlite3 /data/db.sqlite3 \"PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;\"'
```

### Step 1 — network + an empty PostgreSQL

A throwaway Postgres with no named volume (so teardown wipes it cleanly):

```powershell
docker network create vw-migration

docker run -d --name vaultwarden-pg --network vw-migration `
  -e POSTGRES_USER=vaultwarden `
  -e POSTGRES_PASSWORD=vaultwarden `
  -e POSTGRES_DB=vaultwarden `
  postgres:16

# wait until it prints "database system is ready to accept connections"
docker logs vaultwarden-pg --tail 5
```

### Step 2 — let Vaultwarden create the Postgres schema

Run the same image once against Postgres. It runs its migrations, creates every
table, and writes the correct `__diesel_schema_migrations` rows — then we throw
this container away. (No `/data` mount needed; it's disposable.)

```powershell
docker run -d --name vw-schema --network vw-migration `
  -e DATABASE_URL=postgresql://vaultwarden:vaultwarden@vaultwarden-pg:5432/vaultwarden `
  vaultwarden/server:1.36.0

# wait for "Rocket has launched from http://0.0.0.0:80" (migrations ran above it)
docker logs vw-schema --tail 20
docker rm -f vw-schema
```

### Step 3 — run pgloader (the actual data copy)

The command file [`vaultwarden.load`](./data/vaultwarden.load) lives next to the
SQLite DB in `data/`. It loads `data only`, `disable triggers` (skips FK-ordering
pain — the `vaultwarden` role is a superuser, which this needs), and **excludes**
`__diesel_schema_migrations`. It deliberately does **not** `truncate`: pgloader
truncates each table individually and PostgreSQL refuses to `TRUNCATE` a
FK-referenced table (most of this schema), which `disable triggers` does not fix.
The schema Vaultwarden just built is empty, so the first load needs no truncate.

```powershell
docker run --rm --network vw-migration `
  -v ${PWD}/data:/data `
  dimitri/pgloader:latest `
  pgloader /data/vaultwarden.load
```

Read the summary table pgloader prints: every table should show
`read` == `imported` with **0 errors**.

**Re-run reset (idempotency).** pgloader *appends*, so a second load against a
non-empty DB doubles rows / hits duplicate keys. Before re-running pgloader,
truncate every table in one FK-safe statement (this keeps the Postgres
`__diesel_schema_migrations` rows intact):

```powershell
@'
DO $$
DECLARE tbls text;
BEGIN
  SELECT string_agg(format('public.%I', tablename), ', ') INTO tbls
  FROM pg_tables
  WHERE schemaname = 'public' AND tablename <> '__diesel_schema_migrations';
  EXECUTE 'TRUNCATE TABLE ' || tbls || ' RESTART IDENTITY CASCADE';
END $$;
'@ | docker exec -i vaultwarden-pg psql -U vaultwarden -d vaultwarden -v ON_ERROR_STOP=1
```

**Do not start the app yet.** Run the verifier first (`python verify_migration.py`)
— its data gates query the freshly loaded database directly and must be measured
before any Vaultwarden housekeeping runs (see §3). The verifier starts the app for
you once those gates pass.

---

## 3. Verify the migration (fully automated — no human spot-check)

Verification is one command — [`verify_migration.py`](./verify_migration.py), run
from the repo root after the pgloader copy (§2) and **before** you use the vault.
There is no logging in by hand and no eyeballing vault items.

```powershell
python verify_migration.py        # data gates → start app on Postgres → runtime checks
```

It runs five automated gates and **exits non-zero if any fails**, so it drops
straight into CI or a PowerShell `if ($LASTEXITCODE) { ... }` check. Stdlib only —
no `pip install`. It reads SQLite directly with Python's built-in `sqlite3`
(read-only, no CLI), reaches Postgres through `docker exec <pg> psql` (the
migration's Postgres publishes no host port), and uses `docker logs` + an HTTP GET
for the runtime checks.

**Pre-app data gates** — measured against the frozen, just-loaded database. They
must run *before* Vaultwarden starts, because its scheduled housekeeping
(incomplete-2FA purge, trashed-cipher purge, expired send/auth-request cleanup)
deletes rows the SQLite source still holds and would fake a mismatch in a running
DB:

- **Row-count parity** — counts every table in both engines (excluding
  `__diesel_schema_migrations`, whose rows legitimately differ between backends)
  and asserts they match table-for-table.
- **Referential integrity** — enumerates every foreign key from the Postgres
  catalog and runs the `LEFT JOIN … WHERE parent IS NULL` orphan scan against each
  child→parent pair (composite keys included), asserting zero orphans. This catches
  corruption that row counts and logs miss: SQLite ships with FK enforcement off,
  so the source can hold orphaned rows that pgloader's `disable triggers` waves
  straight through.
- **Primary-key identity** — compares the actual `uuid` *sets* of the major tables
  (`users`, `ciphers`, `folders`, `organizations`), proving the *same* rows moved,
  not merely the same count (`--no-identity` to skip).

If the data gates pass, the script **starts Vaultwarden on Postgres itself** and
waits for `/alive`, then runs the **runtime checks**:

- **Log scan** — greps the container log for database errors (`ERROR`, `FATAL`,
  `panic`, `relation … does not exist`, `error returned from database`).
- **Health endpoint** — asserts `GET http://localhost/alive` returns 200.

Run only part of the flow with `--stage data` (the pre-app gates; leaves the app
stopped) or `--stage runtime` (log + health against an already-running app).
Targets and the started container are configurable — `--pg-container`,
`--app-container`, `--sqlite`, `--image`, `--network`, `--database-url`, `--port`,
`--health-url`; see `python verify_migration.py --help`.

---

## 4. Teardown (throwaway test)

```powershell
docker rm -f vaultwarden-pg-app vaultwarden-pg vw-schema 2>$null
docker network rm vw-migration
# Postgres used no named volume, so its data is gone with the container.
# Your db.sqlite3 data is intact (Step 0 only checkpointed the WAL into it and
# dropped WAL mode; Vaultwarden re-enables WAL on boot). Restart SQLite mode with:
#   docker compose up -d
# To revert even that metadata change, restore data/db.sqlite3.premigration.bak.
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Insecure URL not allowed. All URLs must use HTTPS.` | Not a migration problem — the web-vault HTTPS guard. Use the Caddy stack. |
| pgloader: *unable to open database file* | The `-v ...:/data` mount is missing or the path isn't `/data/db.sqlite3`. |
| `relation "..." does not exist` during load | You skipped Step 2 — Postgres has no schema yet. Let Vaultwarden create it first. |
| Vaultwarden won't start, complains about pending/incompatible migrations | The `__diesel_schema_migrations` table got overwritten with SQLite versions. The provided `vaultwarden.load` **excludes** it; don't remove that clause. |
| FK violation during load | Ensure `disable triggers` is in the load file and the Postgres role is a superuser (it is, with this `POSTGRES_USER`). |
| Type-cast errors | Don't let pgloader create the tables. Use the schema-first flow above (`data only`). |

---

## Optional: exercise the real web vault over HTTPS (Caddy stack)

This is **not** a verification step — verification is fully automated in §3. It's
here only if you want to click around the real web vault against Postgres. Add a
Postgres service to `docker-compose.yml` and point Vaultwarden at it instead of
running the standalone container in Step 4:

```yaml
services:
  vaultwarden:
    image: vaultwarden/server:1.36.0
    container_name: vaultwarden
    depends_on: [postgres]
    environment:
      DATABASE_URL: postgresql://vaultwarden:vaultwarden@postgres:5432/vaultwarden
    volumes:
      - ./data:/data
    restart: unless-stopped

  postgres:
    image: postgres:16
    container_name: vaultwarden-pg
    environment:
      POSTGRES_USER: vaultwarden
      POSTGRES_PASSWORD: vaultwarden
      POSTGRES_DB: vaultwarden
    volumes:
      - pg_data:/var/lib/postgresql/data
    restart: unless-stopped

  # caddy service unchanged

volumes:
  caddy_data:
  caddy_config:
  pg_data:
```

Then browse `https://localhost`. (Run pgloader against the `postgres` service
the same way — it shares the compose network; use host `postgres` or
`vaultwarden-pg` accordingly.)
