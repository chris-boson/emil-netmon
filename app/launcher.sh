#!/bin/bash
# EMIL NetMon.app entry point: installs/updates the background monitor, then opens the dashboard.
RES="$(cd "$(dirname "$0")/../Resources" && pwd)"
DATA="$HOME/.netmon"
URL="http://127.0.0.1:8787/"

say() {  # macOS dialog; strip characters that would break the AppleScript string
  local msg="${1//\"/\'}"
  /usr/bin/osascript -e "display dialog \"$msg\" buttons {\"OK\"} default button 1 with title \"EMIL NetMon\"" >/dev/null 2>&1
}

# Find Python 3. /usr/bin/python3 only works once Apple's Command Line Tools are installed.
PY=""
for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -x "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  if /usr/bin/xcode-select -p >/dev/null 2>&1 && /usr/bin/python3 -c 'import sys' >/dev/null 2>&1; then
    PY=/usr/bin/python3
  else
    /usr/bin/xcode-select --install >/dev/null 2>&1
    say "EMIL NetMon needs Apple's free Command Line Tools, which include Python.

A macOS window offering to install them should have opened. Click Install, wait until it finishes (a few minutes), then open EMIL NetMon again."
    exit 0
  fi
fi

mkdir -p "$DATA"
APP="$(cd "$RES/../.." && pwd)"
echo "$APP" > "$DATA/app_path"   # lets "Update now" in the dashboard update this copy of the app

BUNDLED=$("$PY" "$RES/netmon.py" version 2>/dev/null)
INSTALLED=$("$PY" "$DATA/netmon.py" version 2>/dev/null)
newer() { "$PY" -c 'import sys; t=lambda v: tuple(int(x) for x in v.split(".")); sys.exit(0 if t(sys.argv[1]) > t(sys.argv[2]) else 1)' "$1" "$2" 2>/dev/null; }
RUNNING=""; /bin/launchctl print "gui/$(id -u)/com.netmon.agent" >/dev/null 2>&1 && RUNNING=1
FIRST=""; SRC=""
if [ -z "$INSTALLED" ]; then FIRST=1; SRC="$RES/netmon.py"
elif newer "$BUNDLED" "$INSTALLED"; then SRC="$RES/netmon.py"     # this app is newer: update
elif [ -z "$RUNNING" ]; then SRC="$DATA/netmon.py"                # not running: restart what's installed (never downgrade)
fi
if [ -n "$SRC" ]; then
  if ! OUT=$("$PY" "$SRC" install 2>&1); then
    say "EMIL NetMon could not start:

$OUT"
    exit 1
  fi
fi

for _ in $(seq 1 30); do
  /usr/bin/curl -s -o /dev/null "$URL" && break
  sleep 0.5
done
/usr/bin/open "$URL"
if [ -n "$FIRST" ]; then
  say "EMIL NetMon is now checking your connection every 10 seconds in the background, also after restarts.

Your dashboard just opened in the browser. Open EMIL NetMon again any time to get back to it. To stop, use the link at the bottom of the dashboard."
fi
