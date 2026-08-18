# Dockerized OP25 Streaming (MVP)

Runs OP25 against a remote RTL-SDR (via `rtl_tcp` on a separate Pi) and
streams decoded P25 audio to a browser-playable AAC stream over Icecast.

```
antenna -> RTL-SDR -> Pi Zero 2W (rtl_tcp) -> LAN -> [op25] -> [audio-bridge] -> [icecast] -> browser
```

This is a single-system MVP: one hardcoded `trunk.tsv`/`talkgroups.tsv`
pair, no profile switching, no database-backed config, no live status
dashboard. Those are planned for later.

## How the pieces fit together

- **`op25`** container builds this repo's `rx.py` against GNU Radio 3.10 /
  gr-osmosdr and tunes a *remote* SDR over the network using gr-osmosdr's
  `rtl_tcp=<host>:<port>` device arg -- no USB passthrough, no privileged
  container. Decoded call audio is sent out as raw PCM over UDP (OP25's
  `-w`/`-W`/`-u` "wireshark audio" output, the same protocol its own
  `sockaudio.py` player consumes) to the `audio-bridge` service.
- **`audio-bridge`** container runs `op25_udp_shim.py`
  ([docker/audio-bridge/op25_udp_shim.py](docker/audio-bridge/op25_udp_shim.py)),
  which turns OP25's bursty UDP audio (packets only while a call is
  active) into a steady, silence-filled PCM stream over a local TCP
  socket, then feeds that into `ffmpeg` to encode AAC and push it to
  Icecast. The silence fill is what keeps the Icecast stream alive and
  in sync through the gaps between calls.
- **`icecast`** container serves the AAC stream to any browser or audio
  client that can play an Icecast mountpoint.

Only Icecast's port needs to be reachable outside the Docker host (over
Tailscale) -- `op25` and `audio-bridge` talk to each other purely over the
compose-internal network.

## 1. Pi-side setup (not built by this repo, do this separately)

On the Pi Zero 2W near the antenna, install `rtl-sdr` tools and run
`rtl_tcp` as a systemd service bound to the LAN interface, on whatever
port you'll set as `SDR_PORT` below (default `1234`):

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

Edit `.env`:

- `SDR_HOST` / `SDR_PORT` -- the Pi's LAN IP and the `rtl_tcp` port above.
- `ICECAST_SOURCE_PASSWORD` / `ICECAST_ADMIN_PASSWORD` -- pick real
  passwords (used only inside the compose network + whatever you expose
  over Tailscale).
- Leave `SDR_SAMPLE_RATE` and `SDR_GAIN` at their defaults unless you know
  you need to change them for your system.
- `ENABLE_TDMA_CC` / `ENABLE_PHASE2` -- both default to `true`, matching
  the bundled example system below (a P25 Phase II site). Set both to
  `false` if your system is plain P25 Phase 1 (a single FDMA control
  channel, no TDMA voice slots).

The bundled config is a real, working example: North Carolina VIPER
(`radioreference.com/db/sid/7118`), control-site frequencies/NAC from the
Anderson Mountain site (Catawba County -- chosen for stronger local
signal), paired with the full published Lincoln County talkgroup list. A
trunk.tsv's control site and its talkgroups don't have to be the same
county -- multiple sites/RFSS zones on a statewide interop system like
VIPER all carry the same statewide talkgroups, so pick whichever site
control channel is strongest at your location. Swap in your own system the
same way:

- [docker/config/trunk.tsv](docker/config/trunk.tsv) -- one row: system
  name, control channel frequency/frequencies (comma-separated, no
  whitespace, e.g. `773.84375,774.19375`), NAC as a `0x`-prefixed hex
  literal (e.g. `0x1f0`), modulation (`cqpsk` for most P25 systems), and
  `talkgroups.tsv` as the tags file. RadioReference's system/site page is
  the usual source for these values -- site frequency tables and grouped
  talkgroup listings are publicly viewable without a login for most
  systems, though full search requires a RadioReference Premium
  Subscription.
- [docker/config/talkgroups.tsv](docker/config/talkgroups.tsv) -- one
  `<TGID>\t<Alpha Tag>` per line, tab-separated, no header row.

These are bind-mounted read-only into the `op25` container, so edits take
effect on container restart without rebuilding the image.

## 3. Bring the stack up

```
docker compose up --build
```

First build compiles OP25's C++ blocks against GNU Radio -- expect several
minutes. Watch the `op25` logs for `sync established` / talkgroup activity
once a call comes in.

## 4. Listen

Point a browser or audio client at:

```
http://<docker-host>:8000/op25
```

(mountpoint and port come from `ICECAST_MOUNT` / `ICECAST_HOST_PORT` in
`.env`; defaults are `op25` and `8000`). Once Tailscale is layered on top,
the same URL works from anywhere on your tailnet using the host's
Tailscale IP/hostname instead -- nothing in this stack needs to change for
that, since Icecast's container port is the only thing that has to be
reachable and it isn't bound to anything Tailscale-incompatible.

OP25's HTTP status/terminal UI is also published, at
`http://<docker-host>:8080` (`OP25_HTTP_HOST_PORT` in `.env`) -- useful
for confirming control channel lock and talkgroup activity, not required
for the audio path.

## Troubleshooting

- **No audio, but `op25` shows control channel lock**: check
  `docker compose logs audio-bridge` -- confirm ffmpeg connected to the
  shim's TCP port and is pushing to Icecast without auth errors.
- **`op25` can't reach the SDR**: confirm `rtl_tcp` is running on the Pi
  and reachable from the Docker host (`nc -zv $SDR_HOST $SDR_PORT`); the
  Docker host and Pi need to be on the same LAN (or otherwise routed) for
  this MVP -- only the Icecast leg is meant to cross Tailscale.
- **Choppy/garbled audio**: usually an RF/SDR gain issue on the Pi side,
  or `SDR_SAMPLE_RATE` too high for the Pi's WiFi link to `rtl_tcp`
  clients -- try lowering it.

## Out of scope for this MVP

Talkgroup/system profile switching, database-backed config, a config UI,
a live status dashboard, Tailscale setup itself, recording, and
MQTT/Home Assistant integration are all deliberately left out. See the
project context for the longer-term plan.
