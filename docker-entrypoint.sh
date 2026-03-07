#!/bin/sh
set -e

CONFIG="${MIMIR_CONFIG:-mimir.toml}"

# If called with a custom command (not the default serve), pass through directly
if [ "$1" != "serve" ] && [ "$1" != "auto" ]; then
    exec mimir "$@"
fi

# "auto" mode: index then serve (default container behavior)
# Also triggered by "serve" with AUTO_INDEX=1
if [ "$1" = "auto" ] || [ "${AUTO_INDEX:-0}" = "1" ]; then
    echo "==> Indexing repositories from ${CONFIG} ..."
    mimir index --config "$CONFIG" || {
        echo "WARNING: Indexing failed, starting server with whatever data exists"
    }
fi

# Start HTTP server
echo "==> Starting Mimir HTTP server on ${MIMIR_HOST:-0.0.0.0}:${MIMIR_PORT:-8421}"
exec mimir serve \
    --http \
    --http-host "${MIMIR_HOST:-0.0.0.0}" \
    --http-port "${MIMIR_PORT:-8421}" \
    --config "$CONFIG"
