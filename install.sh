#!/bin/bash
# Install systemd timer for Canvas ICS sync
set -e
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

echo "Installing Canvas ICS sync timer from $REPO_DIR/systemd/"
mkdir -p "$SYSTEMD_USER_DIR"
cp "$REPO_DIR/systemd/canvas-sync.service" "$SYSTEMD_USER_DIR/canvas-sync.service"
cp "$REPO_DIR/systemd/canvas-sync.timer" "$SYSTEMD_USER_DIR/canvas-sync.timer"

# If vault exists at ~/ObsidianVault, you may want vault-based service:
# Uncomment in systemd/canvas-sync.service or copy vault variant:
# cp "$REPO_DIR/systemd/canvas-sync.service" "$SYSTEMD_USER_DIR/canvas-sync.service"
# sed -i 's|%h/canvas-ics-sync/canvas_sync.py|%h/ObsidianVault/scripts/canvas_sync.py|' "$SYSTEMD_USER_DIR/canvas-sync.service"

systemctl --user daemon-reload
systemctl --user enable --now canvas-sync.timer
systemctl --user list-timers | grep canvas || true
echo "Done. Logs: journalctl --user -u canvas-sync.service -f  or  cat ~/canvas-ics-sync/.canvas_sync.log"
