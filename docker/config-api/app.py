#!/usr/bin/env python3
# Sidecar CRUD API + minimal admin UI for OP25's SQLite-backed config
# (see docker/config/schema.sql). Stdlib only -- hand-rolled WSGI routing,
# same convention as op25/gr-op25_repeater/apps/http_server.py.
import datetime
import json
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import xmlrpc.client
from wsgiref.simple_server import make_server, WSGIServer
from socketserver import ThreadingMixIn

DB_PATH = os.environ.get("OP25_DB_PATH", "/data/op25.db")
OP25_HTTP_URL = os.environ.get("OP25_HTTP_URL", "http://op25:8080/")
OP25_SUPERVISOR_URL = os.environ.get("OP25_SUPERVISOR_URL", "http://op25:9001/RPC2")
LISTEN_PORT = int(os.environ.get("CONFIG_API_PORT", "8091"))
HISTORY_POLL_INTERVAL = float(os.environ.get("HISTORY_POLL_INTERVAL", "5"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_list(rows):
    return [dict(r) for r in rows]


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _column_exists(conn, table, column):
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def ensure_schema():
    # Idempotent, run on every startup -- brings whatever DB is mounted up
    # to the current schema regardless of how old it is, rather than
    # requiring a manual migration step per deployment per schema change.
    # Every check here is a real gap discovered the hard way: this session
    # added several tables/columns to schema.sql but only ever patched them
    # onto the dev instance by hand, never onto an actual deployed DB --
    # this is what should have existed from the start.
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = OFF")  # migrating between related tables below
    changed = []
    try:
        # categories table + talkgroups.category_id (replaces the original
        # free-text talkgroups.category column from before the FK refactor)
        if not _table_exists(conn, "categories"):
            conn.execute("""
                CREATE TABLE categories (
                    id          INTEGER PRIMARY KEY,
                    tag_set_id  INTEGER NOT NULL REFERENCES tag_sets(id) ON DELETE CASCADE,
                    name        TEXT NOT NULL,
                    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    UNIQUE (tag_set_id, name)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_categories_tag_set ON categories(tag_set_id)")
            changed.append("created categories table")

        if _table_exists(conn, "talkgroups") and not _column_exists(conn, "talkgroups", "category_id"):
            conn.execute("ALTER TABLE talkgroups ADD COLUMN category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_talkgroups_category ON talkgroups(category_id)")
            changed.append("added talkgroups.category_id")
            if _column_exists(conn, "talkgroups", "category"):
                # Migrate the old free-text values into normalized category
                # rows + FK, same as the original one-off migration this
                # session ran by hand against the dev instance.
                rows = conn.execute(
                    "SELECT DISTINCT tag_set_id, category FROM talkgroups WHERE category IS NOT NULL AND category != ''"
                ).fetchall()
                cat_id = {}
                for tag_set_id, cat_name in rows:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO categories (tag_set_id, name) VALUES (?, ?)", (tag_set_id, cat_name)
                    )
                    row = conn.execute(
                        "SELECT id FROM categories WHERE tag_set_id=? AND name=?", (tag_set_id, cat_name)
                    ).fetchone()
                    cat_id[(tag_set_id, cat_name)] = row[0]
                linked = 0
                for (tag_set_id, cat_name), cid in cat_id.items():
                    cur = conn.execute(
                        "UPDATE talkgroups SET category_id=? WHERE tag_set_id=? AND category=?",
                        (cid, tag_set_id, cat_name),
                    )
                    linked += cur.rowcount
                changed.append(f"migrated {linked} talkgroups.category values into {len(cat_id)} categories rows")
                # Old column intentionally left in place (harmless, unused) --
                # not worth the extra risk of an ALTER TABLE DROP COLUMN here.

        # trunked_systems.sort_order (drag-to-reorder)
        if _table_exists(conn, "trunked_systems") and not _column_exists(conn, "trunked_systems", "sort_order"):
            conn.execute("ALTER TABLE trunked_systems ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trunked_systems_sort_order ON trunked_systems(sort_order)")
            changed.append("added trunked_systems.sort_order")

        # systems (new parent table: a logical network like "NC VIPER" that
        # several sites belong to) + trunked_systems -> sites rename/split.
        # Must run before subscriber_registrations/call_history/neighbor_sites
        # below so their REFERENCES clause is created pointing at the right
        # (final) table name on a fresh install.
        if not _table_exists(conn, "systems"):
            conn.execute("""
                CREATE TABLE systems (
                    id           INTEGER PRIMARY KEY,
                    name         TEXT NOT NULL UNIQUE,
                    tag_set_id   INTEGER REFERENCES tag_sets(id) ON DELETE SET NULL,
                    whitelist_id INTEGER REFERENCES access_lists(id) ON DELETE SET NULL,
                    blacklist_id INTEGER REFERENCES access_lists(id) ON DELETE SET NULL,
                    notes        TEXT,
                    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            changed.append("created systems table")

        if _table_exists(conn, "trunked_systems") and not _table_exists(conn, "sites"):
            # Live migration: trunked_systems has real data, sites doesn't
            # exist yet. Group existing rows by their (tag_set_id,
            # whitelist_id, blacklist_id) triple -- in practice this cleanly
            # separates e.g. every NC VIPER site (shared tag_set) from every
            # Charlotte UASI site -- and backfill one systems row per group,
            # named from the common " - "-delimited sysname prefix.
            groups = conn.execute(
                "SELECT DISTINCT tag_set_id, whitelist_id, blacklist_id FROM trunked_systems"
            ).fetchall()
            group_to_system_id = {}
            for g in groups:
                key = (g["tag_set_id"], g["whitelist_id"], g["blacklist_id"])
                sample = conn.execute(
                    """SELECT sysname FROM trunked_systems
                       WHERE tag_set_id IS ? AND whitelist_id IS ? AND blacklist_id IS ?
                       ORDER BY sort_order LIMIT 1""",
                    key,
                ).fetchone()
                base_name = sample["sysname"].split(" - ")[0].strip() if sample and " - " in sample["sysname"] \
                    else (sample["sysname"] if sample else "System")
                name = base_name
                n = 1
                while conn.execute("SELECT 1 FROM systems WHERE name=?", (name,)).fetchone():
                    n += 1
                    name = f"{base_name} ({n})"
                cur = conn.execute(
                    "INSERT INTO systems (name, tag_set_id, whitelist_id, blacklist_id) VALUES (?, ?, ?, ?)",
                    (name, *key),
                )
                group_to_system_id[key] = cur.lastrowid

            # ALTER TABLE ... RENAME TO gets SQLite's automatic cross-table
            # REFERENCES-clause fixup for free (channels/subscriber_
            # registrations/call_history/neighbor_sites all currently say
            # "REFERENCES trunked_systems(id)" -- after this they all say
            # "REFERENCES sites(id)" without those tables being touched).
            # A plain DROP+recreate-under-the-same-name would NOT get this
            # treatment, which is why the column-dropping part below happens
            # as a separate step afterward, never by dropping this identity.
            conn.execute("ALTER TABLE trunked_systems RENAME TO sites")

            conn.execute("""
                CREATE TABLE sites_new (
                    id                    INTEGER PRIMARY KEY,
                    system_id             INTEGER REFERENCES systems(id) ON DELETE SET NULL,
                    sysname               TEXT NOT NULL UNIQUE,
                    nac                   TEXT NOT NULL DEFAULT '0x0',
                    control_channel_list  TEXT NOT NULL,
                    tdma_cc               INTEGER NOT NULL DEFAULT 0 CHECK (tdma_cc IN (0,1)),
                    crypt_behavior        INTEGER NOT NULL DEFAULT 1,
                    notes                 TEXT,
                    sort_order            INTEGER NOT NULL DEFAULT 0,
                    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            old_rows = conn.execute("SELECT * FROM sites").fetchall()
            for r in old_rows:
                key = (r["tag_set_id"], r["whitelist_id"], r["blacklist_id"])
                conn.execute(
                    """INSERT INTO sites_new
                       (id, system_id, sysname, nac, control_channel_list, tdma_cc,
                        crypt_behavior, notes, sort_order, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (r["id"], group_to_system_id[key], r["sysname"], r["nac"], r["control_channel_list"],
                     r["tdma_cc"], r["crypt_behavior"], r["notes"], r["sort_order"], r["created_at"], r["updated_at"]),
                )
            # Safe because PRAGMA foreign_keys is OFF for this whole connection
            # and no DML happens in the brief gap where no table is named
            # "sites" -- other tables' REFERENCES clauses (set by the RENAME
            # above) never change again, they just keep saying "sites".
            conn.execute("DROP TABLE sites")
            conn.execute("ALTER TABLE sites_new RENAME TO sites")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sites_system ON sites(system_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sites_sort_order ON sites(sort_order)")
            changed.append(f"migrated trunked_systems -> sites, backfilled {len(group_to_system_id)} systems rows")
        elif not _table_exists(conn, "sites"):
            # Truly fresh DB -- trunked_systems never existed either, nothing
            # to migrate, just create sites at its final shape directly.
            conn.execute("""
                CREATE TABLE sites (
                    id                    INTEGER PRIMARY KEY,
                    system_id             INTEGER REFERENCES systems(id) ON DELETE SET NULL,
                    sysname               TEXT NOT NULL UNIQUE,
                    nac                   TEXT NOT NULL DEFAULT '0x0',
                    control_channel_list  TEXT NOT NULL,
                    tdma_cc               INTEGER NOT NULL DEFAULT 0 CHECK (tdma_cc IN (0,1)),
                    crypt_behavior        INTEGER NOT NULL DEFAULT 1,
                    notes                 TEXT,
                    sort_order            INTEGER NOT NULL DEFAULT 0,
                    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sites_system ON sites(system_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sites_sort_order ON sites(sort_order)")
            changed.append("created sites table")

        # subscriber_registrations / call_history
        if not _table_exists(conn, "subscriber_registrations"):
            conn.execute("""
                CREATE TABLE subscriber_registrations (
                    id                  INTEGER PRIMARY KEY,
                    trunked_system_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    time                TEXT NOT NULL,
                    tgid                INTEGER,
                    tgid_tag            TEXT,
                    source_rid          INTEGER NOT NULL,
                    tag                 TEXT,
                    UNIQUE (trunked_system_id, source_rid, time)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subreg_time ON subscriber_registrations(time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subreg_tgid ON subscriber_registrations(trunked_system_id, tgid, time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_subreg_rid ON subscriber_registrations(trunked_system_id, source_rid, time)")
            changed.append("created subscriber_registrations table")

        if not _table_exists(conn, "call_history"):
            conn.execute("""
                CREATE TABLE call_history (
                    id                  INTEGER PRIMARY KEY,
                    trunked_system_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    time                TEXT NOT NULL,
                    freq                INTEGER,
                    slot                INTEGER,
                    prio                INTEGER,
                    tgid                INTEGER,
                    tgtag               TEXT,
                    rid                 INTEGER,
                    rtag                TEXT,
                    UNIQUE (trunked_system_id, time, tgid, rid)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_callhist_time ON call_history(time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_callhist_tgid ON call_history(trunked_system_id, tgid, time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_callhist_rid ON call_history(trunked_system_id, rid, time)")
            changed.append("created call_history table")

        # neighbor_sites (roaming foundation -- see docker/config/schema.sql for details)
        if not _table_exists(conn, "neighbor_sites"):
            conn.execute("""
                CREATE TABLE neighbor_sites (
                    id                  INTEGER PRIMARY KEY,
                    trunked_system_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
                    freq                INTEGER NOT NULL,
                    uplink              INTEGER,
                    rfid                INTEGER,
                    stid                INTEGER,
                    lra                 INTEGER,
                    freq_table          INTEGER,
                    conventional        INTEGER,
                    valid               INTEGER,
                    active              INTEGER,
                    last_seen           TEXT NOT NULL,
                    UNIQUE (trunked_system_id, freq)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_neighbor_sites_system ON neighbor_sites(trunked_system_id)")
            changed.append("created neighbor_sites table")

        conn.commit()
    finally:
        conn.close()

    if changed:
        sys.stderr.write("ensure_schema: " + "; ".join(changed) + "\n")
    else:
        sys.stderr.write("ensure_schema: schema already up to date\n")


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


# ---------------------------------------------------------------- systems --

# ----------------------------------------------------------------- sites --
# A site is one physical radio site (was "trunked_systems" -- see systems,
# below, for the logical-network grouping several sites can share).

def list_sites(conn):
    q = """
    SELECT s.*, sy.name AS system_name,
           EXISTS(SELECT 1 FROM channels c WHERE c.trunking_system_id = s.id) AS active
    FROM sites s
    LEFT JOIN systems sy ON s.system_id = sy.id
    ORDER BY s.sort_order, s.sysname
    """
    return rows_to_list(conn.execute(q))


def reorder_sites(conn, body):
    order = body.get("order", [])
    if not order:
        raise ApiError(400, "missing 'order' array of site ids")
    for i, sid in enumerate(order):
        conn.execute("UPDATE sites SET sort_order = ? WHERE id = ?", (i, sid))
    conn.commit()
    return {"status": "reordered"}


def get_site(conn, sid):
    row = conn.execute("SELECT * FROM sites WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise ApiError(404, "site not found")
    return dict(row)


def create_site(conn, body):
    next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM sites").fetchone()[0]
    cur = conn.execute(
        """INSERT INTO sites
           (sysname, nac, control_channel_list, tdma_cc, crypt_behavior,
            system_id, notes, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            body["sysname"],
            body.get("nac", "0x0"),
            body.get("control_channel_list", ""),
            1 if body.get("tdma_cc") else 0,
            body.get("crypt_behavior", 1),
            body.get("system_id"),
            body.get("notes"),
            next_order,
        ),
    )
    conn.commit()
    return get_site(conn, cur.lastrowid)


def update_site(conn, sid, body):
    get_site(conn, sid)  # 404 if missing
    fields = ["sysname", "nac", "control_channel_list", "tdma_cc", "crypt_behavior",
              "system_id", "notes"]
    sets, vals = [], []
    for f in fields:
        if f in body:
            v = body[f]
            if f == "tdma_cc":
                v = 1 if v else 0
            sets.append(f"{f} = ?")
            vals.append(v)
    if sets:
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        vals.append(sid)
        conn.execute(f"UPDATE sites SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    return get_site(conn, sid)


def delete_site(conn, sid):
    get_site(conn, sid)
    conn.execute("DELETE FROM sites WHERE id = ?", (sid,))
    conn.commit()


# --------------------------------------------------------------- systems --
# A logical network (e.g. "NC VIPER") that several sites belong to -- the
# talkgroup tag set / white/blacklist apply here, not per-site.

def list_systems(conn):
    q = """
    SELECT sy.*, tset.name AS tag_set_name,
           wl.name AS whitelist_name, bl.name AS blacklist_name
    FROM systems sy
    LEFT JOIN tag_sets tset ON sy.tag_set_id = tset.id
    LEFT JOIN access_lists wl ON sy.whitelist_id = wl.id
    LEFT JOIN access_lists bl ON sy.blacklist_id = bl.id
    ORDER BY sy.name
    """
    return rows_to_list(conn.execute(q))


def get_system(conn, sysid):
    row = conn.execute("SELECT * FROM systems WHERE id = ?", (sysid,)).fetchone()
    if row is None:
        raise ApiError(404, "system not found")
    return dict(row)


def create_system(conn, body):
    cur = conn.execute(
        """INSERT INTO systems (name, tag_set_id, whitelist_id, blacklist_id, notes)
           VALUES (?, ?, ?, ?, ?)""",
        (body["name"], body.get("tag_set_id"), body.get("whitelist_id"),
         body.get("blacklist_id"), body.get("notes")),
    )
    conn.commit()
    return get_system(conn, cur.lastrowid)


def update_system(conn, sysid, body):
    get_system(conn, sysid)  # 404 if missing
    fields = ["name", "tag_set_id", "whitelist_id", "blacklist_id", "notes"]
    sets, vals = [], []
    for f in fields:
        if f in body:
            sets.append(f"{f} = ?")
            vals.append(body[f])
    if sets:
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        vals.append(sysid)
        conn.execute(f"UPDATE systems SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    return get_system(conn, sysid)


def delete_system(conn, sysid):
    get_system(conn, sysid)
    conn.execute("DELETE FROM systems WHERE id = ?", (sysid,))
    conn.commit()


# --------------------------------------------------------------- tag_sets --

def list_tag_sets(conn):
    return rows_to_list(conn.execute("SELECT * FROM tag_sets ORDER BY name"))


def create_tag_set(conn, body):
    cur = conn.execute(
        "INSERT INTO tag_sets (name, description) VALUES (?, ?)",
        (body["name"], body.get("description")),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM tag_sets WHERE id = ?", (cur.lastrowid,)).fetchone())


def delete_tag_set(conn, tsid):
    conn.execute("DELETE FROM tag_sets WHERE id = ?", (tsid,))
    conn.commit()


# -------------------------------------------------------------- categories --

def find_or_create_category(conn, tag_set_id, name):
    name = (name or "").strip()
    if not name:
        return None
    row = conn.execute(
        "SELECT id FROM categories WHERE tag_set_id = ? AND name = ?", (tag_set_id, name)
    ).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO categories (tag_set_id, name) VALUES (?, ?)", (tag_set_id, name)
    )
    return cur.lastrowid


def list_categories(conn, tag_set_id):
    q = """
    SELECT c.*, COUNT(tg.id) AS talkgroup_count
    FROM categories c
    LEFT JOIN talkgroups tg ON tg.category_id = c.id
    WHERE c.tag_set_id = ?
    GROUP BY c.id
    ORDER BY c.name
    """
    return rows_to_list(conn.execute(q, (tag_set_id,)))


def create_category(conn, tag_set_id, body):
    cat_id = find_or_create_category(conn, tag_set_id, body["name"])
    conn.commit()
    return dict(conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone())


def update_category(conn, cat_id, body):
    row = conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone()
    if row is None:
        raise ApiError(404, "category not found")
    if "name" in body:
        conn.execute(
            "UPDATE categories SET name = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (body["name"], cat_id),
        )
        conn.commit()
    return dict(conn.execute("SELECT * FROM categories WHERE id = ?", (cat_id,)).fetchone())


def delete_category(conn, cat_id):
    # ON DELETE SET NULL: linked talkgroups just lose their category, not deleted.
    conn.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
    conn.commit()


# -------------------------------------------------------------- talkgroups --

def list_talkgroups(conn, tag_set_id):
    q = """
    SELECT tg.*, c.name AS category_name
    FROM talkgroups tg
    LEFT JOIN categories c ON tg.category_id = c.id
    WHERE tg.tag_set_id = ?
    ORDER BY tg.tgid
    """
    return rows_to_list(conn.execute(q, (tag_set_id,)))


def resolve_category_id(conn, tag_set_id, body):
    """category_id wins if present (including explicit null to clear);
    otherwise category_name is resolved via find-or-create. Returns
    (has_update, value) -- has_update is False if body touched neither key."""
    if "category_id" in body:
        return True, body["category_id"]
    if "category_name" in body:
        return True, find_or_create_category(conn, tag_set_id, body["category_name"])
    return False, None


def create_talkgroup(conn, tag_set_id, body):
    _, category_id = resolve_category_id(conn, tag_set_id, body)
    cur = conn.execute(
        "INSERT INTO talkgroups (tag_set_id, tgid, name, category_id, priority, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (tag_set_id, body["tgid"], body["name"], category_id, body.get("priority"), body.get("notes")),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM talkgroups WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_talkgroup(conn, tgid_row, body):
    existing = conn.execute("SELECT tag_set_id FROM talkgroups WHERE id = ?", (tgid_row,)).fetchone()
    if existing is None:
        raise ApiError(404, "talkgroup not found")

    fields = ["tgid", "name", "priority", "notes"]
    sets, vals = [], []
    for f in fields:
        if f in body:
            sets.append(f"{f} = ?")
            vals.append(body[f])

    has_cat_update, category_id = resolve_category_id(conn, existing["tag_set_id"], body)
    if has_cat_update:
        sets.append("category_id = ?")
        vals.append(category_id)

    if sets:
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')")
        vals.append(tgid_row)
        conn.execute(f"UPDATE talkgroups SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM talkgroups WHERE id = ?", (tgid_row,)).fetchone()
    if row is None:
        raise ApiError(404, "talkgroup not found")
    return dict(row)


def delete_talkgroup(conn, tgid_row):
    conn.execute("DELETE FROM talkgroups WHERE id = ?", (tgid_row,))
    conn.commit()


# ------------------------------------------------------------ access_lists --

def list_access_lists(conn, list_type=None):
    if list_type:
        return rows_to_list(conn.execute(
            "SELECT * FROM access_lists WHERE type = ? ORDER BY name", (list_type,)
        ))
    return rows_to_list(conn.execute("SELECT * FROM access_lists ORDER BY name"))


def create_access_list(conn, body):
    cur = conn.execute(
        "INSERT INTO access_lists (name, type, notes) VALUES (?, ?, ?)",
        (body["name"], body["type"], body.get("notes")),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM access_lists WHERE id = ?", (cur.lastrowid,)).fetchone())


def delete_access_list(conn, alid):
    conn.execute("DELETE FROM access_lists WHERE id = ?", (alid,))
    conn.commit()


def list_access_list_entries(conn, alid):
    return rows_to_list(conn.execute(
        "SELECT * FROM access_list_entries WHERE access_list_id = ? ORDER BY tgid", (alid,)
    ))


def create_access_list_entry(conn, alid, body):
    cur = conn.execute(
        "INSERT INTO access_list_entries (access_list_id, tgid, tgid_end, notes) VALUES (?, ?, ?, ?)",
        (alid, body["tgid"], body.get("tgid_end"), body.get("notes")),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM access_list_entries WHERE id = ?", (cur.lastrowid,)).fetchone())


def delete_access_list_entry(conn, entry_id):
    conn.execute("DELETE FROM access_list_entries WHERE id = ?", (entry_id,))
    conn.commit()


# ------------------------------------------------------------------ devices --

def list_devices(conn):
    return rows_to_list(conn.execute("SELECT * FROM devices ORDER BY name"))


def create_device(conn, body):
    cur = conn.execute(
        """INSERT INTO devices (name, args, gains, offset, ppm, usable_bw_pct, rate, tunable)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            body["name"], body.get("args", ""), body.get("gains", ""),
            body.get("offset", 0), body.get("ppm", 0), body.get("usable_bw_pct", 0.85),
            body.get("rate", 1000000), 1 if body.get("tunable", True) else 0,
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM devices WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_device(conn, did, body):
    fields = ["name", "args", "gains", "offset", "ppm", "usable_bw_pct", "rate", "tunable"]
    sets, vals = [], []
    for f in fields:
        if f in body:
            v = body[f]
            if f == "tunable":
                v = 1 if v else 0
            sets.append(f"{f} = ?")
            vals.append(v)
    if sets:
        vals.append(did)
        conn.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()
    if row is None:
        raise ApiError(404, "device not found")
    return dict(row)


def delete_device(conn, did):
    conn.execute("DELETE FROM devices WHERE id = ?", (did,))
    conn.commit()


# ----------------------------------------------------------------- channels --

def list_channels(conn):
    q = """
    SELECT c.*, d.name AS device_name, s.sysname AS trunking_sysname
    FROM channels c
    JOIN devices d ON c.device_id = d.id
    LEFT JOIN sites s ON c.trunking_system_id = s.id
    ORDER BY c.name
    """
    return rows_to_list(conn.execute(q))


def create_channel(conn, body):
    cur = conn.execute(
        """INSERT INTO channels
           (name, device_id, trunking_system_id, demod_type, destination,
            meta_stream_name, excess_bw, filter_type, if_rate, symbol_rate, enable_analog)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            body["name"], body["device_id"], body.get("trunking_system_id"),
            body.get("demod_type", "cqpsk"), body["destination"],
            body.get("meta_stream_name", ""), body.get("excess_bw", 0.2),
            body.get("filter_type", "rc"), body.get("if_rate", 24000),
            body.get("symbol_rate", 4800), body.get("enable_analog", "off"),
        ),
    )
    conn.commit()
    return dict(conn.execute("SELECT * FROM channels WHERE id = ?", (cur.lastrowid,)).fetchone())


def update_channel(conn, cid, body):
    fields = ["name", "device_id", "trunking_system_id", "demod_type", "destination",
              "meta_stream_name", "excess_bw", "filter_type", "if_rate", "symbol_rate", "enable_analog"]
    sets, vals = [], []
    for f in fields:
        if f in body:
            sets.append(f"{f} = ?")
            vals.append(body[f])
    if sets:
        vals.append(cid)
        conn.execute(f"UPDATE channels SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM channels WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise ApiError(404, "channel not found")
    return dict(row)


def delete_channel(conn, cid):
    conn.execute("DELETE FROM channels WHERE id = ?", (cid,))
    conn.commit()


# ------------------------------------------------------ op25 live actions --

def op25_send_command(command, arg1=0, arg2=0):
    body = json.dumps([{"command": command, "arg1": arg1, "arg2": arg2}]).encode()
    req = urllib.request.Request(OP25_HTTP_URL, data=body, method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status, resp.read()
    except urllib.error.URLError as e:
        raise ApiError(502, f"could not reach op25: {e}")


def apply_reload(conn, sid):
    get_site(conn, sid)  # 404 if missing
    # MVP limitation: arg2 (msgq_id) is hardcoded to 0, since this
    # deployment has exactly one channel. See docker/config/schema.sql /
    # the plan doc for the multi-channel follow-up.
    status, _ = op25_send_command("reload", 0, 0)
    return {"status": "reload sent", "op25_status": status}


def restart_op25():
    # supervisord (running as op25's PID 1, see docker/op25/supervisord.conf)
    # exposes an XML-RPC control interface on the compose network -- this
    # restarts the op25 *process* in place, no Docker-level access needed.
    server = xmlrpc.client.ServerProxy(OP25_SUPERVISOR_URL)
    try:
        try:
            server.supervisor.stopProcess("op25", True)
        except xmlrpc.client.Fault as e:
            if "NOT_RUNNING" not in e.faultString:
                raise
        server.supervisor.startProcess("op25", True)
        return "restarted"
    except (xmlrpc.client.Error, OSError) as e:
        raise ApiError(502, f"could not restart op25 via supervisord: {e}")


def activate_site(conn, sid):
    get_site(conn, sid)  # 404 if missing
    channel = conn.execute("SELECT id FROM channels LIMIT 1").fetchone()
    if channel is None:
        raise ApiError(409, "no channel defined to activate this site on")
    conn.execute("UPDATE channels SET trunking_system_id = ? WHERE id = ?", (sid, channel["id"]))
    conn.commit()
    status = restart_op25()
    return {"status": "activated, op25 restarting", "restart_status": status}


# ------------------------------------------------------------------ routing --

ROUTES = [
    ("GET", r"^/api/systems$", lambda conn, m, body, qs: list_systems(conn)),
    ("POST", r"^/api/systems$", lambda conn, m, body, qs: create_system(conn, body)),
    ("GET", r"^/api/systems/(?P<id>\d+)$", lambda conn, m, body, qs: get_system(conn, int(m["id"]))),
    ("PUT", r"^/api/systems/(?P<id>\d+)$", lambda conn, m, body, qs: update_system(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/systems/(?P<id>\d+)$", lambda conn, m, body, qs: delete_system(conn, int(m["id"]))),

    ("GET", r"^/api/sites$", lambda conn, m, body, qs: list_sites(conn)),
    ("POST", r"^/api/sites$", lambda conn, m, body, qs: create_site(conn, body)),
    ("POST", r"^/api/sites/reorder$", lambda conn, m, body, qs: reorder_sites(conn, body)),
    ("GET", r"^/api/sites/(?P<id>\d+)$", lambda conn, m, body, qs: get_site(conn, int(m["id"]))),
    ("PUT", r"^/api/sites/(?P<id>\d+)$", lambda conn, m, body, qs: update_site(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/sites/(?P<id>\d+)$", lambda conn, m, body, qs: delete_site(conn, int(m["id"]))),
    ("POST", r"^/api/sites/(?P<id>\d+)/apply_reload$", lambda conn, m, body, qs: apply_reload(conn, int(m["id"]))),
    ("POST", r"^/api/sites/(?P<id>\d+)/activate$", lambda conn, m, body, qs: activate_site(conn, int(m["id"]))),

    ("GET", r"^/api/tag_sets$", lambda conn, m, body, qs: list_tag_sets(conn)),
    ("POST", r"^/api/tag_sets$", lambda conn, m, body, qs: create_tag_set(conn, body)),
    ("DELETE", r"^/api/tag_sets/(?P<id>\d+)$", lambda conn, m, body, qs: delete_tag_set(conn, int(m["id"]))),
    ("GET", r"^/api/tag_sets/(?P<id>\d+)/talkgroups$", lambda conn, m, body, qs: list_talkgroups(conn, int(m["id"]))),
    ("POST", r"^/api/tag_sets/(?P<id>\d+)/talkgroups$", lambda conn, m, body, qs: create_talkgroup(conn, int(m["id"]), body)),

    ("GET", r"^/api/tag_sets/(?P<id>\d+)/categories$", lambda conn, m, body, qs: list_categories(conn, int(m["id"]))),
    ("POST", r"^/api/tag_sets/(?P<id>\d+)/categories$", lambda conn, m, body, qs: create_category(conn, int(m["id"]), body)),
    ("PUT", r"^/api/categories/(?P<id>\d+)$", lambda conn, m, body, qs: update_category(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/categories/(?P<id>\d+)$", lambda conn, m, body, qs: delete_category(conn, int(m["id"]))),

    ("PUT", r"^/api/talkgroups/(?P<id>\d+)$", lambda conn, m, body, qs: update_talkgroup(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/talkgroups/(?P<id>\d+)$", lambda conn, m, body, qs: delete_talkgroup(conn, int(m["id"]))),

    ("GET", r"^/api/access_lists$", lambda conn, m, body, qs: list_access_lists(conn, qs.get("type", [None])[0])),
    ("POST", r"^/api/access_lists$", lambda conn, m, body, qs: create_access_list(conn, body)),
    ("DELETE", r"^/api/access_lists/(?P<id>\d+)$", lambda conn, m, body, qs: delete_access_list(conn, int(m["id"]))),
    ("GET", r"^/api/access_lists/(?P<id>\d+)/entries$", lambda conn, m, body, qs: list_access_list_entries(conn, int(m["id"]))),
    ("POST", r"^/api/access_lists/(?P<id>\d+)/entries$", lambda conn, m, body, qs: create_access_list_entry(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/access_list_entries/(?P<id>\d+)$", lambda conn, m, body, qs: delete_access_list_entry(conn, int(m["id"]))),

    ("GET", r"^/api/devices$", lambda conn, m, body, qs: list_devices(conn)),
    ("POST", r"^/api/devices$", lambda conn, m, body, qs: create_device(conn, body)),
    ("PUT", r"^/api/devices/(?P<id>\d+)$", lambda conn, m, body, qs: update_device(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/devices/(?P<id>\d+)$", lambda conn, m, body, qs: delete_device(conn, int(m["id"]))),

    ("GET", r"^/api/channels$", lambda conn, m, body, qs: list_channels(conn)),
    ("POST", r"^/api/channels$", lambda conn, m, body, qs: create_channel(conn, body)),
    ("PUT", r"^/api/channels/(?P<id>\d+)$", lambda conn, m, body, qs: update_channel(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/channels/(?P<id>\d+)$", lambda conn, m, body, qs: delete_channel(conn, int(m["id"]))),

    ("GET", r"^/api/subscriber_registrations$", lambda conn, m, body, qs: list_subscriber_registrations(conn, qs)),
    ("GET", r"^/api/call_history$", lambda conn, m, body, qs: list_call_history(conn, qs)),
    ("GET", r"^/api/neighbor_sites$", lambda conn, m, body, qs: list_neighbor_sites(conn, qs)),
    ("GET", r"^/api/analysis/tg_activity$", lambda conn, m, body, qs: tg_activity(conn, qs)),
    ("GET", r"^/api/analysis/hopping_radios$", lambda conn, m, body, qs: hopping_radios(conn, qs)),
]
COMPILED_ROUTES = [(method, re.compile(pattern), handler) for method, pattern, handler in ROUTES]

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def static_file(environ, start_response):
    path = environ["PATH_INFO"]
    if path == "/":
        path = "/index.html"
    filename = os.path.basename(path)
    suf = os.path.splitext(filename)[1]
    if suf == "":
        # Extension-less path (e.g. /talkgroups, /access-lists) -- this is a
        # client-side route, not a real file. Serve the SPA shell and let
        # app.js's router pick the tab from location.pathname on load.
        filename = "index.html"
        suf = ".html"
    full_path = os.path.join(STATIC_DIR, filename)
    if suf not in CONTENT_TYPES or ".." in path or not os.access(full_path, os.R_OK):
        start_response("404 Not Found", [("Content-type", "text/plain")])
        return [b"404 not found"]
    with open(full_path, "rb") as f:
        data = f.read()
    start_response("200 OK", [("Content-type", CONTENT_TYPES[suf]), ("Content-Length", str(len(data)))])
    return [data]


REQUIRED_TABLES = {
    "tag_sets", "talkgroups", "categories", "access_lists",
    "access_list_entries", "systems", "sites", "devices", "channels",
}


def import_db(environ, start_response):
    # Wholesale replace of the live SQLite DB from an uploaded file (the
    # counterpart to export_db()'s download). The raw file bytes are the
    # POST body (Content-Type: application/octet-stream) -- no multipart
    # parsing needed, keeps this stdlib-only.
    length = int(environ.get("CONTENT_LENGTH") or 0)
    if length == 0:
        start_response("400 Bad Request", [("Content-type", "application/json")])
        return [json.dumps({"error": "empty request body"}).encode()]
    data = environ["wsgi.input"].read(length)

    tmp_path = DB_PATH + ".importing"
    with open(tmp_path, "wb") as f:
        f.write(data)

    # Validate before touching the live DB: must be a well-formed SQLite
    # file with the schema this app expects, not just any random upload.
    try:
        check_conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        integrity = check_conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"integrity_check failed: {integrity}")
        tables = {r[0] for r in check_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"missing expected tables: {', '.join(sorted(missing))}")
        check_conn.close()
    except (sqlite3.Error, ValueError) as e:
        os.remove(tmp_path)
        start_response("400 Bad Request", [("Content-type", "application/json")])
        return [json.dumps({"error": f"not a valid op25 config DB: {e}"}).encode()]

    # Back up whatever's live now before replacing it.
    backup_name = None
    if os.path.exists(DB_PATH):
        stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        backup_name = f"{DB_PATH}.bak-{stamp}"
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        with open(DB_PATH, "rb") as src, open(backup_name, "wb") as dst:
            dst.write(src.read())

    # Atomic swap, then drop any stale -wal/-shm sidecar files left over
    # from the PREVIOUS db -- they reference the old file's page layout and
    # would corrupt reads against the newly-swapped-in content otherwise.
    os.replace(tmp_path, DB_PATH)
    # Two sources of stale sidecar files to clean up: whatever the previous
    # DB_PATH left behind, and whatever validating tmp_path read-only above
    # may have created under the pre-rename name (os.replace only renames
    # the main file, not its -wal/-shm siblings).
    for stale in (DB_PATH + "-wal", DB_PATH + "-shm", tmp_path + "-wal", tmp_path + "-shm"):
        if os.path.exists(stale):
            os.remove(stale)

    # Persist WAL mode on the new file -- op25's connections are read-only
    # and can't set journal_mode themselves (see db_config.py).
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.close()

    # Structural data (systems/devices/channels) only takes effect on
    # process start, same as any "Set Active"/topology edit -- restart to
    # apply the imported config.
    try:
        restart_status = restart_op25()
    except ApiError as e:
        start_response("200 OK", [("Content-type", "application/json")])
        return [json.dumps({
            "status": "imported, but restart failed -- restart op25 manually",
            "error": e.message,
            "backup": backup_name,
        }).encode()]

    start_response("200 OK", [("Content-type", "application/json")])
    return [json.dumps({"status": "imported, op25 restarting", "restart_status": restart_status, "backup": backup_name}).encode()]


def export_db(environ, start_response):
    # Whole-file download of the live SQLite DB -- this is the entire config
    # (systems, talkgroups, categories, access lists, devices, channels) in
    # one portable, self-contained file.
    if not os.path.exists(DB_PATH):
        start_response("404 Not Found", [("Content-type", "text/plain")])
        return [b"no database file found"]
    # Force a WAL checkpoint first so the export reflects every committed
    # write, not just what's made it from the -wal file into the main
    # database file yet.
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    with open(DB_PATH, "rb") as f:
        data = f.read()
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    filename = f"op25-config-{stamp}.db"
    start_response("200 OK", [
        ("Content-type", "application/octet-stream"),
        ("Content-Length", str(len(data))),
        ("Content-Disposition", f'attachment; filename="{filename}"'),
    ])
    return [data]


# --------------------------------------------------------- history poller --
#
# Subscriber registrations and call history both only ever exist in op25's
# own in-memory state (registered_wuids / call_log) -- ephemeral there
# (registrations expire per TIA-102.AABD, call_log is a small ring buffer).
# This polls op25's existing 'update' command on a timer and persists
# whatever's new into subscriber_registrations / call_history, entirely
# from config-api's side -- op25 itself is untouched by this (beyond the
# call_log non-destructive-read fix, which was a correctness fix needed
# regardless of who polls it).

def _active_site_id(conn):
    row = conn.execute("""
        SELECT s.id FROM channels c
        JOIN sites s ON c.trunking_system_id = s.id
        LIMIT 1
    """).fetchone()
    return row["id"] if row else None


def _epoch_to_iso(ts):
    return datetime.datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _active_tag_set_id(conn, site_id):
    row = conn.execute("""
        SELECT sy.tag_set_id FROM sites s
        LEFT JOIN systems sy ON s.system_id = sy.id
        WHERE s.id=?
    """, (site_id,)).fetchone()
    return row["tag_set_id"] if row else None


def _ensure_talkgroup_placeholder(conn, tag_set_id, tgid):
    # A TGID showing up in a grant/registration with no corresponding
    # talkgroups row (op25 reads names straight from this table -- see
    # db_config.load_talkgroups()) would otherwise just silently go
    # unnamed forever. Auto-create a placeholder so it surfaces in the
    # config UI and accumulates over time instead of requiring every
    # TGID to be known and entered up front.
    # tgid 0 shows up in subscriber registrations when a radio registers
    # with no group affiliation yet (P25's "no group" convention) -- not a
    # real talkgroup, so skip it rather than creating a bogus placeholder.
    if tag_set_id is None or tgid is None or tgid == 0:
        return
    conn.execute(
        "INSERT OR IGNORE INTO talkgroups (tag_set_id, tgid, name) VALUES (?, ?, ?)",
        (tag_set_id, tgid, f"_Talkgroup {tgid}"),
    )


def poll_history_once(conn):
    site_id = _active_site_id(conn)
    if site_id is None:
        return  # no active site -- nothing is receiving RF, nothing to log
    tag_set_id = _active_tag_set_id(conn, site_id)

    try:
        status, body = op25_send_command("update", 0, 0)
    except ApiError:
        return  # op25 unreachable this cycle (e.g. still starting up) -- retry next tick
    if status != 200:
        return

    try:
        responses = json.loads(body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return

    trunk_update = next((r for r in responses if isinstance(r, dict) and r.get("json_type") == "trunk_update"), None)
    call_log = next((r for r in responses if isinstance(r, dict) and r.get("json_type") == "call_log"), None)

    if trunk_update:
        # Keys are positional indices (0, 1, 2...) per configured site, not
        # NAC/sysid -- see tk_p25.py's rx_ctl.to_json(). Only the currently
        # active site will ever have non-empty wuid_data (inactive sites
        # never process real RF), so which index is which doesn't matter here.
        for key, val in trunk_update.items():
            if key in ("json_type", "nac") or not isinstance(val, dict):
                continue
            for entry in val.get("wuid_data", {}).values():
                if not isinstance(entry, dict) or entry.get("time") is None or entry.get("srcaddr") is None:
                    continue
                _ensure_talkgroup_placeholder(conn, tag_set_id, entry.get("aff_ga"))
                conn.execute(
                    """INSERT OR IGNORE INTO subscriber_registrations
                       (trunked_system_id, time, tgid, tgid_tag, source_rid, tag) VALUES (?, ?, ?, ?, ?, ?)""",
                    (site_id, _epoch_to_iso(entry["time"]), entry.get("aff_ga"), entry.get("aff_ga_tag"),
                     entry["srcaddr"], entry.get("tag")),
                )

            # neighbor sites -- upsert (latest-known-state, not a history log;
            # see neighbor_sites table comment in schema.sql)
            for freq, entry in val.get("adjacent_data", {}).items():
                if not isinstance(entry, dict):
                    continue
                conn.execute(
                    """INSERT INTO neighbor_sites
                       (trunked_system_id, freq, uplink, rfid, stid, lra, freq_table, conventional, valid, active, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(trunked_system_id, freq) DO UPDATE SET
                         uplink=excluded.uplink, rfid=excluded.rfid, stid=excluded.stid, lra=excluded.lra,
                         freq_table=excluded.freq_table, conventional=excluded.conventional,
                         valid=excluded.valid, active=excluded.active, last_seen=excluded.last_seen""",
                    (site_id, int(freq), entry.get("uplink"), entry.get("rfid"), entry.get("stid"), entry.get("lra"),
                     entry.get("table"), entry.get("conventional"), entry.get("valid"), entry.get("active"),
                     _epoch_to_iso(time.time())),
                )

    if call_log:
        for entry in call_log.get("log", []):
            if not isinstance(entry, dict) or entry.get("time") is None:
                continue
            _ensure_talkgroup_placeholder(conn, tag_set_id, entry.get("tgid"))
            conn.execute(
                """INSERT OR IGNORE INTO call_history
                   (trunked_system_id, time, freq, slot, prio, tgid, tgtag, rid, rtag)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (site_id, _epoch_to_iso(entry["time"]), entry.get("freq"), entry.get("slot"), entry.get("prio"),
                 entry.get("tgid"), entry.get("tgtag"), entry.get("rid"), entry.get("rtag")),
            )

    conn.commit()


def history_poller():
    while True:
        try:
            conn = db()
            try:
                poll_history_once(conn)
            finally:
                conn.close()
        except Exception as e:
            sys.stderr.write(f"history_poller: unexpected error: {e}\n")
        time.sleep(HISTORY_POLL_INTERVAL)


# -------------------------------------------------------------- analysis --

def tg_activity(conn, qs):
    # Splits the baseline window into "recent" (last `minutes`) and "prior"
    # (the rest of `baseline_minutes`, i.e. the older comparison period) --
    # returns both raw counts rather than a pre-derived rate/ratio, so the
    # caller decides what actually counts as a spike (recent_count vs.
    # prior_count normalized by their respective durations, a fixed
    # threshold, a z-score, whatever fits) instead of this endpoint
    # guessing on their behalf.
    window_min = float(qs.get("minutes", ["5"])[0])
    baseline_min = float(qs.get("baseline_minutes", ["60"])[0])
    recent_cutoff = f"-{window_min} minutes"
    baseline_cutoff = f"-{baseline_min} minutes"
    q = """
    SELECT tgid,
           COUNT(DISTINCT CASE WHEN time > datetime('now', ?) THEN source_rid END) AS recent_count,
           COUNT(DISTINCT CASE WHEN time <= datetime('now', ?) THEN source_rid END) AS prior_count
    FROM subscriber_registrations
    WHERE time > datetime('now', ?)
    GROUP BY tgid
    ORDER BY recent_count DESC
    """
    rows = conn.execute(q, (recent_cutoff, recent_cutoff, baseline_cutoff)).fetchall()
    return [{
        "tgid": r["tgid"],
        "recent_distinct_radios": r["recent_count"],
        "recent_window_minutes": window_min,
        "prior_distinct_radios": r["prior_count"],
        "prior_window_minutes": max(baseline_min - window_min, 0),
    } for r in rows]


def hopping_radios(conn, qs):
    window_min = float(qs.get("minutes", ["60"])[0])
    limit = int(qs.get("limit", ["20"])[0])
    q = """
    SELECT source_rid, COUNT(DISTINCT tgid) AS tg_count, COUNT(*) AS registration_count
    FROM subscriber_registrations
    WHERE time > datetime('now', ?)
    GROUP BY source_rid
    HAVING tg_count > 1
    ORDER BY tg_count DESC, registration_count DESC
    LIMIT ?
    """
    rows = conn.execute(q, (f"-{window_min} minutes", limit)).fetchall()
    return [{
        "source_rid": r["source_rid"],
        "distinct_talkgroups": r["tg_count"],
        "registration_count": r["registration_count"],
        "window_minutes": window_min,
    } for r in rows]


# --------------------------------------------------- raw history feeds --
# What the New UI's Subscriber Registrations / Call History panels read
# from now, instead of polling op25 directly -- config-api's poller is the
# only thing that talks to op25 for this data; everything else (including
# op25's own UI) reads it back out of the DB.

def list_subscriber_registrations(conn, qs):
    window_min = float(qs.get("minutes", ["15"])[0])
    limit = int(qs.get("limit", ["500"])[0])
    q = """
    SELECT sr.*, s.sysname
    FROM subscriber_registrations sr
    JOIN sites s ON sr.trunked_system_id = s.id
    WHERE sr.time > datetime('now', ?)
    ORDER BY sr.time DESC
    LIMIT ?
    """
    rows = conn.execute(q, (f"-{window_min} minutes", limit)).fetchall()
    return rows_to_list(rows)


def list_call_history(conn, qs):
    window_min = float(qs.get("minutes", ["15"])[0])
    limit = int(qs.get("limit", ["500"])[0])
    q = """
    SELECT ch.*, s.sysname
    FROM call_history ch
    JOIN sites s ON ch.trunked_system_id = s.id
    WHERE ch.time > datetime('now', ?)
    ORDER BY ch.time DESC
    LIMIT ?
    """
    rows = conn.execute(q, (f"-{window_min} minutes", limit)).fetchall()
    return rows_to_list(rows)


def list_neighbor_sites(conn, qs):
    # Latest-known-state table, not history -- no time-window filter, unlike
    # list_subscriber_registrations()/list_call_history() above.
    site_id = qs.get("site_id", [None])[0]
    if site_id is None:
        site_id = _active_site_id(conn)
    if site_id is None:
        return []
    rows = conn.execute(
        "SELECT * FROM neighbor_sites WHERE trunked_system_id = ? ORDER BY freq", (int(site_id),)
    ).fetchall()
    return rows_to_list(rows)


def application(environ, start_response):
    path = environ["PATH_INFO"]
    method = environ["REQUEST_METHOD"]

    if path == "/api/export" and method == "GET":
        return export_db(environ, start_response)

    if path == "/api/import" and method == "POST":
        return import_db(environ, start_response)

    if not path.startswith("/api/"):
        return static_file(environ, start_response)

    from urllib.parse import parse_qs
    qs = parse_qs(environ.get("QUERY_STRING", ""))

    body = {}
    if method in ("POST", "PUT"):
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            start_response("400 Bad Request", [("Content-type", "application/json")])
            return [json.dumps({"error": "invalid JSON body"}).encode()]

    # The New UI (served from op25's own origin/port) fetches the history
    # feeds below from config-api's origin -- cross-origin, so every JSON
    # response needs this. No auth on either side currently (matches the
    # existing "trusted LAN/Tailscale only" posture), so a permissive
    # origin is consistent with what's already exposed.
    cors = ("Access-Control-Allow-Origin", "*")

    for m, pattern, handler in COMPILED_ROUTES:
        if m != method:
            continue
        match = pattern.match(path)
        if not match:
            continue
        conn = db()
        try:
            result = handler(conn, match.groupdict(), body, qs)
            out = json.dumps(result).encode()
            start_response("200 OK", [("Content-type", "application/json"), ("Content-Length", str(len(out))), cors])
            return [out]
        except ApiError as e:
            out = json.dumps({"error": e.message}).encode()
            start_response(f"{e.status} Error", [("Content-type", "application/json"), ("Content-Length", str(len(out))), cors])
            return [out]
        except sqlite3.IntegrityError as e:
            out = json.dumps({"error": f"constraint violation: {e}"}).encode()
            start_response("400 Bad Request", [("Content-type", "application/json"), ("Content-Length", str(len(out))), cors])
            return [out]
        except KeyError as e:
            out = json.dumps({"error": f"missing required field: {e}"}).encode()
            start_response("400 Bad Request", [("Content-type", "application/json"), ("Content-Length", str(len(out))), cors])
            return [out]
        except Exception as e:
            # Anything else -- e.g. "no such table" from a DB schema that
            # hasn't caught up yet -- previously fell through uncaught and
            # produced wsgiref's generic "A server error occurred" page with
            # no useful detail. A real error, still a 500, but at least
            # legible instead of opaque.
            sys.stderr.write(f"unhandled error in {method} {path}: {e}\n")
            out = json.dumps({"error": f"internal error: {e}"}).encode()
            start_response("500 Internal Server Error", [("Content-type", "application/json"), ("Content-Length", str(len(out))), cors])
            return [out]
        finally:
            conn.close()

    start_response("404 Not Found", [("Content-type", "application/json"), cors])
    return [json.dumps({"error": "no such route"}).encode()]


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    if not os.path.exists(DB_PATH):
        sys.stderr.write(f"WARNING: {DB_PATH} does not exist yet -- run migrate_json_to_sqlite.py first\n")
    else:
        try:
            ensure_schema()
        except sqlite3.Error as e:
            sys.stderr.write(f"ensure_schema: failed, continuing anyway: {e}\n")
    threading.Thread(target=history_poller, daemon=True).start()
    httpd = make_server("0.0.0.0", LISTEN_PORT, application, server_class=ThreadingWSGIServer)
    sys.stderr.write(f"config-api listening on :{LISTEN_PORT}, db={DB_PATH}, op25={OP25_HTTP_URL}, "
                      f"history_poll_interval={HISTORY_POLL_INTERVAL}s\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
