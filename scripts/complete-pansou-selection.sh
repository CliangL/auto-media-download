#!/bin/bash
# complete-pansou-selection.sh - finish a verified PanSou selection through the standard pipeline.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROBE_FILE="/tmp/probe_results.json"
PANSOU_FILE="/tmp/pansou_results.json"
MAP_FILE="/tmp/media_selection_map.json"
SELECTION="${1:-1}"

infer_media_type() {
    local current="${1:-}"
    local text="${2:-}"
    if [[ "$current" == "movie" || "$current" == "anime" || "$current" == "variety" || "$current" == "documentary" || "$current" == "tv" ]]; then
        printf '%s\n' "$current"
        return 0
    fi
    if [[ "$text" == *"综艺"* ]]; then
        printf 'variety\n'
    elif [[ "$text" == *"动漫"* ]]; then
        printf 'anime\n'
    elif [[ "$text" == *"纪录片"* ]]; then
        printf 'documentary\n'
    else
        printf 'tv\n'
    fi
}

URL=""
COUNT=""
NOTE=""
PROBE_VIDEO_PATH=""
PROBE_FOLDER_PATH=""
PROBE_SHARE_ID=""
MODE="pansou"

if [ -f "$MAP_FILE" ] && [ "${AUTO_MEDIA_IGNORE_SELECTION_MAP:-0}" != "1" ]; then
    eval "$(python3 - "$MAP_FILE" "$SELECTION" <<'PYEOF'
import json
import shlex
import sys

path, raw_selection = sys.argv[1], sys.argv[2]
try:
    selection = int(raw_selection)
except Exception:
    raise SystemExit
try:
    rows = json.load(open(path, encoding="utf-8"))
except Exception:
    rows = []
entry = next((r for r in rows if int(r.get("display_index") or 0) == selection), None)
if not entry:
    raise SystemExit
kind = str(entry.get("kind") or "")
print("MODE=" + shlex.quote(kind))
if kind == "pansou":
    print("SELECTION=" + shlex.quote(str(entry.get("probe_index") or selection)))
elif kind == "xiaoya":
    print("XIAOYA_SELECTION=" + shlex.quote(str(entry.get("resource_index") or "")))
PYEOF
)"
fi

if [ "${MODE:-pansou}" = "xiaoya" ]; then
    [ -n "${XIAOYA_SELECTION:-}" ] || { echo "❌ 统一选择映射缺少 Xiaoya 序号"; exit 1; }
    echo "✅ 展示序号 $1 映射为 Xiaoya/AList 第 $XIAOYA_SELECTION 项"
    if [ "${AUTO_MEDIA_DRY_RUN:-0}" = "1" ]; then
        echo "DRY_RUN kind=xiaoya resource_index=$XIAOYA_SELECTION"
        exit 0
    fi
    exec bash "$SCRIPT_DIR/handle-selection.sh" "$XIAOYA_SELECTION"
fi

if [ ! -f "$PROBE_FILE" ] && [ ! -f "$PANSOU_FILE" ]; then
    echo "❌ 没有可用的 PanSou 结果，请先运行 pansou-search.py 和 probe_pansou.py"
    exit 1
fi

if [ -f "$PROBE_FILE" ]; then
    eval "$(python3 - "$PROBE_FILE" "$SELECTION" <<'PYEOF'
import json
import shlex
import sys

path, raw_selection = sys.argv[1], sys.argv[2]
try:
    selection = max(1, int(raw_selection))
except Exception:
    selection = 1
try:
    rows = json.load(open(path, encoding="utf-8"))
except Exception:
    rows = []
rows = [r for r in rows if int(r.get("count") or 0) > 0]
if not rows:
    raise SystemExit(0)
idx = min(selection, len(rows)) - 1
row = rows[idx]
print("URL=" + shlex.quote(str(row.get("url") or "")))
print("COUNT=" + shlex.quote(str(row.get("count") or "")))
print("NOTE=" + shlex.quote(str(row.get("note") or "")))
print("PROBE_VIDEO_PATH=" + shlex.quote(str(row.get("video_path") or "")))
print("PROBE_FOLDER_PATH=" + shlex.quote(str(row.get("folder_path") or "")))
print("PROBE_SHARE_ID=" + shlex.quote(str(row.get("share_id") or "")))
PYEOF
)"
fi

if [ -z "$URL" ]; then
    eval "$(python3 - "$PANSOU_FILE" "$SELECTION" <<'PYEOF'
import json
import shlex
import sys

path, raw_selection = sys.argv[1], sys.argv[2]
try:
    selection = max(1, int(raw_selection))
except Exception:
    selection = 1
rows = json.load(open(path, encoding="utf-8"))
idx = min(selection, len(rows)) - 1
row = rows[idx]
print("URL=" + shlex.quote(str(row.get("url") or "")))
print("COUNT=")
print("NOTE=" + shlex.quote(str(row.get("note") or "")))
PYEOF
)"
fi

[ -z "$URL" ] && { echo "❌ 无法解析选择结果"; exit 1; }

export MEDIA_TYPE="$(infer_media_type "${MEDIA_TYPE:-}" "$NOTE")"
if [ -n "$COUNT" ] && [ -z "${TOTAL_EP:-}" ]; then
    export TOTAL_EP="$COUNT"
fi
if [ -n "$PROBE_FOLDER_PATH" ]; then
    export PROBE_FOLDER_PATH
fi
if [ -n "$PROBE_VIDEO_PATH" ]; then
    export PROBE_VIDEO_PATH
fi
if [ -n "$PROBE_SHARE_ID" ]; then
    export PROBE_SHARE_ID
fi
if [ -n "$NOTE" ] && [ -z "${AUTO_MEDIA_TITLE:-}" ]; then
    export AUTO_MEDIA_TITLE="$NOTE"
fi

echo "✅ 使用已核验资源: ${NOTE:-$URL}"
[ -n "${TOTAL_EP:-}" ] && echo "📺 已核验集数: $TOTAL_EP"
[ -n "${PROBE_VIDEO_PATH:-}" ] && echo "📂 已核验视频目录: $PROBE_VIDEO_PATH"
echo "🚀 进入标准导入 + STRM 生成流程..."
if [ "${AUTO_MEDIA_DRY_RUN:-0}" = "1" ]; then
    echo "DRY_RUN kind=pansou selection=$SELECTION url=$URL count=${COUNT:-}"
    exit 0
fi

exec bash "$SCRIPT_DIR/handle-pansou.sh" "$URL"
