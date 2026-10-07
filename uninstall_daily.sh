#!/bin/bash
# Removes the hourly launchd job and any previous daily version.
for LABEL in com.website-monitor.hourly com.website-monitor.daily; do
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
done
echo "Website monitor schedule removed."
