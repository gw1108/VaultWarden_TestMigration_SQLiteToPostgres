#!/usr/bin/env python
"""
verify_migration.py -- fully automated SQLite -> PostgreSQL migration verifier.

Replaces the manual §3 checks in README.md with one command. Run it from the repo
root AFTER the pgloader copy (README §2) and BEFORE you use the vault. It decides
pass/fail with no logging in and no eyeballing vault items.

CHECKS (in the order the README requires them to run)
  Pre-app data gates -- measured against the frozen, just-loaded database. These
  MUST run before Vaultwarden starts: its scheduled housekeeping (incomplete-2FA
  purge, trashed-cipher purge, expired send/auth-request cleanup) deletes rows the
  frozen SQLite source still holds, which would fake a mismatch in a running DB.
    3a  row-count parity   -- count every table in both engines (excluding
                              __diesel_schema_migrations) and assert they match.
    3b  referential integrity -- the catalog-driven orphan scan; assert zero
                              orphaned rows across every foreign key.
    id  primary-key identity (optional, --no-identity to skip) -- compare the
                              actual uuid SETS of users/ciphers/folders/
                              organizations, proving the *same* rows moved, not
                              just the same count.
  Runtime checks -- need the app up. In the default `all` stage this script starts
  Vaultwarden on Postgres ITSELF (only after the data gates pass), waits for
  /alive, then runs:
    3c  log scan            -- grep the container log for DB errors.
    3d  health endpoint     -- GET /alive must return 200.

HOW IT REACHES EACH SIDE (no third-party packages)
  * SQLite  -- read directly with Python's stdlib sqlite3 (read-only; no CLI).
  * Postgres-- queried via `docker exec <pg> psql` (the migration's Postgres
               publishes no host port, so a host-side driver can't reach it).
  * Runtime -- `docker logs` for the scan and an HTTP GET for /alive.

USAGE (PowerShell, from the repo root)
  python verify_migration.py                 # data gates -> start app -> runtime
  python verify_migration.py --stage data    # only the pre-app gates (app stays down)
  python verify_migration.py --stage runtime # only log + health (app already up)
  python verify_migration.py --help          # all flags / defaults

Exit code is non-zero if any gate fails, so it drops straight into CI or a
PowerShell `if ($LASTEXITCODE) { ... }` check.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.error import URLError

# Table whose rows legitimately differ between backends (SQLite vs Postgres
# migration bookkeeping) -- excluded from every count/compare, like the README.
EXCLUDE = "__diesel_schema_migrations"

# Major tables whose primary key is a single text `uuid` column in both engines,
# so the identity check needs no dialect reformatting (README "deeper content check").
IDENTITY_TABLES = {"users": "uuid", "ciphers": "uuid", "folders": "uuid", "organizations": "uuid"}

# What counts as a database error in the Vaultwarden log (README §3c pattern).
# Case-insensitive to mirror PowerShell's Select-String default.
LOG_PATTERN = re.compile(
    r"ERROR|error returned from database|relation .* does not exist|panic|FATAL",
    re.IGNORECASE,
)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# Catalog-driven orphan scan (README §3b), verbatim. ON_ERROR_STOP=1 turns the
# final RAISE EXCEPTION into a non-zero psql exit, so this is a real gate.
ORPHAN_SQL = r"""
DO $$
DECLARE
  r            record;
  join_cond    text;
  notnull_cond text;
  parent_col   text;
  n            bigint;
  total        bigint := 0;
BEGIN
  FOR r IN
    SELECT con.conname,
           cl.relname AS child,
           cf.relname AS parent,
           con.conrelid, con.confrelid, con.conkey, con.confkey
    FROM pg_constraint con
    JOIN pg_class     cl ON cl.oid = con.conrelid
    JOIN pg_class     cf ON cf.oid = con.confrelid
    JOIN pg_namespace ns ON ns.oid = cl.relnamespace
    WHERE con.contype = 'f' AND ns.nspname = 'public'
  LOOP
    -- Build the join + not-null predicates across (possibly composite) FK columns.
    SELECT string_agg(format('c.%I = p.%I', ca.attname, pa.attname), ' AND '),
           string_agg(format('c.%I IS NOT NULL', ca.attname),        ' AND ')
      INTO join_cond, notnull_cond
    FROM unnest(r.conkey, r.confkey) WITH ORDINALITY AS k(c_attnum, p_attnum, ord)
    JOIN pg_attribute ca ON ca.attrelid = r.conrelid  AND ca.attnum = k.c_attnum
    JOIN pg_attribute pa ON pa.attrelid = r.confrelid AND pa.attnum = k.p_attnum;

    -- A referenced column is NOT NULL, so "p.<col> IS NULL" means no parent matched.
    SELECT attname INTO parent_col
    FROM pg_attribute WHERE attrelid = r.confrelid AND attnum = r.confkey[1];

    EXECUTE format(
      'SELECT count(*) FROM %I c LEFT JOIN %I p ON %s WHERE %s AND p.%I IS NULL',
      r.child, r.parent, join_cond, notnull_cond, parent_col
    ) INTO n;

    IF n > 0 THEN
      RAISE WARNING 'ORPHANS: % row(s) in public.% violate FK % -> public.%',
                    n, r.child, r.conname, r.parent;
      total := total + n;
    END IF;
  END LOOP;

  IF total > 0 THEN
    RAISE EXCEPTION 'FAIL: % orphaned row(s) across all foreign keys', total;
  END IF;
  RAISE NOTICE 'PASS: zero orphaned rows across all foreign keys';
