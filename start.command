#!/usr/bin/env bash
# Start Sparrow (macOS). Double-click in Finder, or: bash start.command
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "Sparrow is not installed yet - run install.command first."
    read -r -p "Press return to close." _
    exit 1
fi

# Gatekeeper quarantines everything in a downloaded ZIP. Clearing it on the
# folder we are already running from is safe and saves the tester a support
# call about "Sparrow.app is damaged".
xattr -dr com.apple.quarantine "$ROOT" 2>/dev/null

"$ROOT/.venv/bin/python" run_all.py
status=$?
[ "$status" -eq 0 ] || echo "Sparrow exited with status $status."
read -r -p "Press return to close." _
exit "$status"
