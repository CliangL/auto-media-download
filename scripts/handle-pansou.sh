#!/bin/bash
# handle-pansou.sh v5.1 - PanSou → GBox → xiaoya → STRM 全自动
# 类型判断由 AI agent 完成（上网搜索），通过环境变量传入

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/../config/media-config.json"
SYNC_SCRIPT="$SCRIPT_DIR/sync-drama-state.py"
RESOLVER_SCRIPT="$SCRIPT_DIR/gbox_share_resolver.py"
GBOX_MOUNT_CLEANUP_SCRIPT="$SCRIPT_DIR/../../../devops/gbox-strm-mount-sync/scripts/gbox-dedupe-mounts.py"

infer_media_type() {
    local current="${1:-}"
    shift || true
    local text="$*"

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

NAS_HOST=$(jq -r '.nas.host' "$CONFIG_FILE")
NAS_USER=$(jq -r '.nas.user' "$CONFIG_FILE")
GBOX_URL=$(jq -r '.gbox.internal_url // "http://YOUR_NAS_LAN_IP:4567"' "$CONFIG_FILE")
GBOX_USER=$(jq -r '.gbox.username // "admin"' "$CONFIG_FILE")
GBOX_PASS=$(jq -r '.gbox.password // "admin"' "$CONFIG_FILE")
ALIST_BASE=$(jq -r '([.sources[]? | select(.name == "xiaoya") | .internal_url][0]) // "http://YOUR_NAS_LAN_IP:5678"' "$CONFIG_FILE")
STATE_FILE="$SCRIPT_DIR/../data/drama-state.json"
python3 "$SYNC_SCRIPT" pull >/dev/null 2>&1 || true
PANSOU_FILE="/tmp/pansou_results.json"

# ── 快照机制：记录运行前的 GBox Shares 状态 ──
SNAPSHOT_FILE="/tmp/gbox_shares_before_$$"
python3 - "$GBOX_URL" "$GBOX_USER" "$GBOX_PASS" "$SNAPSHOT_FILE" <<'PYEOF'
import json, urllib.request, sys, os
try:
    gbox_url = sys.argv[1].rstrip('/')
    user, pwd = sys.argv[2], sys.argv[3]
    outfile = sys.argv[4]
    req = urllib.request.Request(f"{gbox_url}/api/accounts/login",
        data=json.dumps({"username": user, "password": pwd}).encode(),
        headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(req, timeout=8).read())['token']
    headers = {"X-ACCESS-TOKEN": token}
    shares = []
    page = 0
    while True:
        req = urllib.request.Request(f"{gbox_url}/api/shares?page={page}&size=100", headers=headers)
        resp = json.loads(urllib.request.urlopen(req, timeout=8).read())
        content = resp.get('content') or []
        shares.extend(content)
        if resp.get('last', True) or not content: break
        page += 1
    with open(outfile, 'w') as f:
        json.dump({s['path']: s for s in shares}, f)
except Exception as e:
    pass
PYEOF

# ── 清理函数：删除所有新增的非选中挂载 ──
cleanup_new_shares() {
    local video_path="$1"
    local is_success="$2"
    
    # 只有成功时才保留对应挂载，否则全部清理
    if [ "$is_success" = "true" ]; then
        KEEP_PATTERN="$video_path"
    else
        KEEP_PATTERN=""
    fi

    python3 - "$GBOX_URL" "$GBOX_USER" "$GBOX_PASS" "$SNAPSHOT_FILE" "$KEEP_PATTERN" "$GBOX_MOUNT_CLEANUP_SCRIPT" <<'PYEOF'
import json, urllib.request, sys, os, subprocess
try:
    gbox_url = sys.argv[1].rstrip('/')
    user, pwd = sys.argv[2], sys.argv[3]
    snap_file = sys.argv[4]
    keep_pattern = sys.argv[5]
    cleanup_script = sys.argv[6]

    # Load Snapshot
    old_paths = set()
    if os.path.exists(snap_file):
        with open(snap_file) as f:
            old_paths = set(json.load(f).keys())

    # Login
    req = urllib.request.Request(f"{gbox_url}/api/accounts/login",
        data=json.dumps({"username": user, "password": pwd}).encode(),
        headers={"Content-Type": "application/json"})
    token = json.loads(urllib.request.urlopen(req, timeout=8).read())['token']
    headers = {"X-ACCESS-TOKEN": token}

    # Fetch Current
    current = []
    page = 0
    while True:
        req = urllib.request.Request(f"{gbox_url}/api/shares?page={page}&size=100", headers=headers)
        resp = json.loads(urllib.request.urlopen(req, timeout=8).read())
        content = resp.get('content') or []
        current.extend(content)
        if resp.get('last', True) or not content: break
        page += 1

    # Identify New
    to_delete = []
    for share in current:
        path = share.get('path', '')
        if path not in old_paths:
            if keep_pattern and (path in keep_pattern or keep_pattern.startswith(path)):
                continue # Keep this one
            to_delete.append(share)

    # Delete
    cleanup_paths = []
    for share in to_delete:
        sid = share.get('id')
        path = share.get('path', 'Unknown')
        if path:
            cleanup_paths.append(path)
        if sid:
            req = urllib.request.Request(f"{gbox_url}/api/shares/{sid}", headers=headers, method='DELETE')
            try:
                urllib.request.urlopen(req, timeout=5)
            except: pass
    if cleanup_paths and cleanup_script and os.path.exists(cleanup_script):
        cmd = [sys.executable, cleanup_script, "--apply", "--storages-only"]
        for path in cleanup_paths:
            cmd.extend(["--path", path])
        subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240)
except Exception as e:
    pass
PYEOF
}

