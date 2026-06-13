#!/usr/bin/env bash
set -euo pipefail

TITLE="${1:-}"
if [ -z "$TITLE" ]; then
  echo "用法: pansou-uc-probe.sh 片名" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

export PANSOU_ONLY_TYPES="${PANSOU_ONLY_TYPES:-uc}"
export PANSOU_DISPLAY_LIMIT="${PANSOU_DISPLAY_LIMIT:-20}"
export PANSOU_PROBE_MAX_CANDIDATES="${PANSOU_PROBE_MAX_CANDIDATES:-8}"
export PANSOU_PROBE_FOLDER_WAIT="${PANSOU_PROBE_FOLDER_WAIT:-8}"
export PANSOU_PROBE_GLOBAL_TIMEOUT="${PANSOU_PROBE_GLOBAL_TIMEOUT:-75}"
export PANSOU_FAST_UNCONFIRMED_UC="${PANSOU_FAST_UNCONFIRMED_UC:-1}"

rm -f /tmp/media_resources.txt /tmp/pansou_results.json /tmp/probe_results.json /tmp/probe_mounts.json /tmp/media_selection_map.json

echo "🎯 PanSou UC 专项搜索: $TITLE"
python3 "$SCRIPT_DIR/pansou-search.py" "$TITLE"
echo
python3 "$SCRIPT_DIR/probe_pansou.py"
