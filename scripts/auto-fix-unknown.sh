#!/bin/bash
# auto-fix-unknown.sh - 自动查询并修正未知总集数的剧集
# 所有总集数判断统一委托 decide-tracking.py，避免把当前更新集数误当全集。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE_FILE="$SCRIPT_DIR/../data/drama-state.json"
DECIDER="$SCRIPT_DIR/decide-tracking.py"
SYNC_SCRIPT="$SCRIPT_DIR/sync-drama-state.py"
python3 "$SYNC_SCRIPT" pull >/dev/null 2>&1 || true

[ -f "$STATE_FILE" ] || exit 0

unknown_count=$(jq '[.dramas[] | select(.status=="ongoing" and ((.total_episodes == null) or (.total_episodes == 0)))] | length' "$STATE_FILE")

if [ "$unknown_count" -eq 0 ]; then
    exit 0
fi

echo "🔍 发现 $unknown_count 部剧集总集数未知，正在联网确认..."

jq -c '.dramas[] | select(.status=="ongoing" and ((.total_episodes == null) or (.total_episodes == 0)))' "$STATE_FILE" | while IFS= read -r drama; do
    name=$(printf '%s' "$drama" | jq -r '.name')
    media_type=$(printf '%s' "$drama" | jq -r '.media_type // "tv"')
    current=$(printf '%s' "$drama" | jq -r '.current_episodes // 0')
    source_path=$(printf '%s' "$drama" | jq -r '.source_path // ""')

    decision=$(python3 "$DECIDER" --shell \
        --title "$name" \
        --media-type "$media_type" \
        --current "$current" \
        --note "$name" \
        --source-path "$source_path") || decision=""

    if [ -z "$decision" ]; then
        echo "  ⚠️ $name: 总集数查询失败，保持追剧"
        continue
    fi

    eval "$decision"

    if [ "${DECISION_STATUS:-needs_total}" = "needs_total" ] || [ -z "${DECISION_TOTAL:-}" ]; then
        echo "  ⚠️ $name: 未查到可信总集数，保持追剧"
        continue
    fi

    tmp="$STATE_FILE.tmp.$$"
    jq --arg n "$name" \
       --arg status "$DECISION_STATUS" \
       --arg reason "$DECISION_REASON" \
       --arg evidence "${DECISION_EVIDENCE:-}" \
       --argjson total "$DECISION_TOTAL" \
       --arg now "$(date '+%Y-%m-%d %H:%M:%S')" \
       '(.dramas[] | select(.name == $n)) |= (.total_episodes = $total | .status = $status | .track_reason = $reason | .last_check = $now | .total_evidence = $evidence)' \
       "$STATE_FILE" > "$tmp" && mv "$tmp" "$STATE_FILE"

    echo "  ✅ $name: $DECISION_REASON"
done
python3 "$SYNC_SCRIPT" push >/dev/null 2>&1 || true
