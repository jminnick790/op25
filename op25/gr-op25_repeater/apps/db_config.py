# SQLite-backed config loading for multi_rx.py / tk_p25.py, replacing the
# flat-file (multi_rx.json / talkgroups.tsv / blacklist.tsv) config path.
# build_config_from_db() returns a dict shaped identically to what
# json.loads() produces from multi_rx.json today, so rx_block and every
# other downstream consumer needs no changes. load_talkgroups() and
# load_access_list() are drop-in replacements for tk_p25.read_tags_file()
# and helper_funcs.get_int_dict(), used both at startup and by
# p25_system.reload_from_db() for live refresh.
import sqlite3

TGID_DEFAULT_PRIO = 3    # kept in sync with tk_p25.TGID_DEFAULT_PRIO


def _connect(db_path, read_only=True):
    # journal_mode=WAL is a persistent property of the DB file, set once at
    # migration time (see migrate_json_to_sqlite.py) -- a read-only
    # connection cannot change it (and doesn't need to; it just benefits
    # from WAL's concurrent-reader-friendly locking once it's set).
    mode = "ro" if read_only else "rw"
    conn = sqlite3.connect("file:%s?mode=%s" % (db_path, mode), uri=True)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _site_row_to_chan_config(row, db_path):
    # Shared by build_config_from_db() (every site, at worker startup) and
    # build_single_site_config() (one site, live -- see tk_p25.py's
    # rx_ctl.add_site()) so the per-site dict shape p25_system.__init__
    # expects can't silently drift apart between the two call sites.
    return {
        "sysname": row["sysname"],
        "nac": row["nac"],
        "control_channel_list": row["control_channel_list"],
        "tdma_cc": bool(row["tdma_cc"]),
        "crypt_behavior": row["crypt_behavior"],
        "whitelist": "",
        "blacklist": "",
        "tgid_tags_file": "",
        "rid_tags_file": "",
        # rfid/stid (P25's own site identity) and the logical-group
        # system_id (distinct from _db_system_id below, which is
        # actually this SITE's own row id) -- roaming's neighbor
        # matcher builds a (system_id, rfid, stid) -> p25_system map
        # from these at startup (tk_p25.py's rx_ctl.__init__()).
        "rfid": row["rfid"],
        "stid": row["stid"],
        "system_id": row["system_id"],
        # The admin's own drag-to-reorder preference for this site (sites
        # table, config-api's Sites tab) -- used as a priority hint by
        # roaming's cold-start candidate ordering (tk_p25.py's rx_ctl.
        # _resolve_same_system_candidates()), which otherwise has no live
        # signal to prioritize candidates by.
        "sort_order": row["sort_order"],
        # roaming_enabled/roaming_stale_seconds are SYSTEM-level
        # (systems table), joined in above -- LEFT JOIN means a site
        # with no system_id gets NULL/None here, which p25_system
        # treats as "roaming off" (falsy) same as an explicit 0.
        "roaming_enabled": bool(row["roaming_enabled"]),
        "roaming_stale_seconds": row["roaming_stale_seconds"],
        "_db_path": db_path,
        "_db_system_id": row["id"],
        "_db_tag_set_id": row["tag_set_id"],
        "_db_whitelist_id": row["whitelist_id"],
        "_db_blacklist_id": row["blacklist_id"],
    }


def build_single_site_config(db_path, site_id):
    # Live counterpart to build_config_from_db()'s per-site query, scoped to
    # one row -- used by multi_rx.py's "add_site" worker command so a
    # brand-new site discovered mid-session (see app.py's
    # _ensure_neighbor_site_placeholder()) can get a p25_system constructed
    # without rebuilding every other already-running site's config too.
    # Returns None if the id doesn't exist (already deleted, or a stale/
    # duplicate notification), which the caller treats as a no-op.
    conn = _connect(db_path)
    try:
        row = conn.execute("""
            SELECT s.*, sy.tag_set_id, sy.whitelist_id, sy.blacklist_id,
                   sy.roaming_enabled, sy.roaming_stale_seconds
            FROM sites s LEFT JOIN systems sy ON s.system_id = sy.id
            WHERE s.id = ?
        """, (site_id,)).fetchone()
        return None if row is None else _site_row_to_chan_config(row, db_path)
    finally:
        conn.close()


