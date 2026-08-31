# Dockerized OP25 Streaming

Runs OP25 (`multi_rx.py`) against a remote RTL-SDR (via `rtl_tcp` on a
separate Pi), decoding one or more P25 trunked systems with config managed
through a SQLite-backed admin UI, and streams decoded audio straight to a
browser via OP25's own built-in WebSocket player -- no separate transcoding
pipeline.

```
antenna -> RTL-SDR -> Pi Zero 2W (rtl_tcp) -> LAN -> [op25 container] -> browser
                                                            |
                                              worker (flowgraph)   server (UI/API/SSE)
                                              restarts on Set        never restarts --
                                              Active/DB import       stays connected
                                                     |________push_______^
```

## How the pieces fit together

One container, one image, two supervisord-managed processes (see
`docker/op25/supervisord.conf`) split by restart domain -- config-api used
to be a genuinely separate container; it's now the `server` process below,
folded in so the whole stack is one deployable unit with a single source
of truth instead of two containers coordinating over HTTP:

- **`worker`** (`multi_rx.py`) builds this repo's C++/GNU Radio blocks and
  tunes a *remote* SDR over the network using gr-osmosdr's
  `rtl_tcp=<host>:<port>` device arg -- no USB passthrough, no privileged
  container. It reads its entire config (SDR device, physical channel,
  every site's definition, talkgroup names, white/black lists) from a
  SQLite database at startup. Decoded call audio goes out over OP25's
  built-in raw-PCM WebSocket server, which the New UI's audio player
  connects to directly -- no transcoding/Icecast hop. This is the only
  process ever restarted for a topology change (Set Active, DB import) --
  GNU Radio's flowgraph can't be reconfigured live, so a full restart is
  unavoidable for those, but it's now scoped to just this process instead
  of taking the whole stack down with it.
- **`server`** (stdlib-only Python, no new dependencies) is a persistent
  process -- never restarted for topology changes -- that owns everything
  browser-facing: the New UI's static assets and live SSE state stream
  (`GET /events`), the admin UI + REST CRUD for systems (logical networks
  like "NC VIPER"), sites (individual physical radio sites), talkgroups,
  categories, devices/channels, and white/blacklists, plus subscriber
  registration/call history capture. It relays browser commands (tune,
  hold, lockout, reload) to `worker` over a loopback-only internal port,
  and `worker` pushes live state back to `server` the same way (a small
  internal port `server` alone can reach) -- no cross-container polling
  either direction. "Set Active" (and DB import) restart `worker` via
  supervisord's loopback-only control interface, not the Docker socket --
  `server` has no Docker-level access to the host at all.

Both processes share the config DB through a named Docker volume
(`op25-config-db`), not a bind mount -- see `docker/config/schema.sql` for
the schema.

## 1. Pi-side setup (not built by this repo, do this separately)

On the Pi Zero 2W near the antenna, install `rtl-sdr` tools and run
`rtl_tcp` as a systemd service bound to the LAN interface:

```
sudo apt-get install rtl-sdr
```

`/etc/systemd/system/rtl_tcp.service`:

```ini
[Unit]
Description=rtl_tcp SDR network server
After=network-online.target
Requires=network-online.target

[Service]
# -a 0.0.0.0 binds all interfaces; narrow this to the Pi's LAN IP if you
# want to restrict who can reach it.
ExecStart=/usr/bin/rtl_tcp -a 0.0.0.0 -p 1234
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now rtl_tcp
```

Verify from the Docker host: `nc -zv <pi-ip> 1234` should connect.

## 2. Configure the Docker host

```
cp .env.example .env
```

`.env` only needs `LOG_VERBOSITY` and the host ports
(`OP25_HTTP_HOST_PORT`, `OP25_WS_AUDIO_HOST_PORT`, `CONFIG_API_HOST_PORT`)
-- the SDR device (host/port/gain/ppm), system/site definitions,
talkgroups, and which site is active all live in the SQLite DB now, set
through the admin UI rather than env vars or bind-mounted files.

## 3. Get a config DB in place

Both the `worker` and `server` processes expect `/data/op25.db` to exist
in the `op25-config-db` named volume. Two ways to get one there:

- **First-time setup**: either add your first system/site/talkgroups by
  hand through the admin UI once it's up (an empty DB is fine to start
  `docker compose up` against -- `op25` will just have nothing to decode
  until a site + channel exist), or seed a DB from a bulk config in one
  shot: copy `docker/config/multi_rx.json.example` to `multi_rx.json` in
  that same directory (and its referenced `.tsv.example` files the same
  way, e.g. `talkgroups_primary.tsv.example` -> `talkgroups_primary.tsv`),
  fill in your own systems/sites/talkgroups, then run
  `docker/config/migrate_json_to_sqlite.py` against it (see the script's
  docstring). Those real files are gitignored -- only the `.example`
  templates are tracked -- so your own system/talkgroup data never ends up
  committed.
- **Moving from another deployment**: use that deployment's admin UI
  "Export Config" button to download its `op25.db`, then:
  ```
  docker cp op25-config-<timestamp>.db op25:/data/op25.db
  docker compose restart op25
  ```

## 4. Bring the stack up

```
docker compose build
docker compose up -d
```

First build compiles OP25's C++ blocks against GNU Radio -- expect several
minutes. Watch `docker compose logs -f op25` for `sync established` /
talkgroup activity once a call comes in. If `/data/op25.db` doesn't exist
yet, `op25` will crash-loop until you've put one there (step 3) -- that's
expected, not a bug.

