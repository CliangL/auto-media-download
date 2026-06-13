#!/bin/bash
# notify-update.sh - 追剧更新通知脚本
# 被 check-update.sh 调用，用于发送飞书通知

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECK_SCRIPT="$SCRIPT_DIR/check-update.sh"

# 执行检查并捕获输出
output=$($CHECK_SCRIPT 2>&1)
has_update=$(echo "$output" | grep -c '🆕' || true)

# 有更新才通知
if [ "$has_update" -gt 0 ]; then
    # 提取更新摘要
    updates=$(echo "$output" | grep '🆕' | sed 's/  //g')
    
    # 通过 openclaw 发飞书消息（由调用方决定如何发）
    echo "HAS_UPDATE=true"
    echo "$updates"
else
    echo "HAS_UPDATE=false"
fi
