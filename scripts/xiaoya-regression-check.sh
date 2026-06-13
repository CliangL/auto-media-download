#!/bin/bash
# Run an isolated Xiaoya STRM regression without touching drama-state.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$SKILL_DIR/config/media-config.json"
MEDIA_SCRIPT="$SCRIPT_DIR/media-download-v2.sh"
COMPLETE_SCRIPT="$SCRIPT_DIR/complete-media-selection.sh"
RUN_STAMP="$(date +%Y%m%d_%H%M%S)"

TITLE="${AUTO_MEDIA_REGRESSION_TITLE:-权力的游戏}"
MEDIA_TYPE="${AUTO_MEDIA_REGRESSION_MEDIA_TYPE:-tv}"
CUSTOM_PATH="${AUTO_MEDIA_REGRESSION_STRM_PATH:-Hermes回归测试/Xiaoya_${RUN_STAMP}}"
KEEP_OUTPUT="${AUTO_MEDIA_REGRESSION_KEEP_OUTPUT:-0}"

if [ ! -f "$CONFIG_FILE" ]; then
  echo "XIAOYA_REGRESSION_OK=0"
  echo "ERROR=missing_config"
  exit 1
fi

quote() {
  printf "%q" "$1"
}

NAS_HOST="$(jq -r '.nas.tailscale_ip // .nas.host' "$CONFIG_FILE")"
NAS_USER="$(jq -r '.nas.user' "$CONFIG_FILE")"
NAS_PASS="$(jq -r '.nas.password // ""' "$CONFIG_FILE")"
SSH_PORT="$(jq -r '.nas.ssh_port // 22' "$CONFIG_FILE")"

IS_LOCAL=0
if [ "$NAS_HOST" = "127.0.0.1" ] || [ "$NAS_HOST" = "localhost" ]; then
  IS_LOCAL=1
fi

SSH_USES_PASSWORD=0
if [ "$IS_LOCAL" -eq 0 ]; then
  if ! ssh -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=3 "$NAS_USER@$NAS_HOST" "echo ok" >/dev/null 2>&1; then
    SSH_USES_PASSWORD=1
  fi
fi

remote_cmd() {
  local script="$1"
  if [ "$IS_LOCAL" -eq 1 ]; then
    sh -c "$script"
  elif [ "$SSH_USES_PASSWORD" -eq 1 ]; then
    sshpass -p "$NAS_PASS" ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$NAS_USER@$NAS_HOST" "$script"
  else
    ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$NAS_USER@$NAS_HOST" "$script"
  fi
}

cleanup() {
  if [ "$KEEP_OUTPUT" = "1" ]; then
    return
  fi
  if [ -n "${CUSTOM_NAS_DIR:-}" ]; then
    remote_cmd "rm -rf $(quote "$CUSTOM_NAS_DIR")" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

CUSTOM_NAS_DIR="$(
  python3 - "$CUSTOM_PATH" "$SCRIPT_DIR" <<'PY'
import sys
sys.path.insert(0, sys.argv[2])
from strm_layout import resolve_strm_dir
print(resolve_strm_dir("HermesXiaoyaRegression", "tv", custom_path=sys.argv[1]))
PY
)"

rm -f /tmp/media_resources.txt /tmp/media_selection_map.json /tmp/probe_results.json

echo "XIAOYA_TEST_TITLE=$TITLE"
echo "XIAOYA_TEST_MEDIA_TYPE=$MEDIA_TYPE"
echo "XIAOYA_TEST_CUSTOM_PATH=$CUSTOM_PATH"

AUTO_MEDIA_AUTO_PANSOU_FALLBACK=0 bash "$MEDIA_SCRIPT" "下载《${TITLE}》"

eval "$(
  python3 - <<'PY'
import json
import shlex
from pathlib import Path

path = Path("/tmp/media_selection_map.json")
rows = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
entry = next((r for r in rows if r.get("kind") == "xiaoya"), None)
if not entry:
    print("DISPLAY_INDEX=")
