#!/usr/bin/env python3
"""Synchronize auto-media-download drama state between Hermes and NAS.

Canonical format is the list form:
    {"dramas": [{...}]}

The NAS legacy dict file is still written for older tools:
    {"剧名": {...}}

Runtime source-of-truth rule:
    /vol1/1000/docker/xiaoya/data/drama-state.json is authoritative.
    Hermes local data/drama-state.json is only a mirror/cache.
    /vol1/1000/docker/xiaoya/drama-state.json is write-only compatibility output.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SKILL_DIR / "config" / "media-config.json"
LOCAL_STATE = SKILL_DIR / "data" / "drama-state.json"
NAS_CANONICAL = "/vol1/1000/docker/xiaoya/data/drama-state.json"
NAS_LEGACY = "/vol1/1000/docker/xiaoya/drama-state.json"
NAS_SSH = Path.home() / ".hermes" / "scripts" / "nas_ssh.py"


def load_config() -> dict[str, Any]:
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def nas_target() -> tuple[str, str]:
    cfg = load_config()
    nas = cfg.get("nas", {})
    host = str(nas.get("ssh_host") or "fn-nas")
    user = str(nas.get("user") or "YOUR_USERNAME")
    return user, host


def _is_local_nas() -> bool:
    """Check if we're running on the NAS itself (local file access available)."""
    # Fast path: check if the canonical NAS path exists locally
    if Path(NAS_CANONICAL).exists():
        return True
    # Fallback: check if any NAS host IP matches local interfaces
    cfg = load_config()
    nas_host = str(cfg.get("nas", {}).get("tailscale_ip") or cfg.get("nas", {}).get("host") or "")
    if not nas_host:
        return False
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        local_ips = result.stdout.strip().split()
        return nas_host in local_ips
    except Exception:
        return False


def run(cmd: list[str], *, input_text: str | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
        check=check,
        timeout=25,
    )


def nas_ssh(command: str, *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    if NAS_SSH.exists():
        return run([sys.executable, str(NAS_SSH), command, "--timeout", "25"], input_text=input_text)
    user, host = nas_target()
    return run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectionAttempts=1",
            "-o", "ConnectTimeout=6",
            "-o", "ServerAliveInterval=3",
            "-o", "ServerAliveCountMax=1",
            "-o", "StrictHostKeyChecking=no",
            f"{user}@{host}",
            command,
        ],
        input_text=input_text,
    )


def ssh_read(path: str) -> Any | None:
    """Read JSON from path. Uses local file if running on NAS, otherwise SSH."""
    if _is_local_nas():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    proc = nas_ssh(f"cat {shq(path)} 2>/dev/null")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def ssh_write(path: str, data: Any) -> None:
    """Write JSON to path. Uses local file if running on NAS, otherwise SSH."""
    if _is_local_nas():
        remote_dir = os.path.dirname(path)
        os.makedirs(remote_dir, exist_ok=True)
        remote_tmp = f"{path}.tmp.{os.getpid()}"
        with open(remote_tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(remote_tmp, path)
        return
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    remote_dir = os.path.dirname(path)
    remote_tmp = f"{path}.tmp.{os.getpid()}"
    script = (
        f"mkdir -p {shq(remote_dir)} && "
        f"cat > {shq(remote_tmp)} && "
        f"mv {shq(remote_tmp)} {shq(path)}"
    )
    proc = nas_ssh(script, input_text=payload)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"failed to write {path}")