END $$;
"""


# ----------------------------------------------------------------- shell -----

def run(cmd: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing text output as UTF-8. Never raises on a
    non-zero exit -- callers inspect .returncode / .stdout / .stderr."""
    return subprocess.run(
        cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def container_state(name: str) -> str:
    """'running', 'stopped', or 'absent' for a container by name."""
    cp = run(["docker", "inspect", "-f", "{{.State.Running}}", name])
    if cp.returncode != 0:
        return "absent"
    return "running" if cp.stdout.strip() == "true" else "stopped"


class PG:
    """Tiny wrapper for talking to the migration's Postgres via `docker exec psql`."""

    def __init__(self, container: str, user: str, db: str):
        self.container, self.user, self.db = container, user, db

    def _base(self, interactive: bool) -> list[str]:
        cmd = ["docker", "exec"]
        if interactive:
            cmd.append("-i")  # keep stdin open so a piped script reaches psql
        cmd += [self.container, "psql", "-U", self.user, "-d", self.db, "-v", "ON_ERROR_STOP=1"]
        return cmd

    def query(self, sql: str, *extra: str) -> subprocess.CompletedProcess:
        """One-shot `-c` query in unaligned tuples-only mode (-At)."""
        return run(self._base(False) + ["-At", *extra, "-c", sql])

    def script(self, sql: str) -> subprocess.CompletedProcess:
        """Feed a multi-statement script on stdin (no shell quoting involved)."""
        return run(self._base(True), stdin=sql)


# ---------------------------------------------------------------- sqlite -----

def connect_sqlite_ro(path: str) -> sqlite3.Connection:
    """Open the SQLite DB read-only so we never write a journal or -wal/-shm
    sidecar (the README's Step 0 leaves it in DELETE mode, so ro opens cleanly)."""
    try:
        return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(path)


def sqlite_counts(con: sqlite3.Connection) -> dict[str, int]:
    tables = [
        r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name <> ? ORDER BY name",
            (EXCLUDE,),
        )
    ]
    return {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables}


# ----------------------------------------------------------------- gates -----

def check_row_counts(con: sqlite3.Connection, pg: PG) -> tuple[str, str]:
    """3a -- per-table row-count parity across every table."""
    src = sqlite_counts(con)

    # query_to_xml runs count(*) per table in a single round-trip (README §3a).
    # NB: keep the `%I` fragment a plain string (no %-formatting); only the EXCLUDE
    # fragment is an f-string, so Python never tries to interpret `%I`.
    q = (
        "SELECT table_name, "
        "(xpath('/row/c/text()', query_to_xml(format('SELECT count(*) c FROM %I', table_name), "
        "false, true, '')))[1]::text::int "
        "FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' "
        f"AND table_name <> '{EXCLUDE}' "
        "ORDER BY table_name;"
    )
    cp = pg.query(q, "-F", "|")
    if cp.returncode != 0:
        return FAIL, f"could not read Postgres counts: {cp.stderr.strip() or 'psql error'}"
    dst: dict[str, int] = {}
    for line in cp.stdout.splitlines():
        if line.strip():
            name, _, cnt = line.partition("|")
            dst[name] = int(cnt)

    problems: list[str] = []
    for t in sorted(set(src) - set(dst)):
        problems.append(f"{t}: in SQLite ({src[t]}) but absent in Postgres")
    for t in sorted(set(dst) - set(src)):
        problems.append(f"{t}: in Postgres ({dst[t]}) but absent in SQLite")
    for t in sorted(set(src) & set(dst)):
        if src[t] != dst[t]:
            problems.append(f"{t}: sqlite={src[t]} pg={dst[t]} (diff {dst[t] - src[t]:+d})")

    if problems:
        return FAIL, f"{len(problems)} mismatch(es): " + "; ".join(problems)
    total = sum(src.values())
    return PASS, f"all {len(src)} tables match row-for-row ({total} rows total)"