# 注册退出清理 (防止清理脚本报错影响主流程)
trap 'cleanup_new_shares "$VIDEO_PATH" "$SUCCESS_FLAG" || true' EXIT
ARG1="${1:-1}"
if [[ "$ARG1" == http* ]]; then
    DIRECT_URL="$ARG1"
    DIRECT_SHARE_ID=$(echo "$DIRECT_URL" | grep -oE '/s/[^?/&]+' | sed 's|/s/||')
    DIRECT_PAN_TYPE="${AUTO_MEDIA_PAN_TYPE:-uc}"
    DIRECT_GBOX_TYPE="${AUTO_MEDIA_GBOX_TYPE:-7}"
    case "$DIRECT_URL" in
        *drive.uc.cn*) DIRECT_PAN_TYPE="${AUTO_MEDIA_PAN_TYPE:-uc}"; DIRECT_GBOX_TYPE="${AUTO_MEDIA_GBOX_TYPE:-7}" ;;
        *pan.quark.cn*) DIRECT_PAN_TYPE="${AUTO_MEDIA_PAN_TYPE:-quark}"; DIRECT_GBOX_TYPE="${AUTO_MEDIA_GBOX_TYPE:-5}" ;;
        *aliyundrive.com*) DIRECT_PAN_TYPE="${AUTO_MEDIA_PAN_TYPE:-aliyun}"; DIRECT_GBOX_TYPE="${AUTO_MEDIA_GBOX_TYPE:-0}" ;;
    esac
    DIRECT_NOTE="${AUTO_MEDIA_TITLE:-${DIRECT_PAN_TYPE}_share_${DIRECT_SHARE_ID:-direct}}"
    DIRECT_PASSWORD="${AUTO_MEDIA_PASSWORD:-}"
    python3 - "$PANSOU_FILE" "$DIRECT_NOTE" "$DIRECT_URL" "$DIRECT_PASSWORD" "$DIRECT_PAN_TYPE" "$DIRECT_GBOX_TYPE" <<'PYEOF'
import json
import sys

path, note, url, password, pan_type, gbox_type = sys.argv[1:]
try:
    gbox_type_value = int(gbox_type)
except Exception:
    gbox_type_value = 7
with open(path, "w", encoding="utf-8") as f:
    json.dump([{
        "note": note,
        "url": url,
        "password": password,
        "source": "direct-link",
        "pan_type": pan_type,
        "gbox_type": gbox_type_value,
    }], f, ensure_ascii=False, indent=2)
