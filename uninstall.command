#!/usr/bin/env bash
# Delete everything Sparrow captured on this machine, and print a receipt.
# Double-click in Finder, or: bash uninstall.command --yes --all
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

if [ ! -x "$ROOT/.venv/bin/python" ]; then
    echo "Sparrow is not installed here - there is nothing to delete."
    echo "If you want the folder gone, just drag it to the Trash."
    read -r -p "Press return to close." _
    exit 1
fi

"$ROOT/.venv/bin/python" "$ROOT/scripts/uninstall.py" "$@"
status=$?
read -r -p "Press return to close." _
exit "$status"
