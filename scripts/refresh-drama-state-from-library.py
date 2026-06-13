#!/usr/bin/env python3
"""Rebuild active drama tracking from the current NAS STRM library."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from strm_layout import get_categories, get_strm_base


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SKILL_DIR / "config" / "media-config.json"
STATE_FILE = SKILL_DIR / "data" / "drama-state.json"
SUMMARY_FILE = SKILL_DIR / "data" / "library-tracking-refresh-summary.json"
SYNC_SCRIPT = SCRIPT_DIR / "sync-drama-state.py"
DECIDER = SCRIPT_DIR / "decide-tracking.py"
RECOVERY = SCRIPT_DIR / "share-source-recovery.py"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".ts", ".flv", ".wmv", ".mov", ".iso", ".m2ts", ".rmvb")
SCAN_MEDIA_TYPES = ("tv", "anime", "variety", "documentary")
STALE_SOURCE_CHECK_DAYS = 3
SHARE_MARKERS = ("我的UC分享", "我的夸克分享", "我的115分享", "我的迅雷分享", "我的阿里分享")
ARCHIVE_REASONS_BLOCKING_READD = {"completed", "inactive"}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    tmp.replace(path)


def run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


SYNC_TIMEOUT = int(os.environ.get("HERMES_DRAMA_SYNC_TIMEOUT", "120"))
SYNC_RETRIES = int(os.environ.get("HERMES_DRAMA_SYNC_RETRIES", "1"))


def sync(mode: str) -> None:
    last_exc: Exception | None = None
    attempts = max(1, SYNC_RETRIES + 1)
    for attempt in range(1, attempts + 1):
        try:
            proc = run(["python3", str(SYNC_SCRIPT), mode], timeout=SYNC_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            last_exc = exc
            if attempt >= attempts:
                raise RuntimeError(
                    f"sync {mode} timed out after {SYNC_TIMEOUT} seconds"
                ) from exc
            time.sleep(2)
            continue
        if proc.returncode == 0:
            return
        detail = (proc.stderr or proc.stdout or "unknown sync error").strip()
        last_exc = RuntimeError(f"sync {mode} failed: {detail}")
        if attempt >= attempts:
            raise last_exc
        time.sleep(2)
    if last_exc is not None:
        raise last_exc


def as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def strip_backup_suffix(value: Any) -> str:
    return re.sub(r"(?:[._\-])?bak(?:cup)?[-_]?\d{6,14}$", "", str(value or "").strip(), flags=re.I)


def is_backup_name(value: Any) -> bool:
    return bool(re.search(r"(?:[._\-])?bak(?:up)?[-_]?\d{6,14}$", str(value or "").strip(), re.I))


def normalize_name(value: Any) -> str:
    text = strip_backup_suffix(value)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[《》【】\[\]（）()·._\-:：]", "", text)
    return text.casefold()


def normalize_series_name(value: Any) -> str:
    key = normalize_name(value)
    return re.sub(r"1$", "", key)


def source_roots(item: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    raw_roots = item.get("source_roots")
    if isinstance(raw_roots, list):
        roots.update(str(root).rstrip("/") for root in raw_roots if str(root or "").strip())
    source_path = str(item.get("source_path") or "").strip().rstrip("/")
    if source_path:
        roots.add(source_path)
    return roots


def same_source_family(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_fp = str(left.get("source_fingerprint") or "").strip()
    right_fp = str(right.get("source_fingerprint") or "").strip()
    if left_fp and right_fp and left_fp == right_fp:
        return True
    left_roots = source_roots(left)
    right_roots = source_roots(right)
    if left_roots & right_roots:
        return True
    for left_root in left_roots:
        for right_root in right_roots:
            if left_root and right_root and (left_root.startswith(right_root + "/") or right_root.startswith(left_root + "/")):
                return True
    return False


def load_recent_archive() -> list[dict[str, Any]]:
    archive_files = sorted(
        (SKILL_DIR / "data").glob("drama-archive-*.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    entries: list[dict[str, Any]] = []
    for path in archive_files[:7]:
        data = load_json(path, {})
        archived = data.get("archived") if isinstance(data, dict) else None
        if isinstance(archived, list):
            entries.extend(item for item in archived if isinstance(item, dict))
    return entries


def archived_block_keys() -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in load_recent_archive():
        name = normalize_name(item.get("name"))
        if not name:
            continue
        reason = str(item.get("archived_reason") or "").strip().lower()
        if reason in ARCHIVE_REASONS_BLOCKING_READD:
            keys.add((name, reason))
    return keys


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def days_since(now_dt: datetime, value: Any) -> int:
    dt = parse_iso(value)
    if dt is None:
        return 999
    # Historical state may contain date-only / naive timestamps such as
    # "2026-06-02", while current runs use timezone-aware local time. Python
    # refuses aware-naive subtraction, so interpret legacy naive values in the
    # same local timezone as the current run.
    if now_dt.tzinfo is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=now_dt.tzinfo)
    elif now_dt.tzinfo is None and dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=dt.tzinfo)
    delta = now_dt - dt
    return max(0, int(delta.total_seconds() // 86400))


def choose_progress_time(old: dict[str, Any] | None, current: int, now_iso: str) -> str:
    previous = old or {}
    previous_current = as_int(previous.get("current_episodes"))
    if current > previous_current:
        return now_iso
    for key in ("last_progress_at", "added_date", "last_check"):
        if previous.get(key):
            return str(previous.get(key))
    return now_iso


def load_config() -> dict[str, Any]:
    return load_json(CONFIG_FILE, {})


def _is_local_nas() -> bool:
    return Path("/vol1/1000/docker/xiaoya/data/drama-state.json").exists()


def media_type_label(media_type: str | None) -> str:
    return {
        "tv": "电视剧",
        "anime": "动漫",
        "variety": "综艺",
        "movie": "电影",
        "documentary": "纪录片",
    }.get((media_type or "tv").lower(), "其他")


class LibraryRefresher:
    def __init__(self, *, title_filters: list[str] | None = None, enable_recovery_scan: bool = True) -> None:
        self.config = load_config()
        nas = self.config.get("nas") or {}
        source = next((item for item in (self.config.get("sources") or []) if item.get("name") == "xiaoya"), {})
        self.nas_host = str(nas.get("tailscale_ip") or nas.get("host") or "")
        self.nas_user = str(nas.get("user") or "")
        self.alist_url = str(source.get("internal_url") or "http://127.0.0.1:5678").rstrip("/")
        self.now_dt = datetime.now().astimezone()
        self.now_iso = self.now_dt.isoformat(timespec="seconds")
        self.base_dir = get_strm_base(self.config)
        categories = get_categories(self.config)
        self.scan_categories = {
            media_type: categories[media_type]
            for media_type in SCAN_MEDIA_TYPES
            if categories.get(media_type)
        }
        raw_filters = title_filters or []
        self.title_filters = {normalize_name(item) for item in raw_filters if normalize_name(item)}
        self.title_series_filters = {normalize_series_name(item) for item in raw_filters if normalize_series_name(item)}
        self.enable_recovery_scan = enable_recovery_scan

    def ssh(self, script: str, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        if _is_local_nas():
            return run(["sh", "-c", script], timeout=timeout)
        return run(
            [
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=15",
                f"{self.nas_user}@{self.nas_host}",
                script,
            ],
            timeout=timeout,
        )

    def scan_library(self) -> list[dict[str, Any]]:
        payload = json.dumps(
            {
                "base_dir": self.base_dir,
                "categories": self.scan_categories,
            },
            ensure_ascii=False,
        )
        py = f"""
