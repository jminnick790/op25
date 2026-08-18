#!/bin/bash
set -euo pipefail

: "${ICECAST_SOURCE_PASSWORD:?ICECAST_SOURCE_PASSWORD env var must be set}"
: "${ICECAST_ADMIN_PASSWORD:?ICECAST_ADMIN_PASSWORD env var must be set}"

envsubst '${ICECAST_SOURCE_PASSWORD} ${ICECAST_ADMIN_PASSWORD}' \
    < /etc/icecast2/icecast.xml.template > /etc/icecast2/icecast.xml

exec icecast2 -c /etc/icecast2/icecast.xml
