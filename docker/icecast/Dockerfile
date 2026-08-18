# Icecast2 image that renders its config from env vars at startup so the
# source/admin passwords never need to be baked into the image or checked
# into icecast.xml.
FROM debian:12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        icecast2 \
        gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY docker/icecast/icecast.xml.template /etc/icecast2/icecast.xml.template
COPY docker/icecast/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