import json
import hashlib
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse, unquote

payload = json.loads({payload!r})
base_dir = Path(payload["base_dir"])
categories = payload["categories"]

def episode_key(name):
    m = re.search(r"S0?(\\d{{1,3}})\\s*E0?(\\d{{1,4}})", name, re.I)
    if m:
        return ("season_episode", int(m.group(1)), int(m.group(2)))
    m = re.search(r"(?:^|[^\\w])E0?(\\d{{1,4}})(?:[^\\w]|$)", name, re.I) or re.match(r"0?(\\d{{1,4}})(?:\\D|$)", name)
    return ("episode", int(m.group(1))) if m else ("file", name)

def parse_source_parent(text):
    parsed = urlparse(text)
    path = unquote(parsed.path or "")
    if "/d/" not in path:
        return ""
    raw = "/" + path.split("/d/", 1)[1].lstrip("/")
    return raw.rsplit("/", 1)[0]

def choose_source_path(parents):
    parents = [p for p in parents if p]
    if not parents:
        return ""
    def season_root(path):
        parts = [part for part in path.strip("/").split("/") if part]
        if not parts:
            return ""
        if len(parts) >= 2 and re.match(r"(?:Season\\s*\\d+|S\\d{{1,3}}|第\\d+季|[\\d一二三四五六七八九十]+季)$", parts[-1], re.I):
            parts = parts[:-1]
        return "/" + "/".join(parts)
    root_counts = Counter(season_root(parent) for parent in parents if season_root(parent))
    if root_counts:
        return root_counts.most_common(1)[0][0]
    counts = Counter(parents)
    return counts.most_common(1)[0][0]

