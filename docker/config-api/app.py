#!/usr/bin/env python3
# Sidecar CRUD API + minimal admin UI for OP25's SQLite-backed config
# (see docker/config/schema.sql). Stdlib only -- hand-rolled WSGI routing,
# same convention as op25/gr-op25_repeater/apps/http_server.py.
import datetime
import http.client
import json
import os
import re
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from wsgiref.simple_server import make_server, WSGIServer
from socketserver import ThreadingMixIn

DB_PATH = os.environ.get("OP25_DB_PATH", "/data/op25.db")
OP25_HTTP_URL = os.environ.get("OP25_HTTP_URL", "http://op25:8080/")
OP25_CONTAINER_NAME = os.environ.get("OP25_CONTAINER_NAME", "op25")
LISTEN_PORT = int(os.environ.get("CONFIG_API_PORT", "8091"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def rows_to_list(rows):
    return [dict(r) for r in rows]


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


# ---------------------------------------------------------------- systems --

def list_systems(conn):
    q = """
    SELECT ts.*, tset.name AS tag_set_name,
           wl.name AS whitelist_name, bl.name AS blacklist_name,
           EXISTS(SELECT 1 FROM channels c WHERE c.trunking_system_id = ts.id) AS active
    FROM trunked_systems ts
    LEFT JOIN tag_sets tset ON ts.tag_set_id = tset.id
    LEFT JOIN access_lists wl ON ts.whitelist_id = wl.id
    LEFT JOIN access_lists bl ON ts.blacklist_id = bl.id
    ORDER BY ts.sort_order, ts.sysname
    """
    return rows_to_list(conn.execute(q))


def reorder_systems(conn, body):
    order = body.get("order", [])
    if not order:
        raise ApiError(400, "missing 'order' array of system ids")
    for i, sid in enumerate(order):
        conn.execute("UPDATE trunked_systems SET sort_order = ? WHERE id = ?", (i, sid))
    conn.commit()
    return {"status": "reordered"}


def get_system(conn, sid):
    row = conn.execute("SELECT * FROM trunked_systems WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise ApiError(404, "system not found")
    return dict(row)


def create_system(conn, body):
    next_order = conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM trunked_systems").fetchone()[0]
    cur = conn.execute(
        """INSERT INTO trunked_systems
           (sysname, nac, control_channel_list, tdma_cc, crypt_behavior,
            tag_set_id, whitelist_id, blacklist_id, notes, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            body["sysname"],
            body.get("nac", "0x0"),
            body.get("control_channel_list", ""),
            1 if body.get("tdma_cc") else 0,
            body.get("crypt_behavior", 1),
            body.get("tag_set_id"),
            body.get("whitelist_id"),
            body.get("blacklist_id"),
            body.get("notes"),
            next_order,
        ),
    )
    conn.commit()
    return get_system(conn, cur.lastrowid)


def update_system(conn, sid, body):
    get_system(conn, sid)  # 404 if missing
    fields = ["sysname", "nac", "control_channel_list", "tdma_cc", "crypt_behavior",
              "tag_set_id", "whitelist_id", "blacklist_id", "notes"]
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
        conn.execute(f"UPDATE trunked_systems SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
    return get_system(conn, sid)


def delete_system(conn, sid):
    get_system(conn, sid)
    conn.execute("DELETE FROM trunked_systems WHERE id = ?", (sid,))
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
    SELECT c.*, d.name AS device_name, ts.sysname AS trunking_sysname
    FROM channels c
    JOIN devices d ON c.device_id = d.id
    LEFT JOIN trunked_systems ts ON c.trunking_system_id = ts.id
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
    get_system(conn, sid)  # 404 if missing
    # MVP limitation: arg2 (msgq_id) is hardcoded to 0, since this
    # deployment has exactly one channel. See docker/config/schema.sql /
    # the plan doc for the multi-channel follow-up.
    status, _ = op25_send_command("reload", 0, 0)
    return {"status": "reload sent", "op25_status": status}


class DockerSocketConnection(http.client.HTTPConnection):
    """Talk to the Docker Engine API over its unix socket, stdlib only."""
    def __init__(self, sock_path="/var/run/docker.sock"):
        super().__init__("localhost")
        self.sock_path = sock_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)


def docker_restart(container_name):
    conn = DockerSocketConnection()
    try:
        conn.request("POST", f"/containers/{container_name}/restart?t=5")
        resp = conn.getresponse()
        resp.read()
        if resp.status not in (204, 304):
            raise ApiError(502, f"docker restart failed: HTTP {resp.status}")
        return resp.status
    except (OSError, http.client.HTTPException) as e:
        raise ApiError(502, f"could not reach docker socket: {e}")
    finally:
        conn.close()


def activate_system(conn, sid):
    get_system(conn, sid)  # 404 if missing
    channel = conn.execute("SELECT id FROM channels LIMIT 1").fetchone()
    if channel is None:
        raise ApiError(409, "no channel defined to activate this system on")
    conn.execute("UPDATE channels SET trunking_system_id = ? WHERE id = ?", (sid, channel["id"]))
    conn.commit()
    status = docker_restart(OP25_CONTAINER_NAME)
    return {"status": "activated, op25 restarting", "docker_status": status}


# ------------------------------------------------------------------ routing --

ROUTES = [
    ("GET", r"^/api/systems$", lambda conn, m, body, qs: list_systems(conn)),
    ("POST", r"^/api/systems$", lambda conn, m, body, qs: create_system(conn, body)),
    ("POST", r"^/api/systems/reorder$", lambda conn, m, body, qs: reorder_systems(conn, body)),
    ("GET", r"^/api/systems/(?P<id>\d+)$", lambda conn, m, body, qs: get_system(conn, int(m["id"]))),
    ("PUT", r"^/api/systems/(?P<id>\d+)$", lambda conn, m, body, qs: update_system(conn, int(m["id"]), body)),
    ("DELETE", r"^/api/systems/(?P<id>\d+)$", lambda conn, m, body, qs: delete_system(conn, int(m["id"]))),
    ("POST", r"^/api/systems/(?P<id>\d+)/apply_reload$", lambda conn, m, body, qs: apply_reload(conn, int(m["id"]))),
    ("POST", r"^/api/systems/(?P<id>\d+)/activate$", lambda conn, m, body, qs: activate_system(conn, int(m["id"]))),

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


def export_db(environ, start_response):
    # Whole-file download of the live SQLite DB -- this is the entire config
    # (systems, talkgroups, categories, access lists, devices, channels) in
    # one portable, self-contained file. Import elsewhere is just placing it
    # at the target deployment's DB path (see README/deploy notes) -- no
    # separate import endpoint needed.
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


def application(environ, start_response):
    path = environ["PATH_INFO"]
    method = environ["REQUEST_METHOD"]

    if path == "/api/export" and method == "GET":
        return export_db(environ, start_response)

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
            start_response("200 OK", [("Content-type", "application/json"), ("Content-Length", str(len(out)))])
            return [out]
        except ApiError as e:
            out = json.dumps({"error": e.message}).encode()
            start_response(f"{e.status} Error", [("Content-type", "application/json"), ("Content-Length", str(len(out)))])
            return [out]
        except sqlite3.IntegrityError as e:
            out = json.dumps({"error": f"constraint violation: {e}"}).encode()
            start_response("400 Bad Request", [("Content-type", "application/json"), ("Content-Length", str(len(out)))])
            return [out]
        except KeyError as e:
            out = json.dumps({"error": f"missing required field: {e}"}).encode()
            start_response("400 Bad Request", [("Content-type", "application/json"), ("Content-Length", str(len(out)))])
            return [out]
        finally:
            conn.close()

    start_response("404 Not Found", [("Content-type", "application/json")])
    return [json.dumps({"error": "no such route"}).encode()]


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def main():
    if not os.path.exists(DB_PATH):
        sys.stderr.write(f"WARNING: {DB_PATH} does not exist yet -- run migrate_json_to_sqlite.py first\n")
    httpd = make_server("0.0.0.0", LISTEN_PORT, application, server_class=ThreadingWSGIServer)
    sys.stderr.write(f"config-api listening on :{LISTEN_PORT}, db={DB_PATH}, op25={OP25_HTTP_URL}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
