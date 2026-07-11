#!/usr/bin/env bash
#
# setup-worker-user.sh — Create the dedicated stockwatch system user and
# prepare directory ownership for the hardened systemd worker service.
#
# Run as root (or with sudo) on the production server BEFORE installing
# the stock-watchlist-report-worker.service unit.
#
# Usage:
#   sudo bash deploy/setup-worker-user.sh [/opt/Stock_watch_list]
#
set -euo pipefail

APP_DIR="${1:-/opt/Stock_watch_list}"
WORKER_USER="stockwatch"
WORKER_GROUP="stockwatch"

# ── Preflight checks ────────────────────────────────────────────
if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: Run this script as root (or with sudo)." >&2
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "ERROR: Application directory not found: $APP_DIR" >&2
  exit 1
fi

echo "==> Setting up worker user for $APP_DIR"

# ── 1. Create dedicated system user/group ───────────────────────
if ! getent group "$WORKER_GROUP" >/dev/null 2>&1; then
  groupadd --system "$WORKER_GROUP"
  echo "    Created system group: $WORKER_GROUP"
else
  echo "    Group already exists: $WORKER_GROUP"
fi

if ! id "$WORKER_USER" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "$WORKER_GROUP" \
    --home-dir "$APP_DIR/data" \
    --no-create-home \
    --shell /usr/sbin/nologin \
    --comment "Stock Watchlist Report Worker" \
    "$WORKER_USER"
  echo "    Created system user: $WORKER_USER"
else
  echo "    User already exists: $WORKER_USER"
fi

# ── 2. Create data/ directory ───────────────────────────────────
DATA_DIR="$APP_DIR/data"
mkdir -p "$DATA_DIR"
echo "    Data directory: $DATA_DIR"

# ── 3. Migrate existing job database if present ─────────────────
OLD_DB="$APP_DIR/daily_report_jobs.db"
NEW_DB="$DATA_DIR/daily_report_jobs.db"

if [[ -f "$OLD_DB" && ! -f "$NEW_DB" ]]; then
  echo "    Migrating daily_report_jobs.db to data/"
  mv "$OLD_DB" "$NEW_DB"
  # Move WAL and SHM files if they exist
  for suffix in "-wal" "-shm"; do
    if [[ -f "${OLD_DB}${suffix}" ]]; then
      mv "${OLD_DB}${suffix}" "${NEW_DB}${suffix}"
    fi
  done
elif [[ -f "$OLD_DB" && -f "$NEW_DB" ]]; then
  echo "    WARNING: Both $OLD_DB and $NEW_DB exist. Skipping migration." >&2
  echo "    Manual resolution required." >&2
fi

# ── 4. Ensure runs/ directory exists ────────────────────────────
RUNS_DIR="$APP_DIR/daily_report/runs"
mkdir -p "$RUNS_DIR"

# ── 5. Set ownership and permissions ────────────────────────────
# The worker needs read access to the application code and venv.
chown -R "$WORKER_USER:$WORKER_GROUP" "$DATA_DIR"
chown -R "$WORKER_USER:$WORKER_GROUP" "$RUNS_DIR"
chmod 700 "$DATA_DIR"
chmod 700 "$RUNS_DIR"

# Ensure the database file is restricted
if [[ -f "$NEW_DB" ]]; then
  chown "$WORKER_USER:$WORKER_GROUP" "$NEW_DB"
  chmod 600 "$NEW_DB"
fi

# The .env file must be readable by the worker user
ENV_FILE="$APP_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
  chown "$WORKER_USER:$WORKER_GROUP" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "    Secured .env file"
fi

# Ensure application code is at least readable by the worker user
# (world-readable is typical for installed application files)
chmod -R a+rX "$APP_DIR" 2>/dev/null || true

echo ""
echo "==> Setup complete."
echo ""
echo "Next steps:"
echo "  1. sudo cp deploy/stock-watchlist-report-worker.service /etc/systemd/system/"
echo "  2. sudo systemctl daemon-reload"
echo "  3. sudo systemctl enable --now stock-watchlist-report-worker"
echo "  4. sudo systemctl status stock-watchlist-report-worker --no-pager"
echo ""
echo "Verify security settings:"
echo "  systemd-analyze security stock-watchlist-report-worker"
echo ""
echo "Worker logs:"
echo "  sudo journalctl -u stock-watchlist-report-worker -f"
