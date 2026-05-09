#!/usr/bin/env bash
# Spike helper — launch a chrome instance bound to V1's already-logged-in
# workstation profile, with the V2 extension auto-loaded.
#
# Why this exists: V1 login_session.py logs the operator into chrome under
# `~/Library/Application Support/FlowHarvester/profiles/<WS_ID>` and then
# closes that chrome. If the V2 extension is loaded in the operator's
# everyday chrome (a different user-data-dir), it sees the operator's
# everyday Google account — not <WS_ID>'s — and tasks dispatched against
# WS_D end up driving the wrong account.
#
# This script starts a *separate* chrome process re-using the V1 profile
# (so the WS_D login stays valid) plus --load-extension so V2 is in that
# chrome too. Result: the extension reports register ws_id=<WS_ID> from
# the right account, and ExtensionFlowPort RPCs hit the right Flow page.
#
# Usage:
#   ./scripts/start-spike-chrome.sh WS_D
#
# Coexists with your everyday chrome (different --user-data-dir = separate
# chrome process tree, separate cookies / extensions).

set -euo pipefail

WS_ID="${1:?Usage: $0 <WS_ID>  (e.g. WS_D)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXT_DIR="$REPO_ROOT/extension/dist"

case "$(uname -s)" in
  Darwin)
    CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    PROFILE_DIR="$HOME/Library/Application Support/FlowHarvester/profiles/$WS_ID"
    ;;
  Linux)
    CHROME_BIN="$(command -v google-chrome 2>/dev/null || command -v chromium 2>/dev/null || true)"
    PROFILE_DIR="$HOME/.local/share/FlowHarvester/profiles/$WS_ID"
    ;;
  *)
    echo "ERROR: unsupported OS '$(uname -s)' — please launch chrome manually" >&2
    exit 1
    ;;
esac

if [ ! -x "$CHROME_BIN" ]; then
  echo "ERROR: chrome binary not found / not executable: $CHROME_BIN" >&2
  exit 1
fi

if [ ! -d "$PROFILE_DIR" ]; then
  echo "ERROR: V1 profile not found at:" >&2
  echo "  $PROFILE_DIR" >&2
  echo >&2
  echo "Run V1 login flow first from the dashboard ('登录' button on the workstation)" >&2
  echo "or double-check the WS_ID spelling (case-sensitive: '$WS_ID')." >&2
  exit 1
fi

if [ ! -d "$EXT_DIR" ]; then
  echo "ERROR: extension not built at $EXT_DIR" >&2
  echo "Run: cd extension && npm install && npm run build" >&2
  exit 1
fi

cat <<EOF
== V2 spike chrome launcher ==
WS_ID:      $WS_ID
profile:    $PROFILE_DIR
extension:  $EXT_DIR
chrome:     $CHROME_BIN

First time using this profile with the V2 extension?
  1. After chrome opens, click the Flow Harvester icon (top-right toolbar).
  2. In the popup, set "Workstation" to exactly "$WS_ID" (match V1 DB).
  3. Click 保存并重连. Co-Pilot's /spike/extension page should show it online.

Coexists with your everyday chrome — they are different processes.
Closing this window does NOT close your other chrome.

EOF

# Some chrome flags worth noting:
#   --user-data-dir       Bind to V1's already-logged-in profile.
#   --load-extension      Auto-install the unpacked V2 build (no chrome://extensions needed).
#   --no-first-run        Skip the welcome screen.
#   --no-default-browser-check
# We deliberately do NOT pass --headless / --new-window — we want a
# normal visible window the operator can interact with.
exec "$CHROME_BIN" \
  --user-data-dir="$PROFILE_DIR" \
  --load-extension="$EXT_DIR" \
  --no-first-run \
  --no-default-browser-check \
  "https://labs.google/fx/tools/flow"
