#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

SCRIPT_PORTS=(8501 8502 8503)
TIMEOUT_SECONDS=15

log() {
  echo "[image-grid] $*"
}

stop_port() {
  local port="$1"
  local pids
  pids="$(lsof -tiTCP:"$port" -sTCP:LISTEN -nP 2>/dev/null || true)"
  if [ -z "$pids" ]; then
    return
  fi

  log "Killing listeners on port $port: $pids"
  if [ -n "$pids" ]; then
    echo "$pids" | tr ' ' '\n' | xargs kill
  fi

  local waited=0
  while true; do
    if ! lsof -tiTCP:"$port" -sTCP:LISTEN -nP >/dev/null 2>&1; then
      break
    fi
    if [ "$waited" -ge "$TIMEOUT_SECONDS" ]; then
      log "Force stopping remaining listeners on $port"
      if [ -n "$pids" ]; then
        echo "$pids" | tr ' ' '\n' | xargs kill -9 || true
      fi
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
}

for port in "${SCRIPT_PORTS[@]}"; do
  stop_port "$port"
done

pkill -f "streamlit run image_grid_app.py" 2>/dev/null || true
pkill -f "streamlit_image_grid_app" 2>/dev/null || true

sleep 0.5
log "Starting image_grid_app.py"
exec uv run streamlit run image_grid_app.py
