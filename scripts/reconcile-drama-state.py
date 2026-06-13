#!/usr/bin/env python3
"""Reconcile active drama tracking with NAS STRM files and source validity."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from strm_layout import resolve_strm_dir

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SKILL_DIR / "config" / "media-config.json"
STATE_FILE = SKILL_DIR / "data" / "drama-state.json"
SUMMARY_FILE = SKILL_DIR / "data" / "library-tracking-refresh-summary.json"
SYNC_SCRIPT = SCRIPT_DIR / "sync-drama-state.py"
GEN_STRM = SCRIPT_DIR / "gen-strm.py"
DECIDER = SCRIPT_DIR / "decide-tracking.py"
RECOVERY = SCRIPT_DIR / "share-source-recovery.py"
STRM_LAYOUT = SCRIPT_DIR / "strm_layout.py"
NAS_GEN_STRM = "/vol1/1000/docker/xiaoya/scripts/gen-strm.py"
NAS_STRM_LAYOUT = "/vol1/1000/docker/xiaoya/scripts/strm_layout.py"
NAS_CANONICAL_PATH = "/vol1/1000/docker/xiaoya/data/drama-state.json"
VIDEO_EXTS = (".mp4", ".mkv", ".avi", ".ts", ".flv", ".wmv", ".mov", ".iso")
SHARE_MARKERS = ("我的UC分享", "我的夸克分享", "我的115分享")
STALE_SOURCE_CHECK_DAYS = 3


def _is_local_nas() -> bool:
    """Check if we're running on the NAS itself."""
    if Path(NAS_CANONICAL_PATH).exists():
        return True
    return False


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(path)


def run(cmd: list[str], *, input_text: str | None = None, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=input_text, text=True, capture_output=True, timeout=timeout)


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


def q(value: str) -> str:
    return shlex.quote(value)


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
    """Canonical key for series-level summary dedupe.

    Completed-missing summaries are series-level diagnostics.  Duplicate STRM
    folders such as "康熙微服私访记" and "康熙微服私访记1" should not appear as two
    separate completed-missing rows when they are the same series aggregate.
    Strip a trailing standalone Arabic number only for this summary key.
    """
    key = normalize_name(value)
    return re.sub(r"\d+$", "", key)


def category_for(media_type: str) -> str:
    if media_type == "anime":
        return "动漫"
    if media_type == "movie":
        return "电影"
    if media_type == "variety":
        return "综艺"
    if media_type == "documentary":
        return "纪录片"
    return "电视剧"


def is_share_source(path: str) -> bool:
    return any(marker in (path or "") for marker in SHARE_MARKERS)


