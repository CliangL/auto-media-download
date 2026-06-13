#!/bin/bash
# auto-media-download installer/updater for Hermes/OpenClaw.
# New install: detect environment, infer container ports, then ask only for
# missing secrets and media paths. Update: keep existing config/state and just
# refresh code compatibility.

set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$SKILL_DIR/config"
DATA_DIR="$SKILL_DIR/data"
CONFIG_FILE="$CONFIG_DIR/media-config.json"
STATE_FILE="$DATA_DIR/drama-state.json"
HISTORY_FILE="$DATA_DIR/download-history.json"
PLAN_FILE="$SKILL_DIR/install-plan.md"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'

YES=false
RECONFIGURE=false
INSTALL_DEPS=false
INSTALL_CONTAINERS=false

for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=true ;;
        --reconfigure) RECONFIGURE=true ;;
        --install-deps) INSTALL_DEPS=true ;;
        --install-containers) INSTALL_CONTAINERS=true ;;
        --help|-h)
            echo "Usage: bash install.sh [--yes] [--reconfigure] [--install-deps] [--install-containers]"
            exit 0
            ;;
    esac
done

ask() {
    local prompt="$1"
    local default="${2:-}"
    local value
    if [ "$YES" = true ] && [ -n "$default" ]; then
        printf '%s' "$default"
        return
    fi
    if [ -n "$default" ] && [ "$default" != "null" ]; then
        read -r -p "$prompt [$default]: " value
        printf '%s' "${value:-$default}"
    else
        read -r -p "$prompt: " value
        printf '%s' "$value"
    fi
}

confirm() {
    local prompt="$1"
    [ "$YES" = true ] && return 0
    local answer
    read -r -p "$prompt (y/N): " answer
    [ "$answer" = "y" ] || [ "$answer" = "Y" ]
}

json_get() {
    local expr="$1"
    [ -f "$CONFIG_FILE" ] || return 0
    jq -r "$expr // empty" "$CONFIG_FILE" 2>/dev/null || true
}

install_local_deps() {
    local missing=("$@")
    [ "${#missing[@]}" -eq 0 ] && return 0
    if [ "$INSTALL_DEPS" != true ] && ! confirm "是否安装缺失依赖: ${missing[*]}"; then
        return 0
    fi
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y "${missing[@]}"
    elif command -v yum >/dev/null 2>&1; then
        sudo yum install -y "${missing[@]}"
    elif command -v brew >/dev/null 2>&1; then
        brew install "${missing[@]}"
    else
        echo -e "${RED}未识别包管理器，请手动安装: ${missing[*]}${NC}"
    fi
}

require_deps() {
    local missing=()
    for dep in "$@"; do
        command -v "$dep" >/dev/null 2>&1 || missing+=("$dep")
    done
    if [ "${#missing[@]}" -gt 0 ]; then
        echo -e "${RED}缺少必需依赖，无法继续: ${missing[*]}${NC}"
        echo "请安装后重试，或运行: bash install.sh --install-deps"
        exit 1
    fi
}

ssh_cmd() {
    if [ -z "${NAS_HOST:-}" ] || [ -z "${NAS_USER:-}" ]; then
        return 1
    fi
    if ssh -p "${SSH_PORT:-22}" -o BatchMode=yes -o ConnectTimeout=4 "$NAS_USER@$NAS_HOST" "echo ok" >/dev/null 2>&1; then
        SSH_CMD=(ssh -p "${SSH_PORT:-22}" -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$NAS_USER@$NAS_HOST")
    else
        SSH_CMD=(sshpass -p "${NAS_PASS:-}" ssh -p "${SSH_PORT:-22}" -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$NAS_USER@$NAS_HOST")
    fi
}

detect_remote() {
    DETECT_XIAOYA_URL=""
    DETECT_GBOX_URL=""
    DETECT_PANSOU_URL=""
    REMOTE_HAS_DOCKER=false
    REMOTE_CONTAINERS=""
    ssh_cmd || return 0
    if "${SSH_CMD[@]}" "command -v docker >/dev/null 2>&1" >/dev/null 2>&1; then
        REMOTE_HAS_DOCKER=true
        REMOTE_CONTAINERS=$("${SSH_CMD[@]}" "docker ps --format '{{.Names}}|{{.Image}}|{{.Ports}}'" 2>/dev/null || true)
    fi
    [ -z "$REMOTE_CONTAINERS" ] && return 0
    while IFS='|' read -r name image ports; do
        line="${name} ${image}"
        port=$(printf '%s' "$ports" | grep -oE '0\.0\.0\.0:[0-9]+->|:[0-9]+->' | head -1 | grep -oE '[0-9]+' | head -1 || true)
        case "$(printf '%s' "$line" | tr '[:upper:]' '[:lower:]')" in
            *xiaoya*|*alist*) [ -n "$port" ] && DETECT_XIAOYA_URL="http://$NAS_HOST:$port" ;;
            *g-box*|*gbox*) [ -n "$port" ] && DETECT_GBOX_URL="http://$NAS_HOST:$port" ;;
            *pansou*) [ -n "$port" ] && DETECT_PANSOU_URL="http://$NAS_HOST:$port" ;;
        esac
    done <<< "$REMOTE_CONTAINERS"
}

