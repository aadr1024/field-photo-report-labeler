#!/bin/zsh
set -euo pipefail

LABEL="com.aadr1024.field-photo-report-labeler.photo-sync"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
APP_DIR="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
PLIST_SRC="$SCRIPT_DIR/$LABEL.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$APP_DIR/.automation/logs"
DOMAIN="gui/$(/usr/bin/id -u)"

/bin/mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
/usr/bin/sed "s#__APP_DIR__#$APP_DIR#g" "$PLIST_SRC" > "$PLIST_DEST"
/bin/chmod 644 "$PLIST_DEST"

/bin/launchctl bootout "$DOMAIN" "$PLIST_DEST" >/dev/null 2>&1 || true
/bin/launchctl bootstrap "$DOMAIN" "$PLIST_DEST"
/bin/launchctl enable "$DOMAIN/$LABEL"
/bin/launchctl kickstart -k "$DOMAIN/$LABEL"

echo "Installed $LABEL"
echo "Runs hourly and at agent load. Wrapper skips sync when not on AC power."
echo "Plist: $PLIST_DEST"
echo "Log: $LOG_DIR/photo-sync-wrapper.log"
