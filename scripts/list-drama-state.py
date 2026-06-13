#!/usr/bin/env python3
"""Print the canonical drama tracking list after synchronizing with NAS."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR.parent / "data" / "drama-state.json"
SYNC_SCRIPT = SCRIPT_DIR / "sync-drama-state.py"


def type_label(media_type: str) -> str:
    return {
        "tv": "电视剧",
        "variety": "综艺",
        "anime": "动漫",
        "movie": "电影",
        "documentary": "纪录片",
    }.get((media_type or "tv").lower(), "电视剧")


def as_int(value) -> int:
    try:
        return int(value or 0)
    except Exception:
        return 0


def main() -> int:
    proc = subprocess.run(
        ["python3", str(SYNC_SCRIPT), "pull"],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "同步失败").strip()
        print(f"无法从 NAS 同步最新追剧列表：{detail}")
        return proc.returncode

    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    dramas = data.get("dramas", [])
    ongoing = [d for d in dramas if d.get("status") == "ongoing"]

    print(f"追剧列表：{len(ongoing)} 部进行中")
    print()

    groups: dict[str, list[dict]] = {"电视剧": [], "综艺": [], "动漫": [], "电影": [], "纪录片": []}
    for d in ongoing:
        groups.setdefault(type_label(str(d.get("media_type") or "tv")), []).append(d)

    print("【全量追剧明细】")
    print("| 类型 | 剧名 | 本地进度 | 源目录 | 今日新增 | 差值 | 总集数 |")
    print("|------|------|----------|--------|----------|------|--------|")
    for label in ["电视剧", "综艺", "动漫", "电影", "纪录片"]:
        items = groups.get(label) or []
        if not items:
            continue
        for d in items:
            display_name = d.get("name") or d.get("title") or "未命名"
            current = as_int(d.get("current_episodes"))
            source = as_int(d.get("source_entries"))
            total = d.get("total_episodes") or "?"
            new_eps = max(0, source - current)
            gap = max(0, current - source)
            print(f"| {label} | {display_name} | {current}集 | {source}集 | +{new_eps}集 | -{gap}集 | {total}集 |")

    print()
    print("【类型汇总】")
    for label in ["电视剧", "综艺", "动漫", "电影", "纪录片"]:
        items = groups.get(label) or []
        if not items:
            continue
        updated = sum(1 for d in items if as_int(d.get("source_entries")) > as_int(d.get("current_episodes")))
        print(f"- {label}：{len(items)} 部，今日有更新 {updated} 部")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
