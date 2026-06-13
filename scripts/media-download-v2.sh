#!/bin/bash
# media-download-v2.sh - 影音自动下载助手（索引搜索版）
# v5.8 - xiaoya 优先；无有效资源或用户指定网盘时切换 HyperMS 混合搜索

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/../config/media-config.json"
RESOURCES_FILE="/tmp/media_resources.txt"
SELECTION_MAP_FILE="/tmp/media_selection_map.json"
HYPERMS_SCRIPT="$HOME/.openclaw/skills/media/hyperms-strm/scripts/hyperms-strm.py"
AUTO_PANSOU_FALLBACK="${AUTO_MEDIA_AUTO_HYPERMS_FALLBACK:-${AUTO_MEDIA_AUTO_PANSOU_FALLBACK:-0}}"
AUTO_PANSOU_PROBE="${AUTO_MEDIA_AUTO_PANSOU_PROBE:-1}"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ 配置文件不存在: $CONFIG_FILE"
    exit 1
fi

NAS_HOST=$(jq -r '.nas.tailscale_ip // .nas.host' "$CONFIG_FILE")
NAS_USER=$(jq -r '.nas.user' "$CONFIG_FILE")
NAS_PASS=$(jq -r '.nas.password // ""' "$CONFIG_FILE")
SSH_PORT=$(jq -r '.nas.ssh_port // 22' "$CONFIG_FILE")

# 本地模式检测
IS_LOCAL=false
if [ "$NAS_HOST" = "127.0.0.1" ] || [ "$NAS_HOST" = "localhost" ]; then
    IS_LOCAL=true
fi

if [ "$IS_LOCAL" = true ]; then
    SSH_CMD=()
else
    SSH_BASE=(ssh -p "$SSH_PORT" -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$NAS_USER@$NAS_HOST")
    if ssh -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=3 "$NAS_USER@$NAS_HOST" "echo ok" >/dev/null 2>&1; then
        SSH_CMD=("${SSH_BASE[@]}")
    else
        SSH_CMD=(sshpass -p "$NAS_PASS" "${SSH_BASE[@]}")
    fi
fi

extract_title() {
    echo "$1" | sed 's/.*《//;s/》.*//' | head -1
}

clean_query_title() {
    local title="$1"
    title=$(printf '%s' "$title" | sed -E 's/[[:space:]]*(HyperMS|hyperms|UC|uc|115|夸克|网盘|资源|链接)[[:space:]]*/ /g')
    title=$(printf '%s' "$title" | sed -E 's/^(帮我|请|麻烦)?[[:space:]]*(下载|搜索|搜|找)[[:space:]]*//')
    title=$(printf '%s' "$title" | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')
    printf '%s\n' "$title"
}

normalize_title() {
    local title="$1"
    case "$title" in
        *"权利的游戏"*)
            printf '%s\n' "${title//权利的游戏/权力的游戏}"
            ;;
        *)
            printf '%s\n' "$title"
            ;;
    esac
}

human_size() {
    local bytes="$1"
    awk -v b="$bytes" 'BEGIN {
        if (b >= 1073741824) printf "%.2fGB", b/1073741824;
        else if (b >= 1048576) printf "%.1fMB", b/1048576;
        else if (b >= 1024) printf "%.1fKB", b/1024;
        else printf "%dB", b;
    }'
}

run_pansou_fallback() {
    local title="$1"
    echo "🔁 xiaoya 无有效资源，自动切换 PanSou 兜底搜索..."
    : > "$RESOURCES_FILE"
    PANSOU_DISPLAY_LIMIT="${PANSOU_DISPLAY_LIMIT:-8}" python3 "$SCRIPT_DIR/pansou-search.py" "$title"
    if [ "$AUTO_PANSOU_PROBE" = "1" ] || [ "$AUTO_PANSOU_PROBE" = "true" ]; then
        echo ""
        echo "🔎 自动核验 PanSou 候选资源..."
        PANSOU_PROBE_MAX_CANDIDATES="${PANSOU_PROBE_MAX_CANDIDATES:-4}" \
        PANSOU_PROBE_FOLDER_WAIT="${PANSOU_PROBE_FOLDER_WAIT:-8}" \
        PANSOU_PROBE_GLOBAL_TIMEOUT="${PANSOU_PROBE_GLOBAL_TIMEOUT:-90}" \
            python3 "$SCRIPT_DIR/probe_pansou.py"
    fi
}

