#!/usr/bin/env python
"""
Seed the Vaultwarden test SQLite DB with a lot of schema-appropriate rows so the
SQLite -> PostgreSQL migration (see README.md) has real volume to copy and verify.

WHAT THIS DOES
  Inserts synthetic users and all their related data -- folders, ciphers (login /
  note / card / identity), organizations, collections, group/collection grants,
  org memberships, devices, sends, attachments, favorites and org policies, plus
  archives, auth_requests, emergency_access, event, invitations,
  organization_api_key, sso_auth/sso_users and the twofactor* tables -- so EVERY
  non-system table gets rows. It also injects edge values (soft-deleted ciphers,
  Argon2 KDF users, disabled/invited accounts, org-owned & password-protected
  sends, access_all groups, NULL-key orgs, custom fields / password history) to
  exercise the type, blob, boolean, timestamp and JSON conversions the migration
  has to carry across.

IMPORTANT -- these rows are NOT decryptable
  The "encrypted" fields (cipher names, passwords, akeys, RSA keys, ...) are
  correctly *shaped* random data (`2.<iv>|<ct>|<mac>` base64), not real Bitwarden
  ciphertext. They are perfect for a migration/volume test (correct types, valid
  FKs, satisfied NOT NULL/UNIQUE constraints) but will show up as undecryptable
  if you open them in the web vault. That is expected and harmless for this test.

SAFETY
  * Every synthetic user has an email like `seed.<batch>.userNNNN@seed.local` and
    every synthetic org a billing_email like `seed.<batch>.org@seed.local`, so the
    data is trivially identifiable and removable with `--clear`.
  * Your real account is never touched.
  * The DB file is backed up to `<db>.bak` first (online backup, WAL-safe) unless
    you pass `--no-backup`.
  * STOP Vaultwarden before running -- SQLite allows only one writer, so seeding a
    DB the running server holds open can fail with "database is locked".

USAGE (PowerShell)
  docker compose stop vaultwarden
  python seed_testdata.py                 # seed with defaults
  python seed_testdata.py --users 200     # more volume
  python seed_testdata.py --clear         # remove ALL seeded data (any batch)
  docker compose start vaultwarden
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

# ---------------------------------------------------------------- helpers -----

# All generated timestamps fall inside this window (Jan 1 2026 .. ~mid Jun 2026).
_BASE = datetime(2026, 1, 1)
_SPAN_SECONDS = 165 * 24 * 3600


def b64(nbytes: int) -> str:
    """Base64 of `nbytes` random bytes -- a stand-in for opaque base64 fields."""
    return base64.b64encode(os.urandom(nbytes)).decode("ascii")


def enc(ct_bytes: int = 32) -> str:
    """A correctly-shaped (but undecryptable) Bitwarden `2.<iv>|<ct>|<mac>` cipher
    string: 16-byte IV, variable ciphertext, 32-byte MAC, all base64."""
    return f"2.{b64(16)}|{b64(ct_bytes)}|{b64(32)}"


def rand_times() -> tuple[str, str]:
    """A (created_at, updated_at) pair, updated >= created, in Vaultwarden's
    'YYYY-MM-DD HH:MM:SS.ffffff' text format."""
    created = _BASE + timedelta(
        seconds=random.randint(0, _SPAN_SECONDS), microseconds=random.randint(0, 999999)
    )
    updated = created + timedelta(seconds=random.randint(0, 5 * 24 * 3600))
    fmt = "%Y-%m-%d %H:%M:%S.%f"
    return created.strftime(fmt), updated.strftime(fmt)


def uid() -> str:
    """A fresh random UUID (uuid4) as Vaultwarden stores them."""
    # Build a v4 UUID string from os.urandom so we never depend on the uuid module's
    # platform RNG behaviour; format is the canonical 8-4-4-4-12.
    h = os.urandom(16).hex()
    return f"{h[0:8]}-{h[8:12]}-4{h[13:16]}-{h[16:20]}-{h[20:32]}"


def cipher_data(atype: int) -> str:
    """A small, valid, type-appropriate JSON document for ciphers.data."""
    if atype == 1:  # login
        doc = {
            "autofillOnPageLoad": None,
            "fido2Credentials": [],
            "password": enc(24),
            "passwordRevisionDate": None,
            "totp": None,
            "uris": [{"match": None, "uri": enc(20)}],
            "username": enc(16),
        }
    elif atype == 2:  # secure note
        doc = {"type": 0}
    elif atype == 3:  # card
        doc = {
            "brand": enc(8),
            "cardholderName": enc(16),
            "code": enc(8),
            "expMonth": enc(4),
            "expYear": enc(4),
            "number": enc(16),
        }
    else:  # identity (4)
        doc = {k: enc(12) for k in ("firstName", "lastName", "email", "phone", "address1", "city")}
    return json.dumps(doc, separators=(",", ":"))


def fields_json() -> str:
    """A small custom-fields JSON array for ciphers.fields (edge: non-null fields)."""
    return json.dumps(
        [{"type": random.choice([0, 1, 2]), "name": enc(8), "value": enc(12)}
         for _ in range(random.randint(1, 3))],
        separators=(",", ":"))


def pwhistory_json() -> str:
    """A password_history JSON array for login ciphers (edge: non-null history)."""
    return json.dumps(
        [{"Password": enc(20), "LastUsedDate": rand_times()[0]} for _ in range(random.randint(1, 3))],
        separators=(",", ":"))


def future_ts(max_days: int = 30) -> str:
    """A timestamp 1..max_days in the future, in Vaultwarden's text datetime format."""
    dt = datetime.now() + timedelta(days=random.randint(1, max_days), seconds=random.randint(0, 86399))
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f")


