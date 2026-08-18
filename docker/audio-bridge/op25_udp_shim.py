#!/usr/bin/env python3
"""
UDP -> continuous PCM/TCP bridge for OP25's decoded audio output.

OP25 (rx.py, started with -w -W <host> -u <port>) sends decoded voice audio
as raw S16LE mono PCM at 8000 Hz to this host, framed exactly like
sockaudio.py expects: 320-byte frames (160 samples / 20ms) while a call is
active, plus 2-byte int16 "flag" packets (0=drain, 1=drop) at call
boundaries, and *nothing at all* when no call is active.

ffmpeg needs a steady, real-time-paced byte stream to encode continuously,
so this listens for those UDP packets and re-emits them over a TCP socket
at a fixed 20ms cadence, filling in low-level dithered "comfort noise"
whenever no audio frame arrived in time. That keeps the downstream
AAC/Icecast stream alive and in sync through gaps between calls instead of
stalling or drifting.

Dither, not pure digital-zero silence: P25 systems spend most of their time
with no call active, so the encoder sees this filler far more than real
audio. True zero-signal PCM lets AAC compress a frame down to almost
nothing, which starves Icecast's burst buffer -- verified against a real
Icecast 2.4 instance, a newly-connecting listener during a quiet gap could
then wait upwards of a minute for enough buffered bytes before hearing
anything. Cheap random dither at a couple bits of amplitude is inaudible
but keeps the encoded bitrate (and therefore Icecast's burst fill rate)
steady regardless of call activity.
"""
import argparse
import queue
import random
import socket
import struct
import sys
import threading
import time

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = int(SAMPLE_RATE * (FRAME_MS / 1000.0))  # 160 samples
FRAME_BYTES = FRAME_SAMPLES * 2  # 320 bytes of S16LE
DITHER_AMPLITUDE = 2  # +/- LSBs; inaudible, but enough to defeat AAC's silence detection
QUEUE_MAXSIZE = 50  # ~1s of audio; drop oldest rather than build up latency


def make_dither_frame():
    samples = [random.randint(-DITHER_AMPLITUDE, DITHER_AMPLITUDE) for _ in range(FRAME_SAMPLES)]
    return struct.pack("<%dh" % FRAME_SAMPLES, *samples)


def log(msg):
    sys.stderr.write(f"[op25_udp_shim] {msg}\n")
    sys.stderr.flush()


def udp_reader(udp_host, udp_port, frame_q):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((udp_host, udp_port))
    log(f"listening for OP25 audio on udp://{udp_host}:{udp_port}")

    while True:
        data, _addr = sock.recvfrom(4096)
        if len(data) == 2:
            # flag packet (drain/drop) - no PCM payload, nothing to forward
            continue
        if not data:
            continue
        # Pad/truncate to a fixed frame size so the TCP side stays paced
        if len(data) < FRAME_BYTES:
            data = data + b"\x00" * (FRAME_BYTES - len(data))
        elif len(data) > FRAME_BYTES:
            data = data[:FRAME_BYTES]
        try:
            frame_q.put_nowait(data)
        except queue.Full:
            try:
                frame_q.get_nowait()
            except queue.Empty:
                pass
            frame_q.put_nowait(data)


def tcp_server(tcp_host, tcp_port, frame_q):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((tcp_host, tcp_port))
    srv.listen(1)
    log(f"serving continuous PCM on tcp://{tcp_host}:{tcp_port}")

    while True:
        conn, addr = srv.accept()
        log(f"ffmpeg connected from {addr}")
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            pace_frames(conn, frame_q)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            conn.close()
            log("ffmpeg disconnected")


def pace_frames(conn, frame_q):
    interval = FRAME_MS / 1000.0
    next_tick = time.monotonic()
    while True:
        try:
            frame = frame_q.get_nowait()
        except queue.Empty:
            frame = make_dither_frame()
        conn.sendall(frame)

        next_tick += interval
        sleep_for = next_tick - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            # fell behind (e.g. slow consumer) - resync instead of free-running
            next_tick = time.monotonic()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--udp-host", default="0.0.0.0", help="host to bind for incoming OP25 audio")
    ap.add_argument("--udp-port", type=int, default=23456, help="port OP25 sends decoded audio to")
    ap.add_argument("--tcp-host", default="0.0.0.0", help="host to bind for the outgoing PCM TCP server")
    ap.add_argument("--tcp-port", type=int, default=8090, help="port ffmpeg connects to for raw PCM")
    args = ap.parse_args()

    frame_q = queue.Queue(maxsize=QUEUE_MAXSIZE)

    reader = threading.Thread(
        target=udp_reader, args=(args.udp_host, args.udp_port, frame_q), daemon=True
    )
    reader.start()

    tcp_server(args.tcp_host, args.tcp_port, frame_q)


if __name__ == "__main__":
    main()