infer_media_type() {
    local input="$1"
    if [[ "$input" == *"电影"* ]]; then
        printf 'movie\n'
    elif [[ "$input" == *"动漫"* || "$input" == *"动画"* ]]; then
        printf 'anime\n'
    elif [[ "$input" == *"综艺"* ]]; then
        printf 'variety\n'
    elif [[ "$input" == *"纪录片"* ]]; then
        printf 'documentary\n'
    else
        printf '%s\n' "${MEDIA_TYPE:-tv}"
    fi
}

run_hyperms_fallback() {
    local title="$1"
    local media_type="$2"
    echo "🔁 xiaoya 无有效资源，自动切换 HyperMS 网盘搜索..."
    : > "$RESOURCES_FILE"
    rm -f /tmp/probe_results.json /tmp/probe_mounts.json "$SELECTION_MAP_FILE"
    if [ ! -f "$HYPERMS_SCRIPT" ]; then
        echo "⚠️ 找不到 HyperMS helper，回退旧 PanSou 搜索"
        run_pansou_fallback "$title"
        return
    fi
    if python3 "$HYPERMS_SCRIPT" hybrid-search "$title" --media-type "$media_type"; then
        python3 "$SCRIPT_DIR/build-selection-map.py" || true
        echo "============================================"
        echo "💡 请回复序号选择资源（如：1）"
        echo "============================================"
        echo ""
        echo "脚本已完成。收到你的选择后，我会执行："
        echo "  MEDIA_TYPE=<movie|tv|anime> bash scripts/complete-media-selection.sh <序号>"
    else
        echo "⚠️ HyperMS 暂无可处理候选"
    fi
}

