#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Single-step cold-start for the Streamlit image grid app.
#
# This script does all of it:
# 1) stop any prior streamlit/listener state
# 2) start the app and clipboard helper
# 3) optionally open the UI in browser (AUTO_OPEN=1)
# 4) wait until both service endpoints respond

AUTO_OPEN="${AUTO_OPEN:-0}"
BOOT_LOG="/tmp/image_grid_app_boot.log"
APP_URL="http://localhost:8501"
HELPER_URL="http://127.0.0.1:8503/index"

./restart_image_grid_app.sh "$@" >"$BOOT_LOG" 2>&1 &
APP_PID=$!

for i in $(seq 1 40); do
  if lsof -tiTCP:8501 -sTCP:LISTEN -nP >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

python3 - <<PY
import urllib.request
import time
HELPER_URL = "${HELPER_URL}"
for _ in range(40):
    try:
        urllib.request.urlopen(HELPER_URL, timeout=0.5)
        break
    except Exception:
        time.sleep(0.25)
PY

auto_open() {
  if [[ "$AUTO_OPEN" == "1" ]]; then
    open "$APP_URL"
  fi
}

auto_open

echo "Image Grid App starting. Log: $BOOT_LOG"
echo "Access URL: $APP_URL"
echo "Clipboard helper: http://127.0.0.1:8503"

echo "Startup complete. Use Ctrl+C to stop."
wait "$APP_PID"
