#!/bin/bash
# Builds "dist/EMIL NetMon/EMIL NetMon.app" and dist/EMIL-NetMon.zip (ready to send to neighbours).
#
#   ./app/build_app.sh                          # uses DEFAULT_HUB from netmon.py
#   ./app/build_app.sh --hub "<web app URL>"    # app that uses your own hub instead
set -euo pipefail
cd "$(dirname "$0")/.."
HUB=""
while [ $# -gt 0 ]; do
  case "$1" in
    --hub) HUB="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 1 ;;
  esac
done

VERSION=$(python3 netmon.py version)
BUILD=build; DIST=dist
NAME="EMIL NetMon"
APP="$DIST/$NAME/$NAME.app"
rm -rf "$BUILD" "$DIST"; mkdir -p "$BUILD" "$APP/Contents/MacOS" "$APP/Contents/Resources"

# 1. Universal Wi-Fi helper (Apple Silicon + Intel, macOS 12+). Neighbours don't need Xcode.
python3 -c 'import netmon,sys; sys.stdout.write(netmon.WIFI_SWIFT)' > "$BUILD/wifistat.swift"
swiftc -O -target arm64-apple-macos12.0 -o "$BUILD/wifistat-arm64" "$BUILD/wifistat.swift"
swiftc -O -target x86_64-apple-macos12.0 -o "$BUILD/wifistat-x86_64" "$BUILD/wifistat.swift"
lipo -create -output "$APP/Contents/Resources/wifistat" "$BUILD/wifistat-arm64" "$BUILD/wifistat-x86_64"

# 2. Payload
cp netmon.py "$APP/Contents/Resources/netmon.py"
cp app/launcher.sh "$APP/Contents/MacOS/netmon"
chmod +x "$APP/Contents/MacOS/netmon"
if [ -n "$HUB" ]; then
  python3 -c 'import json,sys; print(json.dumps({"hub_url": sys.argv[1]}))' "$HUB" > "$APP/Contents/Resources/defaults.json"
fi
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$NAME</string>
  <key>CFBundleDisplayName</key><string>$NAME</string>
  <key>CFBundleIdentifier</key><string>com.emil.netmon</string>
  <key>CFBundleExecutable</key><string>netmon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
cp app/README.txt "$DIST/$NAME/Read me first.txt"

# 3. Ad-hoc signature (lets Apple Silicon Macs run it; still "unidentified developer")
codesign --force --deep -s - "$APP"
codesign --verify --deep "$APP"

# 4. Zip for sharing
ditto -c -k --keepParent "$DIST/$NAME" "$DIST/EMIL-NetMon.zip"
echo "Built $APP (v$VERSION)$( [ -n "$HUB" ] && echo ", own hub")"
echo "Share: $DIST/EMIL-NetMon.zip"
