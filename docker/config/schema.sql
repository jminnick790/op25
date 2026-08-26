PRAGMA foreign_keys = ON;

-- Named, non-colliding TGID namespaces (NC VIPER and Charlotte UASI each
-- allocate TGIDs independently in the same numeric range today via two
-- separate .tsv files -- this is that same split, in DB form).
CREATE TABLE tag_sets (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Categories are RadioReference-style groupings (e.g. "Mecklenburg County
-- Fire/EMS"), scoped per tag_set since the same category name in two
-- different systems' TGID spaces (e.g. NC VIPER vs Charlotte UASI) are
-- unrelated groupings, not the same thing. Renaming a category here
-- updates every talkgroup that references it in one edit.
CREATE TABLE categories (
    id          INTEGER PRIMARY KEY,
    tag_set_id  INTEGER NOT NULL REFERENCES tag_sets(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (tag_set_id, name)
);
CREATE INDEX idx_categories_tag_set ON categories(tag_set_id);

CREATE TABLE talkgroups (
    id          INTEGER PRIMARY KEY,
    tag_set_id  INTEGER NOT NULL REFERENCES tag_sets(id) ON DELETE CASCADE,
    tgid        INTEGER NOT NULL,
    name        TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    priority    INTEGER,          -- NULL = app applies TGID_DEFAULT_PRIO at load time (mirrors add_default_tgid())
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (tag_set_id, tgid)
);
CREATE INDEX idx_talkgroups_tag_set ON talkgroups(tag_set_id);
CREATE INDEX idx_talkgroups_category ON talkgroups(category_id);

-- Whitelist/blacklist; entries mirror helper_funcs.get_int_dict()'s
-- tgid/tgid_end range semantics exactly.
CREATE TABLE access_lists (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL CHECK (type IN ('whitelist','blacklist')),
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE access_list_entries (
    id              INTEGER PRIMARY KEY,
    access_list_id  INTEGER NOT NULL REFERENCES access_lists(id) ON DELETE CASCADE,
    tgid            INTEGER NOT NULL,
    tgid_end        INTEGER,      -- NULL = single-tgid entry
    notes           TEXT,
    CHECK (tgid_end IS NULL OR tgid_end >= tgid)
);
CREATE INDEX idx_access_list_entries_list ON access_list_entries(access_list_id);

-- A logical network (e.g. "NC VIPER", "Charlotte UASI") -- the things that
-- apply across every site of that network, not to one tower.
CREATE TABLE systems (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    tag_set_id   INTEGER REFERENCES tag_sets(id) ON DELETE SET NULL,
    whitelist_id INTEGER REFERENCES access_lists(id) ON DELETE SET NULL,
    blacklist_id INTEGER REFERENCES access_lists(id) ON DELETE SET NULL,
    notes        TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- trunking.chans[] equivalent -- one physical radio site. Several sites can
-- belong to the same logical system (system_id) -- e.g. VIPER's Anderson
-- Mountain/Huntersville/Lincolnton are three sites of one system, which is
-- why NAC isn't unique across them. sysname is kept as-is (not renamed to
-- "site_name") since op25's own New UI (main.js) consumes it as a bare,
-- unaliased JSON column from /api/subscriber_registrations and
-- /api/call_history.
CREATE TABLE sites (
    id                    INTEGER PRIMARY KEY,
    system_id             INTEGER REFERENCES systems(id) ON DELETE SET NULL,
    sysname               TEXT NOT NULL UNIQUE,
    nac                   TEXT NOT NULL DEFAULT '0x0',
    control_channel_list  TEXT NOT NULL,
    tdma_cc               INTEGER NOT NULL DEFAULT 0 CHECK (tdma_cc IN (0,1)),
    crypt_behavior        INTEGER NOT NULL DEFAULT 1,
    notes                 TEXT,   -- carries the existing "#note" field content verbatim
    sort_order            INTEGER NOT NULL DEFAULT 0,   -- user-defined display order (config-api drag-to-reorder)
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_sites_system ON sites(system_id);
CREATE INDEX idx_sites_sort_order ON sites(sort_order);

CREATE TABLE devices (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    args           TEXT NOT NULL,
    gains          TEXT NOT NULL DEFAULT '',
    offset         INTEGER NOT NULL DEFAULT 0,
    ppm            INTEGER NOT NULL DEFAULT 0,
    usable_bw_pct  REAL NOT NULL DEFAULT 0.85,
    rate           INTEGER NOT NULL DEFAULT 1000000,
    tunable        INTEGER NOT NULL DEFAULT 1 CHECK (tunable IN (0,1))
);

CREATE TABLE channels (
    id                   INTEGER PRIMARY KEY,
    name                 TEXT NOT NULL,
    device_id            INTEGER NOT NULL REFERENCES devices(id) ON DELETE RESTRICT,
    trunking_system_id   INTEGER REFERENCES sites(id) ON DELETE SET NULL,  -- the field "Set Active" mutates
    demod_type           TEXT NOT NULL DEFAULT 'cqpsk',
    destination          TEXT NOT NULL,
    meta_stream_name     TEXT NOT NULL DEFAULT '',
    excess_bw            REAL NOT NULL DEFAULT 0.2,
    filter_type          TEXT NOT NULL DEFAULT 'rc',
    if_rate              INTEGER NOT NULL DEFAULT 24000,
    symbol_rate          INTEGER NOT NULL DEFAULT 4800,
    enable_analog        TEXT NOT NULL DEFAULT 'off'
);
CREATE INDEX idx_channels_device ON channels(device_id);
CREATE INDEX idx_channels_trunking_system ON channels(trunking_system_id);

-- Both logged by config-api polling op25's existing 'update' command (see
-- app.py's history_poller()), not by op25 itself -- op25's own in-memory
-- state (registered_wuids / call_log) is where this data originates, but
-- it's ephemeral there (call_log is a small ring buffer, registrations
-- expire per TIA-102.AABD). Tagged with whichever system is active at
-- poll time -- registrations/calls only ever populate for the system
-- actually receiving RF, so there's no ambiguity despite NAC not being
-- unique across sites (e.g. VIPER's Anderson Mountain/Huntersville/
-- Lincolnton all share NAC 0x1f0).
CREATE TABLE subscriber_registrations (
    id                  INTEGER PRIMARY KEY,
    trunked_system_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    time                TEXT NOT NULL,
    tgid                INTEGER,
    tgid_tag            TEXT,   -- op25's own resolved talkgroup tag at registration time (aff_ga_tag) --
                                 -- denormalized like call_history.tgtag, so history reflects what a
                                 -- talkgroup was called at the time, not retroactively renamed later
    source_rid          INTEGER NOT NULL,
    tag                 TEXT,   -- op25's per-radio tag, if any (usually empty)
    UNIQUE (trunked_system_id, source_rid, time)
);
CREATE INDEX idx_subreg_time ON subscriber_registrations(time);
CREATE INDEX idx_subreg_tgid ON subscriber_registrations(trunked_system_id, tgid, time);
CREATE INDEX idx_subreg_rid ON subscriber_registrations(trunked_system_id, source_rid, time);

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
);
CREATE INDEX idx_callhist_time ON call_history(time);
CREATE INDEX idx_callhist_tgid ON call_history(trunked_system_id, tgid, time);
CREATE INDEX idx_callhist_rid ON call_history(trunked_system_id, rid, time);

-- Latest-known-state table (upsert, not an append-only log like the two
-- above) -- op25 rebroadcasts its neighbor list continuously on the
-- control channel, so we want current neighbors with a freshness
-- timestamp, not a growing history of every rebroadcast. Populated from
-- p25_system.adjacent_data (tk_p25.py), itself decoded from P25's
-- "adjacent status" TSBKs (opcodes 0x3c/0xfc/0xfe). Foundation for
-- eventual site-roaming: resolving a neighbor to one of this DB's own
-- sites rows (and thus which system it belongs to) is deliberately not
-- done here (see roaming plan).
CREATE TABLE neighbor_sites (
    id                  INTEGER PRIMARY KEY,
    trunked_system_id   INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    freq                INTEGER NOT NULL,   -- downlink/control channel Hz (channel_id_to_frequency() output --
                                             -- NOTE: sites.control_channel_list stores decimal-MHz
                                             -- text, not Hz; unit conversion is the roaming phase's job)
    uplink              INTEGER,            -- Hz, subscriber TX freq (freq + repeater offset)
    rfid                INTEGER,
    stid                INTEGER,
    lra                 INTEGER,
    freq_table          INTEGER,
    conventional        INTEGER,
    valid               INTEGER,
    active              INTEGER,
    last_seen           TEXT NOT NULL,
    UNIQUE (trunked_system_id, freq)
);
CREATE INDEX idx_neighbor_sites_system ON neighbor_sites(trunked_system_id);

PRAGMA user_version = 4;