PYEOF
    if [ -z "${PROBE_SHARE_ID:-}" ] && [ -z "${PROBE_FOLDER_PATH:-}" ]; then
        rm -f /tmp/probe_results.json /tmp/probe_mounts.json /tmp/media_selection_map.json
    fi
    START_SELECTION=$(python3 - "$PANSOU_FILE" "$ARG1" <<'PYEOF'
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
else
    START_SELECTION="$ARG1"
fi

# 使用 SSH 快捷方式
if ssh -o BatchMode=yes -o ConnectTimeout=3 "$NAS_USER@$NAS_HOST" "echo ok" 2>/dev/null; then
    SSH_CMD="ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 $NAS_USER@$NAS_HOST"
else
    NAS_PASS=$(jq -r '.nas.password // ""' "$CONFIG_FILE")
    SSH_CMD="sshpass -p '$NAS_PASS' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 $NAS_USER@$NAS_HOST"
fi

# ── 清理逻辑：确保退出时不留垃圾 ──
# 运行前的 GBox shares 快照已写入 $SNAPSHOT_FILE；后续先走 GBox API 删 share，
# 再调用统一清理脚本同步收掉 live DB 里的 x_storages 残留。
# 当前正在处理的挂载（用于 Trap 兜底）
CURRENT_MOUNT=""
SELECTED_MOUNT=""
SUCCESS_FLAG=false

mount_exists_before() {
    python3 - "$SNAPSHOT_FILE" "$1" <<'PYEOF'
import json
import sys

snap_file, raw = sys.argv[1], (sys.argv[2] or "").strip()
try:
    old_paths = set(json.load(open(snap_file, encoding="utf-8")).keys())
except Exception:
    old_paths = set()

roots = ("🍓我的UC分享", "🍊我的夸克分享", "🍑我的阿里分享", "🏷️我的115分享", "🍒我的迅雷分享")

def strip_root(path):
    parts = path.strip("/").split("/")
    if parts and parts[0].startswith(roots):
        parts = parts[1:]
    return "/".join(p for p in parts if p)

base = strip_root(raw)
candidates = {raw, "/" + raw.strip("/")}
if base:
    candidates.add(base)
    candidates.add("/" + base)
    for root in roots:
        candidates.add(f"/{root}/{base}")

raise SystemExit(0 if any(c in old_paths for c in candidates if c) else 1)
PYEOF
}

strip_mount_root() {
    python3 - "$1" <<'PYEOF'
import sys

raw = (sys.argv[1] or "").strip().strip("/")
parts = [p for p in raw.split("/") if p]
if parts and parts[0].startswith(("🍓", "🍊", "🍑", "🏷️", "🍒")):
    parts = parts[1:]
print("/".join(parts))
PYEOF
}

resolve_share_mount_path_via_api() {
    local share_root="${1:-}"
    local share_id="${2:-}"
    shift 2 || true
    local cmd=(python3 "$RESOLVER_SCRIPT" --gbox-url "$GBOX_URL" --token "$TOKEN" --alist-base "$ALIST_BASE" --root "$share_root" --share-id "$share_id" --wait 24 --poll 2)
    local alias
    for alias in "$@"; do
        [ -n "$alias" ] && cmd+=(--alias "$alias")
    done
    "${cmd[@]}" 2>/dev/null || true
}

delete_mount_if_new() {
    local mount_path="${1:-}"
    [ -z "$mount_path" ] && return 0
    if mount_exists_before "$mount_path"; then
        echo "  (已有挂载，跳过清理: $mount_path)"
        return 0
    fi
    delete_gbox_share_records "$mount_path"
}

delete_gbox_share_records() {
    local mount_path="${1:-}"
    [ -z "$mount_path" ] && return 0
    GBOX_URL="$GBOX_URL" GBOX_USER="$GBOX_USER" GBOX_PASS="$GBOX_PASS" MOUNT_PATH="$mount_path" python3 <<'PYEOF' 2>/dev/null || true
import json
import os
import urllib.error
import urllib.request

gbox_url = os.environ["GBOX_URL"].rstrip("/")
mount_path = (os.environ.get("MOUNT_PATH") or "").strip()
if not mount_path:
    raise SystemExit

roots = ("🍓我的UC分享", "🍊我的夸克分享", "🍑我的阿里分享", "🏷️我的115分享", "🍒我的迅雷分享")

def strip_root(path):
    parts = path.strip("/").split("/")
    if parts and parts[0].startswith(roots):
        parts = parts[1:]
    return "/".join(p for p in parts if p)

base = strip_root(mount_path)
targets = {mount_path, "/" + mount_path.strip("/")}
if base:
    targets.add(base)
    targets.add("/" + base)
    for root in roots:
        targets.add(f"/{root}/{base}")

login_body = json.dumps({"username": os.environ["GBOX_USER"], "password": os.environ["GBOX_PASS"]}).encode()
login_req = urllib.request.Request(gbox_url + "/api/accounts/login", data=login_body, headers={"Content-Type": "application/json"})
with urllib.request.urlopen(login_req, timeout=8) as resp:
    token = json.loads(resp.read()).get("token")
if not token:
    raise SystemExit

headers = {"X-ACCESS-TOKEN": token}
for page in range(0, 5):
    req = urllib.request.Request(f"{gbox_url}/api/shares?page={page}&size=200", headers=headers)
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())
    rows = data.get("content") or []
    for row in rows:
        if row.get("path") in targets and row.get("id") is not None:
            delete_req = urllib.request.Request(f"{gbox_url}/api/shares/{row['id']}", headers=headers, method="DELETE")
            try:
                urllib.request.urlopen(delete_req, timeout=8).read()
            except urllib.error.HTTPError:
                pass
    if data.get("last", True):
        break
PYEOF
}

delete_probe_mount_force() {
    local mount_path="${1:-}"
    [ -z "$mount_path" ] && return 0
    local variants
    variants=$(python3 - "$mount_path" <<'PYEOF'
import sys

raw = (sys.argv[1] or "").strip().strip("/")
roots = ("🍓我的UC分享", "🍊我的夸克分享", "🍑我的阿里分享", "🏷️我的115分享", "🍒我的迅雷分享")

def strip_root(path):
    parts = path.strip("/").split("/")
    if parts and parts[0].startswith(roots):
        parts = parts[1:]
    return "/".join(p for p in parts if p)

base = strip_root(raw)
items = {raw, "/" + raw if raw else "", base, "/" + base if base else ""}
for root in roots:
    if base:
        items.add(f"/{root}/{base}")
for item in sorted(x for x in items if x):
    print(item)
PYEOF
)
    while IFS= read -r candidate; do
        [ -z "$candidate" ] && continue
        delete_gbox_share_records "$candidate"
    done <<< "$variants"
}

cleanup() {
    if [ "$SUCCESS_FLAG" = false ] && [ -n "$CURRENT_MOUNT" ]; then
        echo "🧹 清理未完成资源挂载..."
        delete_mount_if_new "$CURRENT_MOUNT"
    fi
    cleanup_new_shares "$VIDEO_PATH" "$SUCCESS_FLAG" || true
}
trap cleanup EXIT