else:
    print("DISPLAY_INDEX=" + shlex.quote(str(entry.get("display_index") or "")))
    print("RESOURCE_INDEX=" + shlex.quote(str(entry.get("resource_index") or "")))
    print("SOURCE_PATH=" + shlex.quote(str(entry.get("path") or "")))
    print("SOURCE_TITLE=" + shlex.quote(str(entry.get("title") or "")))
PY
)"

if [ -z "${DISPLAY_INDEX:-}" ]; then
  echo "XIAOYA_SEARCH_OK=0"
  echo "XIAOYA_REGRESSION_OK=0"
  exit 2
fi

echo "XIAOYA_SEARCH_OK=1"
echo "XIAOYA_DISPLAY_INDEX=$DISPLAY_INDEX"
echo "XIAOYA_RESOURCE_INDEX=$RESOURCE_INDEX"
echo "XIAOYA_SOURCE_PATH=$SOURCE_PATH"

AUTO_MEDIA_DRY_RUN=1 bash "$COMPLETE_SCRIPT" "$DISPLAY_INDEX"

GENERATE_LOG="$(mktemp "${TMPDIR:-/tmp}/xiaoya-regression-generate.XXXXXX")"
MEDIA_TYPE="$MEDIA_TYPE" \
STRM_CUSTOM_PATH="$CUSTOM_PATH" \
AUTO_MEDIA_SKIP_STATE=1 \
  bash "$COMPLETE_SCRIPT" "$DISPLAY_INDEX" | tee "$GENERATE_LOG"

STRM_TOTAL="$(grep '===STRM_TOTAL===' "$GENERATE_LOG" | tail -1 | grep -oE '[0-9]+' || true)"
STRM_ADDED="$(grep '===STRM_COUNT===' "$GENERATE_LOG" | tail -1 | grep -oE '[0-9]+' || true)"
STRM_TOTAL="${STRM_TOTAL:-0}"
STRM_ADDED="${STRM_ADDED:-0}"

if [ "$STRM_TOTAL" -le 0 ]; then
  echo "XIAOYA_STRM_OK=0"
  echo "XIAOYA_REGRESSION_OK=0"
  exit 3
fi

STRM_COUNT_FS="$(
  remote_cmd "find $(quote "$CUSTOM_NAS_DIR") -type f -name '*.strm' 2>/dev/null | wc -l | tr -d ' '"
)"
STRM_COUNT_FS="${STRM_COUNT_FS:-0}"
echo "XIAOYA_STRM_TOTAL=$STRM_TOTAL"
echo "XIAOYA_STRM_ADDED=$STRM_ADDED"
echo "XIAOYA_STRM_COUNT_FS=$STRM_COUNT_FS"

