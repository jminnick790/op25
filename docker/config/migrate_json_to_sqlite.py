#!/usr/bin/env python3
# One-time, run-by-hand migration of the flat-file OP25 config (a multi_rx.json
# plus whatever talkgroups*.tsv/blacklist*.tsv/whitelist*.tsv files it
# references) into the SQLite schema defined in schema.sql. Never modifies or
# deletes the source files.
#
#   cp docker/config/multi_rx.json.example docker/config/multi_rx.json
#   # ...edit multi_rx.json and its referenced .tsv files with your own
#   # systems/sites/talkgroups (see the .example files for the expected
#   # format) -- these are gitignored, so your real data never gets committed...
#   python3 migrate_json_to_sqlite.py --config docker/config/multi_rx.json --db docker/config/op25.db
#
# Every tag_set/access_list this script creates is discovered from the
# config itself (whatever tgid_tags_file/blacklist/whitelist filenames the
# chans[] entries actually reference) -- nothing here is tied to any
# particular set of systems or filenames.
import argparse
import csv
import json
import os
import sqlite3
import sys


def tag_set_name_from_filename(filename):
    # "talkgroups_west_metro.tsv" -> "west_metro", "talkgroups.tsv" -> "talkgroups"
    # -- just a readable slug; rename via the admin UI's Systems tab afterward
    # if you want something friendlier.
    stem = os.path.splitext(filename)[0]
    prefix = "talkgroups_"
    return stem[len(prefix):] if stem.startswith(prefix) else stem


def category_filename_for(tag_set_name):
    return f"tg_categories_{tag_set_name}.tsv"


def load_category_map(path):
    mapping = {}
    if not os.path.exists(path):
        return mapping
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t", 1)
            if len(parts) != 2:
                continue
            try:
                mapping[int(parts[0])] = parts[1]
            except ValueError:
                continue
    return mapping


def load_talkgroups_tsv(path):
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        for lineno, row in enumerate(reader, start=1):
            if not row or not row[0].strip():
                continue
            try:
                tgid = int(row[0].strip())
                name = row[1].strip() if len(row) > 1 else ""
            except (ValueError, IndexError):
                print(f"  WARN {path}:{lineno}: skipping malformed line {row!r}", file=sys.stderr)
                continue
            if not name:
                print(f"  WARN {path}:{lineno}: skipping tgid {tgid} with empty name", file=sys.stderr)
                continue
            rows.append((tgid, name))
    return rows