def build_config_from_db(db_path):
    conn = _connect(db_path)
    try:
        devices = []
        for row in conn.execute("SELECT * FROM devices"):
            devices.append({
                "name": row["name"],
                "args": row["args"],
                "gains": row["gains"],
                "offset": row["offset"],
                "ppm": row["ppm"],
                "usable_bw_pct": row["usable_bw_pct"],
                "rate": row["rate"],
                "tunable": bool(row["tunable"]),
            })

        chans = []
        sys_id_to_sysname = {}
        for row in conn.execute("""
            SELECT s.*, sy.tag_set_id, sy.whitelist_id, sy.blacklist_id,
                   sy.roaming_enabled, sy.roaming_stale_seconds
            FROM sites s LEFT JOIN systems sy ON s.system_id = sy.id
        """):
            sys_id_to_sysname[row["id"]] = row["sysname"]
            chans.append(_site_row_to_chan_config(row, db_path))

        channels = []
        device_rows = list(conn.execute(
            """SELECT c.*, d.name AS device_name
               FROM channels c JOIN devices d ON c.device_id = d.id"""
        ))
        # A scout channel has no static trunking_system_id (roaming points
        # it dynamically -- see tk_p25.py) but still needs SOME resolvable
        # sysname at startup, since add_receiver() requires one to construct
        # a real p25_receiver at all. Bind it initially to wherever the
        # primary channel starts.
        primary_sysname = "undefined"
        for row in device_rows:
            if row["role"] == "primary" and row["trunking_system_id"] in sys_id_to_sysname:
                primary_sysname = sys_id_to_sysname[row["trunking_system_id"]]
                break
        for row in device_rows:
            if row["trunking_system_id"] in sys_id_to_sysname:
                sysname = sys_id_to_sysname[row["trunking_system_id"]]
            elif row["role"] == "scout":
                sysname = primary_sysname
            else:
                sysname = "undefined"
            channels.append({
                "name": row["name"],
                "device": row["device_name"],
                "trunking_sysname": sysname,
                "role": row["role"],
                "demod_type": row["demod_type"],
                "destination": row["destination"],
                "meta_stream_name": row["meta_stream_name"],
                "excess_bw": row["excess_bw"],
                "filter_type": row["filter_type"],
                "if_rate": row["if_rate"],
                "plot": "",
                "symbol_rate": row["symbol_rate"],
                "enable_analog": row["enable_analog"],
                "whitelist": "",
                "blacklist": "",
                "crypt_keys": "",
            })

        return {
            "channels": channels,
            "devices": devices,
            "trunking": {"module": "tk_p25.py", "chans": chans},
            # Not part of the DB-editable CRUD surface (not requested) --
            # hardcoded to match the values multi_rx.json has always used.
            # terminal_type is loopback-only and on a non-public port: the
            # "server" process (docker/config-api/app.py) is what browsers
            # actually talk to now, relaying commands to this port -- see
            # http_server.py's module docstring for the split rationale.
            "terminal": {
                "module": "terminal.py",
                "terminal_type": "http:127.0.0.1:8082",
                "curses_plot_interval": 0.1,
                "http_plot_interval": 1.0,
                "http_plot_directory": "../www/images",
                "tuning_step_large": 1200,
                "tuning_step_small": 100,
            },
        }
    finally:
        conn.close()


def load_talkgroups(db_path, tag_set_id):
    tgs = {}
    if tag_set_id is None:
        return tgs
    conn = _connect(db_path)
    try:
        for row in conn.execute(
            "SELECT tgid, name, priority FROM talkgroups WHERE tag_set_id = ?", (tag_set_id,)
        ):
            tgid = row["tgid"]
            tgs[tgid] = {
                "counter": 0,
                "tgid": tgid,
                "prio": row["priority"] if row["priority"] is not None else TGID_DEFAULT_PRIO,
                "tag": row["name"],
                "srcaddr": 0,
                "time": 0,
                "frequency": None,
                "tdma_slot": None,
                "encrypted": 0,
                "svcopts": 0x04,
                "algid": -1,
                "keyid": -1,
                "receiver": None,
            }
        return tgs
    finally:
        conn.close()


def load_access_list(db_path, access_list_id):
    d = {}
    if access_list_id is None:
        return d
    conn = _connect(db_path)
    try:
        for row in conn.execute(
            "SELECT tgid, tgid_end FROM access_list_entries WHERE access_list_id = ?", (access_list_id,)
        ):
            v0 = row["tgid"]
            v1 = row["tgid_end"] if row["tgid_end"] is not None and row["tgid_end"] > v0 else v0
            for tg in range(v0, v1 + 1):
                d[tg] = None
        return dict.fromkeys(d)
    finally:
        conn.close()