main() {
    local input="${1:-}"
    [ -z "$input" ] && { echo "用法: $0 \"下载《片名》\""; exit 1; }
    rm -f "$RESOURCES_FILE" "$SELECTION_MAP_FILE" /tmp/pansou_results.json /tmp/probe_results.json /tmp/probe_mounts.json /tmp/hyperms_selection_map.json

    local title
    title=$(extract_title "$input")
    [ -z "$title" ] && title="$input"
    title=$(clean_query_title "$title")
    [ -z "$title" ] && title="$input"
    local raw_title="$title"
    title=$(normalize_title "$title")
    local media_type
    media_type=$(infer_media_type "$input")

    local lower_input force_pansou force_hyperms
    lower_input=$(printf '%s' "$input" | tr '[:upper:]' '[:lower:]')
    force_pansou="${AUTO_MEDIA_FORCE_PANSOU:-0}"
    force_hyperms="${AUTO_MEDIA_FORCE_HYPERMS:-0}"
    if [[ "$lower_input" == *"hyperms"* || "$input" == *"网盘"* || "$input" == *"UC"* || "$input" == *"uc"* || "$input" == *"夸克"* || "$input" == *"115"* ]]; then
        force_hyperms=1
    elif [[ "$lower_input" == *"pansou"* || "$lower_input" == *"pan sou"* ]]; then
        force_pansou=1
    fi
    if [ "$force_hyperms" = "1" ] || [ "$force_hyperms" = "true" ]; then
        echo "============================================"
        echo "🎬 影音自动下载助手 v5.8（HyperMS 网盘优先）"
        echo "============================================"
        echo "片名: $title"
        if [ "$raw_title" != "$title" ]; then
            echo "规范搜索词: $raw_title -> $title"
        fi
        echo ""
        echo "📡 已按指令跳过 xiaoya，直接搜索 HyperMS..."
        run_hyperms_fallback "$title" "$media_type"
        exit 0
    fi
    if [ "$force_pansou" = "1" ] || [ "$force_pansou" = "true" ]; then
        echo "============================================"
        echo "🎬 影音自动下载助手 v5.7（PanSou 优先）"
        echo "============================================"
        echo "片名: $title"
        if [ "$raw_title" != "$title" ]; then
            echo "规范搜索词: $raw_title -> $title"
        fi
        echo ""
        echo "📡 已按指令跳过 xiaoya，直接搜索 PanSou..."
        exec env PANSOU_DISPLAY_LIMIT="${PANSOU_DISPLAY_LIMIT:-8}" python3 "$SCRIPT_DIR/pansou-search.py" "$title"
    fi

    echo "============================================"
    echo "🎬 影音自动下载助手 v5.8（xiaoya 全库优先）"
    echo "============================================"
    echo "片名: $title"
    if [ "$raw_title" != "$title" ]; then
        echo "规范搜索词: $raw_title -> $title"
    fi
    echo ""
    echo "📡 搜索 xiaoya 全库索引（极速召回 + 分批核验）..."

    local title_b64 remote_output remote_file
    title_b64=$(printf '%s' "$title" | base64 | tr -d '\n')

    # Write Python search script to temp file
    PYREMOTE_FILE=$(mktemp "${TMPDIR:-/tmp}/_xiaoya_search_XXXXXX")
    cat > "$PYREMOTE_FILE" << 'PYREMOTE_CODE'
import base64
import json
import os
import re
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

TITLE = base64.b64decode(os.environ.get("TITLE_B64", "")).decode("utf-8", "replace").strip()
SEARCH_TITLES = [TITLE]
if "权利" in TITLE:
    SEARCH_TITLES.append(TITLE.replace("权利", "权力"))
INDEX = "/vol1/1000/docker/xiaoya/data/index.zip"
ALIST_GET = "http://127.0.0.1:5678/api/fs/get"
ALIST_LIST = "http://127.0.0.1:5678/api/fs/list"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".ts", ".flv", ".wmv", ".mov", ".iso", ".m2ts", ".rmvb")
EXCLUDE = ("有声书", "音乐", "电子书", "non.video", ".wma", ".mp3", ".flac", ".mobi", ".epub", "教程", "教育")
MEDIA_HINTS = ("电视剧", "电影", "动漫", "纪录片", "综艺", "每日更新", "实时同步更新", "115", "ISO", "4K", "1080")
REALTIME_ROOTS = (
    "/每日更新/同步更新中/电视剧实时同步更新",
    "/每日更新/同步更新中/动漫实时同步更新",
    "/每日更新/同步更新中/电影实时同步更新",
    "/每日更新/同步更新中/综艺实时同步更新",
)
FAST_LIMIT = 12
EXTEND_LIMIT = 48
TARGET_RESULTS = 6

def clean_path(path):
    return path.strip().lstrip("./").lstrip("🏷️").strip()

def alist_path(path):
    return "/" + clean_path(path).lstrip("/")

def parse_line(line):
    parts = line.rstrip("\n").split("#")
    path = clean_path(parts[0]) if parts else ""
    name = parts[1].strip() if len(parts) > 1 else os.path.basename(path)
    rating = parts[3].strip() if len(parts) > 3 else "未知"
    year = parts[5].strip() if len(parts) > 5 else "未知"
    return path, name, rating or "未知", year or "未知"

def score_item(path, name, line):
    title_keys = [x.lower().replace(" ", "") for x in SEARCH_TITLES if x]
    n = name.lower().replace(" ", "")
    l = line.lower()
    if not title_keys:
        return None
    if any(x.lower() in l for x in EXCLUDE):
        return None
    normalized_line = l.replace(" ", "")
    score = None
    for t in title_keys:
        if t == n and n:
            score = max(score or 0, 100)
        elif len(t) >= 2 and t in n:
            score = max(score or 0, 70)
        elif len(n) >= 3 and n in t:
            score = max(score or 0, 55)
        elif len(t) >= 2 and t in normalized_line:
            score = max(score or 0, 40)
    if score is None:
        return None
    score += sum(5 for hint in MEDIA_HINTS if hint.lower() in l)
    if any(q in l for q in ("4k", "2160", "remux", "bluray", "蓝光")):
        score += 8
    return score