cleanup_unselected_probe_mounts() {
    local selected_mount="${1:-}"
    [ -f /tmp/probe_results.json ] || [ -f /tmp/probe_mounts.json ] || return 0
    echo "🧹 清理探路阶段未选中的 GBox 挂载..."
    python3 - "$selected_mount" <<'PYEOF' | while IFS= read -r mount_path; do
import json
import sys

selected = sys.argv[1]
rows = []
for path in ("/tmp/probe_mounts.json", "/tmp/probe_results.json"):
    try:
        rows.extend(json.load(open(path, encoding="utf-8")))
    except Exception:
        pass

def normalize_mount(path):
    path = (path or "").strip("/")
    if not path:
        return ""
    parts = path.split("/")
    if parts and parts[0].startswith(("🍓", "🍊", "🍑", "🏷️", "🍒")):
        parts = parts[1:]
    return "/".join(parts)

selected = normalize_mount(selected)
protected = {selected} if selected else set()

# Probe records contain both folder_path (the actual GBox mount root) and
# video_path (a subdirectory inside that mount). Only folder_path should be
# deleted; deleting by video_path can accidentally miss the stale mount root
# while leaving the selected root unprotected for a later cleanup pass.
for row in rows:
    folder = normalize_mount(row.get("folder_path", ""))
    video = normalize_mount(row.get("video_path", ""))
    if selected and (folder == selected or video == selected or video.startswith(selected + "/")):
        protected.add(folder or selected)

seen = set()
for row in rows:
    mount = normalize_mount(row.get("folder_path", ""))
    if not mount or mount in protected or mount in seen:
        continue
    seen.add(mount)
    print(mount)
PYEOF
        delete_probe_mount_force "$mount_path"
    done
}

TOTAL_RESULTS=$(python3 -c "import json; print(len(json.load(open('$PANSOU_FILE'))))" 2>/dev/null || echo 0)
[ ! -f "$PANSOU_FILE" ] && { echo "❌ 请先运行 pansou-search.py"; exit 1; }
[ "$TOTAL_RESULTS" -eq 0 ] && { echo "❌ 无搜索结果"; exit 1; }

echo "============================================"
echo "📥 PanSou 全自动导入 v5.1"
echo "============================================"

if [ "${AUTO_MEDIA_DRY_RUN:-0}" = "1" ]; then
    echo "DRY_RUN kind=pansou selection=$START_SELECTION source=$PANSOU_FILE"
    exit 0
fi

# 运行前已有挂载以 $SNAPSHOT_FILE 中的 GBox shares API 快照为准，用于区分“新增垃圾”和“用户已有”。

