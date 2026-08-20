# Dockerized OP25 Streaming

Runs OP25 (`multi_rx.py`) against a remote RTL-SDR (via `rtl_tcp` on a
separate Pi), decoding one or more P25 trunked systems with config managed
through a SQLite-backed admin UI, and streams decoded audio straight to a
browser via OP25's own built-in WebSocket player -- no separate transcoding
pipeline.

```
antenna -> RTL-SDR -> Pi Zero 2W (rtl_tcp) -> LAN -> [op25] -> browser (New UI, WS audio)
                                                          |
                                                     [config-api] -> browser (admin UI)
```

## How the pieces fit together

- **`op25`** container builds this repo's `multi_rx.py` against GNU Radio
  3.10 / gr-osmosdr and tunes a *remote* SDR over the network using
  gr-osmosdr's `rtl_tcp=<host>:<port>` device arg -- no USB passthrough, no
  privileged container. It reads its entire config (SDR device, physical
  channel, every trunked system definition, talkgroup names, white/black
  lists) from a SQLite database rather than the JSON/TSV files earlier
  versions of this MVP used. Decoded call audio goes out over OP25's
  built-in raw-PCM WebSocket server, which the New UI's audio player
  connects to directly -- no transcoding/Icecast hop.
- **`config-api`** container is a small sidecar (stdlib-only Python, no
  new dependencies) exposing REST CRUD + a browser admin UI for systems,
  talkgroups, categories (RadioReference-style groupings), and
  white/blacklists, all backed by the same SQLite file `op25` reads. It
  also holds Docker-socket access so its "Set Active" action can restart
  `op25` after a system/device/channel change (anything structural always
  needs a restart -- talkgroup/list edits can apply live via "Apply", no
  restart, since `op25` re-queries the DB in place for those).

Both containers share the config DB through a named Docker volume
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
-- the SDR device (host/port/gain/ppm), trunked system definitions,
talkgroups, and which system is active all live in the SQLite DB now, set
through config-api's UI rather than env vars or bind-mounted files.

## 3. Get a config DB in place

`op25` and `config-api` both expect `/data/op25.db` to exist in the
`op25-config-db` named volume. Two ways to get one there:

- **First-time setup**: run `docker/config/migrate_json_to_sqlite.py`
  against a `multi_rx.json`-style config to seed a fresh DB (see the
  script's docstring), or just add your first system/talkgroups by hand
  through config-api's UI once it's up (an empty DB is fine to start
  `docker compose up` against -- `op25` will just have nothing to decode
  until a system + channel exist).
- **Moving from another deployment**: use that deployment's config-api
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

- **Admin UI** (manage systems, talkgroups, groups, white/blacklists,
  reorder/search/sort, export config, switch which system is active):
  `http://<docker-host>:8091` (`CONFIG_API_HOST_PORT`).
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
- **Edited a talkgroup/list in config-api but don't see it change**:
  click "Apply" on that system's row (only does something if that system
  is the currently *active* one), then wait for that talkgroup to
  transmit again -- the New UI's tag display is a client-side cache
  seeded from live call events, not a static list, so it won't show a
  renamed tag until the talkgroup keys up again after your edit.
- **Choppy/garbled audio**: usually an RF/SDR gain issue on the Pi side,
  or the device's sample rate too high for the Pi's WiFi link to
  `rtl_tcp` clients -- adjust via the device's `rate`/`gains` fields in
  config-api (or `sqlite3` directly against the `devices` table for now;
  no dedicated Devices UI panel yet).

## Out of scope for now

Tailscale setup itself, recording, MQTT/Home Assistant integration, and
true no-restart hot-swap between active systems (switching still restarts
`op25` -- see `docker/config/schema.sql`'s comments) are deliberately left
out.