def request_json(url, payload, timeout=4):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def inspect_path(item):
    score, path, name, rating, year = item
    apath = alist_path(path)
    try:
        info = request_json(ALIST_GET, {"path": apath}, timeout=4)
        data = info.get("data")
        if data:
            item_name = data.get("name", "") or os.path.basename(apath)
            size = int(data.get("size") or 0)
            is_dir = bool(data.get("is_dir")) or int(data.get("type") or 0) == 1 or size == 0
            if not is_dir and item_name.lower().endswith(VIDEO_EXTS):
                return {"score": score + 20, "path": path, "name": name, "rating": rating, "year": year, "eps": 1, "size": size, "kind": "file", "first": item_name}
    except Exception:
        pass

    try:
        listed = request_json(ALIST_LIST, {"path": apath, "page": 1, "per_page": 300}, timeout=8)
        data = listed.get("data") or {}
        content = data.get("content") or []
        videos = [x for x in content if int(x.get("type") or 0) == 2 and x.get("name", "").lower().endswith(VIDEO_EXTS)]
        dirs = [x for x in content if int(x.get("type") or 0) == 1 or x.get("is_dir")]
        final_path = path
        all_videos = []
        total = 0
        first = videos[0].get("name", "?") if videos else ""
        for v in videos:
            all_videos.append((apath, v))
            total += int(v.get("size") or 0)
        if not videos and dirs:
            # 稳妥起见做浅层 BFS：很多 xiaoya 目录是“剧名/Season 1/视频”或“剧名/子目录/视频”。
            queue = [apath.rstrip("/") + "/" + d.get("name", "") for d in dirs if d.get("name")]
            seen = set(queue)
            while queue:
                sub_path = queue.pop(0)
                try:
                    sub = request_json(ALIST_LIST, {"path": sub_path, "page": 1, "per_page": 300}, timeout=8)
                except Exception:
                    continue
                sub_content = (sub.get("data") or {}).get("content") or []
                sub_videos = [x for x in sub_content if int(x.get("type") or 0) == 2 and x.get("name", "").lower().endswith(VIDEO_EXTS)]
                if sub_videos:
                    all_videos.extend((sub_path, x) for x in sub_videos)
                    total += sum(int(x.get("size") or 0) for x in sub_videos)
                    first = first or sub_videos[0].get("name", "?")
                if sub_path.count("/") - apath.count("/") >= 2:
                    continue
                for entry in sub_content:
                    if int(entry.get("type") or 0) == 1 or entry.get("is_dir"):
                        child = sub_path.rstrip("/") + "/" + entry.get("name", "")
                        if child and child not in seen:
                            seen.add(child)
                            queue.append(child)
        if all_videos:
            return {"score": score + min(len(all_videos), 30), "path": final_path, "name": name, "rating": rating, "year": year, "eps": len(all_videos), "size": total, "kind": "dir", "first": first}
    except Exception:
        pass
    return None

def realtime_candidates(seen_paths):
    items = []
    seen = set()
    for root in REALTIME_ROOTS:
        try:
            listed = request_json(ALIST_LIST, {"path": root, "page": 1, "per_page": 500}, timeout=8)
            data = listed.get("data") or {}
            content = data.get("content") or []
        except Exception:
            continue

        for entry in content:
            name = entry.get("name", "") or ""
            path = root.rstrip("/") + "/" + name
            if path in seen or path.lstrip("/") in seen_paths:
                continue
            score = score_item(path, name, path)
            if score is None:
                continue
            seen.add(path)
            items.append((score + 35, path.lstrip("/"), name, "未知", "未知"))

    items.sort(key=lambda x: x[0], reverse=True)
    return items[:12]

# === 索引缓存：检测 index.zip mtime，未更新则复用缓存 ===
INDEX_CACHE = "/vol1/1000/docker/xiaoya/data/.index_cache.txt"
INDEX_MTIME_FILE = "/vol1/1000/docker/xiaoya/data/.index_mtime"

