#!/usr/bin/env bash
set -euo pipefail

APP_NAME="sui-traffic-reset"
IMAGE_REPO="${SUI_TRAFFIC_RESET_IMAGE:-oldwangnewbe/sui-traffic-reset}"
IMAGE_VERSION="${SUI_TRAFFIC_RESET_VERSION:-latest}"
INSTALL_DIR="${INSTALL_DIR:-/opt/sui-traffic-reset}"
DB_DIR="${SUI_DB_DIR:-/usr/local/s-ui/db}"
WEB_BIND="${RESET_WEB_BIND:-127.0.0.1:8787}"
TZ_VALUE="${TZ:-Asia/Shanghai}"
CHECK_INTERVAL_VALUE="${CHECK_INTERVAL:-60}"

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

generate_password() {
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

if [ ! -f "$DB_DIR/s-ui.db" ]; then
  echo "s-ui database was not found at: $DB_DIR/s-ui.db" >&2
  echo "Set SUI_DB_DIR=/path/to/s-ui/db and rerun this script." >&2
  exit 1
fi

if [ "$(id -u)" -ne 0 ] && [ ! -w "$(dirname "$INSTALL_DIR")" ]; then
  echo "No permission to create $INSTALL_DIR. Run as root or set INSTALL_DIR to a writable path." >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

if [ ! -f .env ]; then
  ADMIN_USER="${RESET_ADMIN_USER:-admin}"
  ADMIN_PASSWORD="${RESET_ADMIN_PASSWORD:-$(generate_password)}"
  cat > .env <<EOF
SUI_DB_DIR=$DB_DIR
CHECK_INTERVAL=$CHECK_INTERVAL_VALUE
TZ=$TZ_VALUE
RESET_ADMIN_USER=$ADMIN_USER
RESET_ADMIN_PASSWORD=$ADMIN_PASSWORD
RESET_WEB_BIND=$WEB_BIND
RESET_SESSION_TTL=604800
RESET_LOGIN_MAX_ATTEMPTS=8
RESET_LOGIN_WINDOW=600
RESET_COOKIE_SECURE=0
SUI_PANEL_URL=${SUI_PANEL_URL:-}
SUI_API_TOKEN=${SUI_API_TOKEN:-}
SUI_API_TIMEOUT=3
SUI_ONLINE_CACHE_TTL=5
SUI_TRAFFIC_RESET_IMAGE=$IMAGE_REPO
SUI_TRAFFIC_RESET_VERSION=$IMAGE_VERSION
EOF
  echo "Created $INSTALL_DIR/.env"
else
  ADMIN_USER="$(grep '^RESET_ADMIN_USER=' .env 2>/dev/null | sed 's/^RESET_ADMIN_USER=//' || true)"
  ADMIN_PASSWORD="$(grep '^RESET_ADMIN_PASSWORD=' .env 2>/dev/null | sed 's/^RESET_ADMIN_PASSWORD=//' || true)"
  if [ -z "$ADMIN_USER" ]; then
    ADMIN_USER="${RESET_ADMIN_USER:-admin}"
    printf '\nRESET_ADMIN_USER=%s\n' "$ADMIN_USER" >> .env
  fi
  if [ -z "$ADMIN_PASSWORD" ]; then
    ADMIN_PASSWORD="${RESET_ADMIN_PASSWORD:-$(generate_password)}"
    printf 'RESET_ADMIN_PASSWORD=%s\n' "$ADMIN_PASSWORD" >> .env
  fi
  if ! grep -q '^SUI_TRAFFIC_RESET_IMAGE=' .env; then
    printf 'SUI_TRAFFIC_RESET_IMAGE=%s\n' "$IMAGE_REPO" >> .env
  fi
  if ! grep -q '^SUI_TRAFFIC_RESET_VERSION=' .env; then
    printf 'SUI_TRAFFIC_RESET_VERSION=%s\n' "$IMAGE_VERSION" >> .env
  fi
  echo ".env already exists; keeping current configuration."
fi

cat > docker-compose.yml <<'EOF'
services:
  sui-traffic-reset:
    image: ${SUI_TRAFFIC_RESET_IMAGE:-oldwangnewbe/sui-traffic-reset}:${SUI_TRAFFIC_RESET_VERSION:-latest}
    container_name: sui-traffic-reset
    restart: unless-stopped
    environment:
      SUI_DB: /data/s-ui.db
      CHECK_INTERVAL: ${CHECK_INTERVAL:-60}
      TZ: ${TZ:-Asia/Shanghai}
      RESET_ADMIN_USER: ${RESET_ADMIN_USER:-admin}
      RESET_ADMIN_PASSWORD: ${RESET_ADMIN_PASSWORD:?please set RESET_ADMIN_PASSWORD in .env}
      RESET_SESSION_TTL: ${RESET_SESSION_TTL:-604800}
      RESET_LOGIN_MAX_ATTEMPTS: ${RESET_LOGIN_MAX_ATTEMPTS:-8}
      RESET_LOGIN_WINDOW: ${RESET_LOGIN_WINDOW:-600}
      RESET_COOKIE_SECURE: ${RESET_COOKIE_SECURE:-0}
      SUI_PANEL_URL: ${SUI_PANEL_URL:-}
      SUI_API_TOKEN: ${SUI_API_TOKEN:-}
      SUI_API_TIMEOUT: ${SUI_API_TIMEOUT:-3}
      SUI_ONLINE_CACHE_TTL: ${SUI_ONLINE_CACHE_TTL:-5}
      RESET_WEB_PORT: 8080
    ports:
      - ${RESET_WEB_BIND:-127.0.0.1:8787}:8080
    extra_hosts:
      - host.docker.internal:host-gateway
    volumes:
      - ${SUI_DB_DIR:-/usr/local/s-ui/db}:/data
EOF

BACKUP="$DB_DIR/s-ui.db.bak.$(date +%Y%m%d-%H%M%S)"
cp -p "$DB_DIR/s-ui.db" "$BACKUP"
echo "Database backup: $BACKUP"

$COMPOSE pull
$COMPOSE up -d

echo
echo "$APP_NAME is running."
echo "Install dir: $INSTALL_DIR"
echo "Image: $IMAGE_REPO:$IMAGE_VERSION"
echo "Web UI: http://$WEB_BIND"
echo "Admin user: $ADMIN_USER"
echo "Admin password: $ADMIN_PASSWORD"
echo
echo "Logs:"
echo "  cd $INSTALL_DIR && $COMPOSE logs -f"