def episode_number(name: Any) -> int:
    text = str(name or "")
    patterns = [
        r"S0?\d{1,3}\s*E0?(\d{1,4})",
        r"(?:^|[^\w])E0?(\d{1,4})(?:[^\w]|$)",
        r"(?:第\s*)?(\d{1,4})\s*(?:集|话|期)",
        r"^0?(\d{1,4})(?:\D|$)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return 0
    return 0


class Reconciler:
    def __init__(self) -> None:
        self.config = load_json(CONFIG_FILE, {})
        nas = self.config.get("nas", {})
        source = (self.config.get("sources") or [{}])[0]
        storage = self.config.get("storage", {})
        self.nas_host = str(nas.get("tailscale_ip") or nas.get("host") or "")
        self.nas_user = str(nas.get("user") or "")
        self.alist_url = str(source.get("internal_url") or "http://127.0.0.1:5678").rstrip("/")
        self.strm_base = str(storage.get("base_dir") or nas.get("strm_base_dir") or "/vol1/1000/docker/xiaoya/strm/C-每日更新")
        self.strm_prefix = str(nas.get("strm_url_prefix") or "")
        self.now = datetime.now().astimezone().isoformat(timespec="seconds")

    def _local_exec(self, cmd_str: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        """Run a command locally (when on NAS) instead of via SSH."""
        return run(["sh", "-c", cmd_str], timeout=timeout)

    def ssh(self, script: str, *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        if _is_local_nas():
            return self._local_exec(script, timeout=timeout)
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

    def scp_gen_strm(self) -> None:
        if _is_local_nas():
            import shutil as _shutil
            Path(NAS_GEN_STRM).parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy2(str(GEN_STRM), NAS_GEN_STRM)
            _shutil.copy2(str(STRM_LAYOUT), NAS_STRM_LAYOUT)
            return
        mkdir_proc = self.ssh(f"mkdir -p {q(str(Path(NAS_GEN_STRM).parent))}", timeout=30)
        if mkdir_proc.returncode != 0:
            raise RuntimeError((mkdir_proc.stderr or mkdir_proc.stdout or "mkdir remote script dir failed").strip())
        for src, dst in ((GEN_STRM, NAS_GEN_STRM), (STRM_LAYOUT, NAS_STRM_LAYOUT)):
            proc = run(
                [
                    "scp",
                    "-o",
                    "StrictHostKeyChecking=no",
                    "-o",
                    "ConnectTimeout=15",
                    str(src),
                    f"{self.nas_user}@{self.nas_host}:{dst}",
                ],
                timeout=45,
            )
            if proc.returncode != 0:
                raise RuntimeError((proc.stderr or proc.stdout or f"scp {src.name} failed").strip())

    def strm_dir(self, drama: dict[str, Any]) -> str:
        return resolve_strm_dir(
            str(drama.get("name") or ""),
            str(drama.get("media_type") or "tv"),
            config=self.config,
            custom_path=str(drama.get("strm_path") or ""),
        )

    def local_strm_stats(self, drama: dict[str, Any]) -> tuple[int, int]:
        """Return (unique_episode_count, max_episode_number) for local STRM.

        For ongoing dramas with gaps (e.g. 1-28,31-36), the user-facing
        progress should be the highest available episode number (36), while
        source_entries keeps the unique file count (34). Counting only len(keys)
        made reports show 34 even when episodes 35/36 already existed.
        """
        path = self.strm_dir(drama)
        py = r"""
import os, re, sys
root = sys.argv[1]
keys = set()
episodes = set()
def episode_key(name):
    m = re.search(r'S0?(\d{1,3})\s*E0?(\d{1,4})', name, re.I)
    if m:
        ep = int(m.group(2))
        episodes.add(ep)
        return ('season_episode', int(m.group(1)), ep)
    m = re.search(r'(?:^|[^\w])E0?(\d{1,4})(?:[^\w]|$)', name, re.I) or re.match(r'0?(\d{1,4})(?:\D|$)', name)
    if m:
        ep = int(m.group(1))
        episodes.add(ep)
        return ('episode', ep)
    return ('file', name)
if os.path.isdir(root):
    for base, _, files in os.walk(root):
        for fn in files:
            if not fn.endswith('.strm'):
                continue
            full = os.path.join(base, fn)
            if not os.path.getsize(full):
                continue
            keys.add(episode_key(fn))
print(f"{len(keys)} {max(episodes) if episodes else 0}")
"""
        script = f"python3 - {q(path)} <<'PY'\n{py}\nPY"
        proc = self.ssh(script, timeout=30)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"failed to count {path}").strip())
        parts = (proc.stdout or "").strip().split()
        try:
            count = int(parts[0]) if parts else 0
            max_ep = int(parts[1]) if len(parts) > 1 else count
            return count, max_ep
        except ValueError:
            return 0, 0

    def count_local_strm(self, drama: dict[str, Any]) -> int:
        return self.local_strm_stats(drama)[0]

    def source_path(self, drama: dict[str, Any]) -> str:
        path = str(drama.get("source_path") or "").strip()
        if path and not path.startswith("/"):
            path = "/" + path
        return path

    def list_source_videos(self, path: str, depth: int = 0) -> tuple[str, list[tuple[str, str]]]:
        if not path:
            return "missing_path", []
        if path.lower().endswith(VIDEO_EXTS):
            return "ok", [(str(Path(path).parent), Path(path).name)]
        if depth > 5:
            return "ok", []

        videos: list[tuple[str, str]] = []
        page = 1
        per_page = 500
        while True:
            payload = json.dumps({"path": path, "page": page, "per_page": per_page}).encode()
            req = urllib.request.Request(
                f"{self.alist_url}/api/fs/list",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read())
            except Exception:
                return "api_unreachable", []
            if result.get("code", 200) != 200:
                return "source_missing", []

            data = result.get("data") or {}
            items = data.get("content") or []
            total = int(data.get("total") or 0)
            if not items:
                break
            for item in items:
                name = str(item.get("name") or "")
                child = f"{path.rstrip('/')}/{name}"
                if item.get("is_dir") or item.get("type") == 1:
                    status, child_videos = self.list_source_videos(child, depth + 1)
                    if status == "api_unreachable":
                        return status, []
                    videos.extend(child_videos)
                elif name.lower().endswith(VIDEO_EXTS):
                    videos.append((path, name))
            if page * per_page >= total:
                break
            page += 1
        media_type = self.current_media_type if hasattr(self, "current_media_type") else "tv"
        return "ok", dedupe_videos(videos, media_type)

    def generate_strm(self, drama: dict[str, Any]) -> tuple[int, int, str]:
        source = self.source_path(drama)
        name = str(drama.get("name") or "")
        media_type = str(drama.get("media_type") or "tv")
        script = (
            f"SOURCE_PATH={q(source)} STRM_URL_PREFIX={q(self.strm_prefix)} "
            f"NAME={q(name)} MEDIA_TYPE={q(media_type)} "
            "ALIST_API='http://127.0.0.1:5678/api/fs/list' "
            f"python3 {q(NAS_GEN_STRM)}"
        )
        proc = self.ssh(script, timeout=180)
        output = (proc.stdout or "") + (proc.stderr or "")
        total = parse_marker(output, "STRM_TOTAL")
        added = parse_marker(output, "STRM_COUNT")
        return total, added, output

    def decide_tracking(self, drama: dict[str, Any], current: int) -> dict[str, Any] | None:
        cmd = [
            "python3",
            str(DECIDER),
            "--title",
            str(drama.get("name") or ""),
            "--media-type",
            str(drama.get("media_type") or "tv"),
            "--current",
            str(current),
            "--source-path",
            str(drama.get("source_path") or ""),
        ]
        proc = run(cmd, timeout=90)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None

    def archive_path(self) -> Path:
        return SKILL_DIR / "data" / f"drama-archive-{datetime.now().strftime('%Y%m%d')}.json"

    def prepare_share_recovery(self, drama: dict[str, Any], total: int) -> tuple[int, str]:
        cmd = [
            "python3",
            str(RECOVERY),
            "scan",
            "--title",
            str(drama.get("name") or ""),
            "--media-type",
            str(drama.get("media_type") or "tv"),
            "--old-source",
            str(drama.get("source_path") or ""),
            "--total",
            str(total),
        ]
        proc = run(cmd, timeout=240)
        output = (proc.stdout or "") + (proc.stderr or "")
        match = re.search(r"找到 (\d+) 个可切换候选", output)
        count = int(match.group(1)) if match else 0
        return count, output.strip()

    def auto_apply_recovery(self, title: str, total: int) -> int:
        """尝试自动 apply 第一个满足集数要求的候选。返回 0 表示成功。"""
        cmd = [
            "python3",
            str(RECOVERY),
            "apply",
            "--title",
            title,
            "--selection",
            "1",
        ]
        try:
            proc = run(cmd, timeout=300)
            return proc.returncode
        except Exception:
            return 1

    def archive(self, entries: list[dict[str, Any]]) -> None:
        if not entries:
            return
        path = self.archive_path()
        doc = load_json(path, {"archived": []})
        archived = doc.setdefault("archived", [])
        known = {(normalize_name(x.get("name")), x.get("archived_reason")) for x in archived if isinstance(x, dict)}
        for entry in entries:
            key = (normalize_name(entry.get("name")), entry.get("archived_reason"))
            if key not in known:
                archived.append(entry)
                known.add(key)
        write_json(path, doc)

    def reconcile(self) -> int:
        doc = load_json(STATE_FILE, {"dramas": []})
        if not isinstance(doc, dict) or not isinstance(doc.get("dramas"), list):
            sync("pull")
            doc = load_json(STATE_FILE, {"dramas": []})
        self.scp_gen_strm()

        dramas = [d for d in doc.get("dramas", []) if isinstance(d, dict)]
        active: list[dict[str, Any]] = []
        archived: list[dict[str, Any]] = []
        completed_missing: list[dict[str, Any]] = []
        updates: list[str] = []
        now_dt = parse_iso(self.now) or datetime.now().astimezone()

        for drama in dramas:
            if drama.get("status") != "ongoing":
                archived.append(mark_archived(drama, self.now, "inactive", "非追剧状态，移出 active 列表"))
                continue

            name = str(drama.get("name") or "")
            total = as_int(drama.get("total_episodes"))
            current_before = as_int(drama.get("current_episodes"))
            last_progress_at = str(
                drama.get("last_progress_at")
                or drama.get("added_date")
                or drama.get("last_check")
                or self.now
            )
            stale_days = days_since(now_dt, last_progress_at)
            local_unique_count, local_max_episode = self.local_strm_stats(drama)
            local_count = local_max_episode or local_unique_count
            self.current_media_type = str(drama.get("media_type") or "tv")
            source_status, source_videos = self.list_source_videos(self.source_path(drama))
            source_count = len(source_videos)
            source_max_episode = max((episode_number(fn) for _, fn in source_videos), default=0)
            source_progress = source_max_episode or source_count
            source_valid = source_status == "ok" and source_count > 0

            if local_count == 0:
                archived.append(mark_archived(drama, self.now, "local_strm_missing", "本地 STRM 已删除，视为不再追剧"))
                updates.append(f"REMOVE:{name}:本地 STRM 已删除")
                continue

            if source_progress > local_count:
                local_before = local_count
                gen_total, added, _ = self.generate_strm(drama)
                local_unique_count, local_max_episode = self.local_strm_stats(drama)
                local_count = local_max_episode or local_unique_count
                if local_count > local_before or added > 0:
                    total_s = str(total) if total > 0 else "?"
                    updates.append(f"UPDATE:{name}:{local_before}/{total_s} → {local_count}/{total_s} (+{added})")
                if local_count > current_before:
                    last_progress_at = self.now
                    stale_days = 0
                if gen_total > source_count:
                    source_count = gen_total

            progress_candidates = [x for x in (local_count, source_progress, current_before) if x > 0]
            decision_current = max(progress_candidates) if progress_candidates else max(local_count, current_before, source_progress)
            if total <= 0 or total == 999 or (source_progress > 0 and local_count > source_progress and total <= local_count):
                decision = self.decide_tracking(drama, decision_current)
                if decision and as_int(decision.get("total_episodes")) > 0:
                    total = as_int(decision.get("total_episodes"))
                    drama["total_episodes"] = total
                    drama["total_evidence"] = decision.get("evidence")
                    if decision.get("status") == "completed" and decision_current >= total:
                        local_count = decision_current

            drama["current_episodes"] = local_count
            drama["source_entries"] = source_count
            if local_max_episode and local_unique_count and local_max_episode > local_unique_count:
                drama["source_max_episode"] = source_progress
                drama["missing_episode_count"] = local_max_episode - local_unique_count
            else:
                drama.pop("source_max_episode", None)
                drama.pop("missing_episode_count", None)
            drama["last_check"] = self.now
            drama["last_progress_at"] = last_progress_at

            final_decision = None
            if decision_current > 0:
                final_decision = self.decide_tracking(drama, decision_current)
                if final_decision and as_int(final_decision.get("total_episodes")) > 0:
                    total = as_int(final_decision.get("total_episodes"))
                    drama["total_episodes"] = total
                    drama["total_evidence"] = final_decision.get("evidence")

            if total > 0 and local_count > total:
                if final_decision and final_decision.get("status") == "completed":
                    archived.append(mark_archived(drama, self.now, "completed", f"集数完整({local_count}/{total})，自动移出追剧"))
                    updates.append(f"DONE:{name}:{local_count}/{total} (含额外文件/SP)")
                else:
                    drama.pop("total_episodes", None)
                    drama["track_reason"] = f"总集数疑似错误：本地已有{local_count}集 > 记录{total}集，继续追剧"
                    active.append(drama)
            elif total > 0 and local_count >= total:
                archived.append(mark_archived(drama, self.now, "completed", f"集数完整({local_count}/{total})，自动移出追剧"))
                updates.append(f"DONE:{name}:{local_count}/{total}")
            elif final_decision and final_decision.get("status") == "needs_recovery":
                # 已完结但源不全：移出“正在更新”，只作为缺集提醒/待换源问题保留在摘要或归档中。
                archived.append(mark_archived(
                    drama, self.now, "completed_incomplete_source",
                    f"已完结但源不全({local_count}/{total})，移出正在更新列表"
                ))
                completed_missing.append({
                    "name": name,
                    "media_type": str(drama.get("media_type") or "tv"),
                    "current_episodes": local_count,
                    "total_episodes": total,
                    "missing_episodes": max(0, total - local_count),
                    "source_path": self.source_path(drama),
                    "reason": f"已完结但缺集({local_count}/{total})，不再纳入正在更新",
                })
                updates.append(f"MISSING:{name}:{local_count}/{total} (已完结缺集，不再追剧)")
            elif source_status == "api_unreachable":
                drama["last_check"] = self.now
                drama["track_reason"] = "AList 暂时不可达，已重新核验追剧状态后暂留"
                active.append(drama)
                updates.append(f"WARN:{name}:AList 不可达，已核验后保留追剧")
            elif not source_valid:
                should_scan = stale_days >= STALE_SOURCE_CHECK_DAYS or not drama.get("last_source_ok_at") or bool(drama.get("recovery_pending"))
                recovery_count = as_int(drama.get("recovery_candidates"))
                recovery_output = ""
                last_scan_days = days_since(now_dt, drama.get("recovery_scanned_at"))
                if should_scan and last_scan_days >= 1:
                    recovery_count, recovery_output = self.prepare_share_recovery(drama, total)
                    drama["recovery_scanned_at"] = self.now
                drama["recovery_pending"] = True
                drama["recovery_candidates"] = recovery_count
                if stale_days >= STALE_SOURCE_CHECK_DAYS:
                    drama["track_reason"] = f"超过{stale_days}天未更新且源目录失效，待你选新源"
                    updates.append(f"ACTION:{name}:超过{stale_days}天未更新且源失效，待手动换源")
                elif drama.get("last_source_ok_at"):
                    drama["track_reason"] = f"源目录当前不可用，距上次更新 {stale_days} 天；满 {STALE_SOURCE_CHECK_DAYS} 天后继续验源"
                    updates.append(f"ALERT:{name}:源目录异常，未到{STALE_SOURCE_CHECK_DAYS}天阈值")
                else:
                    drama["track_reason"] = "新增追剧项源目录失效，待你选新源"
                    updates.append(f"ACTION:{name}:新增追剧项源失效，待手动换源")
                active.append(drama)
                if recovery_output:
                    print(recovery_output)
            else:
                drama.pop("recovery_pending", None)
                drama.pop("recovery_candidates", None)
                drama["last_source_ok_at"] = self.now
                if source_count > 0 and local_count > source_count:
                    if total > 0:
                        drama["track_reason"] = f"源目录疑似残缺：本地{local_count}集，源仅{source_count}集（总集数{total}）"
                    else:
                        drama["track_reason"] = f"源目录疑似残缺：本地{local_count}集，源仅{source_count}集"
                elif stale_days >= STALE_SOURCE_CHECK_DAYS and total > 0:
                    drama["track_reason"] = f"超过{stale_days}天未更新，已核验源目录有效 ({local_count}/{total}集)"
                elif total > 0:
                    drama["track_reason"] = f"更新中 ({local_count}/{total}集)"
                else:
                    drama["track_reason"] = f"更新中，当前本地 {local_count} 集，源目录 {source_count} 集"
                active.append(drama)

        doc["dramas"] = active
        write_json(STATE_FILE, doc)
        if completed_missing:
            summary = load_json(SUMMARY_FILE, {})
            if not isinstance(summary, dict):
                summary = {}
            existing = summary.get("completed_missing") if isinstance(summary.get("completed_missing"), list) else []
            merged: dict[str, dict[str, Any]] = {}
            for item in list(existing) + completed_missing:
                if not isinstance(item, dict):
                    continue
                key = normalize_series_name(item.get("name"))
                if not key:
                    continue
                prev = merged.get(key)
                if prev is None:
                    merged[key] = item
                    continue
                # Keep the more complete scan result for duplicate series folders.
                # Example: 康熙微服私访记(114/144) beats 康熙微服私访记1(106/144).
                prev_current = as_int(prev.get("current_episodes"))
                item_current = as_int(item.get("current_episodes"))
                prev_missing = as_int(prev.get("missing_episodes"))
                item_missing = as_int(item.get("missing_episodes"))
                if item_current > prev_current or (item_current == prev_current and item_missing < prev_missing):
                    merged[key] = item
            summary["completed_missing"] = sorted(
                merged.values(),
                key=lambda item: (-as_int(item.get("missing_episodes")), str(item.get("name") or "")),
            )
            summary["generated_at"] = self.now
            summary["active_total"] = len(active)
            write_json(SUMMARY_FILE, summary)
        self.archive(archived)
        sync("replace")

        ongoing = sum(1 for d in active if d.get("status") == "ongoing")
        print(f"追剧校准完成：{ongoing} 部仍在追剧，移出 {len(archived)} 部")
        for line in updates:
            print(line)
        return 0


def parse_marker(output: str, marker: str) -> int:
    match = re.search(rf"==={re.escape(marker)}===(\d+)", output)
    return int(match.group(1)) if match else 0


def as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


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
    if now_dt.tzinfo is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=now_dt.tzinfo)
    elif now_dt.tzinfo is None and dt.tzinfo is not None:
        now_dt = now_dt.replace(tzinfo=dt.tzinfo)
    delta = now_dt - dt
    return max(0, int(delta.total_seconds() // 86400))


def episode_key(name: str) -> Any:
    season_match = re.search(r"S0?(\d{1,3})\s*E0?(\d{1,4})", name, re.I)
    if season_match:
        return ("season_episode", int(season_match.group(1)), int(season_match.group(2)))
    match = re.search(r"(?:^|[^\w])E0?(\d{1,4})(?:[^\w]|$)", name, re.I) or re.match(r"0?(\d{1,4})(?:\D|$)", name)
    return ("episode", int(match.group(1))) if match else ("file", name)


def quality_score(name: str) -> int:
    lower = name.lower()
    if "4k" in lower or "2160p" in lower:
        return 40
    if "1080p" in lower or "bluray" in lower or "蓝光" in lower:
        return 30
    if "720p" in lower:
        return 20
    return 10


def dedupe_videos(videos: list[tuple[str, str]], media_type: str = "tv") -> list[tuple[str, str]]:
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
        key = episode_key(name)
        score = quality_score(name)
        if key not in best or score > best[key][2]:
            best[key] = (path, name, score)
    return [(path, name) for path, name, _ in sorted(best.values(), key=lambda x: str(episode_key(x[1])))]


def mark_archived(drama: dict[str, Any], now: str, reason_code: str, reason: str) -> dict[str, Any]:
    item = dict(drama)
    item["status"] = "archived"
    item["archived_at"] = now
    item["archived_reason"] = reason_code
    item["track_reason"] = reason
    item["last_check"] = now
    return item


def main() -> int:
    try:
        return Reconciler().reconcile()
    except Exception as exc:
        print(f"追剧校准失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
