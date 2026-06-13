#!/usr/bin/env python3
"""Prepare and apply manual source recovery for expired share-backed dramas."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SKILL_DIR / "config" / "media-config.json"
PENDING_FILE = SKILL_DIR / "data" / "share-recovery-pending.json"
MEDIA_DOWNLOAD = SCRIPT_DIR / "media-download-v2.sh"
HANDLE_SELECTION = SCRIPT_DIR / "handle-selection.sh"
RESOURCES_FILE = Path("/tmp/media_resources.txt")

SHARE_MARKERS = ("我的UC分享", "我的夸克分享", "我的115分享")
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".ts", ".flv", ".wmv", ".mov", ".iso", ".m2ts", ".rmvb")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def load_config() -> dict[str, Any]:
    return load_json(CONFIG_FILE, {})


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def normalize_name(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[《》【】\[\]（）()·._\-:：]", "", text)
    return text.casefold()


def is_share_path(path: str) -> bool:
    return any(marker in (path or "") for marker in SHARE_MARKERS)


def load_pending() -> dict[str, Any]:
    doc = load_json(PENDING_FILE, {"recoveries": []})
    if not isinstance(doc, dict):
        return {"recoveries": []}
    recoveries = doc.get("recoveries")
    if not isinstance(recoveries, list):
        doc["recoveries"] = []
    return doc


def save_pending(doc: dict[str, Any]) -> None:
    write_json(PENDING_FILE, doc)


def store_recovery(entry: dict[str, Any]) -> None:
    doc = load_pending()
    items = [x for x in doc["recoveries"] if normalize_name(x.get("title")) != normalize_name(entry.get("title"))]
    items.append(entry)
    items.sort(key=lambda x: normalize_name(x.get("title")))
    doc["recoveries"] = items
    save_pending(doc)


def find_recovery(title: str) -> dict[str, Any] | None:
    doc = load_pending()
    key = normalize_name(title)
    for item in doc.get("recoveries", []):
        if normalize_name(item.get("title")) == key:
            return item
    return None


def remove_recovery(title: str) -> None:
    doc = load_pending()
    key = normalize_name(title)
    doc["recoveries"] = [x for x in doc.get("recoveries", []) if normalize_name(x.get("title")) != key]
    save_pending(doc)


def parse_resources() -> list[dict[str, Any]]:
    if not RESOURCES_FILE.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in RESOURCES_FILE.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.startswith("PANSOU\t"):
            continue
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        source_path, name = parts[0].strip(), parts[1].strip()
        rows.append(
            {
                "source_path": source_path,
                "name": name,
                "is_share": is_share_path(source_path),
            }
        )
    return rows


def alist_url() -> str:
    cfg = load_config()
    for source in cfg.get("sources", []):
        if source.get("name") == "xiaoya":
            return str(source.get("internal_url") or "http://YOUR_NAS_IP:5678").rstrip("/")
    return "http://YOUR_NAS_IP:5678"


def gbox_config() -> tuple[str, str, str]:
    cfg = load_config()
    gbox = cfg.get("gbox", {})
    return (
        str(gbox.get("internal_url") or "http://YOUR_NAS_IP:4567").rstrip("/"),
        str(gbox.get("username") or "admin"),
        str(gbox.get("password") or "admin"),
    )


def request_json(url: str, *, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: int = 15) -> dict[str, Any]:
    payload = None if data is None else json.dumps(data, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=payload, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def list_live_shares() -> list[dict[str, Any]]:
    base, user, password = gbox_config()
    try:
        login = request_json(
            f"{base}/api/accounts/login",
            data={"username": user, "password": password},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        token = str(login.get("token") or "")
        if not token:
            return []
        rows: list[dict[str, Any]] = []
        page = 0
        while True:
            data = request_json(
                f"{base}/api/shares?page={page}&size=200",
                headers={"X-ACCESS-TOKEN": token},
                timeout=10,
            )
            content = data.get("content") or []
            rows.extend(x for x in content if isinstance(x, dict))
            if data.get("last", True) or not content:
                break
            page += 1
        return rows
    except Exception:
        return []


def list_alist(path: str, *, page: int = 1, per_page: int = 200) -> dict[str, Any]:
    return request_json(
        f"{alist_url()}/api/fs/list",
        data={"path": path, "page": page, "per_page": per_page},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )


def get_alist(path: str) -> dict[str, Any]:
    return request_json(
        f"{alist_url()}/api/fs/get",
        data={"path": path},
        headers={"Content-Type": "application/json"},
        timeout=15,
    )


def find_first_video(path: str, depth: int = 0, max_depth: int = 4) -> tuple[int, str | None]:
    if depth > max_depth:
        return 0, None
    try:
        data = list_alist(path, page=1, per_page=500)
    except Exception:
        return 0, None
    if data.get("code") != 200:
        return 0, None
    items = (data.get("data") or {}).get("content") or []
    total = 0
    sample: str | None = None
    for item in items:
        name = str(item.get("name") or "")
        child = path.rstrip("/") + "/" + name
        is_dir = item.get("is_dir") or item.get("type") == 1
        if is_dir:
            child_total, child_sample = find_first_video(child, depth + 1, max_depth)
            total += child_total
            sample = sample or child_sample
        elif name.lower().endswith(VIDEO_EXTS):
            total += 1
            sample = sample or child
    return total, sample


def is_playable(sample_path: str | None) -> bool:
    if not sample_path:
        return False
    try:
        data = get_alist(sample_path)
    except Exception:
        return False
    if data.get("code") != 200:
        return False
    payload = data.get("data") or {}
    return bool(payload.get("raw_url") or payload.get("url") or payload.get("sign"))


def live_share_candidates(title: str) -> list[dict[str, Any]]:
    key = normalize_name(title)
    if not key:
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in list_live_shares():
        path = str(row.get("path") or "").strip()
        if not path or not is_share_path(path):
            continue
        if key not in normalize_name(path):
            continue
        total, sample = find_first_video(path)
        if total <= 0 or not sample or not is_playable(sample):
            continue
        source_path = str(Path(sample).parent).replace("\\", "/")
        if source_path in seen:
            continue
        seen.add(source_path)
        candidates.append(
            {
                "source_path": source_path,
                "name": title,
                "is_share": True,
                "live_share": True,
                "share_path": path,
                "count": total,
                "sample": sample,
            }
        )
    candidates.sort(key=lambda x: (-int(x.get("count") or 0), str(x.get("share_path") or "")))
    return candidates


def print_options(title: str, candidates: list[dict[str, Any]]) -> None:
    print(f"⚠️ {title} 的网盘分享源目录已失效")
    if not candidates:
        print("❌ 当前没有找到可切换的有效候选，请手动重新搜索")
        return
    print(f"✅ 找到 {len(candidates)} 个可切换候选，请确认序号后再继续")
    for idx, item in enumerate(candidates, 1):
        if item.get("live_share"):
            marker = f"当前活挂载/{item.get('count', '?')}集"
        else:
            marker = "网盘分享" if item.get("is_share") else "稳定目录"
        print(f"[{idx}] {item.get('name')} | {item.get('source_path')} | {marker}")
    print(f"💾 已记录待恢复项，可执行: python3 {SCRIPT_DIR / 'share-source-recovery.py'} apply --title {title!r} --selection 序号")


def candidate_episode_count(candidate: dict[str, Any]) -> int:
    source_path = str(candidate.get("source_path") or "").strip()
    if not source_path:
        return 0
    count = int(candidate.get("count") or 0)
    if count > 0:
        return count
    total, _sample = find_first_video(source_path)
    return int(total or 0)


def cmd_scan(args: argparse.Namespace) -> int:
    title = str(args.title or "").strip()
    if not title:
        print("缺少标题", file=sys.stderr)
        return 2

    query = f"下载《{title}》"
    proc = subprocess.run(
        ["bash", str(MEDIA_DOWNLOAD), query],
        text=True,
        capture_output=True,
        timeout=180,
    )
    candidates = live_share_candidates(title)
    candidates.extend(parse_resources())

    old_norm = normalize_name(args.old_source or "")
    if old_norm:
        filtered = [item for item in candidates if normalize_name(item.get("source_path")) != old_norm]
        if filtered:
            candidates = filtered

    deduped: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in candidates:
        source_path = str(item.get("source_path") or "").strip()
        key = normalize_name(source_path)
        if not key or key in seen_paths:
            continue
        seen_paths.add(key)
        deduped.append(item)
    candidates = sorted(
        deduped,
        key=lambda x: (
            0 if x.get("live_share") else 1,
            0 if x.get("is_share") else 1,
            -int(x.get("count") or 0),
            str(x.get("source_path") or ""),
        ),
    )

    recovery = {
        "title": title,
        "media_type": args.media_type or "tv",
        "old_source_path": args.old_source or "",
        "total_episodes": int(args.total or 0),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidates": candidates,
        "search_stdout": proc.stdout,
        "search_stderr": proc.stderr,
    }
    store_recovery(recovery)
    print_options(title, candidates)
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    recovery = find_recovery(args.title)
    if not recovery:
        print(f"❌ 未找到待恢复项目: {args.title}")
        return 1

    candidates = recovery.get("candidates") or []
    try:
        selection = max(1, int(args.selection))
    except Exception:
        selection = 1
    if selection > len(candidates):
        print(f"❌ 无效选择，当前只有 {len(candidates)} 个候选")
        return 1

    chosen = candidates[selection - 1]
    expected_total = int(recovery.get("total_episodes") or 0)
    actual_count = candidate_episode_count(chosen)
    if expected_total > 0 and actual_count > 0 and actual_count < expected_total:
        print(
            f"❌ 候选集数不足，禁止自动切换：{recovery.get('title') or args.title} 需要 {expected_total} 集，候选仅核验到 {actual_count} 集\n"
            f"候选路径: {chosen.get('source_path')}",
            file=sys.stderr,
        )
        return 1

    payload = "\n".join(f"{item['source_path']}\t{item['name']}" for item in candidates) + "\n"
    RESOURCES_FILE.write_text(payload, encoding="utf-8")

    env = dict(os.environ)
    env["MEDIA_TYPE"] = str(recovery.get("media_type") or "tv")
    total = int(recovery.get("total_episodes") or 0)
    if total > 0:
        env["TOTAL_EP"] = str(total)

    proc = subprocess.run(
        ["bash", str(HANDLE_SELECTION), str(selection)],
        text=True,
        env=env,
        timeout=240,
    )
    if proc.returncode == 0:
        remove_recovery(str(recovery.get("title") or args.title))
    return proc.returncode


def cmd_list(_: argparse.Namespace) -> int:
    doc = load_pending()
    items = doc.get("recoveries") or []
    if not items:
        print("当前没有待确认的失效分享源恢复项")
        return 0
    print(f"待确认恢复项：{len(items)} 个")
    for item in items:
        title = item.get("title") or "未命名"
        count = len(item.get("candidates") or [])
        old = item.get("old_source_path") or ""
        print(f"- {title}：{count} 个候选 | 原源目录: {old}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="share source recovery")
    sub = ap.add_subparsers(dest="cmd", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--title", required=True)
    scan.add_argument("--media-type", default="tv")
    scan.add_argument("--old-source", default="")
    scan.add_argument("--total", default="0")
    scan.set_defaults(func=cmd_scan)

    apply_cmd = sub.add_parser("apply")
    apply_cmd.add_argument("--title", required=True)
    apply_cmd.add_argument("--selection", required=True)
    apply_cmd.set_defaults(func=cmd_apply)

    ls = sub.add_parser("list")
    ls.set_defaults(func=cmd_list)
    return ap


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
