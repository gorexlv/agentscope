#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
VERSION="$(sed -n 's/^VERSION = "\([^"]*\)"/\1/p' "$ROOT/../agentscope_lib/cleanup.py")"
test -n "$VERSION"
swift build --package-path "$ROOT/AgentscopeBar" -c release
BIN="$(swift build --package-path "$ROOT/AgentscopeBar" -c release --show-bin-path)"
APP="$ROOT/build/AgentscopeBar.app"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN/AgentscopeBar" "$APP/Contents/MacOS/AgentscopeBar"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.agentscope.bar</string>
<key>CFBundleName</key><string>AgentscopeBar</string>
<key>CFBundleExecutable</key><string>AgentscopeBar</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>${VERSION}</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSAppleEventsUsageDescription</key><string>Open agentscope reports in Terminal.</string>
</dict></plist>
PLIST
codesign --force --sign - "$APP"
printf '%s\n' "$APP"