def get_index_lines():
    """返回索引行的迭代器，优先使用缓存"""
    try:
        current_mtime = str(os.path.getmtime(INDEX))
    except OSError:
        current_mtime = ""

    # 检查缓存是否有效
    cache_valid = False
    if os.path.exists(INDEX_CACHE) and os.path.exists(INDEX_MTIME_FILE):
        try:
            saved_mtime = Path(INDEX_MTIME_FILE).read_text().strip()
            if saved_mtime == current_mtime and os.path.getsize(INDEX_CACHE) > 1000:
                cache_valid = True
        except OSError:
            pass

    if cache_valid:
        # 使用缓存文件
        with open(INDEX_CACHE, "r", errors="ignore") as f:
            for line in f:
                yield line
    else:
        # 重新解压并写入缓存
        proc = subprocess.Popen(["unzip", "-p", INDEX], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, errors="ignore")
        try:
            cache_f = open(INDEX_CACHE + ".tmp", "w", errors="ignore")
        except OSError:
            cache_f = None
        for line in proc.stdout:
            if cache_f:
                cache_f.write(line)
            yield line
        proc.wait(timeout=10)
        if cache_f:
            cache_f.close()
            try:
                os.rename(INDEX_CACHE + ".tmp", INDEX_CACHE)
                Path(INDEX_MTIME_FILE).write_text(current_mtime)
            except OSError:
                pass

seen = set()
candidates = []
for raw in get_index_lines():
    path, name, rating, year = parse_line(raw)
    if not path or path in seen:
        continue
    score = score_item(path, name, raw)
    if score is None:
        continue
    seen.add(path)
    candidates.append((score, path, name, rating, year))

def season_number(text):
    value = str(text or "")
    match = re.search(r"S(?:eason)?\s*0?(\d{1,2})(?:\D|$)", value, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:第|so|s)\s*0?(\d{1,2})\s*(?:季|$|\D)", value, re.I)
    if match:
        return int(match.group(1))
    match = re.search(r"S0?(\d{1,2})E\d{1,3}", value, re.I)
    if match:
        return int(match.group(1))
    return None

def add_parent_candidates(items):
    groups = {}
    for score, path, name, rating, year in items:
        path = str(path).rstrip("/")
        if "/" not in path:
            continue
        parent, leaf = path.rsplit("/", 1)
        sn = season_number(leaf) or season_number(name)
        if parent and sn:
            groups.setdefault(parent, []).append((sn, score, path, name, rating, year))

    additions = []
    for parent, rows in groups.items():
        seasons = sorted({row[0] for row in rows})
        if len(seasons) < 2:
            continue
        best = max(rows, key=lambda row: row[1])
        display_title = SEARCH_TITLES[-1] if SEARCH_TITLES else parent.rsplit("/", 1)[-1]
        additions.append((best[1] + 120 + min(len(seasons), 20), parent, display_title, best[4], best[5]))

    merged = {}
    for item in additions + items:
        path = str(item[1]).rstrip("/")
        if path not in merged or item[0] > merged[path][0]:
            merged[path] = item
    return list(merged.values())

candidates.sort(key=lambda x: x[0], reverse=True)
candidates = add_parent_candidates(candidates)
candidates.sort(key=lambda x: x[0], reverse=True)
candidates = candidates[:48]
print(f"INFO\t候选\t{len(candidates)}")

results = []
validated = 0

def validate_batch(rows):
    global validated
    found = []
    if not rows:
        return found
    with ThreadPoolExecutor(max_workers=min(12, len(rows))) as pool:
        futures = [pool.submit(inspect_path, item) for item in rows]
        for future in as_completed(futures):
            validated += 1
            try:
                res = future.result()
                if res:
                    found.append(res)
            except Exception:
                pass
    return found

