# Audio bridge sidecar: UDP (OP25 decoded PCM) -> continuous PCM/TCP -> AAC -> Icecast.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY docker/audio-bridge/op25_udp_shim.py /usr/local/bin/op25_udp_shim.py
COPY docker/audio-bridge/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# UDP audio in from OP25
EXPOSE 23456/udp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