## 5. Use it

- **Admin UI** (manage systems, sites, talkgroups, groups, white/blacklists,
  reorder/search/sort, TSV/CSV bulk import for sites and talkgroups, export
  config, switch which site is active -- responsive down to phone-sized
  screens): `http://<docker-host>:8091` (`CONFIG_API_HOST_PORT`).
- **New UI** (live channel status, call log, low-latency WebSocket audio
  player): `http://<docker-host>:8080` (`OP25_HTTP_HOST_PORT`).

Both are LAN/Tailscale-only by default -- don't forward either port on a
public router. Layer Tailscale on top and use the host's Tailscale
IP/hostname instead of the LAN IP; nothing in the stack needs to change
for that.

## Troubleshooting

- **`op25` crash-loops with a SQLite "unable to open database file"
  error**: no DB in the named volume yet -- see step 3.
- **`op25` can't reach the SDR**: confirm `rtl_tcp` is running on the Pi
  and reachable from the Docker host (`nc -zv <pi-ip> <port>`); the Docker
  host and Pi need to be on the same LAN (or otherwise routed).
- **Edited a talkgroup/list in the admin UI but don't see it change**:
  click "Apply" on that site's row (only does something if that site
  is the currently *active* one), then wait for that talkgroup to
  transmit again -- the New UI's tag display is a client-side cache
  seeded from live call events, not a static list, so it won't show a
  renamed tag until the talkgroup keys up again after your edit.
- **Choppy/garbled audio**: usually an RF/SDR gain issue on the Pi side,
  or the device's sample rate too high for the Pi's WiFi link to
  `rtl_tcp` clients -- adjust via the device's `rate`/`gains` fields on
  the admin UI's Devices tab, then restart `op25` to apply.

## Automatic site roaming

For mobile/vehicle use, a system can hand itself off between sites live,
without restarting `op25` -- a dedicated **scout** channel (a second SDR)
continuously evaluates neighbor sites in the background, and the primary
receiver retargets to a better one the moment the active site's voice
quality degrades or its control channel goes stale. This is separate from
manually clicking "Set Active" on the Sites tab, which still restarts
`op25` (see below) -- roaming's handoff never does.

To turn it on: give a system's row on the Systems tab a checked
**Roaming** box (optionally a **Stale (s)** override), and add a second
channel on the Devices tab with **Role** set to `scout` bound to a second
SDR device. Roam decisions (scout attempts, rejections, commits) are
logged and queryable via `GET /api/roam_events`.

**Known limitation: roaming candidates are fixed at worker startup, not
live.** `op25` builds its neighbor-resolution map once when it starts and
never updates it while running. Neighbor sites *are* auto-discovered in
real time as they're observed via a system's own adjacent-site broadcasts
-- they show up on the Sites tab right away -- but a newly-discovered site
only becomes an actual roaming *candidate* after the next `op25` restart
(see `docker/config/schema.sql`'s comments).

In practice this means: driving through territory that was never
configured or previously observed, `op25` can only ever hand off to sites
it already knew about (with `rfid`/`stid` populated) as of its last
restart -- it can't chain through a second or third tier of sites that are
only discovered *during* the same drive, because each new tier would need
its own restart to become usable, and restarting mid-drive isn't
practical. So on a genuinely long trip -- several counties beyond where
you started -- expect roaming to carry you through the first couple of
hops and then stop, once you run out of sites it already knew about.

Two ways to work around this today: **pre-seed the route** before a long
trip -- bulk-import (see the Admin UI bullet above) or hand-enter the
sites you expect to pass through, including their `rfid`/`stid` if you
know them (the site editor accepts these directly, no need to have
observed them first) --
or **do a scouting pass**: drive the route once with roaming on to let
auto-discovery populate the Sites tab for the whole area, then restart
`op25` once before the trip that actually matters, so every site along
the way is already a live candidate. Making the neighbor map refresh
without a restart is a real possible improvement, just not implemented
yet -- restarting is the one thing this project's worker/server split
was specifically built to make cheap for exactly this class of change,
so it's more an inconvenience than a redesign away from being fixed
someday, not a fundamental limit.

**Only one SDR?** Roaming still works with a single dongle -- if a
`roaming_enabled` system has no scout channel configured at all, it
automatically falls back to a single-dongle mode instead of doing nothing.
No separate setting for this; it's purely inferred from whether a scout
channel exists. The tradeoff is real: with no second receiver to evaluate
a candidate silently, the one receiver you have retargets directly onto
the best neighbor for a few seconds to prove it out -- a genuine, audible
gap in live audio, unlike scout mode's make-before-break handoff. If that
candidate doesn't pan out, it retargets straight back to whatever it was
on before trying, rather than hunting through every configured
alternative; if the site it started on is *also* not decoding, it gives
up for a short cooldown and lets the receiver's normal control-channel
retry take over. Same `roam_events` log either way (tagged
`single_dongle` in the `detail` field so the two modes are distinguishable
after the fact).

## Out of scope for now

Tailscale setup itself, recording, and MQTT/Home Assistant integration are
deliberately left out. Manually switching the active site via the admin UI
still restarts `op25` -- GNU Radio's flowgraph can't be reconfigured live
for a change like that -- automatic roaming (above) is the no-restart path.