# --------------------------------------------------------------- clearing -----

# Subqueries that select every synthetic row by its marker email.
_SU = "(SELECT uuid FROM users WHERE email LIKE 'seed.%@seed.local')"
_SO = "(SELECT uuid FROM organizations WHERE billing_email LIKE 'seed.%@seed.local')"
_SC = f"(SELECT uuid FROM collections WHERE org_uuid IN {_SO})"
_SF = f"(SELECT uuid FROM folders WHERE user_uuid IN {_SU})"
_SCI = f"(SELECT uuid FROM ciphers WHERE user_uuid IN {_SU} OR organization_uuid IN {_SO})"
_SG = f"(SELECT uuid FROM groups WHERE organizations_uuid IN {_SO})"
_SUO = f"(SELECT uuid FROM users_organizations WHERE user_uuid IN {_SU} OR org_uuid IN {_SO})"

# Full-coverage tables are all leaf children of users / organizations / ciphers, so
# they must be cleared BEFORE those parents -> they go first. event / sso_auth /
# twofactor_duo_ctx / invitations have no FK, so they carry their own seed markers
# ('seed-' state prefix, seeded act_user/email).
_CLEAR_NEW = [
    f"DELETE FROM archives             WHERE user_uuid IN {_SU} OR cipher_uuid IN {_SCI}",
    f"DELETE FROM auth_requests        WHERE user_uuid IN {_SU} OR organization_uuid IN {_SO}",
    f"DELETE FROM emergency_access     WHERE grantor_uuid IN {_SU} OR grantee_uuid IN {_SU} OR email LIKE 'seed.%@seed.local'",
    f"DELETE FROM event                WHERE act_user_uuid IN {_SU}",
    "DELETE FROM invitations          WHERE email LIKE 'seed.%@seed.local'",
    f"DELETE FROM organization_api_key WHERE org_uuid IN {_SO}",
    "DELETE FROM sso_auth             WHERE state LIKE 'seed-%'",
    f"DELETE FROM sso_users            WHERE user_uuid IN {_SU}",
    f"DELETE FROM twofactor            WHERE user_uuid IN {_SU}",
    "DELETE FROM twofactor_duo_ctx    WHERE user_email LIKE 'seed.%@seed.local'",
    f"DELETE FROM twofactor_incomplete WHERE user_uuid IN {_SU}",
]

