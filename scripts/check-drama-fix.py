#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from strm_layout import resolve_strm_dir

CANONICAL_STATE = "/vol1/1000/docker/xiaoya/data/drama-state.json"
LEGACY_STATE = "/vol1/1000/docker/xiaoya/drama-state.json"
ALIST_URL = "http://127.0.0.1:5678/api/fs/list"
STRM_URL_PREFIX = "http://YOUR_NAS_LAN_IP:5678/d"
GEN_STRM = str(Path(__file__).resolve().parent / "gen-strm.py")


def type_label(media_type):
    return {
        "tv": "电视剧",
        "variety": "综艺",
        "anime": "动漫",
        "movie": "电影",
        "documentary": "纪录片",
    }.get((media_type or "tv").lower(), "电视剧")


def normalize_entry(raw):
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or raw.get("title") or "").strip()
    if not name:
        return None
    return {
        "name": name,
        "title": name,
        "current_episodes": int(raw.get("current_episodes", 0) or 0),
        "total_episodes": int(raw.get("total_episodes", 0) or 0),
        "status": raw.get("status", "ongoing"),
        "source_path": str(raw.get("source_path") or "").lstrip("/"),
        "media_type": raw.get("media_type", "tv"),
        "last_check": raw.get("last_check", ""),
        "source_entries": int(raw.get("source_entries", 0) or 0),
        "track_reason": raw.get("track_reason", ""),
        "total_evidence": raw.get("total_evidence", ""),
        "added_date": raw.get("added_date", ""),
    }


def list_entries(doc):
    if isinstance(doc, dict) and isinstance(doc.get("dramas"), list):
        return [x for x in (normalize_entry(row) for row in doc.get("dramas") or []) if x]
    if isinstance(doc, dict):
        rows = []
        for key, value in doc.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("name", key)
                rows.append(item)
        return [x for x in (normalize_entry(row) for row in rows) if x]
    return []


def load_state():
    merged = {}
    for path in (CANONICAL_STATE, LEGACY_STATE):
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            continue
        for entry in list_entries(doc):
            merged[entry["name"]] = entry
    return merged


def count_videos(source_path, depth=0):
    if depth > 5:
        return 0
    api_path = "/" + source_path.lstrip("/")
    api_data = json.dumps({"path": api_path, "page": 1, "per_page": 1000}).encode()
    req = urllib.request.Request(ALIST_URL, data=api_data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read())
    if result.get("code") != 200 or result.get("data") is None:
        raise RuntimeError(result.get("message") or "路径不存在")
    items = result.get("data", {}).get("content", []) or []
    total = 0
    for item in items:
        name = str(item.get("name") or "")
        is_dir = item.get("is_dir") or item.get("type") == 1
        if is_dir:
            total += count_videos(source_path.rstrip("/") + "/" + name, depth + 1)
        elif item.get("type", 0) == 2:
            total += 1
    return total


def count_local_strm(drama):
    base = resolve_strm_dir(str(drama.get("name") or ""), str(drama.get("media_type") or "tv"))
    if not os.path.isdir(base):
        return 0
    seen = set()
    for root, _, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".strm"):
                continue
            full = os.path.join(root, fn)
            if not os.path.getsize(full):
                continue
            match = re.search(r"S\d+E(\d+)", fn, re.I) or re.search(r"E(\d+)", fn, re.I) or re.match(r"(\d+)", fn)
            seen.add(int(match.group(1)) if match else fn)
    return len(seen)


def generate_missing_strm(drama):
    env = os.environ.copy()
    env["SOURCE_PATH"] = "/" + str(drama.get("source_path") or "").lstrip("/")
    env["STRM_URL_PREFIX"] = STRM_URL_PREFIX
    env["NAME"] = str(drama.get("name") or "")
    env["MEDIA_TYPE"] = str(drama.get("media_type") or "tv")
    proc = subprocess.run(["python3", GEN_STRM], env=env, text=True, capture_output=True, timeout=180)
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def save_states(entries):
    list_doc = {"dramas": list(entries.values())}
    with open(CANONICAL_STATE, "w", encoding="utf-8") as f:
        json.dump(list_doc, f, indent=2, ensure_ascii=False)
        f.write("\n")
    legacy_doc = {row["name"]: row for row in entries.values() if row.get("name")}
    with open(LEGACY_STATE, "w", encoding="utf-8") as f:
        json.dump(legacy_doc, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main():
    all_dramas = load_state()
    print("共加载 {} 个追更条目".format(len(all_dramas)))
    grouped_lines = defaultdict(list)
    updates_log = []
    alerts_log = []

    for name, drama in all_dramas.items():
        if drama.get("status") != "ongoing":
            continue
        label = type_label(drama.get("media_type", "tv"))
        source_path = drama.get("source_path", "")
        current_ep = int(drama.get("current_episodes", 0) or 0)
        if not source_path:
            grouped_lines[label].append("[跳过] {}: 无 source_path".format(name))
            continue
        try:
            source_count = count_videos(source_path)
            if source_count <= 0:
                drama["track_reason"] = "源目录失效或无有效视频"
                grouped_lines[label].append("[失效] {}: {}".format(name, drama["track_reason"]))
                alerts_log.append("{} {}: 源失效".format(label, name))
                continue
            local_count = count_local_strm(drama)
            if source_count > local_count:
                ok, detail = generate_missing_strm(drama)
                if ok:
                    local_count = count_local_strm(drama)
                else:
                    grouped_lines[label].append("[错误] {}: 补 STRM 失败 {}".format(name, detail[:120]))
            if local_count > current_ep:
                grouped_lines[label].append("[更新] {}: {} -> {}".format(name, current_ep, local_count))
                updates_log.append("{} {}: {}->{}".format(label, name, current_ep, local_count))
                drama["current_episodes"] = local_count
                drama["source_entries"] = source_count
                drama["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                total = int(drama.get("total_episodes", 0) or 0)
                if total > 0 and local_count >= total:
                    drama["status"] = "completed"
                    grouped_lines[label].append("  ✅ {} 已全集".format(name))
            else:
                grouped_lines[label].append("[无更新] {}: {}".format(name, current_ep))
                drama["source_entries"] = source_count
                drama["last_check"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        except Exception as e:
            grouped_lines[label].append("[错误] {}: {}".format(name, e))

    for label in ["电视剧", "综艺", "动漫", "电影", "纪录片"]:
        lines = grouped_lines.get(label)
        if not lines:
            continue
        print("\n【{}】".format(label))
        for line in lines:
            print(line)

    save_states(all_dramas)
    print("\n✅ 状态已更新")

    if updates_log:
        print("\n【更新摘要】")
        print("\n".join(updates_log))
    if alerts_log:
        print("\n【失效告警】")
        print("\n".join(alerts_log))
    if not updates_log and not alerts_log:
        print("\n✅ 所有追更条目均为最新")


if __name__ == "__main__":
    main()
