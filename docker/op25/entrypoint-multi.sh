#!/bin/bash
# Entrypoint for multi_rx.py (JSON-config-driven, more actively developed
# app) instead of rx.py (the CLI/trunk.tsv-driven legacy app used by
# entrypoint.sh).
#
# Config source: if MULTI_RX_DB is set, config (systems/devices/channels)
# is read from that SQLite DB, with talkgroup/whitelist/blacklist data
# live-refreshable via the 'reload' UI command -- see docker/config/schema.sql
# and apps/db_config.py. Otherwise, falls back to the static JSON file at
# MULTI_RX_CONFIG (default multi_rx.json), unchanged from the original
# experimental JSON-only setup.
set -euo pipefail

: "${MULTI_RX_CONFIG:=multi_rx.json}"
: "${MULTI_RX_DB:=}"
: "${LOG_VERBOSITY:=10}"

if [ -n "${MULTI_RX_DB}" ]; then
    echo "Starting multi_rx.py: ./multi_rx.py --db ${MULTI_RX_DB} -v ${LOG_VERBOSITY}"
    exec ./multi_rx.py --db "${MULTI_RX_DB}" -v "${LOG_VERBOSITY}"
else
    echo "Starting multi_rx.py: ./multi_rx.py -c ${MULTI_RX_CONFIG} -v ${LOG_VERBOSITY}"
    exec ./multi_rx.py -c "${MULTI_RX_CONFIG}" -v "${LOG_VERBOSITY}"
fi