# Child-table deletes run before parent-table deletes so FK checks stay satisfied.
_CLEAR_SQL = _CLEAR_NEW + [
    f"DELETE FROM favorites          WHERE user_uuid IN {_SU} OR cipher_uuid IN {_SCI}",
    f"DELETE FROM folders_ciphers    WHERE folder_uuid IN {_SF} OR cipher_uuid IN {_SCI}",
    f"DELETE FROM ciphers_collections WHERE collection_uuid IN {_SC} OR cipher_uuid IN {_SCI}",
    f"DELETE FROM attachments        WHERE cipher_uuid IN {_SCI}",
    f"DELETE FROM collections_groups WHERE collections_uuid IN {_SC} OR groups_uuid IN {_SG}",
    f"DELETE FROM groups_users       WHERE users_organizations_uuid IN {_SUO} OR groups_uuid IN {_SG}",
    f"DELETE FROM users_collections  WHERE user_uuid IN {_SU} OR collection_uuid IN {_SC}",
    f"DELETE FROM sends              WHERE user_uuid IN {_SU} OR organization_uuid IN {_SO}",
    f"DELETE FROM devices            WHERE user_uuid IN {_SU}",
    f"DELETE FROM ciphers            WHERE user_uuid IN {_SU} OR organization_uuid IN {_SO}",
    f"DELETE FROM folders            WHERE user_uuid IN {_SU}",
    f"DELETE FROM org_policies       WHERE org_uuid IN {_SO}",
    f"DELETE FROM groups             WHERE organizations_uuid IN {_SO}",
    f"DELETE FROM users_organizations WHERE user_uuid IN {_SU} OR org_uuid IN {_SO}",
    f"DELETE FROM collections        WHERE org_uuid IN {_SO}",
    f"DELETE FROM organizations      WHERE uuid IN {_SO}",
    f"DELETE FROM users              WHERE uuid IN {_SU}",
]


def clear(con: sqlite3.Connection) -> None:
    cur = con.cursor()
    total = 0
    for stmt in _CLEAR_SQL:
        cur.execute(stmt)
        total += cur.rowcount if cur.rowcount > 0 else 0
    con.commit()
    print(f"Cleared {total} seeded rows.")


def finalize(con: sqlite3.Connection) -> None:
    """Leave the DB in rollback (DELETE) journal mode with NO -wal/-shm sidecars.

    We write from the *Windows host*, but Vaultwarden reads the file from inside a
    *Linux container* over a bind mount. A -wal/-shm pair written by the host's
    SQLite cannot be reopened by the container's SQLite -- Vaultwarden panics with
    'Failed to turn on WAL: disk I/O error'. Shipping the file with no WAL sidecars
    avoids that entirely: on next start Vaultwarden re-enables WAL itself and
    creates fresh, container-native -wal/-shm. (Requires the server to be STOPPED
    so the switch can take an exclusive lock.)
    """
    cur = con.cursor()
    cur.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    mode = cur.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    con.commit()
    print(f"Finalized journal_mode={mode} (no -wal/-shm left behind).")


# --------------------------------------------------------------- seeding ------

