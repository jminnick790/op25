#!/bin/bash
# Alternate entrypoint: runs multi_rx.py (JSON-config-driven, more actively
# developed app) instead of rx.py (the CLI/trunk.tsv-driven legacy app used
# by entrypoint.sh). Kept as a separate script/config so the working rx.py
# setup is untouched -- invoke this one explicitly to try multi_rx.py:
#
#   docker compose run --rm --entrypoint /usr/local/bin/entrypoint-multi.sh op25
#
# multi_rx.json is currently a static config (not env-var templated like
# entrypoint.sh) since this is an experimental side-by-side trial, not yet
# the primary path.
set -euo pipefail

: "${MULTI_RX_CONFIG:=multi_rx.json}"
: "${LOG_VERBOSITY:=10}"

echo "Starting multi_rx.py: ./multi_rx.py -c ${MULTI_RX_CONFIG} -v ${LOG_VERBOSITY}"
exec ./multi_rx.py -c "${MULTI_RX_CONFIG}" -v "${LOG_VERBOSITY}"
