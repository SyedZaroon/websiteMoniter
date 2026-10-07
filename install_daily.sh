#!/bin/bash
# Installs a macOS launchd job that runs the checker every hour.
# The filename is retained for compatibility with earlier versions.
set -euo pipefail

if [ "$#" -ne 0 ]; then
  echo "This job runs every hour; no time arguments are accepted." >&2
  exit 2
fi

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.website-monitor.hourly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LEGACY_PLIST="$HOME/Library/LaunchAgents/com.website-monitor.daily.plist"

if [ ! -x "$PROJECT_DIR/.venv/bin/python" ]; then
  echo "Set up the virtual environment first (see README: Installation)." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/logs"

launchctl bootout "gui/$(id -u)" "$LEGACY_PLIST" 2>/dev/null || true
rm -f "$LEGACY_PLIST"

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
  <key>StartInterval</key><integer>3600</integer>
  <key>StandardOutPath</key><string>$PROJECT_DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/logs/launchd.err.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
printf 'Installed. The checker will run every hour.\n'
echo "Run it right now with:  launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "Logs: $PROJECT_DIR/logs/   Reports: $PROJECT_DIR/reports/latest-report.html"
