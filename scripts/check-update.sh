#!/bin/bash
# check-update.sh - 唯一追更校准入口

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
python3 "$SCRIPT_DIR/refresh-drama-state-from-library.py" "$@" || exit $?
exec python3 "$SCRIPT_DIR/reconcile-drama-state.py"
