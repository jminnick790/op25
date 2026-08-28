# OP25 -- Dockerized P25 Trunked Radio Monitoring

This is a further fork of [boatbod/op25](https://github.com/boatbod/op25) -- itself
the actively maintained fork of the original op25 project (see
[History](#history) below for the full lineage) -- built out into a
self-contained, Docker-deployed P25 trunked-system monitoring stack. The
`rx.py`/`multi_rx.py` decoder engine and everything it can do (see the
capability lists below) are unchanged and fully credited upstream to boatbod
and the original op25/osmocom project; what this fork adds sits on top of
that engine, not in place of it.

## What this fork adds

- **Docker deployment, SQLite-backed config** -- one container, one image,
  config managed through a database instead of hand-edited JSON/TSV files.
  See [README-docker-streaming.md](README-docker-streaming.md) for the full
  setup guide: remote-SDR-over-`rtl_tcp`, the worker/server process split,
  and the named-volume config DB.
- **Web admin UI** -- manage systems, sites, talkgroups, groups,
  devices/channels, and white/blacklists without touching a config file;
  drag-to-reorder sites, search/sort, export/import the whole DB. Responsive
  down to phone-sized screens, with collapsible per-row cards for browsing
  long lists in the field.
- **TSV/CSV bulk import** -- paste or upload a spreadsheet of sites or
  talkgroups instead of adding them one at a time.
- **Automatic site roaming** -- a dedicated scout SDR channel continuously
  evaluates neighbor sites (via live adjacent-site broadcasts) and hands the
  primary receiver off make-before-break when the active site's voice
  quality degrades or its control channel goes stale -- built for
  vehicle/mobile use where a single site's coverage runs out. Neighbor sites
  are auto-discovered from a system's own broadcasts as they're observed,
  and a roam-events log records the full decision trail for after-the-fact
  review.
- **Live web UI + persistent history** -- Server-Sent Events push live
  channel/call state to the browser, backed by a persistent call log,
  subscriber registration history, and neighbor-site table for querying
  activity after the fact.

For setup, see [README-docker-streaming.md](README-docker-streaming.md).

## `rx.py` capabilities

- P25 Conventional (single frequency)
- P25 Trunking Phase 1, Phase 2 and TDMA Control Channel
- P25 Phase 2 tone synthesis
- Single SDR (dongle) tuning regardless of bandwidth
- TGID Blacklist, Whitelist with dynamic reloading
- TGID Priority with mid-call preemption
- Multi-system scanning (switches between multiple systems sequentially)
- TGID text tagging and metadata upload to Icecast server for streaming
- Dynamically controllable real-time plots: FFT, Constellation, Symbol, Datascope, Mixer, Tuning
- Dynamically controllable log level
- Curses or HTTP based terminal
- Demodulator symbol capture and replay
- Voice Encryption detection and skipping (configurable behavior)
- Automatic fine tune tracking using Frequency Locked Loop (FLL).

## `multi_rx.py` capabilities

- P25 Conventional (multiple frequencies)
- P25 Trunking Phase 1, Phase 2 and TDMA Control Channel
- P25 Phase 2 tone synthesis
- Motorola SmartZone Trunking (requires two dongles)
- Motorola Connect+ TRBO DMR Trunking (experimental, requires two dongles)
- DMR BS Mode (non-trunked)
- NBFM analog (conventional or SmartZone trunked)
- Multi-system/multi-channel concurrent operation (full time, not sequential)
- Single, Multiple and Shared SDR devices (e.g. wideband devices such as Airspy etc)
- TGID Blacklist, Whitelist with dynamic reloading
- TGID Priority with mid-call preemption
- TGID text tagging and metadata upload to Icecast server for streaming
- RID text tagging
- Dynamically controllable real-time plots: FFT, Constellation, Symbol, Datascope, Mixer, Tuning
- Dynamically controllable log level
- Awesome new HTTP based terminal by Outerdog(RR)/Triptolemus510(github) with websocket audio
- JSON based configuration
- DSD .wav and .iq file replay
- Dynamic demodulator symbol capture and replay (commanded through terminal)
- Voice Encryption detection and skipping (configurable behavior)
- Automatic fine tune tracking using Frequency Locked Loop (FLL)

# Contributed by W1JPI fork of op25
NBFM squelch algorithms based on the work of PA3FWM (https://www.pa3fwm.nl/technotes/tn16e.html)
A noise squelch calibrated in dB of quieting (no per-device threshold hunting) and an optional speech
detector, selected via `nbfm_squelch_mode`. See `op25/gr-op25_repeater/apps/README-analog.md` for details.

## Encryption capabilities
Real-time decryption of encrypted P25 voice traffic is supported for several commonly used protocols
as long as you know and enter the encryption key for this to work. OP25 does not reverse
the encryption on traffic with unknown keys.
- ADP/RC4
- DES-OFB
- AES-256

## Roadmap (under development)
- Demodulator improvements to speed up channel lock-time
- Additional encryption algorithms
- Well written code contributions of new features or other improvements are welcome.
  Please submit pull requests using the "dev" branch to make integration simpler.

## History

- Forked from git://git.osmocom.org/op25 "max" branch on 9/10/2017
- Up to date with osmocom "max" branch as of 3/3/2018
- Note: as of 2019, codebase has diverged too far to continue syncing with osmocom

### Many changes:
- new DQPSK demodulator chain with automatic fine tuning & tracking
- udp python audio server sockaudio.py and remote player audio.py
- wireshark fixes (experimental)
- ability to configure NAC 0x000 in trunk.tsv and have system use first NAC decoded
- integrated N8UR logging changes to trunking.py
- ability to adjust fine tuning in real time (,./<> keys in terminal) 
- ability to dynamically resize the curses terminal
- ability to dunamically turn plots on and off from the terminal (keys 1-5)
- new 'mixer' and 'fll' output plots (terminal keys 5 & 6)
- reworked trunking hold/release logic that improves Phase 1 audio on some systems
- decoding and logging of encryption sync info ("ESS") at log level 10 (`-v 10`)
- ability to silence the playing of encrypted audio
- encrypted audio flag shown on terminal screen
- source radio id displayed on terminal screen (if available)
- supports IP addresses or host names for `--wireshark-host` (`-W`) parameter
- decode and pass voice channel sourced trunk signaling up to trunking module
- added optional trunk group priority parameter to end of tgid-tags.tsv file
- added ability to handle ranges of tgid in blacklist/whitelist files
- support for streaming metadata updates (both `rx.py` and `multi_rx.py`)
- support for MotoTrbo Connect+ and Motorola SmartNet/SmartZone trunking (`multi_rx.py`)
- enhanced `multi_rx.py` now supports P25, DMR, SmartNet trunking, terminal and built-in audio player

### New command line options:
- `--fine-tune`: sub-ppm tuning adjustment
- `--wireshark-port`: facilitates multiple instances of `rx.py`
- `--udp-player`: enable built-in audio player
- `--nocrypt`: silence encrypted audio

**Note 1:** using the `--nocrypt` command line option will silence encrypted audio, but the trunking logic will cause the application to remain on the active tgid until the transmission ends.  It is generally preferable to blacklist tgids that are always encrypted rather than simply silence them.  Use the `--nocrypt` functionality to silence occasional encrypted transmissions on mixed use tgids.

**Note 2:** trunk id to tag mapping file (`tgid-tags.tsv`) can contain an optional 3rd numeric parameter to be used as the trunk priority when simultaneous calls are present on the system being monitored.  Default priority is 3 if not explicitly specified.  Lower numeric value = higher priority.  Data columns are separated by a single TAB character.

e.g.
```
11501	TB FIRE DISP	2
11502	TB FIRE TAC2	3
11503	TB FIRE TAC3	3
11504	TB FIRE TAC4	4
11505	TB FIRE TAC5	3 
```