# Insert column lists kept next to the executemany call so order stays in sync.
def seed(con: sqlite3.Connection, n_users: int, n_orgs: int) -> None:
    batch = datetime.now().strftime("%Y%m%d%H%M%S")
    cur = con.cursor()

    users, folders, ciphers = [], [], []
    orgs, collections, groups = [], [], []
    memberships, user_cols, col_groups, grp_users = [], [], [], []
    ciph_cols, fold_ciph, favs = [], [], []
    devices, sends, attachments, policies = [], [], [], []
    # extra tables added for full per-table coverage
    archives, auth_requests, emergency = [], [], []
    events, invitations, org_api_keys = [], [], []
    sso_auths, sso_user_rows = [], []
    twofactors, duo_ctx, tf_incomplete = [], [], []
    # cross-references captured while building the core rows, reused below
    personal_ciphers: list[tuple[str, str]] = []   # (owner_uuid, cipher_uuid)
    user_devices: dict[str, list[str]] = {}
    org_members: dict[str, list[str]] = {}

    # ---- organizations (+ their collections and groups) --------------------
    org_ids: list[str] = []
    org_collections: dict[str, list[str]] = {}
    org_groups: dict[str, list[str]] = {}
    for _ in range(n_orgs):
        oid = uid()
        org_ids.append(oid)
        # edge: ~1 in 4 orgs has NULL private/public key (keyless org)
        if random.random() < 0.25:
            orgs.append((oid, enc(12), f"seed.{batch}.org@seed.local", None, None))
        else:
            orgs.append((oid, enc(12), f"seed.{batch}.org@seed.local", enc(400), b64(270)))
        org_collections[oid] = []
        for _ in range(random.randint(4, 6)):
            cid = uid()
            org_collections[oid].append(cid)
            collections.append((cid, oid, enc(12), None))
        org_groups[oid] = []
        for _ in range(random.randint(1, 2)):
            gid = uid()
            org_groups[oid].append(gid)
            cd, rd = rand_times()
            groups.append((gid, oid, enc(10), random.choice([0, 0, 1]), None, cd, rd))  # edge: access_all
        # a couple of distinct-atype org policies
        for atype in random.sample(range(0, 8), random.randint(2, 3)):
            policies.append((uid(), oid, atype, random.randint(0, 1), "{}"))

    # ---- users (+ folders, personal ciphers, devices, sends, favorites) ----
    user_ids: list[str] = []
    for i in range(n_users):
        usr = uid()
        user_ids.append(usr)
        ca, ua = rand_times()
        # edge: ~30% Argon2id (kdf_type 1, small iters + memory/parallelism), rest PBKDF2
        if random.random() < 0.3:
            kdf_type, kdf_iter = 1, random.choice([2, 3])
            kdf_mem, kdf_par = random.choice([19, 64, 128]), random.choice([2, 4])
        else:
            kdf_type, kdf_iter, kdf_mem, kdf_par = 0, 600000, None, None
        enabled = 0 if random.random() < 0.1 else 1            # edge: ~10% disabled accounts
        hint = None if random.random() < 0.2 else "test seed hint"
        avatar = random.choice([None, "#1559a3", "#aa4444", "#33aa77"])
        users.append((
            usr, ca, ua, f"seed.{batch}.user{i:04d}@seed.local", f"Seed User {i:04d}",
            os.urandom(32), os.urandom(64), kdf_iter, hint, enc(48),
            enc(800), b64(270), None, None, uid(), "[]", "[]",
            kdf_type, kdf_iter, None, None, 0, None, None, enabled, None, None, avatar,
            kdf_mem, kdf_par, None,
        ))

        # folders
        my_folders = []
        for _ in range(3):
            fid = uid()
            my_folders.append(fid)
            fca, fua = rand_times()
            folders.append((fid, fca, fua, usr, enc(10)))

        # personal ciphers (weighted toward logins)
        for _ in range(random.randint(20, 35)):
            cid = uid()
            atype = random.choices([1, 2, 3, 4], weights=[70, 15, 8, 7])[0]
            cca, cua = rand_times()
            notes = enc(40) if random.random() < 0.3 else None
            fields = fields_json() if random.random() < 0.25 else None         # edge: custom fields
            pwhist = pwhistory_json() if atype == 1 and random.random() < 0.3 else None
            key = enc(64) if random.random() < 0.15 else None                  # edge: per-cipher key
            deleted = cua if random.random() < 0.1 else None                   # edge: ~10% in trash
            ciphers.append((
                cid, cca, cua, usr, None, atype, enc(12), notes, fields,
                cipher_data(atype), pwhist, deleted, random.randint(0, 1), key,
            ))
            personal_ciphers.append((usr, cid))
            if random.random() < 0.6:  # file into a folder
                fold_ciph.append((cid, random.choice(my_folders)))
            if random.random() < 0.2:  # favorite
                favs.append((usr, cid))
            if random.random() < 0.05:  # attachment(s)
                for _ in range(random.randint(1, 2)):
                    attachments.append((uid(), cid, enc(8), random.randint(1024, 5_000_000), enc(48)))

        # devices
        my_devices = []
        for name in random.sample(["firefox", "chrome", "android", "ios", "desktop"], random.randint(1, 2)):
            dca, dua = rand_times()
            duuid = uid()
            my_devices.append(duuid)
            devices.append((duuid, dca, dua, usr, name, random.choice([0, 7, 8, 10, 14]),
                            None, b64(64), None, uid()))
        user_devices[usr] = my_devices

        # sends (edge: password-protected, disabled, expiring, already-accessed)
        for _ in range(random.randint(0, 3)):
            cd, rd = rand_times()
            protected = random.random() < 0.3
            phash = os.urandom(32) if protected else None
            psalt = os.urandom(16) if protected else None
            piter = 100000 if protected else None
            disabled = 1 if random.random() < 0.15 else 0
            expiration = future_ts() if random.random() < 0.4 else None
            sends.append((uid(), usr, None, enc(10), None, random.choice([0, 1]),
                          cipher_data(1), enc(48), phash, psalt, piter,
                          random.choice([None, 5, 10]), random.randint(0, 4), cd, rd,
                          expiration, future_ts(), disabled, random.choice([0, 1])))

    # ---- org ciphers + memberships + grants --------------------------------
    for oid in org_ids:
        cols = org_collections[oid]
        # org-owned ciphers, each placed in 1-2 collections
        for _ in range(random.randint(30, 45)):
            cid = uid()
            atype = random.choices([1, 2, 3, 4], weights=[70, 15, 8, 7])[0]
            cca, cua = rand_times()
            fields = fields_json() if random.random() < 0.25 else None
            key = enc(64) if random.random() < 0.15 else None
            ciphers.append((cid, cca, cua, None, oid, atype, enc(12), None, fields,
                            cipher_data(atype), None, None, 0, key))
            for col in random.sample(cols, random.randint(1, min(2, len(cols)))):
                ciph_cols.append((cid, col))
            if random.random() < 0.05:  # edge: attachment on an ORG-owned cipher
                attachments.append((uid(), cid, enc(8), random.randint(1024, 5_000_000), enc(48)))
        # group -> collection grants
        for gid in org_groups[oid]:
            for col in random.sample(cols, random.randint(1, len(cols))):
                col_groups.append((col, gid, random.randint(0, 1), random.randint(0, 1), 0))
        # memberships: a random subset of users join this org
        members = random.sample(user_ids, min(len(user_ids), random.randint(3, 8)))
        org_members[oid] = members
        for j, usr in enumerate(members):
            uo_id = uid()
            # first member is owner (atype 0), rest users/admins/managers
            atype = 0 if j == 0 else random.choice([2, 2, 2, 1, 3])
            # edge: mix invited(0)/accepted(1)/confirmed(2) status and some access_all grants
            status = 2 if j == 0 else random.choice([0, 1, 2, 2])
            access_all = 1 if random.random() < 0.2 else 0
            memberships.append((uo_id, usr, oid, access_all, enc(48), status, atype, None, None, None))
            # grant the member a couple of collections directly
            for col in random.sample(cols, random.randint(1, min(2, len(cols)))):
                user_cols.append((usr, col, random.randint(0, 1), random.randint(0, 1), 0))
            # add member to a group
            if org_groups[oid] and random.random() < 0.5:
                grp_users.append((random.choice(org_groups[oid]), uo_id))

        # edge: a few ORG-owned sends (organization_uuid set, created by a member)
        for _ in range(random.randint(0, 2)):
            cd, rd = rand_times()
            creator = random.choice(members)
            sends.append((uid(), creator, oid, enc(10), None, random.choice([0, 1]),
                          cipher_data(1), enc(48), None, None, None,
                          random.choice([None, 5, 10]), 0, cd, rd, None, future_ts(), 0, 1))

    # ---- extra tables: full per-table coverage + edge rows -----------------
    # archives: a slice of personal ciphers archived by their owner
    if personal_ciphers:
        for owner, cid in random.sample(personal_ciphers, max(1, len(personal_ciphers) // 12)):
            archives.append((owner, cid, rand_times()[0]))

    # auth_requests: device-login approvals (edge: approved NULL=pending / 1=ok / 0=denied)
    for usr in random.sample(user_ids, max(1, len(user_ids) // 5)):
        cd, _ = rand_times()
        approved = random.choice([None, 1, 0])
        resp = None if approved is None else rand_times()[1]
        auth_requests.append((
            uid(), usr, None, uid(), random.choice([0, 7, 8, 10, 14]),
            f"198.51.100.{random.randint(1, 254)}",
            None if approved is None else uid(),
            b64(24), enc(256),
            None if approved is None else enc(64),
            None if approved is None else b64(32),
            approved, cd, resp, resp if approved == 1 else None,
        ))

    # emergency_access: grantor -> grantee (edge: linked grantee vs invited-by-email)
    for _ in range(max(1, len(user_ids) // 8)):
        grantor = random.choice(user_ids)
        cca, cua = rand_times()
        if random.random() < 0.5 and len(user_ids) > 1:
            grantee = random.choice([u for u in user_ids if u != grantor])
            email, keyenc = None, enc(64)
        else:
            grantee, keyenc = None, None
            email = f"seed.{batch}.ea{random.randint(0, 9999):04d}@seed.local"
        emergency.append((uid(), grantor, grantee, email, keyenc,
                          random.choice([0, 1]), random.choice([0, 1, 2]),
                          random.choice([1, 2, 7, 30]), None, None, cua, cca))

    # event: append-only audit log (no FKs); act_user_uuid is always a seeded user (clear marker)
    EVENT_TYPES = [1000, 1100, 1101, 1102, 1107, 1114, 1300, 1600, 1601, 1700]
    for _ in range(len(user_ids) * 2):
        actor = random.choice(user_ids)
        ed, _ = rand_times()
        events.append((
            uid(), random.choice(EVENT_TYPES),
            actor if random.random() < 0.8 else None,
            random.choice(org_ids) if org_ids and random.random() < 0.5 else None,
            random.choice(personal_ciphers)[1] if personal_ciphers and random.random() < 0.4 else None,
            None, None, None, actor,
            random.choice([None, 0, 7, 10, 14]),
            f"203.0.113.{random.randint(1, 254)}" if random.random() < 0.7 else None,
            ed, None, None, None, None,
        ))

    # invitations: pending invites for users that don't exist yet
    for k in range(random.randint(5, 10)):
        invitations.append((f"seed.{batch}.invite{k:03d}@seed.local",))

    # organization_api_key: org-level API credential (skip a few for partial coverage)
    for oid in org_ids:
        if random.random() < 0.85:
            org_api_keys.append((uid(), oid, 0, b64(32), rand_times()[1]))

    # sso_auth: in-flight SSO handshakes (no FK; 'seed-' state prefix = clear marker)
    for _ in range(random.randint(4, 7)):
        cd, ud = rand_times()
        sso_auths.append((f"seed-{b64(12)}", b64(32), b64(24),
                          "https://sso.seed.local/callback", None, None, cd, ud, None))

    # sso_users: link a subset of users to an external IdP identity
    for usr in random.sample(user_ids, max(1, len(user_ids) // 5)):
        sso_user_rows.append((usr, f"seed-idp|{uid()}", rand_times()[0]))

    # twofactor: per-user 2FA providers (edge: several atypes, enabled 0/1)
    for usr in random.sample(user_ids, max(1, len(user_ids) // 4)):
        for atype in random.sample([0, 1, 2, 3, 6], random.randint(1, 2)):
            twofactors.append((uid(), usr, atype, random.choice([0, 1]),
                               json.dumps({"k": b64(24)}), random.randint(0, 1_700_000_000)))

    # twofactor_duo_ctx: transient Duo contexts (no FK; seeded user_email = clear marker)
    for _ in range(random.randint(3, 5)):
        duo_ctx.append((f"seed-{b64(12)}",
                        f"seed.{batch}.user{random.randint(0, n_users - 1):04d}@seed.local",
                        b64(16), int((datetime.now() + timedelta(minutes=15)).timestamp())))

    # twofactor_incomplete: half-finished 2FA logins (PK user_uuid+device_uuid)
    for usr in random.sample(user_ids, max(1, len(user_ids) // 6)):
        dev = random.choice(user_devices.get(usr) or [uid()])
        tf_incomplete.append((usr, dev, random.choice(["firefox", "chrome", "desktop"]),
                              rand_times()[0], f"198.51.100.{random.randint(1, 254)}",
                              random.choice([0, 7, 10, 14])))

    # ---- bulk insert in FK-safe order --------------------------------------
    def many(sql: str, rows: list) -> int:
        cur.executemany(sql, rows)
        return len(rows)

    inserted = {}
    inserted["organizations"] = many(
        "INSERT INTO organizations(uuid,name,billing_email,private_key,public_key) VALUES(?,?,?,?,?)", orgs)
    inserted["collections"] = many(
        "INSERT INTO collections(uuid,org_uuid,name,external_id) VALUES(?,?,?,?)", collections)
    inserted["groups"] = many(
        "INSERT INTO groups(uuid,organizations_uuid,name,access_all,external_id,creation_date,revision_date) "
        "VALUES(?,?,?,?,?,?,?)", groups)
    inserted["org_policies"] = many(
        "INSERT INTO org_policies(uuid,org_uuid,atype,enabled,data) VALUES(?,?,?,?,?)", policies)
    inserted["users"] = many(
        "INSERT INTO users(uuid,created_at,updated_at,email,name,password_hash,salt,password_iterations,"
        "password_hint,akey,private_key,public_key,totp_secret,totp_recover,security_stamp,equivalent_domains,"
        "excluded_globals,client_kdf_type,client_kdf_iter,verified_at,last_verifying_at,login_verify_count,"
        "email_new,email_new_token,enabled,stamp_exception,api_key,avatar_color,client_kdf_memory,"
        "client_kdf_parallelism,external_id) VALUES(" + ",".join("?" * 31) + ")", users)
    inserted["folders"] = many(
        "INSERT INTO folders(uuid,created_at,updated_at,user_uuid,name) VALUES(?,?,?,?,?)", folders)
    inserted["ciphers"] = many(
        "INSERT INTO ciphers(uuid,created_at,updated_at,user_uuid,organization_uuid,atype,name,notes,fields,"
        "data,password_history,deleted_at,reprompt,key) VALUES(" + ",".join("?" * 14) + ")", ciphers)
    inserted["users_organizations"] = many(
        "INSERT INTO users_organizations(uuid,user_uuid,org_uuid,access_all,akey,status,atype,reset_password_key,"
        "external_id,invited_by_email) VALUES(?,?,?,?,?,?,?,?,?,?)", memberships)
    inserted["users_collections"] = many(
        "INSERT INTO users_collections(user_uuid,collection_uuid,read_only,hide_passwords,manage) VALUES(?,?,?,?,?)",
        user_cols)
    inserted["collections_groups"] = many(
        "INSERT INTO collections_groups(collections_uuid,groups_uuid,read_only,hide_passwords,manage) "
        "VALUES(?,?,?,?,?)", col_groups)
    inserted["groups_users"] = many(
        "INSERT INTO groups_users(groups_uuid,users_organizations_uuid) VALUES(?,?)", grp_users)
    inserted["ciphers_collections"] = many(
        "INSERT INTO ciphers_collections(cipher_uuid,collection_uuid) VALUES(?,?)", ciph_cols)
    inserted["folders_ciphers"] = many(
        "INSERT INTO folders_ciphers(cipher_uuid,folder_uuid) VALUES(?,?)", fold_ciph)
    inserted["favorites"] = many(
        "INSERT INTO favorites(user_uuid,cipher_uuid) VALUES(?,?)", favs)
    inserted["devices"] = many(
        "INSERT INTO devices(uuid,created_at,updated_at,user_uuid,name,atype,push_token,refresh_token,"
        "twofactor_remember,push_uuid) VALUES(?,?,?,?,?,?,?,?,?,?)", devices)
    inserted["sends"] = many(
        "INSERT INTO sends(uuid,user_uuid,organization_uuid,name,notes,atype,data,akey,password_hash,password_salt,"
        "password_iter,max_access_count,access_count,creation_date,revision_date,expiration_date,deletion_date,"
        "disabled,hide_email) VALUES(" + ",".join("?" * 19) + ")", sends)
    inserted["attachments"] = many(
        "INSERT INTO attachments(id,cipher_uuid,file_name,file_size,akey) VALUES(?,?,?,?,?)", attachments)
    inserted["archives"] = many(
        "INSERT INTO archives(user_uuid,cipher_uuid,archived_at) VALUES(?,?,?)", archives)
    inserted["auth_requests"] = many(
        "INSERT INTO auth_requests(uuid,user_uuid,organization_uuid,request_device_identifier,device_type,"
        "request_ip,response_device_id,access_code,public_key,enc_key,master_password_hash,approved,"
        "creation_date,response_date,authentication_date) VALUES(" + ",".join("?" * 15) + ")", auth_requests)
    inserted["emergency_access"] = many(
        "INSERT INTO emergency_access(uuid,grantor_uuid,grantee_uuid,email,key_encrypted,atype,status,"
        "wait_time_days,recovery_initiated_at,last_notification_at,updated_at,created_at) "
        "VALUES(" + ",".join("?" * 12) + ")", emergency)
    inserted["event"] = many(
        "INSERT INTO event(uuid,event_type,user_uuid,org_uuid,cipher_uuid,collection_uuid,group_uuid,"
        "org_user_uuid,act_user_uuid,device_type,ip_address,event_date,policy_uuid,provider_uuid,"
        "provider_user_uuid,provider_org_uuid) VALUES(" + ",".join("?" * 16) + ")", events)
    inserted["invitations"] = many(
        "INSERT INTO invitations(email) VALUES(?)", invitations)
    inserted["organization_api_key"] = many(
        "INSERT INTO organization_api_key(uuid,org_uuid,atype,api_key,revision_date) VALUES(?,?,?,?,?)",
        org_api_keys)
    inserted["sso_auth"] = many(
        "INSERT INTO sso_auth(state,client_challenge,nonce,redirect_uri,code_response,auth_response,"
        "created_at,updated_at,binding_hash) VALUES(" + ",".join("?" * 9) + ")", sso_auths)
    inserted["sso_users"] = many(
        "INSERT INTO sso_users(user_uuid,identifier,created_at) VALUES(?,?,?)", sso_user_rows)
    inserted["twofactor"] = many(
        "INSERT INTO twofactor(uuid,user_uuid,atype,enabled,data,last_used) VALUES(?,?,?,?,?,?)", twofactors)
    inserted["twofactor_duo_ctx"] = many(
        "INSERT INTO twofactor_duo_ctx(state,user_email,nonce,exp) VALUES(?,?,?,?)", duo_ctx)
    inserted["twofactor_incomplete"] = many(
        "INSERT INTO twofactor_incomplete(user_uuid,device_uuid,device_name,login_time,ip_address,device_type) "
        "VALUES(?,?,?,?,?,?)", tf_incomplete)

    con.commit()

    total = sum(inserted.values())
    print(f"\nSeeded batch {batch}: {total} rows across {len(inserted)} tables")
    for t, n in sorted(inserted.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>6}  {t}")


# ------------------------------------------------------------------ main ------

def main() -> int:
    p = argparse.ArgumentParser(description="Seed the Vaultwarden test SQLite DB with bulk rows.")
    p.add_argument("--db", default="data/db.sqlite3", help="path to the SQLite file (default: data/db.sqlite3)")
    p.add_argument("--users", type=int, default=50, help="number of synthetic users (default: 50)")
    p.add_argument("--orgs", type=int, default=6, help="number of synthetic organizations (default: 6)")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible structure")
    p.add_argument("--clear", action="store_true", help="remove ALL previously-seeded data and exit")
    p.add_argument("--no-backup", action="store_true", help="skip the <db>.bak backup before writing")
    args = p.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: {args.db} not found (run from the project folder). Run vaultwarden with sqlite first.", file=sys.stderr)
        return 1
    if args.seed is not None:
        random.seed(args.seed)

    con = sqlite3.connect(args.db)
    con.execute("PRAGMA foreign_keys=ON")     # validate referential integrity of generated rows
    con.execute("PRAGMA busy_timeout=5000")   # tolerate a brief lock; STOP the server for real safety

    try:
        if not args.no_backup:
            bak = args.db + ".bak"
            with sqlite3.connect(bak) as b:   # online backup -> WAL-consistent snapshot
                con.backup(b)
            print(f"Backed up {args.db} -> {bak}")

        if args.clear:
            clear(con)
        else:
            seed(con, args.users, args.orgs)

        finalize(con)   # critical: drop -wal/-shm so the Linux container can reopen the file
    except sqlite3.OperationalError as e:
        print(f"\nERROR: {e}\n(Is Vaultwarden still running? Stop it first: docker compose stop vaultwarden)",
              file=sys.stderr)
        return 2
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