SAMPLE_URLS_FILE="$(mktemp "${TMPDIR:-/tmp}/xiaoya-regression-urls.XXXXXX")"
remote_cmd "python3 -c 'import os,sys
root=sys.argv[1]
limit=int(sys.argv[2])
count=0
for base, _, files in os.walk(root):
    for name in sorted(files):
        if not name.endswith(\".strm\"):
            continue
        url=open(os.path.join(base, name), encoding=\"utf-8\", errors=\"replace\").read().strip()
        if url:
            print(url)
            count += 1
        if count >= limit:
            raise SystemExit
' $(quote "$CUSTOM_NAS_DIR") 8" > "$SAMPLE_URLS_FILE"

SAMPLE_URL_COUNT="$(wc -l < "$SAMPLE_URLS_FILE" | tr -d ' ')"
SAMPLE_URL_COUNT="${SAMPLE_URL_COUNT:-0}"
echo "XIAOYA_SAMPLE_URL_COUNT=$SAMPLE_URL_COUNT"

if [ "$SAMPLE_URL_COUNT" -le 0 ]; then
  echo "XIAOYA_SAMPLE_URL_OK=0"
  echo "XIAOYA_REGRESSION_OK=0"
  exit 4
fi

echo "XIAOYA_SAMPLE_URL_OK=1"
HTTP_CODE=0
RANGE_BYTES=0
PLAYABLE_INDEX=0
PLAYABLE_CHECK_MODE=none
idx=0
while IFS= read -r SAMPLE_URL; do
  idx=$((idx + 1))
  [ -n "$SAMPLE_URL" ] || continue
  RANGE_FILE="$(mktemp "${TMPDIR:-/tmp}/xiaoya-regression-range.XXXXXX")"
  HTTP_INFO="$(curl -L --range 0-127 -sS --connect-timeout 8 --max-time 45 -o "$RANGE_FILE" -w '%{http_code} %{size_download}' "$SAMPLE_URL" || true)"
  candidate_code="$(printf '%s' "$HTTP_INFO" | awk '{print $1}')"
  candidate_bytes="$(printf '%s' "$HTTP_INFO" | awk '{print $2}')"
  candidate_code="${candidate_code:-0}"
  candidate_bytes="${candidate_bytes:-0}"
  rm -f "$RANGE_FILE"
  if { [ "$candidate_code" = "200" ] || [ "$candidate_code" = "206" ]; } && [ "$candidate_bytes" -gt 0 ]; then
    HTTP_CODE="$candidate_code"
    RANGE_BYTES="$candidate_bytes"
    PLAYABLE_INDEX="$idx"
    PLAYABLE_CHECK_MODE=local
    break
  fi

  REMOTE_TMP="/tmp/xiaoya-regression-range-${RUN_STAMP}-${idx}.bin"
  HTTP_INFO="$(
    remote_cmd "curl -L --range 0-127 -sS --connect-timeout 8 --max-time 45 -o $(quote "$REMOTE_TMP") -w '%{http_code} %{size_download}' $(quote "$SAMPLE_URL"); rc=\$?; rm -f $(quote "$REMOTE_TMP"); exit \$rc" || true
  )"
  candidate_code="$(printf '%s' "$HTTP_INFO" | awk '{print $1}')"
  candidate_bytes="$(printf '%s' "$HTTP_INFO" | awk '{print $2}')"
  candidate_code="${candidate_code:-0}"
  candidate_bytes="${candidate_bytes:-0}"
  if { [ "$candidate_code" = "200" ] || [ "$candidate_code" = "206" ]; } && [ "$candidate_bytes" -gt 0 ]; then
    HTTP_CODE="$candidate_code"
    RANGE_BYTES="$candidate_bytes"
    PLAYABLE_INDEX="$idx"
    PLAYABLE_CHECK_MODE=remote_same_url
    break
  fi

  LOOPBACK_URL="$(python3 - "$SAMPLE_URL" <<'PY'
import sys
from urllib.parse import urlsplit, urlunsplit
url = sys.argv[1]
parts = urlsplit(url)
if parts.scheme and parts.netloc:
    print(urlunsplit((parts.scheme, "127.0.0.1:5678", parts.path, parts.query, parts.fragment)))
else:
    print(url)
PY
)"
  HTTP_INFO="$(
    remote_cmd "curl -L --range 0-127 -sS --connect-timeout 8 --max-time 45 -o $(quote "$REMOTE_TMP") -w '%{http_code} %{size_download}' $(quote "$LOOPBACK_URL"); rc=\$?; rm -f $(quote "$REMOTE_TMP"); exit \$rc" || true
  )"
  candidate_code="$(printf '%s' "$HTTP_INFO" | awk '{print $1}')"
  candidate_bytes="$(printf '%s' "$HTTP_INFO" | awk '{print $2}')"
  candidate_code="${candidate_code:-0}"
  candidate_bytes="${candidate_bytes:-0}"
  if { [ "$candidate_code" = "200" ] || [ "$candidate_code" = "206" ]; } && [ "$candidate_bytes" -gt 0 ]; then
    HTTP_CODE="$candidate_code"
    RANGE_BYTES="$candidate_bytes"
    PLAYABLE_INDEX="$idx"
    PLAYABLE_CHECK_MODE=remote_loopback
    break
  fi
  HTTP_CODE="$candidate_code"
  RANGE_BYTES="$candidate_bytes"
done < "$SAMPLE_URLS_FILE"
rm -f "$SAMPLE_URLS_FILE"

