#!/usr/bin/env python3
"""Build a unified media selection map for displayed search results.

The assistant may show a mixed list: Xiaoya/AList items first, then verified
PanSou items sorted by probe quality.  Raw script files use different numbering
schemes, so this file is the single source of truth for "the number the user
sees".
"""

from __future__ import annotations

import json
from pathlib import Path


RESOURCES_FILE = Path("/tmp/media_resources.txt")
PROBE_FILE = Path("/tmp/probe_results.json")
HYPERMS_FILE = Path("/tmp/hyperms_selection_map.json")
MAP_FILE = Path("/tmp/media_selection_map.json")


def _read_probe_rows() -> list[dict]:
    if not PROBE_FILE.exists():
        return []
    try:
        rows = json.loads(PROBE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    usable = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            count = int(row.get("count") or 0)
        except Exception:
            count = 0
        if count <= 0:
            continue
        usable.append(row)
    return usable


def _xiaoya_entries() -> list[dict]:
    entries: list[dict] = []
    if not RESOURCES_FILE.exists():
        return entries
    try:
        lines = RESOURCES_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return entries
    for line_no, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("PANSOU\t"):
            continue
        parts = line.split("\t")
        path = parts[0].strip() if parts else ""
        title = parts[1].strip() if len(parts) > 1 else Path(path).name
        if not path:
            continue
        entries.append(
            {
                "kind": "xiaoya",
                "resource_index": line_no,
                "title": title or path,
                "path": path,
            }
        )
    return entries


def _pansou_entries() -> list[dict]:
    entries: list[dict] = []
    for probe_index, row in enumerate(_read_probe_rows(), 1):
        entries.append(
            {
                "kind": "pansou",
                "probe_index": probe_index,
                "title": str(row.get("note") or row.get("url") or "").strip(),
                "url": str(row.get("url") or "").strip(),
                "count": int(row.get("count") or 0),
                "size_gb": row.get("size_gb"),
                "type": row.get("type"),
                "folder_path": row.get("folder_path"),
                "video_path": row.get("video_path"),
                "playable_sample": row.get("playable_sample"),
            }
        )
    return entries


def _hyperms_entries() -> list[dict]:
    entries: list[dict] = []
    if not HYPERMS_FILE.exists():
        return entries
    try:
        rows = json.loads(HYPERMS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return entries
    if not isinstance(rows, list):
        return entries
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            hyperms_index = int(row.get("display_index") or 0)
        except Exception:
            hyperms_index = 0
        if hyperms_index <= 0:
            continue
        cloud = str(row.get("cloud_type") or "").strip().lower()
        route = str(row.get("route") or ("uc_gbox" if cloud == "uc" else "hyperms")).strip().lower()
        entries.append(
            {
                "kind": "hyperms",
                "hyperms_index": hyperms_index,
                "title": str(row.get("title") or row.get("group_name") or row.get("keyword") or "").strip(),
                "keyword": str(row.get("keyword") or "").strip(),
                "media_type": row.get("media_type"),
                "cloud_type": cloud,
                "route": route,
                "size_text": row.get("size_text"),
                "browse_count": row.get("browse_count"),
                "browse_message": row.get("browse_message"),
                "uc_count": row.get("uc_count"),
                "uc_size_gb": row.get("uc_size_gb"),
                "uc_video_path": row.get("uc_video_path"),
                "uc_folder_path": row.get("uc_folder_path"),
            }
        )
    return entries


def build_map() -> list[dict]:
    hyperms_entries = _hyperms_entries()
    # HyperMS UC pre-probing intentionally writes /tmp/probe_results.json for
    # details. In that case the probe file is internal metadata, not a separate
    # PanSou candidate list to expose before the HyperMS rows.
    pansou_entries = [] if hyperms_entries else _pansou_entries()
    entries = _xiaoya_entries() + pansou_entries + hyperms_entries
    for display_index, entry in enumerate(entries, 1):
        entry["display_index"] = display_index
    return entries


def main() -> int:
    entries = build_map()
    MAP_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    xiaoya = sum(1 for entry in entries if entry.get("kind") == "xiaoya")
    pansou = sum(1 for entry in entries if entry.get("kind") == "pansou")
    hyperms = sum(1 for entry in entries if entry.get("kind") == "hyperms")
    print(f"✅ 统一选择映射已保存: {MAP_FILE} ({len(entries)} 项: xiaoya={xiaoya}, pansou={pansou}, hyperms={hyperms})")
    if entries:
        print("   用户回复序号后，统一执行: bash scripts/complete-media-selection.sh <序号>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
