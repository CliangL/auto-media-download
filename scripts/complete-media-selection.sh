#!/bin/bash
# complete-media-selection.sh - consume the exact display index shown to user.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MAP_FILE="/tmp/media_selection_map.json"
HYPERMS_SCRIPT="$HOME/.openclaw/skills/media/hyperms-strm/scripts/hyperms-strm.py"
SELECTION="${1:-}"

[ -n "$SELECTION" ] || { echo "❌ 缺少选择序号"; exit 1; }
[ -f "$MAP_FILE" ] || { echo "❌ 缺少统一选择映射: $MAP_FILE，请先重新搜索/核验资源"; exit 1; }

eval "$(python3 - "$MAP_FILE" "$SELECTION" <<'PYEOF'
import json
import shlex
import sys

path, raw_selection = sys.argv[1], sys.argv[2]
try:
    selection = int(raw_selection)
except Exception:
    print("MODE=")
    raise SystemExit
try:
    rows = json.load(open(path, encoding="utf-8"))
except Exception:
    rows = []
entry = next((r for r in rows if int(r.get("display_index") or 0) == selection), None)
if not entry:
    print("MODE=")
    raise SystemExit
kind = str(entry.get("kind") or "")
print("MODE=" + shlex.quote(kind))
print("TITLE=" + shlex.quote(str(entry.get("title") or "")))
if kind == "xiaoya":
    print("RESOURCE_INDEX=" + shlex.quote(str(entry.get("resource_index") or "")))
elif kind == "pansou":
    print("PROBE_INDEX=" + shlex.quote(str(entry.get("probe_index") or "")))
    print("COUNT=" + shlex.quote(str(entry.get("count") or "")))
    print("VIDEO_PATH=" + shlex.quote(str(entry.get("video_path") or "")))
elif kind == "hyperms":
    print("HYPERMS_INDEX=" + shlex.quote(str(entry.get("hyperms_index") or "")))
    print("MEDIA_TYPE_FROM_MAP=" + shlex.quote(str(entry.get("media_type") or "")))
    print("CLOUD_TYPE=" + shlex.quote(str(entry.get("cloud_type") or "")))
    print("ROUTE=" + shlex.quote(str(entry.get("route") or "")))
    print("KEYWORD=" + shlex.quote(str(entry.get("keyword") or "")))
PYEOF
)"

[ -n "${MODE:-}" ] || { echo "❌ 无效的展示序号: $SELECTION"; exit 1; }

case "$MODE" in
  xiaoya)
    echo "✅ 使用展示序号 $SELECTION -> Xiaoya/AList 第 ${RESOURCE_INDEX} 项: ${TITLE:-}"
    if [ "${AUTO_MEDIA_DRY_RUN:-0}" = "1" ]; then
      echo "DRY_RUN kind=xiaoya resource_index=$RESOURCE_INDEX"
      exit 0
    fi
    exec bash "$SCRIPT_DIR/handle-selection.sh" "$RESOURCE_INDEX"
    ;;
  pansou)
    echo "✅ 使用展示序号 $SELECTION -> PanSou 已核验第 ${PROBE_INDEX} 项: ${TITLE:-}"
    [ -n "${COUNT:-}" ] && [ -z "${TOTAL_EP:-}" ] && export TOTAL_EP="$COUNT"
    if [ "${AUTO_MEDIA_DRY_RUN:-0}" = "1" ]; then
      echo "DRY_RUN kind=pansou probe_index=$PROBE_INDEX count=${COUNT:-}"
      exit 0
    fi
    AUTO_MEDIA_IGNORE_SELECTION_MAP=1 exec bash "$SCRIPT_DIR/complete-pansou-selection.sh" "$PROBE_INDEX"
    ;;
  hyperms)
    echo "✅ 使用展示序号 $SELECTION -> HyperMS 第 ${HYPERMS_INDEX} 项: ${TITLE:-}"
    [ -f "$HYPERMS_SCRIPT" ] || { echo "❌ 找不到 HyperMS helper: $HYPERMS_SCRIPT"; exit 1; }
    media_type="${MEDIA_TYPE:-${MEDIA_TYPE_FROM_MAP:-tv}}"
    if [ -z "$media_type" ]; then
      media_type="tv"
    fi
    generate_title="${KEYWORD:-${TITLE:-}}"
    if [ "${AUTO_MEDIA_DRY_RUN:-0}" = "1" ]; then
      echo "DRY_RUN kind=hyperms hyperms_index=$HYPERMS_INDEX media_type=$media_type title=$generate_title cloud=${CLOUD_TYPE:-} route=${ROUTE:-}"
      exit 0
    fi
    exec python3 "$HYPERMS_SCRIPT" generate "$HYPERMS_INDEX" --title "$generate_title" --media-type "$media_type" --wait
    ;;
  *)
    echo "❌ 未知选择类型: $MODE"
    exit 1
    ;;
esac