def check_orphans(pg: PG) -> tuple[str, str]:
    """3b -- zero orphaned rows across every foreign key."""
    cp = pg.script(ORPHAN_SQL)
    notes = [ln.strip() for ln in cp.stderr.splitlines()
             if any(k in ln for k in ("ORPHANS", "PASS:", "FAIL:"))]
    if cp.returncode == 0:
        return PASS, "zero orphaned rows across all foreign keys"
    return FAIL, " | ".join(notes) or (cp.stderr.strip() or "orphan scan failed")


def check_identity(con: sqlite3.Connection, pg: PG) -> tuple[str, str]:
    """Optional -- compare the primary-key SETS of the major tables, proving the
    same rows transferred (not merely the same count). Set comparison sidesteps
    cross-engine collation differences in ORDER BY."""
    details: list[str] = []
    bad: list[str] = []
    for table, pk in IDENTITY_TABLES.items():
        src = {r[0] for r in con.execute(f'SELECT "{pk}" FROM "{table}"')}
        cp = pg.query(f'SELECT "{pk}" FROM "{table}"')
        if cp.returncode != 0:
            return FAIL, f"could not read Postgres {table}: {cp.stderr.strip() or 'psql error'}"
        dst = {ln for ln in cp.stdout.splitlines() if ln != ""}
        if src == dst:
            details.append(f"{table}={len(src)}")
        else:
            bad.append(f"{table}: sqlite_only={len(src - dst)} pg_only={len(dst - src)} "
                       f"(sqlite={len(src)} pg={len(dst)})")
    if bad:
        return FAIL, "; ".join(bad)
    return PASS, "identical PK sets [" + ", ".join(details) + "]"


def check_logs(app: str) -> tuple[str, str]:
    """3c -- no database errors in the app container log."""
    cp = run(["docker", "logs", app])
    if cp.returncode != 0:
        return FAIL, f"could not read logs for '{app}': {cp.stderr.strip()}"
    hits = [ln for ln in (cp.stdout + "\n" + cp.stderr).splitlines() if LOG_PATTERN.search(ln)]
    if hits:
        shown = "; ".join(ln.strip() for ln in hits[:3])
        more = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
        return FAIL, f"{len(hits)} DB-error line(s): {shown}{more}"
    return PASS, "no DB errors in log"


def check_health(url: str) -> tuple[str, str]:
    """3d -- the /alive health endpoint returns 200."""
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            code = resp.getcode()
    except (URLError, OSError) as e:
        return FAIL, f"{url} unreachable: {e}"
    return (PASS, f"{url} returned 200") if code == 200 else (FAIL, f"{url} returned {code}")


# ---------------------------------------------------------------- app run ----

def start_app(app: str, image: str, network: str, data_dir: str, database_url: str, port: int) -> None:
    """Start Vaultwarden on Postgres exactly as README §3 does (raises on failure)."""
    host_data = Path(data_dir).resolve().as_posix()  # forward slashes for Docker Desktop
    cp = run([
        "docker", "run", "-d", "--name", app, "--network", network,
        "-v", f"{host_data}:/data",
        "-e", f"DATABASE_URL={database_url}",
        "-p", f"{port}:80", image,
    ])
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or "docker run failed")


