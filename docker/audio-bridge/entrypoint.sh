#!/bin/bash
# Runs the op25_udp_shim.py (UDP -> continuous PCM/TCP) alongside ffmpeg
# (PCM -> AAC -> Icecast) as a single sidecar. Keeping both in one container
# avoids an extra network hop for a pipeline that's really one logical stage.
set -euo pipefail

: "${AUDIO_UDP_PORT:=23456}"
: "${PCM_TCP_PORT:=8090}"
: "${ICECAST_HOST:?ICECAST_HOST env var must be set}"
: "${ICECAST_PORT:=8000}"
: "${ICECAST_MOUNT:=op25}"
: "${ICECAST_SOURCE_PASSWORD:?ICECAST_SOURCE_PASSWORD env var must be set}"
: "${AAC_BITRATE:=32k}"

running=true
cleanup() {
    running=false
    [ -n "${FFMPEG_PID:-}" ] && kill "${FFMPEG_PID}" 2>/dev/null || true
    [ -n "${SHIM_PID:-}" ] && kill "${SHIM_PID}" 2>/dev/null || true
}
trap cleanup TERM INT

python3 /usr/local/bin/op25_udp_shim.py \
    --udp-host 0.0.0.0 --udp-port "${AUDIO_UDP_PORT}" \
    --tcp-host 127.0.0.1 --tcp-port "${PCM_TCP_PORT}" &
SHIM_PID=$!

# give the shim's TCP server a moment to bind before ffmpeg dials in
sleep 2

echo "Starting ffmpeg -> icecast://${ICECAST_HOST}:${ICECAST_PORT}/${ICECAST_MOUNT}"
while "${running}"; do
    ffmpeg -hide_banner -loglevel warning \
        -f s16le -ar 8000 -ac 1 -i "tcp://127.0.0.1:${PCM_TCP_PORT}" \
        -c:a aac -b:a "${AAC_BITRATE}" \
        -content_type audio/aac -f adts \
        "icecast://source:${ICECAST_SOURCE_PASSWORD}@${ICECAST_HOST}:${ICECAST_PORT}/${ICECAST_MOUNT}" &
    FFMPEG_PID=$!
    wait "${FFMPEG_PID}" || true
    "${running}" || break
    echo "ffmpeg exited, retrying in 5s..." >&2
    sleep 5
done

kill "${SHIM_PID}" 2>/dev/null || true
wait || true