items = []
for media_type, category in categories.items():
    root = base_dir / category
    if not root.is_dir():
        continue
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        keys = set()
        parents = []
        file_count = 0
        for p in child.rglob("*.strm"):
            try:
                rel = p.relative_to(child)
            except Exception:
                continue
            if any(part.startswith(".") for part in rel.parts):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                continue
            if not text:
                continue
            file_count += 1
            keys.add(episode_key(p.name))
            parent = parse_source_parent(text)
            if parent:
                parents.append(parent)
        if not keys:
            continue
        source_roots = sorted(set(choose_source_path([parent]) for parent in parents if parent))
        fingerprint = hashlib.sha1("\\n".join(sorted(set(parents))).encode("utf-8", "ignore")).hexdigest()[:16] if parents else ""
        items.append(
            {{
                "name": child.name.strip(),
                "media_type": media_type,
                "strm_path": str(child),
                "current_episodes": len(keys),
                "source_path": choose_source_path(parents),
                "source_fingerprint": fingerprint,
                "source_roots": source_roots,
                "source_parent_count": len(set(parents)),
                "strm_files": file_count,
            }}
        )

print(json.dumps(items, ensure_ascii=False))
"""
        proc = self.ssh(f"python3 - <<'PY'\n{py}\nPY", timeout=180)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "scan library failed").strip())
        items = json.loads(proc.stdout or "[]")
        rows = [row for row in items if isinstance(row, dict)]
        rows = [row for row in rows if not is_backup_name(row.get("name"))]
        if not self.title_filters:
            return rows
        return [
            row
            for row in rows
            if normalize_name(row.get("name")) in self.title_filters
            or normalize_series_name(row.get("name")) in self.title_series_filters
        ]

    def list_source_videos(self, path: str, media_type: str, depth: int = 0) -> tuple[str, list[tuple[str, str]]]:
        if not path:
            return "missing_path", []
        if path.lower().endswith(VIDEO_EXTS):
            return "ok", [(str(Path(path).parent), Path(path).name)]
        if depth > 5:
            return "ok", []
        payload = json.dumps({"path": path, "page": 1, "per_page": 500}).encode()
        req = urllib.request.Request(
            f"{self.alist_url}/api/fs/list",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except Exception:
            return "api_unreachable", []
        if data.get("code") != 200:
            return "source_missing", []
        items = (data.get("data") or {}).get("content") or []
        videos: list[tuple[str, str]] = []
        for item in items:
            name = str(item.get("name") or "")
            child = path.rstrip("/") + "/" + name
            if item.get("is_dir") or item.get("type") == 1:
                status, child_videos = self.list_source_videos(child, media_type, depth + 1)
                if status == "api_unreachable":
                    return status, []
                videos.extend(child_videos)
            elif name.lower().endswith(VIDEO_EXTS):
                videos.append((path, name))
        return "ok", self.dedupe_videos(videos, media_type)

    def dedupe_videos(self, videos: list[tuple[str, str]], media_type: str) -> list[tuple[str, str]]:
        if media_type == "variety":
            seen: set[tuple[str, str]] = set()
            kept: list[tuple[str, str]] = []
            for path, name in sorted(videos, key=lambda x: x[1]):
                normalized = re.sub(r"\(\d+\)(?=\.[^.]+$)", "", name)
                key = (path, normalized)
                if key in seen:
                    continue
                seen.add(key)
                kept.append((path, name))
            return kept

        best: dict[Any, tuple[str, str, int]] = {}
        for path, name in videos:
            key = self.episode_key(name)
            score = self.quality_score(name)
            if key not in best or score > best[key][2]:
                best[key] = (path, name, score)
        return [(path, name) for path, name, _ in best.values()]

    @staticmethod
    def episode_key(name: str) -> Any:
        match = re.search(r"S0?(\d{1,3})\s*E0?(\d{1,4})", name, re.I)
        if match:
            return ("season_episode", int(match.group(1)), int(match.group(2)))
        match = re.search(r"(?:^|[^\w])E0?(\d{1,4})(?:[^\w]|$)", name, re.I) or re.match(r"0?(\d{1,4})(?:\D|$)", name)
        return ("episode", int(match.group(1))) if match else ("file", name)

    @staticmethod
    def quality_score(name: str) -> int:
        lower = name.lower()
        if "4k" in lower or "2160p" in lower:
            return 40
        if "1080p" in lower or "bluray" in lower or "蓝光" in lower:
            return 30
        if "720p" in lower:
            return 20
        return 10

    def decide_tracking(self, title: str, media_type: str, current: int, source_path: str) -> dict[str, Any]:
        proc = run(
            [
                "python3",
                str(DECIDER),
                "--title",
                title,
                "--media-type",
                media_type,
                "--current",
                str(current),
                "--source-path",
                source_path,
            ],
            timeout=90,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {
                "title": title,
                "media_type": media_type,
                "current_episodes": current,
                "total_episodes": None,
                "status": "needs_total",
                "track_reason": "未能从互联网确认总集数，暂不自动纳入追剧",
                "confidence": 0.0,
                "evidence": "decision-failed",
                "origin": "none",
            }
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return {
                "title": title,
                "media_type": media_type,
                "current_episodes": current,
                "total_episodes": None,
                "status": "needs_total",
                "track_reason": "总集数判定返回不可解析结果，暂不自动纳入追剧",
                "confidence": 0.0,
                "evidence": "decision-json-error",
                "origin": "none",
            }

    def scan_recovery(self, entry: dict[str, Any], total: int) -> tuple[int, str]:
        proc = run(
            [
                "python3",
                str(RECOVERY),
                "scan",
                "--title",
                str(entry.get("name") or ""),
                "--media-type",
                str(entry.get("media_type") or "tv"),
                "--old-source",
                str(entry.get("source_path") or ""),
                "--total",
                str(total),
            ],
            timeout=240,
        )
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        match = re.search(r"找到 (\d+) 个可切换候选", output)
        count = int(match.group(1)) if match else 0
        return count, output

    def collapse_duplicate_items(self, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        kept: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []
        for item in sorted(
            items,
            key=lambda row: (
                str(row.get("media_type") or ""),
                normalize_series_name(row.get("name")),
                -as_int(row.get("current_episodes")),
                -as_int(row.get("strm_files")),
                str(row.get("name") or ""),
            ),
        ):
            series_key = normalize_series_name(item.get("name"))
            matched_index: int | None = None
            for idx, existing in enumerate(kept):
                if str(existing.get("media_type") or "") != str(item.get("media_type") or ""):
                    continue
                if normalize_series_name(existing.get("name")) != series_key:
                    continue
                if not same_source_family(existing, item):
                    continue
                matched_index = idx
                break
            if matched_index is None:
                kept.append(item)
                continue
            existing = kept[matched_index]
            existing_score = (
                as_int(existing.get("current_episodes")),
                as_int(existing.get("strm_files")),
                -len(str(existing.get("name") or "")),
            )
            item_score = (
                as_int(item.get("current_episodes")),
                as_int(item.get("strm_files")),
                -len(str(item.get("name") or "")),
            )
            if item_score > existing_score:
                kept[matched_index] = item
                duplicate = dict(existing)
                duplicate["kept_name"] = item.get("name")
                duplicate["reason"] = "duplicate_strm_source"
                duplicates.append(duplicate)
            else:
                duplicate = dict(item)
                duplicate["kept_name"] = existing.get("name")
                duplicate["reason"] = "duplicate_strm_source"
                duplicates.append(duplicate)
        kept.sort(key=lambda row: (str(row.get("media_type") or ""), str(row.get("name") or "")))
        duplicates.sort(key=lambda row: (str(row.get("kept_name") or ""), str(row.get("name") or "")))
        return kept, duplicates

    def build_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        previous_doc = load_json(STATE_FILE, {"dramas": []})
        previous_entries = {
            normalize_name(item.get("name")): item
            for item in previous_doc.get("dramas", [])
            if isinstance(item, dict) and normalize_name(item.get("name"))
        }
        scanned_items_raw = self.scan_library()
        scanned_items, duplicate_items = self.collapse_duplicate_items(scanned_items_raw)

        active: list[dict[str, Any]] = []
        archive_blocks = archived_block_keys()
        summary = {
            "generated_at": self.now_iso,
            "scanned_total": 0,
            "scanned_raw_total": len(scanned_items_raw),
            "duplicate_sources": [],
            "active_total": 0,
            "completed_missing": [],
            "unknown_total": [],
            "manual_source_selection": [],
            "completed_full": 0,
        }
        for duplicate in duplicate_items:
            summary["duplicate_sources"].append(
                {
                    "name": duplicate.get("name"),
                    "kept_name": duplicate.get("kept_name"),
                    "media_type": duplicate.get("media_type"),
                    "current_episodes": as_int(duplicate.get("current_episodes")),
                    "strm_path": duplicate.get("strm_path"),
                    "source_path": duplicate.get("source_path"),
                    "reason": duplicate.get("reason") or "duplicate_strm_source",
                }
            )

        for idx, item in enumerate(scanned_items, 1):
            summary["scanned_total"] += 1
            name = str(item.get("name") or "").strip()
            print(f"[{idx}/{len(scanned_items)}] {name}", file=sys.stderr, flush=True)
            media_type = str(item.get("media_type") or "tv")
            current = as_int(item.get("current_episodes"))
            key = normalize_name(name)
            old = previous_entries.get(key)
            source_path = str(item.get("source_path") or (old.get("source_path") if old else "")).strip()
            if (key, "completed") in archive_blocks or (key, "inactive") in archive_blocks:
                summary["completed_full"] += 1
                continue
            decision = self.decide_tracking(name, media_type, current, source_path)
            total = as_int(decision.get("total_episodes"))
            source_status, source_videos = self.list_source_videos(source_path, media_type)
            source_entries = len(source_videos)
            source_valid = source_status == "ok" and source_entries > 0
            last_progress_at = choose_progress_time(old, current, self.now_iso)
            stale_days = days_since(self.now_dt, last_progress_at)

            base_entry = {
                "name": name,
                "title": name,
                "media_type": media_type,
                "current_episodes": current,
                "source_entries": source_entries,
                "source_path": source_path,
                "strm_path": str(item.get("strm_path") or ""),
                "last_check": self.now_iso,
                "added_date": str((old or {}).get("added_date") or self.now_iso),
                "last_progress_at": last_progress_at,
                "status": "ongoing",
                "track_reason": str(decision.get("track_reason") or ""),
            }
            if total > 0:
                base_entry["total_episodes"] = total
            if decision.get("evidence"):
                base_entry["total_evidence"] = decision.get("evidence")
            if source_valid:
                base_entry["last_source_ok_at"] = self.now_iso
            elif old and old.get("last_source_ok_at"):
                base_entry["last_source_ok_at"] = old.get("last_source_ok_at")

            status = str(decision.get("status") or "needs_total")
            if status == "ongoing" and total > 0 and current > total:
                # 多数是合集/多季/额外文件导致本地文件数大于分季总集数；不能因此继续放入“正在更新”。
                status = "completed"
                base_entry["track_reason"] = f"集数完整({current}/{total})，本地多出的文件按额外文件/SP处理"
            if status == "ongoing":
                if not source_valid:
                    should_scan = (
                        not old
                        or stale_days >= STALE_SOURCE_CHECK_DAYS
                        or not old.get("recovery_pending")
                    )
                    recovery_count = as_int((old or {}).get("recovery_candidates"))
                    if should_scan and self.enable_recovery_scan:
                        recovery_count, _output = self.scan_recovery(base_entry, total)
                        base_entry["recovery_scanned_at"] = self.now_iso
                    elif old and old.get("recovery_scanned_at"):
                        base_entry["recovery_scanned_at"] = old.get("recovery_scanned_at")
                    base_entry["recovery_pending"] = True
                    base_entry["recovery_candidates"] = recovery_count
                    if stale_days >= STALE_SOURCE_CHECK_DAYS:
                        base_entry["track_reason"] = f"超过{stale_days}天未更新且源目录失效，待你选新源"
                    else:
                        base_entry["track_reason"] = "源目录失效，待你选新源"
                    summary["manual_source_selection"].append(
                        {
                            "name": name,
                            "media_type": media_type,
                            "current_episodes": current,
                            "total_episodes": total,
                            "source_path": source_path,
                            "recovery_candidates": recovery_count,
                            "days_since_progress": stale_days,
                            "reason": base_entry["track_reason"],
                        }
                    )
                elif stale_days >= STALE_SOURCE_CHECK_DAYS and total > 0:
                    base_entry["track_reason"] = f"超过{stale_days}天未更新，已核验源目录有效 ({current}/{total}集)"
                    base_entry.pop("recovery_pending", None)
                    base_entry.pop("recovery_candidates", None)
                active.append(base_entry)
                continue

            if status in {"completed", "needs_recovery"} and total > 0 and current < total:
                summary["completed_missing"].append(
                    {
                        "name": name,
                        "media_type": media_type,
                        "current_episodes": current,
                        "total_episodes": total,
                        "missing_episodes": max(0, total - current),
                        "source_path": source_path,
                        "reason": str(decision.get("track_reason") or ""),
                    }
                )
                continue

            if status == "completed":
                summary["completed_full"] += 1
                continue

            summary["unknown_total"].append(
                {
                    "name": name,
                    "media_type": media_type,
                    "current_episodes": current,
                    "source_path": source_path,
                    "reason": str(decision.get("track_reason") or "未确认总集数"),
                }
            )

        active.sort(key=lambda item: (media_type_label(item.get("media_type")), str(item.get("name") or "")))
        summary["active_total"] = len(active)
        summary["completed_missing"].sort(key=lambda item: (-as_int(item.get("missing_episodes")), item.get("name") or ""))
        summary["manual_source_selection"].sort(key=lambda item: (-as_int(item.get("recovery_candidates")), item.get("name") or ""))
        summary["unknown_total"].sort(key=lambda item: item.get("name") or "")
        return {"dramas": active}, summary


def build_output(summary: dict[str, Any]) -> list[str]:
    lines = [
        f"本地库扫描完成：共 {as_int(summary.get('scanned_total'))} 部候选，纳入追剧 {as_int(summary.get('active_total'))} 部。",
    ]
    missing = summary.get("completed_missing") or []
    if missing:
        lines.append(f"已完结但缺集（不追剧）：{len(missing)} 部")
        for item in missing[:20]:
            lines.append(
                f"- {item.get('name')}（{media_type_label(item.get('media_type'))}）："
                f"本地 {as_int(item.get('current_episodes'))}/{as_int(item.get('total_episodes'))}，"
                f"缺 {as_int(item.get('missing_episodes'))} 集"
            )
        if len(missing) > 20:
            lines.append(f"- 其余 {len(missing) - 20} 部已写入摘要文件")
    pending = summary.get("manual_source_selection") or []
    if pending:
        lines.append(f"待你选新源：{len(pending)} 部")
        for item in pending[:20]:
            lines.append(
                f"- {item.get('name')}：{item.get('reason')}，候选 {as_int(item.get('recovery_candidates'))} 个"
            )
    unresolved = summary.get("unknown_total") or []
    if unresolved:
        lines.append(f"总集数未确认，暂未自动纳入追剧：{len(unresolved)} 部")
        for item in unresolved[:10]:
            lines.append(f"- {item.get('name')}：{item.get('reason')}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="refresh drama tracking from local library")
    parser.add_argument("--title", action="append", default=[], help="only scan one title; repeatable")
    parser.add_argument("--dry-run", action="store_true", help="do not overwrite local drama-state mirror")
    args = parser.parse_args()

    try:
        sync("pull")
        refresher = LibraryRefresher(title_filters=args.title, enable_recovery_scan=not args.dry_run)
        doc, summary = refresher.build_state()
        write_json(SUMMARY_FILE, summary)
        if not args.dry_run:
            write_json(STATE_FILE, doc)
        for line in build_output(summary):
            print(line)
        return 0
    except Exception as exc:
        print(f"本地库追剧重建失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
