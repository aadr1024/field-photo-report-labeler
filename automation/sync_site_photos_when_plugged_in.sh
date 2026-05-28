#!/bin/zsh
set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
SYNC_SCRIPT="$APP_DIR/sync_site_photos.py"
STATE_DIR="$APP_DIR/.automation"
LOG_DIR="$STATE_DIR/logs"
LOCK_DIR="$STATE_DIR/photo-sync.lock"

mkdir -p "$LOG_DIR"

timestamp() {
  /bin/date -u +"%Y-%m-%dT%H:%M:%SZ"
}

{
  echo "[$(timestamp)] sync check requested"

  if ! /usr/bin/pmset -g batt | /usr/bin/head -n 1 | /usr/bin/grep -q "'AC Power'"; then
    echo "[$(timestamp)] skipped: Mac is not on AC power"
    exit 0
  fi

  if ! /bin/mkdir "$LOCK_DIR" 2>/dev/null; then
    echo "[$(timestamp)] skipped: previous sync still running"
    exit 0
  fi

  cleanup() {
    /bin/rmdir "$LOCK_DIR" 2>/dev/null || true
  }
  trap cleanup EXIT INT TERM

  cd "$APP_DIR" || exit 1
  /usr/bin/python3 "$SYNC_SCRIPT"
  status=$?
  echo "[$(timestamp)] sync finished with status $status"
  exit "$status"
} >> "$LOG_DIR/photo-sync-wrapper.log" 2>&1
