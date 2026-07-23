#!/bin/sh
set -eu

# Bind mounts are created by Docker as root:root.  The upstream entrypoint drops
# privileges to the grok2api user, so prepare the writable paths before that
# happens.  SQLite also expects the database file to exist when opened in
# read/write mode.
mkdir -p /app/data /app/data/media /app/logs
chown grok2api:grok2api /app/data /app/data/media /app/logs

if [ ! -e /app/data/backend.db ]; then
    : > /app/data/backend.db
fi

for database_file in \
    /app/data/backend.db \
    /app/data/backend.db-wal \
    /app/data/backend.db-shm
do
    if [ -e "$database_file" ]; then
        chown grok2api:grok2api "$database_file"
        chmod 0600 "$database_file"
    fi
done

exec /usr/local/bin/grok2api-entrypoint "$@"
