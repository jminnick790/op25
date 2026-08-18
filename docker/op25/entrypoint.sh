#!/bin/bash
# Launches rx.py against a remote rtl_tcp source and streams decoded audio
# out via UDP to the audio-bridge sidecar (see docker/audio-bridge).
#
# NOTE: this intentionally does NOT use rx.py's -U/--udp-player flag. That
# flag spawns OP25's own local ALSA/PulseAudio player thread and, as a side
# effect, forces the audio UDP destination back to 127.0.0.1 -- useless in a
# headless container and unreachable from another container. Instead we set
# -W/-u directly ("wireshark" audio output), which is the same raw PCM
# S16LE/8kHz + 2-byte flag protocol sockaudio.py consumes, just addressed at
# the audio-bridge container instead of localhost.
#
# -U also has a second, easy-to-miss side effect: it's the only thing that
# sets self.options.vocoder = True in rx.py, which is what actually turns on
# AMBE/IMBE-to-PCM decoding (do_imbe/do_audio_output in the frame assembler).
# Without it, rx.py still receives and error-corrects voice frames (visible
# in the logs) but never synthesizes audio from them -- the audio path stays
# silent no matter how much real traffic decodes. -V/--vocoder is the same
# flag on its own, with none of -U's other side effects, so we pass it
# explicitly instead.
set -euo pipefail

: "${SDR_HOST:?SDR_HOST env var must be set to the rtl_tcp host (the Pi LAN IP)}"
: "${SDR_PORT:=1234}"
: "${SDR_SAMPLE_RATE:=960000}"
: "${SDR_PPM:=0}"
: "${SDR_GAIN:=}"
: "${TRUNK_CONF:=trunk.tsv}"
: "${AUDIO_BRIDGE_HOST:=audio-bridge}"
: "${AUDIO_UDP_PORT:=23456}"
: "${HTTP_PORT:=8080}"
: "${LOG_VERBOSITY:=1}"
: "${ENABLE_TDMA_CC:=false}"
: "${ENABLE_PHASE2:=false}"

args=(
    --args "rtl_tcp=${SDR_HOST}:${SDR_PORT}"
    -S "${SDR_SAMPLE_RATE}"
    -q "${SDR_PPM}"
    -T "${TRUNK_CONF}"
    -V -w -W "${AUDIO_BRIDGE_HOST}" -u "${AUDIO_UDP_PORT}"
    -l "http:0.0.0.0:${HTTP_PORT}"
    -v "${LOG_VERBOSITY}"
)

if [ -n "${SDR_GAIN}" ]; then
    args+=(-N "${SDR_GAIN}")
fi

if [ "${ENABLE_TDMA_CC}" = "true" ]; then
    args+=(--tdma-cc)
fi

# Phase 2 voice decode (-2) is separate from TDMA control channel decode
# (--tdma-cc) -- statewide P25 Phase II systems like VIPER need both.
if [ "${ENABLE_PHASE2}" = "true" ]; then
    args+=(-2)
fi

echo "Starting rx.py: ./rx.py ${args[*]}"
exec ./rx.py "${args[@]}"