def wait_for_alive(url: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.getcode() == 200:
                    return True
        except (URLError, OSError):
            pass
        time.sleep(2)
    return False


# ------------------------------------------------------------------ main -----

def record(results: list, label: str, status: str, detail: str) -> None:
    """Append a result and echo it immediately so long runs show progress."""
    results.append((label, status, detail))
    print(f"  [{status:<4}] {label} -- {detail}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Automated SQLite -> PostgreSQL migration verifier (README section 3).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--stage", choices=["all", "data", "runtime"], default="all",
                   help="all: data gates -> start app -> runtime; data: pre-app gates only; "
                        "runtime: log+health against an already-running app")
    p.add_argument("--sqlite", default="data/db.sqlite3", help="path to the source SQLite file")
    p.add_argument("--pg-container", default="vaultwarden-pg", help="Postgres container name")
    p.add_argument("--pg-user", default="vaultwarden", help="Postgres user (psql -U)")
    p.add_argument("--pg-db", default="vaultwarden", help="Postgres database (psql -d)")
    p.add_argument("--app-container", default="vaultwarden-pg-app",
                   help="name for the Vaultwarden-on-Postgres container")
    p.add_argument("--image", default="vaultwarden/server:1.36.0", help="Vaultwarden image")
    p.add_argument("--network", default="vw-migration", help="Docker network the stack is on")
    p.add_argument("--data", default="data", help="host data dir to mount at /data when starting the app")
    p.add_argument("--database-url",
                   default="postgresql://vaultwarden:vaultwarden@vaultwarden-pg:5432/vaultwarden",
                   help="DATABASE_URL the started app uses")
    p.add_argument("--port", type=int, default=80, help="host port to publish for the app")
    p.add_argument("--health-url", default=None, help="override the /alive URL (default derives from --port)")
    p.add_argument("--no-identity", action="store_true", help="skip the primary-key identity gate")
    p.add_argument("--alive-timeout", type=int, default=90,
                   help="seconds to wait for /alive after starting the app")
    args = p.parse_args()

    url = args.health_url or (
        "http://localhost/alive" if args.port == 80 else f"http://localhost:{args.port}/alive"
    )
    pg = PG(args.pg_container, args.pg_user, args.pg_db)
    do_data = args.stage in ("all", "data")
    do_runtime = args.stage in ("all", "runtime")

    # ---- preflight ---------------------------------------------------------
    if run(["docker", "version", "--format", "{{.Server.Version}}"]).returncode != 0:
        print("ERROR: docker is not available on PATH / the daemon isn't running.", file=sys.stderr)
        return 2
    if container_state(args.pg_container) != "running":
        print(f"ERROR: Postgres container '{args.pg_container}' is not running. "
              f"Do README §1/§2 (stand up Postgres and run pgloader) first.", file=sys.stderr)
        return 2
    if do_data and not Path(args.sqlite).exists():
        print(f"ERROR: SQLite file '{args.sqlite}' not found (run from the repo root).", file=sys.stderr)
        return 2

    print(f"Verifying migration  (stage={args.stage}, pg={args.pg_container}, sqlite={args.sqlite})\n")
    results: list = []

    # ---- pre-app data gates ------------------------------------------------
    if do_data:
        state = container_state(args.app_container)
        if args.stage == "all" and state != "absent":
            print(f"ERROR: app container '{args.app_container}' already exists ({state}). The data "
                  f"gates must measure the frozen DB before Vaultwarden's housekeeping runs.\n"
                  f"       Remove it:  docker rm -f {args.app_container}\n"
                  f"       ...then re-run. (Or run only the runtime checks: --stage runtime.)",
                  file=sys.stderr)
            return 2
        if args.stage == "data" and state == "running":
            print(f"WARNING: app container '{args.app_container}' is running -- its housekeeping may "
                  f"already have changed row counts, so the data gates can report a false mismatch.\n")

        con = connect_sqlite_ro(args.sqlite)
        try:
            record(results, "3a row-count parity", *check_row_counts(con, pg))
            record(results, "3b referential integrity", *check_orphans(pg))
            if not args.no_identity:
                record(results, "id primary-key identity", *check_identity(con, pg))
        finally:
            con.close()

    data_failed = any(status == FAIL for _, status, _ in results)

    # ---- runtime checks (need the app up) ----------------------------------
    if do_runtime:
        if args.stage == "all":
            if data_failed:
                print("\nData gates FAILED -- not starting Vaultwarden. Fix the migration and re-run.")
                record(results, "3c log scan", SKIP, "data gates failed")
                record(results, "3d health endpoint", SKIP, "data gates failed")
            else:
                print(f"\nData gates passed -- starting Vaultwarden on Postgres ('{args.app_container}')...")
                try:
                    start_app(args.app_container, args.image, args.network,
                              args.data, args.database_url, args.port)
                except RuntimeError as e:
                    record(results, "app start", FAIL, str(e))
                    record(results, "3c log scan", SKIP, "app did not start")
                    record(results, "3d health endpoint", SKIP, "app did not start")
                else:
                    if not wait_for_alive(url, args.alive_timeout):
                        print(f"  (note: {url} not 200 within {args.alive_timeout}s -- scanning logs anyway)")
                    record(results, "3c log scan", *check_logs(args.app_container))
                    record(results, "3d health endpoint", *check_health(url))
        else:  # --stage runtime: app must already be up
            if container_state(args.app_container) != "running":
                print(f"ERROR: app container '{args.app_container}' is not running. Start it (README §3) "
                      f"or run the full flow with --stage all.", file=sys.stderr)
                return 2
            record(results, "3c log scan", *check_logs(args.app_container))
            record(results, "3d health endpoint", *check_health(url))

    # ---- summary -----------------------------------------------------------
    width = max(len(label) for label, _, _ in results)
    print("\n" + "=" * 64)
    print("VERIFICATION SUMMARY")
    print("=" * 64)
    for label, status, detail in results:
        print(f"  [{status:<4}] {label:<{width}}  {detail}")
    n_fail = sum(1 for _, s, _ in results if s == FAIL)
    if n_fail:
        print(f"\n{n_fail} check(s) FAILED -- investigate above.")
    else:
        print("\nAll checks passed -- the data transferred with no difference.")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
