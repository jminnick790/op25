# SQLite CRUD API + admin UI sidecar for OP25's DB-backed config
# (docker/config/schema.sql). Stdlib only -- no pip/apt dependencies beyond
# the base image, matching the rest of this repo's apt-only convention.
FROM python:3.11-slim

COPY docker/config-api/app.py /usr/local/bin/app.py
COPY docker/config-api/static /usr/local/bin/static
COPY docker/config-api/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY docker/config/schema.sql /usr/local/bin/schema.sql
COPY docker/config/migrate_json_to_sqlite.py /usr/local/bin/migrate_json_to_sqlite.py
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8091

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
