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

-- trunking.chans[] equivalent.
CREATE TABLE trunked_systems (
    id                    INTEGER PRIMARY KEY,
    sysname               TEXT NOT NULL UNIQUE,
    nac                   TEXT NOT NULL DEFAULT '0x0',
    control_channel_list  TEXT NOT NULL,
    tdma_cc               INTEGER NOT NULL DEFAULT 0 CHECK (tdma_cc IN (0,1)),
    crypt_behavior        INTEGER NOT NULL DEFAULT 1,
    tag_set_id            INTEGER REFERENCES tag_sets(id) ON DELETE SET NULL,
    whitelist_id          INTEGER REFERENCES access_lists(id) ON DELETE SET NULL,
    blacklist_id          INTEGER REFERENCES access_lists(id) ON DELETE SET NULL,
    notes                 TEXT,   -- carries the existing "#note" field content verbatim
    sort_order            INTEGER NOT NULL DEFAULT 0,   -- user-defined display order (config-api drag-to-reorder)
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX idx_trunked_systems_tag_set ON trunked_systems(tag_set_id);
CREATE INDEX idx_trunked_systems_sort_order ON trunked_systems(sort_order);

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
    trunking_system_id   INTEGER REFERENCES trunked_systems(id) ON DELETE SET NULL,  -- the field "Set Active" mutates
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

PRAGMA user_version = 1;
