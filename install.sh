#!/bin/sh
set -eu

APP_NAME="sui-traffic-reset"
DEFAULT_DB_DIR="/usr/local/s-ui/db"
DEFAULT_BIND="127.0.0.1:8787"
DEFAULT_TZ="Asia/Shanghai"
DEFAULT_INTERVAL="60"

cd "$(dirname "$0")"

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing command: $1" >&2
    exit 1
  fi
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
    return
  fi
  echo "Docker Compose is required." >&2
  exit 1
}

generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY'
import secrets
print(secrets.token_hex(24))
PY
    return
  fi
  date +%s | sed 's/.*/change-me-&/'
}

need_cmd docker
COMPOSE="$(compose_cmd)"

DB_DIR="${SUI_DB_DIR:-$DEFAULT_DB_DIR}"
WEB_BIND="${RESET_WEB_BIND:-$DEFAULT_BIND}"
TZ_VALUE="${TZ:-$DEFAULT_TZ}"
CHECK_INTERVAL_VALUE="${CHECK_INTERVAL:-$DEFAULT_INTERVAL}"

if [ ! -f "$DB_DIR/s-ui.db" ]; then
  echo "s-ui database was not found at: $DB_DIR/s-ui.db" >&2
  echo "Set SUI_DB_DIR=/path/to/s-ui/db and rerun this script." >&2
  exit 1
fi

if [ ! -f .env ]; then
  TOKEN="$(generate_token)"
  cat > .env <<EOF
SUI_DB_DIR=$DB_DIR
CHECK_INTERVAL=$CHECK_INTERVAL_VALUE
TZ=$TZ_VALUE
RESET_WEB_TOKEN=$TOKEN
RESET_WEB_BIND=$WEB_BIND
EOF
  echo "Created .env"
else
  TOKEN="$(grep '^RESET_WEB_TOKEN=' .env 2>/dev/null | sed 's/^RESET_WEB_TOKEN=//' || true)"
  echo ".env already exists; keeping current configuration."
fi

BACKUP="$DB_DIR/s-ui.db.bak.$(date +%Y%m%d-%H%M%S)"
cp "$DB_DIR/s-ui.db" "$BACKUP"
echo "Database backup: $BACKUP"

$COMPOSE up -d --build

echo
echo "$APP_NAME is running."
echo "Web UI: http://$WEB_BIND"
if [ -n "${TOKEN:-}" ]; then
  echo "Token: $TOKEN"
else
  echo "Token: see RESET_WEB_TOKEN in .env"
fi
echo
echo "Logs:"
echo "  $COMPOSE logs -f"