write_plan() {
    cat > "$PLAN_FILE" <<EOF
# auto-media-download 环境检测结果

生成时间: $(date '+%Y-%m-%d %H:%M:%S')

## 本机依赖

- python3/jq/curl/ssh/sshpass 会由 install.sh 检测；如用户授权，可用 \`--install-deps\` 自动安装。

## NAS 检测

- NAS: ${NAS_USER:-?}@${NAS_HOST:-?}:${SSH_PORT:-22}
- Docker: ${REMOTE_HAS_DOCKER:-false}
- Xiaoya/AList: ${XIAOYA_URL:-未配置}
- GBox: ${GBOX_URL:-未配置}
- PanSou: ${PANSOU_URL:-未配置}

## AI 后续动作

如果缺少容器，请 AI 根据用户 NAS 平台选择合适镜像部署：

1. AList/Xiaoya: 暴露 5678，提供 /api/fs/list 和 /d 访问。
2. GBox: 暴露 4567，提供 /api/accounts/login 和 /api/import-shares-with-result。
3. PanSou: 暴露 8080 或用户指定端口，提供搜索 API。
4. 部署完成后重新运行 \`bash install.sh --reconfigure\`，自动识别端口并写入 config/media-config.json。

用户只需要授权安装，并提供 NAS SSH 密码、GBox 密码、STRM 保存目录、可选 Brave/TMDB API key。
EOF
}

echo -e "${BLUE}============================================${NC}"
echo -e "${GREEN}auto-media-download 安装/更新向导${NC}"
echo -e "${BLUE}============================================${NC}"

mkdir -p "$CONFIG_DIR" "$DATA_DIR"
NEW_INSTALL=true
[ -f "$CONFIG_FILE" ] && NEW_INSTALL=false

echo -e "${YELLOW}[1/5] 检测本机依赖${NC}"
missing=()
for dep in python3 jq curl ssh; do
    command -v "$dep" >/dev/null 2>&1 || missing+=("$dep")
done
command -v sshpass >/dev/null 2>&1 || missing+=("sshpass")
if [ "${#missing[@]}" -gt 0 ]; then
    echo "  缺失: ${missing[*]}"
    install_local_deps "${missing[@]}"
else
    echo "  本机依赖齐全"
fi
require_deps python3 jq curl ssh

echo -e "${YELLOW}[2/5] 判断安装模式${NC}"
if [ "$NEW_INSTALL" = false ] && [ "$RECONFIGURE" = false ]; then
    echo "  检测到已有配置：进入更新模式，保留原有接口/API/密码/追剧状态。"
else
    echo "  进入新装/重新配置模式。"
fi

NAS_USER="$(json_get '.nas.user')"
NAS_HOST="$(json_get '.nas.host')"
NAS_PASS="$(json_get '.nas.password')"
SSH_PORT="$(json_get '.nas.ssh_port')"
XIAOYA_URL="$(json_get '.sources[]? | select(.name=="xiaoya") | .internal_url')"
STRM_DIR="$(json_get '.nas.strm_base_dir')"
GBOX_URL="$(json_get '.gbox.internal_url')"
GBOX_USER="$(json_get '.gbox.username')"
GBOX_PASS="$(json_get '.gbox.password')"
PANSOU_URL="$(json_get '.sources[]? | select(.name=="pansou") | .internal_url')"
BRAVE_KEY="$(json_get '.brave.api_key')"
TMDB_KEY="$(json_get '.tmdb.api_key')"

if [ "$NEW_INSTALL" = true ] || [ "$RECONFIGURE" = true ]; then
    NAS_LOGIN="$(ask 'NAS SSH 地址(user@IP)' "${NAS_USER:+$NAS_USER@}$NAS_HOST")"
    NAS_PASS="$(ask 'NAS SSH 密码' "$NAS_PASS")"
    SSH_PORT="$(ask 'NAS SSH 端口' "${SSH_PORT:-22}")"
    NAS_USER="${NAS_LOGIN%@*}"
    NAS_HOST="${NAS_LOGIN#*@}"
    if [ "$NAS_LOGIN" = "$NAS_HOST" ]; then
        NAS_USER="$(ask 'NAS SSH 用户名' "$NAS_USER")"
    fi
    echo -e "${YELLOW}[3/5] 自动识别 NAS 容器和端口${NC}"
    detect_remote
    XIAOYA_URL="$(ask 'Xiaoya/AList 地址' "${XIAOYA_URL:-${DETECT_XIAOYA_URL:-http://$NAS_HOST:5678}}")"
    STRM_DIR="$(ask 'STRM 保存目录' "${STRM_DIR:-/vol1/xxx/docker/xiaoya/strm/C-每日更新}")"
    GBOX_URL="$(ask 'GBox 地址' "${GBOX_URL:-${DETECT_GBOX_URL:-http://$NAS_HOST:4567}}")"
    GBOX_USER="$(ask 'GBox 用户名' "${GBOX_USER:-admin}")"
    GBOX_PASS="$(ask 'GBox 密码' "$GBOX_PASS")"
    PANSOU_URL="$(ask 'PanSou 地址' "${PANSOU_URL:-${DETECT_PANSOU_URL:-http://$NAS_HOST:8080}}")"
    BRAVE_KEY="$(ask 'Brave Search API Key(可选，用于查总集数)' "$BRAVE_KEY")"
    TMDB_KEY="$(ask 'TMDB API Key(可选)' "$TMDB_KEY")"
else
    echo -e "${YELLOW}[3/5] 自动检测现有环境${NC}"
    detect_remote
    XIAOYA_URL="${XIAOYA_URL:-$DETECT_XIAOYA_URL}"
    GBOX_URL="${GBOX_URL:-$DETECT_GBOX_URL}"
    PANSOU_URL="${PANSOU_URL:-$DETECT_PANSOU_URL}"
fi

if [ "$INSTALL_CONTAINERS" = true ]; then
    echo "  容器自动安装由 AI 根据 $PLAN_FILE 执行；本脚本不硬编码第三方镜像，避免部署错误镜像。"
fi

echo -e "${YELLOW}[4/5] 写入配置${NC}"
tmp="$CONFIG_FILE.tmp.$$"
jq -n \
  --arg nas_host "$NAS_HOST" \
  --arg nas_user "$NAS_USER" \
  --arg nas_pass "$NAS_PASS" \
  --argjson ssh_port "${SSH_PORT:-22}" \
  --arg xiaoya "$XIAOYA_URL" \
  --arg strm_dir "${STRM_DIR:-/vol1/xxx/docker/xiaoya/strm/C-每日更新}" \
  --arg gbox_url "$GBOX_URL" \
  --arg gbox_user "${GBOX_USER:-admin}" \
  --arg gbox_pass "$GBOX_PASS" \
  --arg pansou "$PANSOU_URL" \
  --arg brave "$BRAVE_KEY" \
  --arg tmdb "$TMDB_KEY" \
  '{
    nas: {
      host: $nas_host,
      user: $nas_user,
      password: $nas_pass,
      ssh_port: $ssh_port,
      strm_url_prefix: ($xiaoya + "/d"),
      strm_base_dir: $strm_dir,
      remote_scripts_dir: "/vol1/xxx/docker/xiaoya/scripts",
      index_zip: "/vol1/xxx/docker/xiaoya/data/index.zip"
    },
    sources: [
      {name: "xiaoya", internal_url: $xiaoya, priority: 1, auth: "none", description: "Xiaoya/AList"},
      {name: "pansou", internal_url: $pansou, priority: 2, auth: "none", description: "PanSou"}
    ],
    gbox: {internal_url: $gbox_url, username: $gbox_user, password: $gbox_pass},
    strm: {url_prefix: ($xiaoya + "/d"), container_url_prefix: ($xiaoya + "/d")},
    monitor: {enabled: true, drama_state_file: "data/drama-state.json"},
    brave: {api_key: $brave},
    tmdb: {api_key: $tmdb},
    cron: {check_updates: ["08:00", "18:00"], timezone: "Asia/Shanghai"}
  }' > "$tmp"
if [ -f "$CONFIG_FILE" ]; then
    merged="$CONFIG_FILE.merged.$$"
    jq -s '.[0] * .[1]' "$CONFIG_FILE" "$tmp" > "$merged"
    mv "$merged" "$CONFIG_FILE"
    rm -f "$tmp"
else
    mv "$tmp" "$CONFIG_FILE"
fi
chmod 600 "$CONFIG_FILE"

[ -f "$STATE_FILE" ] || printf '{"dramas":[]}\n' > "$STATE_FILE"
[ -f "$HISTORY_FILE" ] || printf '{"history":[]}\n' > "$HISTORY_FILE"
write_plan

echo -e "${YELLOW}[5/5] 语法检查${NC}"
python3 -m py_compile "$SKILL_DIR"/scripts/*.py >/dev/null
for sh in "$SKILL_DIR"/scripts/*.sh; do
    bash -n "$sh"
done

echo -e "${GREEN}完成。配置、追剧状态和历史记录已保留。${NC}"
echo "环境检测报告: $PLAN_FILE"