def shq(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def load_local(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def strip_backup_suffix(value: Any) -> str:
    return re.sub(r"(?:[._\-])?bak(?:cup)?[-_]?\d{6,14}$", "", str(value or "").strip(), flags=re.I)


def is_backup_name(value: Any) -> bool:
    return bool(re.search(r"(?:[._\-])?bak(?:up)?[-_]?\d{6,14}$", str(value or "").strip(), re.I))


def normalize_name(name: Any) -> str:
    text = strip_backup_suffix(name)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[《》【】\[\]（）()·._\-:：]", "", text)
    return text.casefold()


def parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text[:10])
        except ValueError:
            return None


def score(entry: dict[str, Any]) -> tuple[int, int, int, int, int, int, str]:
    """Score entry for merge priority. Higher tuple wins.
    
    Priority order:
    1. has_evidence (100): entry has total_evidence field → most trustworthy
    2. is_completed_valid (80): completed with current >= total → definitive
    3. has_valid_total (50): total_episodes is reasonable (not 999 placeholder)
    4. ongoing (1): still tracking (lower than evidence-based scores)
    5. current: number of episodes (tie-breaker)
    6. ts: last_check timestamp (tie-breaker)
    """
    last = parse_time(entry.get("last_check") or entry.get("updated_at") or entry.get("added_date"))
    ts = int(last.timestamp()) if last else 0
    current = as_int(entry.get("current_episodes"))
    total = as_int(entry.get("total_episodes"))
    ongoing = 1 if entry.get("status") == "ongoing" else 0
    
    # Evidence-based priority scores
    has_evidence = 100 if entry.get("total_evidence") else 0
    has_valid_total = 50 if (total > 0 and total < 500) else 0  # exclude 999 placeholder
    is_completed_valid = 80 if (entry.get("status") == "completed" and current >= total and total > 0) else 0
    
    return (has_evidence, is_completed_valid, has_valid_total, ongoing, current, ts, json.dumps(entry, ensure_ascii=False, sort_keys=True))


def as_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def list_entries(data: Any) -> list[dict[str, Any]]:
    if not data:
        return []
    if isinstance(data, dict) and isinstance(data.get("dramas"), list):
        return [dict(x) for x in data["dramas"] if isinstance(x, dict)]
    if isinstance(data, dict):
        entries: list[dict[str, Any]] = []
        for key, value in data.items():
            if isinstance(value, dict):
                item = dict(value)
                item.setdefault("name", key)
                entries.append(item)
        return entries
    return []


def valid_state_doc(data: Any) -> bool:
    if isinstance(data, dict) and isinstance(data.get("dramas"), list):
        return True
    if isinstance(data, dict) and all(isinstance(v, dict) for v in data.values()):
        return True
    return False


def canonical_entries(nas_list: Any, nas_legacy: Any, local: Any) -> tuple[str, list[dict[str, Any]]]:
    """Read from exactly one source, in authority order, to avoid stale merges."""
    candidates = (
        ("nas_canonical", nas_list),
        ("nas_legacy_fallback", nas_legacy),
        ("local_fallback", local),
    )
    for label, data in candidates:
        if valid_state_doc(data):
            return label, [clean_entry(e) for e in list_entries(data)]
    return "empty", []


def require_local_entries(local: Any, mode: str) -> list[dict[str, Any]]:
    if not valid_state_doc(local):
        raise RuntimeError(f"{mode} refused: local drama-state.json is missing or invalid")
    return [clean_entry(e) for e in list_entries(local)]


def fingerprint(data: Any) -> str:
    entries = [clean_entry(e) for e in list_entries(data)]
    payload = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def merge_entries(*sources: Any) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    aliases: dict[str, str] = {}
    for data in sources:
        for raw in list_entries(data):
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            key = normalize_name(name)
            if not key:
                continue
            entry = clean_entry(raw)
            if key not in merged:
                merged[key] = entry
                aliases[key] = name
                continue
            old = merged[key]
            winner, loser = (entry, old) if score(entry) >= score(old) else (old, entry)
            combined = dict(loser)
            combined.update({k: v for k, v in winner.items() if v not in (None, "", [])})
            combined["name"] = winner.get("name") or aliases[key]
            merged[key] = clean_entry(combined)

    return sorted(merged.values(), key=lambda x: (x.get("status") != "ongoing", normalize_name(x.get("name"))))


def clean_entry(entry: dict[str, Any]) -> dict[str, Any]:
    out = dict(entry)
    canonical_name = str(out.get("name") or out.get("title") or "").strip()
    out["name"] = canonical_name
    if canonical_name:
        out["title"] = canonical_name
    else:
        out.pop("title", None)
    if "current_episodes" in out:
        out["current_episodes"] = as_int(out.get("current_episodes"))
    if "total_episodes" in out:
        total = as_int(out.get("total_episodes"))
        if total > 0:
            out["total_episodes"] = total
        else:
            out.pop("total_episodes", None)
    out.setdefault("status", "ongoing")
    if out.get("source_path") and isinstance(out["source_path"], str):
        out["source_path"] = out["source_path"].strip()
    return out


def to_list_doc(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"dramas": entries}


def to_legacy_doc(entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {str(e["name"]): e for e in entries if e.get("name")}


def write_local(doc: dict[str, Any]) -> None:
    LOCAL_STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="drama-state.", suffix=".json", dir=str(LOCAL_STATE.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    shutil.move(tmp_name, LOCAL_STATE)


def split_active(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active: list[dict[str, Any]] = []
    inactive: list[dict[str, Any]] = []
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    for entry in entries:
        if entry.get("status") == "ongoing":
            active.append(entry)
            continue
        archived = dict(entry)
        archived["status"] = "archived"
        archived.setdefault("archived_at", now)
        archived.setdefault("archived_reason", "inactive")
        archived.setdefault("track_reason", "非追剧状态，移出 active 列表")
        archived["last_check"] = archived.get("last_check") or now
        inactive.append(archived)
    return active, inactive


def archive_inactive(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    archive_file = SKILL_DIR / "data" / f"drama-archive-{datetime.now().strftime('%Y%m%d')}.json"
    archive_file.parent.mkdir(parents=True, exist_ok=True)
    current = load_local(archive_file) or {"archived": []}
    archived = current.setdefault("archived", [])
    known = {(normalize_name(x.get("name")), x.get("archived_reason")) for x in archived if isinstance(x, dict)}
    for entry in entries:
        key = (normalize_name(entry.get("name")), entry.get("archived_reason"))
        if key not in known:
            archived.append(entry)
            known.add(key)
    fd, tmp_name = tempfile.mkstemp(prefix="drama-archive.", suffix=".json", dir=str(archive_file.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
        f.write("\n")
    shutil.move(tmp_name, archive_file)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "merge"
    local = load_local(LOCAL_STATE)
    nas_list = ssh_read(NAS_CANONICAL)
    nas_legacy = ssh_read(NAS_LEGACY)

    if mode in {"pull", "merge"}:
        source, entries = canonical_entries(nas_list, nas_legacy, local)
        if source != "nas_canonical":
            print(f"warning: using {source}; NAS canonical unavailable or invalid", file=sys.stderr)
    elif mode == "push":
        entries = require_local_entries(local, mode)
    elif mode == "replace":
        entries = require_local_entries(local, mode)
    elif mode == "status":
        source, _ = canonical_entries(nas_list, nas_legacy, local)
        print(f"source_of_truth: nas_canonical ({NAS_CANONICAL})")
        print(f"read_source_now: {source}")
        for label, role, data in (
            ("nas_canonical", "AUTHORITATIVE", nas_list),
            ("local", "mirror/cache", local),
            ("nas_legacy", "compatibility output", nas_legacy),
        ):
            if data is None:
                print(f"{label}: unavailable ({role})")
                continue
            entries = list_entries(data)
            print(f"{label}: {len(entries)} entries sha={fingerprint(data)} ({role})")
            for e in entries:
                print(f"  {e.get('status','?'):9} {e.get('name')} {e.get('current_episodes','?')}/{e.get('total_episodes','?')}")
        return 0
    else:
        print(f"usage: {Path(sys.argv[0]).name} [pull|push|replace|merge|status]", file=sys.stderr)
        return 2

    entries, inactive = split_active(entries)
    archive_inactive(inactive)
    doc = to_list_doc(entries)
    write_local(doc)
    ssh_write(NAS_CANONICAL, doc)
    ssh_write(NAS_LEGACY, to_legacy_doc(entries))
    ongoing = sum(1 for e in entries if e.get("status") == "ongoing")
    print(f"synced drama state: {len(entries)} total, {ongoing} ongoing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