def load_blacklist_tsv(path):
    entries = []
    with open(path, "r", encoding="utf-8-sig") as f:
        for lineno, line in enumerate(f, start=1):
            v = line.strip()
            if not v:
                continue
            try:
                tgid = int(v)
            except ValueError:
                print(f"  WARN {path}:{lineno}: skipping malformed line {line!r}", file=sys.stderr)
                continue
            entries.append(tgid)
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="docker/config/multi_rx.json")
    ap.add_argument("--schema", default="docker/config/schema.sql")
    ap.add_argument("--tsv-dir", default="docker/config")
    ap.add_argument("--db", default="docker/config/op25.db")
    ap.add_argument("--overwrite", action="store_true", help="allow running against an existing DB file")
    args = ap.parse_args()

    if os.path.exists(args.db):
        if not args.overwrite:
            print(f"ERROR: {args.db} already exists. Pass --overwrite to run anyway.", file=sys.stderr)
            sys.exit(1)
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(args.db + suffix):
                os.remove(args.db + suffix)

    with open(args.config, "r", encoding="utf-8-sig") as f:
        cfg = json.load(f)

    chans = list(cfg.get("trunking", {}).get("chans", []))

    # Discover tag_sets and access_lists from whatever the config's own
    # chans[] entries reference, rather than a hardcoded list of filenames --
    # this makes the script work for any set of systems, not just whichever
    # ones it was originally written against.
    TAG_SET_FILES = {}   # tgid_tags_file -> (tag_set name, description)
    for chan in chans:
        filename = chan.get("tgid_tags_file")
        if filename and filename not in TAG_SET_FILES:
            name = tag_set_name_from_filename(filename)
            TAG_SET_FILES[filename] = (name, f"Imported from {filename}")

    ACCESS_LIST_FILES = {}   # filename -> (access_list name, type)
    for chan in chans:
        for field, list_type in (("blacklist", "blacklist"), ("whitelist", "whitelist")):
            filename = chan.get(field)
            if filename and filename not in ACCESS_LIST_FILES:
                name = os.path.splitext(filename)[0]
                ACCESS_LIST_FILES[filename] = (name, list_type)

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL is a persistent property of the DB file (survives reconnects from
    # other processes) -- set it once here since op25's read-only connections
    # can't change journal mode themselves.
    conn.execute("PRAGMA journal_mode = WAL")
    with open(args.schema, "r") as f:
        conn.executescript(f.read())

    counts = {}

    # --- tag_sets + talkgroups ---
    tag_set_ids = {}   # filename -> tag_set id
    for filename, (name, description) in TAG_SET_FILES.items():
        path = os.path.join(args.tsv_dir, filename)
        cur = conn.execute(
            "INSERT INTO tag_sets (name, description) VALUES (?, ?)", (name, description)
        )
        tag_set_id = cur.lastrowid
        tag_set_ids[filename] = tag_set_id
        rows = load_talkgroups_tsv(path)
        cat_map = load_category_map(os.path.join(args.tsv_dir, category_filename_for(name)))

        # Categories are scoped per tag_set (see schema.sql) -- create one row per
        # distinct category name seen for this tag_set, then resolve tgid->category_id.
        category_ids = {}  # category name -> id, within this tag_set
        for cat_name in sorted(set(cat_map.values())):
            cur2 = conn.execute(
                "INSERT INTO categories (tag_set_id, name) VALUES (?, ?)", (tag_set_id, cat_name)
            )
            category_ids[cat_name] = cur2.lastrowid

        conn.executemany(
            "INSERT INTO talkgroups (tag_set_id, tgid, name, category_id) VALUES (?, ?, ?, ?)",
            [(tag_set_id, tgid, tgname, category_ids.get(cat_map.get(tgid))) for tgid, tgname in rows],
        )
        counts[f"talkgroups[{name}]"] = len(rows)
        counts[f"talkgroups[{name}]_categories"] = len(category_ids)
        counts[f"talkgroups[{name}]_with_category"] = sum(1 for tgid, _ in rows if tgid in cat_map)

    # --- access_lists + access_list_entries ---
    access_list_ids = {}  # filename -> access_list id
    for filename, (name, list_type) in ACCESS_LIST_FILES.items():
        path = os.path.join(args.tsv_dir, filename)
        cur = conn.execute(
            "INSERT INTO access_lists (name, type) VALUES (?, ?)", (name, list_type)
        )
        access_list_id = cur.lastrowid
        access_list_ids[filename] = access_list_id
        tgids = load_blacklist_tsv(path)
        conn.executemany(
            "INSERT INTO access_list_entries (access_list_id, tgid, tgid_end) VALUES (?, ?, NULL)",
            [(access_list_id, tgid) for tgid in tgids],
        )
        counts[f"access_list_entries[{name}]"] = len(tgids)

    # --- systems + sites ---
    # A "system" groups the chans[] entries that share the same
    # (tag_set_id, whitelist_id, blacklist_id) triple -- e.g. every site of
    # one logical network shares a tag_set, another network's sites share a
    # different one.
    group_to_system_id = {}
    for chan in chans:
        tag_set_id = tag_set_ids.get(chan.get("tgid_tags_file", ""))
        whitelist_id = access_list_ids.get(chan.get("whitelist", ""))
        blacklist_id = access_list_ids.get(chan.get("blacklist", ""))
        key = (tag_set_id, whitelist_id, blacklist_id)
        if key in group_to_system_id:
            continue
        sysname = chan["sysname"]
        base_name = sysname.split(" - ")[0].strip() if " - " in sysname else sysname
        name = base_name
        n = 1
        while conn.execute("SELECT 1 FROM systems WHERE name=?", (name,)).fetchone():
            n += 1
            name = f"{base_name} ({n})"
        cur = conn.execute(
            "INSERT INTO systems (name, tag_set_id, whitelist_id, blacklist_id) VALUES (?, ?, ?, ?)",
            (name, tag_set_id, whitelist_id, blacklist_id),
        )
        group_to_system_id[key] = cur.lastrowid
    counts["systems"] = len(group_to_system_id)

    sysname_to_id = {}
    note_count = 0
    for sort_order, chan in enumerate(chans):
        tag_set_id = tag_set_ids.get(chan.get("tgid_tags_file", ""))
        whitelist_id = access_list_ids.get(chan.get("whitelist", ""))
        blacklist_id = access_list_ids.get(chan.get("blacklist", ""))
        system_id = group_to_system_id[(tag_set_id, whitelist_id, blacklist_id)]
        notes = chan.get("#note")
        if notes:
            note_count += 1
        cur = conn.execute(
            """INSERT INTO sites
               (sysname, nac, control_channel_list, tdma_cc, crypt_behavior,
                system_id, notes, sort_order)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                chan["sysname"],
                chan.get("nac", "0x0"),
                chan.get("control_channel_list", ""),
                1 if chan.get("tdma_cc") else 0,
                chan.get("crypt_behavior", 1),
                system_id,
                notes,
                sort_order,
            ),
        )
        sysname_to_id[chan["sysname"]] = cur.lastrowid
    counts["sites"] = len(sysname_to_id)
    counts["sites_with_notes"] = note_count

    # --- devices ---
    device_name_to_id = {}
    for dev in cfg.get("devices", []):
        cur = conn.execute(
            """INSERT INTO devices (name, args, gains, offset, ppm, usable_bw_pct, rate, tunable)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dev["name"],
                dev.get("args", ""),
                dev.get("gains", ""),
                dev.get("offset", 0),
                dev.get("ppm", 0),
                dev.get("usable_bw_pct", 0.85),
                dev.get("rate", 1000000),
                1 if dev.get("tunable", True) else 0,
            ),
        )
        device_name_to_id[dev["name"]] = cur.lastrowid
    counts["devices"] = len(device_name_to_id)

    # --- channels ---
    n_channels = 0
    for ch in cfg.get("channels", []):
        device_id = device_name_to_id.get(ch.get("device"))
        trunking_system_id = sysname_to_id.get(ch.get("trunking_sysname"))
        conn.execute(
            """INSERT INTO channels
               (name, device_id, trunking_system_id, demod_type, destination,
                meta_stream_name, excess_bw, filter_type, if_rate, symbol_rate, enable_analog)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ch.get("name", ""),
                device_id,
                trunking_system_id,
                ch.get("demod_type", "cqpsk"),
                ch.get("destination", ""),
                ch.get("meta_stream_name", ""),
                ch.get("excess_bw", 0.2),
                ch.get("filter_type", "rc"),
                ch.get("if_rate", 24000),
                ch.get("symbol_rate", 4800),
                ch.get("enable_analog", "off"),
            ),
        )
        n_channels += 1
    counts["channels"] = n_channels

    conn.commit()
    conn.close()

    print("\nMigration summary:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print(f"\nWrote {args.db}")


if __name__ == "__main__":
    main()
