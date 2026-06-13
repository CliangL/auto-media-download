#!/bin/bash
# handle-selection.sh v5.1 - STRM 生成 + 追剧
# 类型判断由 AI agent 完成（上网搜索），通过环境变量传入

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/../config/media-config.json"
SYNC_SCRIPT="$SCRIPT_DIR/sync-drama-state.py"

infer_media_type() {
    local current="${1:-}"
    local source_path="${2:-}"
    local title="${3:-}"
    local text="${source_path} ${title}"

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

normalize_media_title() {
    local title="${1:-}"
    local source_path="${2:-}"

    python3 - "$title" "$source_path" <<'PYEOF'
import re
import sys

title = sys.argv[1]
source_path = sys.argv[2]
base = source_path.rstrip("/").split("/")[-1]
normalized = title

# Xiaoya realtime folders may prefix titles with an index bucket:
# "Y 月鳞绮纪" or "W我在大学修文物（2026）".  The bucket is not part
# of the media title and must not create duplicate STRM directories.
if base == title:
    match = re.match(r"^[A-Z]\s+(.+)$", title)
    if not match:
        match = re.match(r"^[A-Z]([^A-Za-z0-9].*[（(]\d{4}[）)]\s*)$", title)
    if match:
        normalized = match.group(1).strip()
        normalized = re.sub(r"\s*[（(]\d{4}[）)]\s*$", "", normalized).strip()

print(normalized or title)
PYEOF
}

NAS_HOST=$(jq -r '.nas.host' "$CONFIG_FILE")
NAS_USER=$(jq -r '.nas.user' "$CONFIG_FILE")
STRM_URL_PREFIX=$(jq -r '.nas.strm_url_prefix // "http://127.0.0.1:5678/d"' "$CONFIG_FILE")
STATE_FILE="$SCRIPT_DIR/../data/drama-state.json"
python3 "$SYNC_SCRIPT" pull >/dev/null 2>&1 || true

# 本地模式检测：host 为 127.0.0.1/localhost 时直接执行，不走 SSH
IS_LOCAL=false
if [ "$NAS_HOST" = "127.0.0.1" ] || [ "$NAS_HOST" = "localhost" ]; then
    IS_LOCAL=true
    SSH_CMD="local"
else
    if ssh -o BatchMode=yes -o ConnectTimeout=3 "$NAS_USER@$NAS_HOST" "echo ok" 2>/dev/null; then
        SSH_CMD="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 $NAS_USER@$NAS_HOST"
    else
        NAS_PASS=$(jq -r '.nas.password // ""' "$CONFIG_FILE")
        SSH_CMD="sshpass -p '$NAS_PASS' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 $NAS_USER@$NAS_HOST"
    fi
fi

SELECTION="${1:-1}"
RESOURCES_FILE="/tmp/media_resources.txt"
[ ! -f "$RESOURCES_FILE" ] && { echo "❌ 请先运行 index-search.py 或 pansou-search.py"; exit 1; }

LINE=$(sed -n "${SELECTION}p" "$RESOURCES_FILE")
[ -z "$LINE" ] && { echo "❌ 无效的选择"; exit 1; }

# 解析资源类型
if [[ "$LINE" =~ ^PANSOU ]]; then
    # PanSou 资源，调用 handle-pansou.sh
    # 格式：PANSOU\t网盘类型\tURL\t密码\t备注
    PAN_TYPE=$(echo "$LINE" | cut -f2)
    PAN_URL=$(echo "$LINE" | cut -f3)
    PAN_PWD=$(echo "$LINE" | cut -f4)
    PAN_NOTE=$(echo "$LINE" | cut -f5)
    
    # 内部序号（PanSou 的）
    PANSOU_JSON="/tmp/pansou_results.json"
    PANSOU_IDX="1"
    if [ -f "$PANSOU_JSON" ]; then
        PANSOU_IDX=$(python3 - "$PANSOU_JSON" "$PAN_URL" <<'PYEOF'
import json, sys
path, url = sys.argv[1], sys.argv[2]
try:
    items = json.load(open(path, encoding="utf-8"))
    for i, item in enumerate(items, 1):
        if item.get("url") == url:
            print(i)
            break
    else:
        print(1)
except Exception:
    print(1)
PYEOF
)
    fi
    
    echo "============================================"
    echo "📥 PanSou 资源处理"
    echo "============================================"
    echo "网盘: $PAN_TYPE | URL: ${PAN_URL:0:50}"
    
    # 转调 handle-pansou.sh
    export MEDIA_TYPE TOTAL_EP
    bash "$SCRIPT_DIR/handle-pansou.sh" "$PANSOU_IDX"
    exit $?
fi

# 索引资源
# 格式：路径\t剧名
SOURCE_PATH=$(echo "$LINE" | cut -f1)
name=$(echo "$LINE" | cut -f2)
name="$(normalize_media_title "$name" "$SOURCE_PATH")"

# ── 类型判断：AI agent 传入 ──
# 用法：MEDIA_TYPE=movie ./handle-selection.sh 1
# 用法：MEDIA_TYPE=tv TOTAL_EP=24 ./handle-selection.sh 1
# 用法：MEDIA_TYPE=anime TOTAL_EP=12 ./handle-selection.sh 1
# 用法：MEDIA_TYPE=variety ./handle-selection.sh 1
media_type="$(infer_media_type "${MEDIA_TYPE:-}" "$SOURCE_PATH" "$name")"
total_ep="${TOTAL_EP:-}"

echo "============================================"
echo "📝 生成 STRM"
echo "============================================"
echo "片名: $name | 类型: $media_type"
[ -n "$total_ep" ] && echo "总集数: $total_ep"

# 上传 gen-strm.py 到 NAS（仅首次）
GEN_STRM="$SCRIPT_DIR/gen-strm.py"
STRM_LAYOUT="$SCRIPT_DIR/strm_layout.py"
NAS_GEN_STRM="/vol1/1000/docker/xiaoya/scripts/gen-strm.py"
NAS_STRM_LAYOUT="/vol1/1000/docker/xiaoya/scripts/strm_layout.py"

if [ "$IS_LOCAL" = true ]; then
    # 本地模式：直接创建目录并执行，不走 SSH/SCP
    mkdir -p /vol1/1000/docker/xiaoya/scripts
    cp "$GEN_STRM" "$NAS_GEN_STRM"
    cp "$STRM_LAYOUT" "$NAS_STRM_LAYOUT"
    ssh_output=$(SOURCE_PATH="/${SOURCE_PATH}" STRM_URL_PREFIX="${STRM_URL_PREFIX}" NAME="${name}" MEDIA_TYPE="${media_type}" STRM_CUSTOM_PATH="${STRM_CUSTOM_PATH:-}" python3 "$NAS_GEN_STRM" 2>&1)
else
    $SSH_CMD "mkdir -p /vol1/1000/docker/xiaoya/scripts" 2>/dev/null
    scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$GEN_STRM" $NAS_USER@$NAS_HOST:"$NAS_GEN_STRM" 2>/dev/null
    scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$STRM_LAYOUT" $NAS_USER@$NAS_HOST:"$NAS_STRM_LAYOUT" 2>/dev/null
    STRM_CUSTOM_PATH_REMOTE="${STRM_CUSTOM_PATH:-}"
    ssh_output=$($SSH_CMD "SOURCE_PATH='/${SOURCE_PATH}' STRM_URL_PREFIX='${STRM_URL_PREFIX}' NAME='${name}' MEDIA_TYPE='${media_type}' STRM_CUSTOM_PATH='${STRM_CUSTOM_PATH_REMOTE}' python3 $NAS_GEN_STRM" 2>&1)
fi

echo "$ssh_output"
strm_count=$(echo "$ssh_output" | grep '===STRM_COUNT===' | tail -1 | grep -oE '[0-9]+')
    strm_total=$(echo "$ssh_output" | grep '===STRM_TOTAL===' | tail -1 | grep -oE '[0-9]+')
    [ -z "$strm_total" ] && strm_total="$strm_count"
if [ -z "$strm_total" ] || [ "$strm_total" -eq 0 ]; then
    echo ""; echo "❌ STRM 生成失败"; exit 1
fi

# ── 追剧判断：必须由判定器联网确认总集数 ──
echo ""
echo "============================================"
echo "📊 追剧状态判断"
echo "============================================"
echo "   本地文件数: $strm_total"

DECIDER="$SCRIPT_DIR/decide-tracking.py"
eval "$(python3 "$DECIDER" --shell --title "$name" --media-type "$media_type" --current "$strm_total" --provided-total "${total_ep:-}" --note "$name" --source-path "$SOURCE_PATH")"
name="$DECISION_TITLE"
media_type="$DECISION_MEDIA_TYPE"
drama_status="$DECISION_STATUS"
track_reason="$DECISION_REASON"
total_ep="${DECISION_TOTAL:-0}"
total_evidence="${DECISION_EVIDENCE:-}"
decision_origin="${DECISION_ORIGIN:-}"

if [ "$drama_status" = "needs_total" ]; then
    echo "   ❌ 未查到总集数，禁止自动判断是否追剧"
    echo "   说明: $track_reason"
    exit 2
fi

# 规范化状态：needs_recovery 不是有效的追剧状态，转换为明确的结论。
# 保护：只有判定器证据明确含“已完结/完结/全集/xx集全”时，才移出追剧；
# 否则把它降级为 ongoing，避免把“共/全 xx 集”这种总集数元数据误当完结。
if [ "$drama_status" = "needs_recovery" ]; then
    if printf '%s\n%s\n%s\n' "$DECISION_REASON" "$DECISION_EVIDENCE" "${DECISION_SOURCE_TRACE:-}" | grep -Eq '已完结|完结|全集|[0-9]+集全'; then
        # 已完结但源不全 — 不纳入追剧，只报告
        echo "   判定: 已完结但源不全 | 总集数: ${total_ep:-未知} | 置信度: $DECISION_CONFIDENCE"
        echo "   依据: $DECISION_EVIDENCE"
        echo "   来源: ${decision_origin:-unknown}"
        echo "   原因: $track_reason"
        echo ""
        echo "   ⚠️ 该剧已完结，当前资源仅 $strm_total 集（共 ${total_ep:-?} 集），不纳入追剧。"
        echo "   如需补全集数，请手动换源或等待源更新。"
        drama_status="completed"
        track_reason="已完结但源不全(${strm_total}/${total_ep:-?})，不纳入追剧"
    else
        echo "   判定器给出缺源，但未见明确完结证据，按更新中保留追剧"
        echo "   依据: $DECISION_EVIDENCE"
        drama_status="ongoing"
        track_reason="未确认完结，当前资源 ${strm_total}/${total_ep:-?} 集，继续追剧等待更新"
    fi
fi

if [ "$drama_status" = "completed" ]; then
    echo "   判定: 已完结 | 总集数: ${total_ep:-未知} | 置信度: $DECISION_CONFIDENCE"
    echo "   依据: $DECISION_EVIDENCE"
    echo "   来源: ${decision_origin:-unknown}"
    echo "   原因: $track_reason"
    echo ""
    echo "   ✅ 该剧已完结，不纳入追剧列表。"
    # 已完结的剧不写入 drama-state（不追剧）
    echo ""
    echo "============================================"
    echo "🎉 完成！"
    echo "============================================"
    echo "剧名: $name | 类型: $media_type"
    echo "文件: $strm_total | 新增: $strm_count | 状态: $drama_status"
    echo "原因: $track_reason"
    python3 "$SYNC_SCRIPT" push >/dev/null 2>&1 || true
    exit 0
fi
echo "   判定: $drama_status | 总集数: ${total_ep:-未知} | 置信度: $DECISION_CONFIDENCE"
echo "   依据: $DECISION_EVIDENCE"
echo "   来源: ${decision_origin:-unknown}"
echo "   原因: $track_reason"

if [ "${AUTO_MEDIA_SKIP_STATE:-0}" = "1" ]; then
    echo ""
    echo "🧪 AUTO_MEDIA_SKIP_STATE=1，已完成 STRM 与追剧判定测试，跳过写入/同步 drama-state"
    echo "============================================"
    echo "🎉 完成！"
    echo "============================================"
    echo "剧名: $name | 类型: $media_type"
    echo "文件: $strm_total | 新增: $strm_count | 状态: $drama_status"
    echo "原因: $track_reason"
    exit 0
fi

# ── 更新追剧记录 ──
echo ""
mkdir -p "$(dirname "$STATE_FILE")"
[ ! -f "$STATE_FILE" ] && echo '{"dramas":[]}' > "$STATE_FILE"
now=$(date -Iseconds)
today=$(date -I)

if ! jq -e --arg n "$name" '.dramas[] | select((.name == $n) or (.title == $n))' "$STATE_FILE" > /dev/null 2>&1; then
    jq --arg n "$name" --argjson ep "$strm_total" --arg date "$today" \
        --arg status "$drama_status" --arg reason "$track_reason" \
        --arg mt "$media_type" --arg sp "$SOURCE_PATH" \
        --arg evidence "$total_evidence" \
        --argjson total "${total_ep:-0}" \
        '.dramas += [{
            "name": $n, "title": $n, "current_episodes": $ep, "status": $status,
            "added_date": $date, "track_reason": $reason,
            "media_type": $mt, "source_path": $sp, "last_check": $date
        }
        | if ($total > 0 and $total >= $ep) then . + {"total_episodes": $total} else . end
        | if ($evidence != "") then . + {"total_evidence": $evidence} else . end]' "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
    echo "📝 新增: $name ($media_type, $drama_status)"
else
    jq --arg n "$name" --argjson ep "$strm_total" \
        --arg status "$drama_status" --arg reason "$track_reason" \
        --arg mt "$media_type" --arg sp "$SOURCE_PATH" --arg now "$now" \
        --arg evidence "$total_evidence" \
        --argjson total "${total_ep:-0}" \
        '(.dramas[] | select((.name == $n) or (.title == $n))) |= (. + {
            "name": $n, "title": $n, "current_episodes": $ep, "status": $status,
            "track_reason": $reason, "media_type": $mt, "source_path": $sp,
            "last_check": $now
        }
        | if ($total > 0 and $total >= $ep) then . + {"total_episodes": $total} else . end
        | if ($evidence != "") then . + {"total_evidence": $evidence} else . end)' "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
    echo "📝 更新: $name ($media_type, $drama_status)"
fi

echo ""
echo "============================================"
echo "🎉 完成！"
echo "============================================"
echo "剧名: $name | 类型: $media_type"
echo "文件: $strm_total | 新增: $strm_count | 状态: $drama_status"
echo "原因: $track_reason"
python3 "$SYNC_SCRIPT" push >/dev/null 2>&1 || true
