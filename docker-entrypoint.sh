#!/bin/sh
set -eu

: "${SUI_DB:=/data/s-ui.db}"
: "${CHECK_INTERVAL:=60}"
: "${RESET_WEB_HOST:=0.0.0.0}"
: "${RESET_WEB_PORT:=8080}"

if [ "$#" -gt 0 ]; then
  exec python /app/sui_traffic_reset.py --db "$SUI_DB" "$@"
fi

exec python /app/sui_traffic_reset.py --db "$SUI_DB" serve --host "$RESET_WEB_HOST" --port "$RESET_WEB_PORT" --interval "$CHECK_INTERVAL"