def add_parent_collections(rows):
    """Merge sibling season folders into a parent collection candidate.

    Xiaoya often indexes each season separately while the web UI shows a parent
    folder containing all seasons. Selecting the parent is what lets gen-strm.py
    recurse and create one STRM per episode across all seasons.
    """
    groups = {}
    for row in rows:
        path = str(row.get("path") or "").rstrip("/")
        parent, leaf = path.rsplit("/", 1) if "/" in path else ("", path)
        sn = season_number(leaf) or season_number(row.get("first"))
        if parent and sn:
            groups.setdefault(parent, []).append((sn, row))

    additions = []
    existing_paths = {str(row.get("path") or "").rstrip("/") for row in rows}
    for parent, entries in groups.items():
        seasons = sorted({sn for sn, _ in entries})
        if len(seasons) < 2 or parent in existing_paths:
            continue
        sample = max((row for _, row in entries), key=lambda r: (int(r.get("score") or 0), int(r.get("size") or 0)))
        eps = sum(int(row.get("eps") or 0) for _, row in entries)
        size = sum(int(row.get("size") or 0) for _, row in entries)
        display_title = SEARCH_TITLES[-1] if SEARCH_TITLES else (sample.get("name") or parent.rsplit("/", 1)[-1])
        additions.append({
            "score": int(sample.get("score") or 0) + 80 + min(len(seasons), 20),
            "path": parent,
            "name": display_title,
            "rating": sample.get("rating") or "未知",
            "year": sample.get("year") or "未知",
            "eps": eps,
            "size": size,
            "kind": "collection",
            "first": sample.get("first") or "多季目录",
        })
    return additions + rows

fast_batch = candidates[:FAST_LIMIT]
results.extend(validate_batch(fast_batch))
print(f"INFO\t快速核验\t{len(fast_batch)}")
print(f"INFO\t快速可用\t{len(results)}")

if len(results) < TARGET_RESULTS and len(candidates) > FAST_LIMIT:
    extend_batch = candidates[FAST_LIMIT:EXTEND_LIMIT]
    if extend_batch:
        extra = validate_batch(extend_batch)
        results.extend(extra)
        print(f"INFO\t补充核验\t{len(extend_batch)}")
        print(f"INFO\t补充可用\t{len(results)}")

results = add_parent_collections(results)
results.sort(key=lambda r: (r.get("eps") or 0, r.get("size") or 0, r.get("score") or 0), reverse=True)
print(f"INFO\t已核验\t{validated}")
if not results:
    rt = realtime_candidates({c[1] for c in candidates})
    print(f"INFO\t实时候选\t{len(rt)}")
    if rt:
        for res in validate_batch(rt):
            try:
                res["score"] += 10
                results.append(res)
            except Exception:
                pass
        results.sort(key=lambda r: (r.get("eps") or 0, r.get("size") or 0, r.get("score") or 0), reverse=True)
    print(f"INFO\t实时可用\t{len(results)}")

for r in results[:15]:
    print("RESULT_JSON\t" + json.dumps(r, ensure_ascii=False, separators=(",", ":")))
