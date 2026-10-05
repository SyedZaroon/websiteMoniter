#!/bin/bash
# Removes the daily launchd job.
LABEL="com.website-monitor.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Daily schedule removed."
