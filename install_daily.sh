#!/bin/bash
# Installs a macOS launchd job that runs the checker every day.
# Usage: scripts/install_daily.sh [HOUR] [MINUTE]     (default 07:30)
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
HOUR="${1:-7}"
MINUTE="${2:-30}"
LABEL="com.website-monitor.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  echo "Set up the virtual environment first (see README: Installation)." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT_DIR/.venv/bin/python</string>
    <string>$PROJECT_DIR/main.py</string>
    <string>--exit-zero</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$PROJECT_DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
printf 'Installed. The checker will run every day at %02d:%02d.\n' "$HOUR" "$MINUTE"
echo "Run it right now with:  launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "Logs: $PROJECT_DIR/logs/   Reports: $PROJECT_DIR/reports/latest-report.html"