print(f"INFO\t可用\t{len(results)}")
PYREMOTE_CODE

    if [ "$IS_LOCAL" = true ]; then
        # 本地模式：直接执行
        if ! remote_output=$(TITLE_B64="$title_b64" python3 "$PYREMOTE_FILE"); then
            echo "❌ 搜索脚本执行失败"
            exit 1
        fi
    else
        # 远程模式：通过 SSH 执行
        if ! remote_output=$("${SSH_CMD[@]}" "TITLE_B64=$title_b64" python3 - < "$PYREMOTE_FILE"); then
            echo "❌ 搜索脚本执行失败"
            exit 1
        fi
    fi
    rm -f "$PYREMOTE_FILE"

    local candidates available
    candidates=$(printf '%s\n' "$remote_output" | awk -F'\t' '$1=="INFO" && $2=="候选" {print $3}' | tail -1)
    available=$(printf '%s\n' "$remote_output" | awk -F'\t' '$1=="INFO" && $2=="可用" {print $3}' | tail -1)
    realtime_candidates=$(printf '%s\n' "$remote_output" | awk -F'\t' '$1=="INFO" && $2=="实时候选" {print $3}' | tail -1)
    realtime_available=$(printf '%s\n' "$remote_output" | awk -F'\t' '$1=="INFO" && $2=="实时可用" {print $3}' | tail -1)
    candidates=${candidates:-0}
    available=${available:-0}
    realtime_candidates=${realtime_candidates:-0}
    realtime_available=${realtime_available:-0}

    if [ "$candidates" = "0" ] && [ "$available" = "0" ]; then
        echo "⚠️ xiaoya 索引和实时目录均未找到可用资源"
        if [ "$AUTO_PANSOU_FALLBACK" = "1" ] || [ "$AUTO_PANSOU_FALLBACK" = "true" ]; then
            run_hyperms_fallback "$title" "$media_type"
        else
            echo "💡 可以尝试 HyperMS 网盘搜索"
            : > "$RESOURCES_FILE"
        fi
        exit 0
    fi

    if [ "$available" = "0" ]; then
        echo "📋 xiaoya 找到 $candidates 个候选，但 AList 核验后暂无可用资源"
        if [ "$AUTO_PANSOU_FALLBACK" = "1" ] || [ "$AUTO_PANSOU_FALLBACK" = "true" ]; then
            run_hyperms_fallback "$title" "$media_type"
        else
            echo "💡 可以尝试 HyperMS 网盘搜索"
            : > "$RESOURCES_FILE"
        fi
        exit 0
    fi

    remote_file=$(mktemp)
    printf '%s\n' "$remote_output" > "$remote_file"

    if [ "$candidates" = "0" ] && [ "$available" != "0" ]; then
        echo "📋 静态索引未命中；xiaoya 实时目录找到 $realtime_candidates 个候选，核验后 $realtime_available 个可用"
    else
        echo "📋 找到 $candidates 个候选，核验后 $available 个可用"
    fi
    echo ""
    echo "============================================"
    echo "📊 资源列表"
    echo "============================================"
    echo ""

    python3 - "$remote_file" "$RESOURCES_FILE" <<'PYLOCAL'
import json
import sys

remote_file, resources_file = sys.argv[1], sys.argv[2]

def human_size(value):
    try:
        b = int(value)
    except Exception:
        b = 0
    if b >= 1024 ** 3:
        return f"{b / 1024 ** 3:.2f}GB"
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.1f}MB"
    if b >= 1024:
        return f"{b / 1024:.1f}KB"
    return f"{b}B"

def detect_fmt(path, first):
    s = f"{path} {first}".lower()
    if "4k" in s or "2160p" in s:
        return "4K"
    if "1080p" in s:
        return "1080P"
    if "remux" in s:
        return "REMUX"
    if "bluray" in s or "蓝光" in s:
        return "蓝光"
    for ext, label in ((".mkv", "MKV"), (".mp4", "MP4"), (".iso", "ISO"), (".ts", "TS")):
        if ext in s:
            return label
    return "目录"

results = []
with open(remote_file, encoding="utf-8", errors="replace") as f:
    for line in f:
        if line.startswith("RESULT_JSON\t"):
            try:
                results.append(json.loads(line.split("\t", 1)[1]))
            except Exception:
                pass

with open(resources_file, "w", encoding="utf-8") as out:
    for r in results:
        out.write(f"{r.get('path','')}\t{r.get('name') or r.get('first') or r.get('path','')}\n")

for i, r in enumerate(results, 1):
    path = r.get("path", "")
    name = r.get("name") or r.get("first") or path.rsplit("/", 1)[-1] or "未知"
    rating = r.get("rating") or "未知"
    year = r.get("year") or "未知"
    eps = r.get("eps", 0)
    size = human_size(r.get("size", 0))
    first = r.get("first") or "未知"
    fmt = detect_fmt(path, first)
    print(f"[{i}] {name}")
    print(f"    年份: {year} | 评分: {rating} | 格式: {fmt} | 集/文件: {eps} | 大小: {size}")
    print(f"    首文件: {first}")
    print(f"    路径: {path}")
    print()
PYLOCAL
    python3 "$SCRIPT_DIR/build-selection-map.py" || true
    rm -f "$remote_file"

    echo "============================================"
    echo "💡 请回复序号选择资源（如：1）"
    echo "============================================"
    echo ""
    echo "脚本已完成。收到你的选择后，我会执行："
    echo "  MEDIA_TYPE=<movie|tv|anime> [TOTAL_EP=N] bash scripts/complete-media-selection.sh <序号>"
}

main "$@"