echo "XIAOYA_HTTP_CODE=$HTTP_CODE"
echo "XIAOYA_RANGE_BYTES=$RANGE_BYTES"
echo "XIAOYA_PLAYABLE_SAMPLE_INDEX=$PLAYABLE_INDEX"
echo "XIAOYA_PLAYABLE_CHECK_MODE=$PLAYABLE_CHECK_MODE"

if [ "$HTTP_CODE" != "200" ] && [ "$HTTP_CODE" != "206" ]; then
  echo "XIAOYA_PLAYABLE_OK=0"
  echo "XIAOYA_REGRESSION_OK=0"
  exit 5
fi
if [ "$RANGE_BYTES" -le 0 ]; then
  echo "XIAOYA_PLAYABLE_OK=0"
  echo "XIAOYA_REGRESSION_OK=0"
  exit 5
fi
echo "XIAOYA_PLAYABLE_OK=1"

DELETED_STRM="$(
  remote_cmd "python3 -c 'import os,sys
root=sys.argv[1]
for base, _, files in os.walk(root):
    for name in sorted(files):
        if name.endswith(\".strm\"):
            path=os.path.join(base, name)
            os.remove(path)
            print(path)
            raise SystemExit
' $(quote "$CUSTOM_NAS_DIR")"
)"

UPDATE_CHECK_LOG="$(mktemp "${TMPDIR:-/tmp}/xiaoya-regression-update.XXXXXX")"
python3 - "$TITLE" "$MEDIA_TYPE" "$SOURCE_PATH" "$CUSTOM_PATH" <<'PY' | tee "$UPDATE_CHECK_LOG"
import importlib.util
import sys
from pathlib import Path

title, media_type, source_path, custom_path = sys.argv[1:5]
script = Path("$HOME/.openclaw/skills/media/auto-media-download/scripts/reconcile-drama-state.py")
spec = importlib.util.spec_from_file_location("reconcile_drama_state", script)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.path.insert(0, str(script.parent))
spec.loader.exec_module(mod)

rec = mod.Reconciler()
drama = {
    "name": title,
    "title": title,
    "status": "ongoing",
    "media_type": media_type,
    "source_path": source_path,
    "strm_path": custom_path,
}
local_count = rec.count_local_strm(drama)
rec.current_media_type = media_type
source_status, source_videos = rec.list_source_videos(rec.source_path(drama))
source_count = len(source_videos)
print(f"AUTO_UPDATE_SOURCE_STATUS={source_status}")
print(f"AUTO_UPDATE_SOURCE_COUNT={source_count}")
print(f"AUTO_UPDATE_LOCAL_COUNT_AFTER_DELETE={local_count}")
print(f"AUTO_UPDATE_DELETED_ONE_STRM=1")
print(f"AUTO_UPDATE_DETECTS_MISSING_STRM={1 if source_status == 'ok' and source_count > local_count else 0}")
PY

AUTO_UPDATE_OK="$(awk -F= '$1=="AUTO_UPDATE_DETECTS_MISSING_STRM" {print $2}' "$UPDATE_CHECK_LOG" | tail -1)"
AUTO_UPDATE_OK="${AUTO_UPDATE_OK:-0}"
echo "AUTO_UPDATE_DELETED_PATH=$DELETED_STRM"
rm -f "$UPDATE_CHECK_LOG" "$GENERATE_LOG"

if [ "$AUTO_UPDATE_OK" != "1" ]; then
  echo "XIAOYA_AUTO_UPDATE_OK=0"
  echo "XIAOYA_REGRESSION_OK=0"
  exit 6
fi
echo "XIAOYA_AUTO_UPDATE_OK=1"

if [ "$KEEP_OUTPUT" = "1" ]; then
  echo "XIAOYA_CLEANUP_SKIPPED=1"
else
  cleanup
  trap - EXIT
  EXISTS_AFTER="$(remote_cmd "[ -e $(quote "$CUSTOM_NAS_DIR") ] && echo 1 || echo 0")"
  echo "XIAOYA_CLEANUP_EXISTS_AFTER=$EXISTS_AFTER"
fi

echo "XIAOYA_REGRESSION_OK=1"
