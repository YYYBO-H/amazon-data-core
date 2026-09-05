#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

cd "$REPO_DIR"

USE_EXISTING=false
TERMINAL_CONFIG=false
if [ "${1:-}" = "--use-existing-config" ]; then
  USE_EXISTING=true
elif [ "${1:-}" = "--terminal-config" ]; then
  TERMINAL_CONFIG=true
elif [ "$#" -gt 0 ]; then
  echo "Usage: ./scripts/onboard.sh [--use-existing-config|--terminal-config]" >&2
  exit 2
fi

"$SCRIPT_DIR/install.sh"

if [ "$USE_EXISTING" = true ]; then
  python3 "$SCRIPT_DIR/configure.py" --status --require-sp-api
else
  echo
  if [ "$TERMINAL_CONFIG" = true ]; then
    echo "The local runtime passed. Amazon credentials will now be entered privately in this terminal."
    python3 "$SCRIPT_DIR/configure.py"
    "$SCRIPT_DIR/sync-all.sh"
  else
    echo "The local runtime passed. Launching private localhost authorization in the background."
    python3 "$SCRIPT_DIR/onboard_background.py" launch
  fi
fi

if [ "$USE_EXISTING" = true ]; then
  "$SCRIPT_DIR/sync-all.sh"
fi