# ── 从选择的序号开始尝试，失败自动试下一个 ──
for SELECTION in $(seq "$START_SELECTION" "$TOTAL_RESULTS"); do
    echo ""
    echo "🔄 尝试第 ${SELECTION} 个资源（共${TOTAL_RESULTS}个）"
    echo "----------------------------------------"

    # 读取当前选中项
    IDX=$((SELECTION-1))
    NOTE=$(python3 -c "import json; sel=json.load(open('$PANSOU_FILE'))[$IDX]; print(sel['note'])")
    URL=$(python3 -c "import json; sel=json.load(open('$PANSOU_FILE'))[$IDX]; print(sel['url'])")
    PASSWORD=$(python3 -c "import json; sel=json.load(open('$PANSOU_FILE'))[$IDX]; print(sel.get('password',''))")
    PAN_TYPE=$(python3 -c "import json; sel=json.load(open('$PANSOU_FILE'))[$IDX]; print(sel['pan_type'])")
    GBOX_TYPE=$(python3 -c "import json; sel=json.load(open('$PANSOU_FILE'))[$IDX]; print(sel['gbox_type'])")

    [ -z "$URL" ] && { echo "  ⚠️ 无效，跳过"; continue; }

    SHARE_ID=$(echo "$URL" | grep -oE '/s/[^?/&]+' | sed 's|/s/||')
    [ -z "$SHARE_ID" ] && { echo "  ⚠️ 无法提取 shareId，跳过"; continue; }

    # 清洗剧名
    CLEAN_NAME=$(python3 << PYEOF
import re
name = """$NOTE"""
name = re.sub(r'^【[^】]*】', '', name)
name = re.sub(r'^\[.*?\]', '', name)
name = re.sub(r'^（[^）]*）', '', name)
name = re.sub(r'\s*[\[【].*?[\]】]\s*$', '', name)
name = re.sub(r'[._ -]*(?:更至?|更新至?)\s*\d+\s*(?:集|话)?$', '', name, flags=re.I)
name = re.sub(r'[：:]+$', '', name)
name = name.strip()
print(name)
PYEOF
    )

    # 类型判断：AI agent 传入
    # 用法：MEDIA_TYPE=movie ./handle-pansou.sh 1
    # 用法：MEDIA_TYPE=tv MEDIA_CAT=电视剧 TOTAL_EP=24 ./handle-pansou.sh 1
    MEDIA_TYPE="$(infer_media_type "${MEDIA_TYPE:-}" "$NOTE" "$CLEAN_NAME" "$PAN_TYPE")"
    case "$MEDIA_TYPE" in
        movie) MEDIA_CAT="电影" ;;
        anime) MEDIA_CAT="动漫" ;;
        variety) MEDIA_CAT="综艺" ;;
        documentary) MEDIA_CAT="纪录片" ;;
        *) MEDIA_CAT="电视剧" ;;
    esac
    total_ep="${TOTAL_EP:-}"

    echo "  资源: $CLEAN_NAME | 网盘: $PAN_TYPE | 类型: $MEDIA_CAT"

    # Final import must be separate from probe import. Probe mounts are temporary
    # verification artifacts; the selected source is imported again only after the
    # user confirms which resource to use.
    PROBED_FOLDER_PATH="${PROBE_FOLDER_PATH:-}"
    PROBED_SHARE_ID="${PROBE_SHARE_ID:-}"

    # GBox 登录
    TOKEN=$(curl -s -X POST ${GBOX_URL}/api/accounts/login -H 'Content-Type: application/json' -d "{\"username\":\"${GBOX_USER}\",\"password\":\"${GBOX_PASS}\"}" 2>/dev/null | python3 -c "import json,sys;print(json.load(sys.stdin)['token'])" 2>/dev/null)
    [ -z "$TOKEN" ] && { echo "  ⚠️ GBox 登录失败，跳过"; continue; }

    RESOLVED_MOUNT_PATH=""
    if [ -n "$PROBED_FOLDER_PATH" ] && [ -n "$PROBED_SHARE_ID" ] && [ "$PROBED_SHARE_ID" = "$SHARE_ID" ]; then
        echo "  ♻️ 复用已核验的 UC 挂载: $PROBED_FOLDER_PATH"
        RESOLVED_MOUNT_PATH="$PROBED_FOLDER_PATH"
        CURRENT_MOUNT="$(strip_mount_root "$RESOLVED_MOUNT_PATH")"
    fi

    if [ -z "$RESOLVED_MOUNT_PATH" ]; then
        # GBox 导入
        echo "  📥 导入 GBox..."
        MOUNT_NOTE="${CLEAN_NAME:-$NOTE}"
        # Always include the share short id in the final mount name. Without this,
        # same-title shares can resolve to stale mounts from previous attempts.
        if [[ "$MOUNT_NOTE" != *"${SHARE_ID:0:6}"* ]]; then
            MOUNT_NOTE="${NOTE}_${SHARE_ID:0:6}"
        fi
        MOUNT_NOTE=$(python3 - "$MOUNT_NOTE" "$SHARE_ID" <<'PYEOF'
import re
import sys

raw, share_id = sys.argv[1], sys.argv[2]
safe = []
for ch in raw:
    if ch.isspace():
        safe.append("_")
    elif ch.isalnum() or ch in "-_":
        safe.append(ch)
    elif ch in ".·":
        safe.append("_")
safe = re.sub(r"_+", "_", "".join(safe)).strip("_")
if not safe:
    safe = "media_share"
short = (share_id or "")[:6]
if short and short not in safe:
    safe = f"{safe}_{short}"
print(safe[:80])
PYEOF
        )
        PLANNED_MOUNT_PATH="${MEDIA_CAT}/${MOUNT_NOTE}"

        # 检查该挂载是否已存在（避免误删用户手动添加的）
        IS_NEW_MOUNT=true
        if mount_exists_before "$PLANNED_MOUNT_PATH"; then
            IS_NEW_MOUNT=false
        fi
        CURRENT_MOUNT="$PLANNED_MOUNT_PATH"

        MOUNT_PATH_B64=$(printf '%s' "$PLANNED_MOUNT_PATH" | base64 | tr -d '\n')
        SHARE_ID_B64=$(printf '%s' "$SHARE_ID" | base64 | tr -d '\n')
        PASSWORD_B64=$(printf '%s' "$PASSWORD" | base64 | tr -d '\n')
        set +e
        IMPORT_RESULT=$(MOUNT_PATH_B64="$MOUNT_PATH_B64" SHARE_ID_B64="$SHARE_ID_B64" PASSWORD_B64="$PASSWORD_B64" GBOX_TYPE="$GBOX_TYPE" GBOX_URL="$GBOX_URL" TOKEN="$TOKEN" python3 <<'PYEOF' 2>&1
import base64, json, os, urllib.request
mount_path = base64.b64decode(os.environ.get('MOUNT_PATH_B64', '')).decode('utf-8', 'replace')
share_id = base64.b64decode(os.environ.get('SHARE_ID_B64', '')).decode('utf-8', 'replace')
password = base64.b64decode(os.environ.get('PASSWORD_B64', '')).decode('utf-8', 'replace')
gbox_type = int(os.environ['GBOX_TYPE'])
content = mount_path + chr(9) + share_id
if password:
    # GBox passworded shares require: mount<TAB>share_id<TAB>password.
    content += chr(9) + password
elif gbox_type == 7 and str(os.environ.get("PANSOU_UC_SHARE_ID_PASSWORD_FALLBACK", "")).lower() in ("1", "true", "yes"):
    # Optional debug fallback only. Some UC shares break if share_id is used
    # as password, so keep normal no-password import as the default.
    content += chr(9) + share_id
body = json.dumps({'type': gbox_type, 'content': content}, ensure_ascii=False).encode()
req = urllib.request.Request(os.environ['GBOX_URL'] + '/api/import-shares-with-result', data=body, headers={'X-ACCESS-TOKEN': os.environ['TOKEN'], 'Content-Type': 'application/json'})
with urllib.request.urlopen(req, timeout=20) as resp:
    print(resp.read().decode())
PYEOF
        )
        IMPORT_STATUS=$?
        set -e

        if [ "$IMPORT_STATUS" -ne 0 ]; then
            echo "  ⚠️ GBox 导入请求失败(exit=$IMPORT_STATUS): $(printf '%s' "$IMPORT_RESULT" | head -c 500)"
            cleanup_new_shares "" false || true
            CURRENT_MOUNT=""
            continue
        fi

        if ! printf '%s' "$IMPORT_RESULT" | jq -e . >/dev/null 2>&1; then
            echo "  ⚠️ GBox 导入响应非 JSON: $(printf '%s' "$IMPORT_RESULT" | head -c 500)"
            cleanup_new_shares "" false || true
            CURRENT_MOUNT=""
            continue
        fi

        SUCCESS=$(echo "$IMPORT_RESULT" | jq -r '.success // 0')
        FAILED=$(echo "$IMPORT_RESULT" | jq -r '.failed // 0')
        ERRORS_LEN=$(echo "$IMPORT_RESULT" | jq -r '(.errors // []) | length')
        echo "  导入结果: success=$SUCCESS"
        if [ "$SUCCESS" = "0" ]; then
            if [ "$FAILED" = "0" ] && [ "$ERRORS_LEN" = "0" ]; then
                echo "  ⚠️ 导入未确认，继续按 share_id 查找最终挂载"
            else
                echo "  ⚠️ 导入失败，跳过"
                cleanup_new_shares "" false || true
                CURRENT_MOUNT=""
                continue
            fi
        fi

        case "$PAN_TYPE" in
            uc) SHARE_DIR="/🍓我的UC分享" ;;
            quark) SHARE_DIR="/🍊我的夸克分享" ;;
            aliyun) SHARE_DIR="/🍑我的阿里分享" ;;
            115) SHARE_DIR="/🏷️我的115分享" ;;
            xunlei) SHARE_DIR="/🍒我的迅雷分享" ;;
            *) SHARE_DIR="" ;;
        esac

        RESOLVED_MOUNT_PATH="$(resolve_share_mount_path_via_api "$SHARE_DIR" "$SHARE_ID" "$PLANNED_MOUNT_PATH" "$MOUNT_NOTE" "$CLEAN_NAME" "$NOTE")"
        if [ -z "$RESOLVED_MOUNT_PATH" ]; then
            echo "  ⚠️ 未能解析最终挂载路径，跳过"
            cleanup_new_shares "" false || true
            CURRENT_MOUNT=""
            continue
        fi

        CURRENT_MOUNT="$(strip_mount_root "$RESOLVED_MOUNT_PATH")"
    fi
    VIDEO_PATH="NOT_FOUND"

    for attempt in 1 2; do
        echo "  ⏳ 极速等待 第${attempt}次"
        sleep $((3 * attempt))

        VIDEO_INFO=$($SSH_CMD "python3 <<'PYEOF'
import json, os, urllib.request
base = '${RESOLVED_MOUNT_PATH}'
api = 'http://localhost:5678/api/fs/list'
video_exts = ('.mp4','.mkv','.avi','.ts','.flv','.wmv','.mov','.m2ts','.rmvb')

def collect_videos(path, depth=0):
    if depth > 3:
        return []
    try:
        data = json.dumps({'page':1,'per_page':200,'path':path}).encode()
        req = urllib.request.Request(api, data=data, headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        items = result.get('data',{}).get('content',[])
        if not items:
            return []
        videos = [
            path.rstrip('/') + '/' + i.get('name','')
            for i in items
            if not (i.get('is_dir') or i.get('type') == 1)
            and i.get('name','').lower().endswith(video_exts)
        ]
        for item in items:
            if item.get('is_dir') or item.get('type') == 1:
                videos.extend(collect_videos(path + '/' + item['name'], depth+1))
        return videos
    except:
        return []

videos = collect_videos(base)
if not videos:
    print('NOT_FOUND')
else:
    dirs = [v.rsplit('/', 1)[0] for v in videos]
    print((os.path.commonpath(dirs) if dirs else base) + '\t' + str(len(videos)))
PYEOF
" 2>/dev/null)
        VIDEO_PATH="${VIDEO_INFO%%$'\t'*}"
        VIDEO_FOUND_COUNT=""
        if [[ "$VIDEO_INFO" == *$'\t'* ]]; then
            VIDEO_FOUND_COUNT="${VIDEO_INFO##*$'\t'}"
        fi

        if [ "$VIDEO_PATH" != "NOT_FOUND" ] && [ -n "$VIDEO_PATH" ]; then
            break
        fi
    done

    if [ "$VIDEO_PATH" = "NOT_FOUND" ] || [ -z "$VIDEO_PATH" ]; then
        echo "  ⚠️ 未找到视频文件，尝试下一个资源..."
        cleanup_new_shares "" false || true
        CURRENT_MOUNT=""
        continue
    fi

    MEDIA_TYPE="$(infer_media_type "$MEDIA_TYPE" "$VIDEO_PATH" "$NOTE" "$CLEAN_NAME")"
    case "$MEDIA_TYPE" in
        movie) MEDIA_CAT="电影" ;;
        anime) MEDIA_CAT="动漫" ;;
        variety) MEDIA_CAT="综艺" ;;
        documentary) MEDIA_CAT="纪录片" ;;
        *) MEDIA_CAT="电视剧" ;;
    esac

    echo "  ✅ 找到: $VIDEO_PATH"
    [ -n "${VIDEO_FOUND_COUNT:-}" ] && echo "  📺 递归视频数: $VIDEO_FOUND_COUNT"

    echo "  ✅ 已确认网盘目录可枚举且包含视频，按要求跳过逐文件直链校验"

    # ── 成功！生成 STRM ──
    echo ""
    echo "📝 生成 STRM..."
    GEN_STRM="$SCRIPT_DIR/gen-strm.py"
    STRM_LAYOUT="$SCRIPT_DIR/strm_layout.py"
    NAS_GEN_STRM="/vol1/1000/docker/xiaoya/scripts/gen-strm.py"
    NAS_STRM_LAYOUT="/vol1/1000/docker/xiaoya/scripts/strm_layout.py"
    $SSH_CMD "mkdir -p /vol1/1000/docker/xiaoya/scripts" 2>/dev/null
    scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$GEN_STRM" $NAS_USER@$NAS_HOST:"$NAS_GEN_STRM" 2>/dev/null
    scp -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$STRM_LAYOUT" $NAS_USER@$NAS_HOST:"$NAS_STRM_LAYOUT" 2>/dev/null

    STRM_URL_PREFIX=$(jq -r '.nas.strm_url_prefix // "http://YOUR_NAS_LAN_IP:5678/d"' "$CONFIG_FILE")
    STRM_CUSTOM_PATH_REMOTE="${STRM_CUSTOM_PATH:-}"
    ssh_output=$($SSH_CMD "SOURCE_PATH='/${VIDEO_PATH#/}' STRM_URL_PREFIX='${STRM_URL_PREFIX}' NAME='${CLEAN_NAME}' MEDIA_TYPE='${MEDIA_TYPE}' STRM_CUSTOM_PATH='${STRM_CUSTOM_PATH_REMOTE}' VALIDATE_STRM_PLAYABLE=0 python3 $NAS_GEN_STRM" 2>&1)

    echo "$ssh_output"
    strm_count=$(echo "$ssh_output" | grep '===STRM_COUNT===' | tail -1 | grep -oE '[0-9]+')
    strm_total=$(echo "$ssh_output" | grep '===STRM_TOTAL===' | tail -1 | grep -oE '[0-9]+')
    [ -z "$strm_total" ] && strm_total="$strm_count"
    if [ -z "$strm_total" ] || [ "$strm_total" -eq 0 ]; then
        echo "  ⚠️ STRM 生成失败，尝试下一个资源..."
        cleanup_new_shares "" false || true
        CURRENT_MOUNT=""
        continue
    fi

    # ── 追剧状态：必须由判定器联网确认总集数 ──
    echo ""
    echo "============================================"
    echo "📊 追剧状态"
    echo "============================================"
    mkdir -p "$(dirname "$STATE_FILE")"
    [ ! -f "$STATE_FILE" ] && echo '{"dramas":[]}' > "$STATE_FILE"

    DECIDER="$SCRIPT_DIR/decide-tracking.py"
    eval "$(python3 "$DECIDER" --shell --title "$CLEAN_NAME" --media-type "$MEDIA_TYPE" --current "$strm_total" --provided-total "${total_ep:-}" --note "$NOTE" --source-path "$VIDEO_PATH")"
    CLEAN_NAME="$DECISION_TITLE"
    MEDIA_TYPE="$DECISION_MEDIA_TYPE"
    drama_status="$DECISION_STATUS"
    track_reason="$DECISION_REASON"
    total_json="${DECISION_TOTAL:-0}"

    if [ "$drama_status" = "needs_total" ]; then
        echo "  ❌ 未查到总集数，禁止自动判断是否追剧"
        echo "  说明: $track_reason"
        exit 2
    fi

    # 规范化状态：needs_recovery 不是有效的追剧状态。
    # 保护：只有判定器证据明确含“已完结/完结/全集/xx集全”时，才移出追剧；
    # 否则降级为 ongoing，避免把“共/全 xx 集”总集数元数据误当完结。
    if [ "$drama_status" = "needs_recovery" ]; then
        if printf '%s\n%s\n%s\n' "$DECISION_REASON" "$DECISION_EVIDENCE" "${DECISION_SOURCE_TRACE:-}" | grep -Eq '已完结|完结|全集|[0-9]+集全'; then
            echo "  判定: 已完结但源不全 | 总集数: ${total_json:-未知} | 置信度: $DECISION_CONFIDENCE"
            echo "  依据: $DECISION_EVIDENCE"
            echo "  原因: $track_reason"
            echo ""
            echo "  ⚠️ 该剧已完结，当前资源仅 $strm_total 集（共 ${total_json:-?} 集），不纳入追剧。"
            drama_status="completed"
            track_reason="已完结但源不全(${strm_total}/${total_json:-?})，不纳入追剧"
        else
            echo "  判定器给出缺源，但未见明确完结证据，按更新中保留追剧"
            echo "  依据: $DECISION_EVIDENCE"
            drama_status="ongoing"
            track_reason="未确认完结，当前资源 ${strm_total}/${total_json:-?} 集，继续追剧等待更新"
        fi
    fi

    if [ "$drama_status" = "completed" ]; then
        echo "  判定: 已完结 | 总集数: ${total_json:-未知} | 置信度: $DECISION_CONFIDENCE"
        echo "  依据: $DECISION_EVIDENCE"
        echo "  原因: $track_reason"
        echo ""
        echo "  ✅ 该剧已完结，不纳入追剧列表。"
        echo ""
        echo "============================================"
        echo "🎉 全部完成！"
        echo "============================================"
        echo "剧名: $CLEAN_NAME | 类型: $MEDIA_TYPE"
        echo "文件: $strm_total | 新增: $strm_count | 状态: $drama_status"
        echo "使用资源: 第${SELECTION}个（共${TOTAL_RESULTS}个候选）"
        echo "原因: $track_reason"
        python3 "$SYNC_SCRIPT" push >/dev/null 2>&1 || true
        SELECTED_MOUNT="$CURRENT_MOUNT"
        cleanup_unselected_probe_mounts "$SELECTED_MOUNT"
        rm -f /tmp/probe_mounts.json /tmp/probe_results.json /tmp/pansou_results.json /tmp/media_selection_map.json
        SUCCESS_FLAG=true
        CURRENT_MOUNT=""
        exit 0
    fi

    # 更新追剧记录
    today=$(date -I)
    now=$(date -Iseconds)
    if ! jq -e --arg n "$CLEAN_NAME" '.dramas[] | select((.name == $n) or (.title == $n))' "$STATE_FILE" > /dev/null 2>&1; then
        jq --arg n "$CLEAN_NAME" --argjson ep "$strm_total" --arg date "$today" --arg now "$today" \
            --arg status "$drama_status" --arg reason "$track_reason" --arg evidence "$DECISION_EVIDENCE" \
            --arg mt "$MEDIA_TYPE" --arg sp "$VIDEO_PATH" --argjson total "$total_json" \
            '.dramas += [({"name": $n, "title": $n, "current_episodes": $ep, "status": $status, "added_date": $date, "track_reason": $reason, "media_type": $mt, "source_path": $sp, "last_check": $now, "source_entries": $ep, "total_evidence": $evidence} | if ($total > 0 and $total >= $ep) then . + {"total_episodes": $total} else . end)]' \
            "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
    else
        jq --arg n "$CLEAN_NAME" --argjson ep "$strm_total" --arg now "$now" \
            --arg status "$drama_status" --arg reason "$track_reason" --arg evidence "$DECISION_EVIDENCE" \
            --arg mt "$MEDIA_TYPE" --arg sp "$VIDEO_PATH" --argjson total "$total_json" \
            '(.dramas[] | select((.name == $n) or (.title == $n))) |= (. + {"name": $n, "title": $n, "current_episodes": $ep, "status": $status, "track_reason": $reason, "media_type": $mt, "source_path": $sp, "last_check": $now, "source_entries": $ep, "total_evidence": $evidence} | if ($total > 0 and $total >= $ep) then . + {"total_episodes": $total} else . end)' \
            "$STATE_FILE" > "${STATE_FILE}.tmp" && mv "${STATE_FILE}.tmp" "$STATE_FILE"
    fi

    echo ""
    echo "============================================"
    echo "🎉 全部完成！"
    echo "============================================"
    echo "剧名: $CLEAN_NAME | 类型: $MEDIA_TYPE"
    echo "文件: $strm_total | 新增: $strm_count | 状态: $drama_status"
    echo "使用资源: 第${SELECTION}个（共${TOTAL_RESULTS}个候选）"
    echo "原因: $track_reason"
    python3 "$SYNC_SCRIPT" push >/dev/null 2>&1 || true

    SELECTED_MOUNT="$CURRENT_MOUNT"
    cleanup_unselected_probe_mounts "$SELECTED_MOUNT"
    rm -f /tmp/probe_mounts.json /tmp/probe_results.json /tmp/pansou_results.json /tmp/media_selection_map.json
    SUCCESS_FLAG=true
    CURRENT_MOUNT=""

    # 成功了，退出
    exit 0
done

# 所有候选都失败
echo ""
echo "❌ 所有 ${TOTAL_RESULTS} 个候选资源都失败了"
exit 1
