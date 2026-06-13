#!/usr/bin/env python3
"""Check whether titles already exist in the NAS STRM library."""

from __future__ import annotations

import argparse
import base64
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from strm_layout import get_categories, load_media_config, resolve_strm_dir


SCRIPT_DIR = Path(__file__).resolve().parent
NAS_CANONICAL_PATH = "/vol1/1000/docker/xiaoya/data/drama-state.json"
REMOTE_CHECK = r"""
import json
import os
import re
import sys

payload = json.loads(sys.stdin.read())
sample_limit = max(int(payload.get("sample_limit", 5) or 5), 1)
items = payload.get("items") or []

def episode_key(name):
    season_match = re.search(r"S0?(\d{1,3})\s*E0?(\d{1,4})", name, re.I)
    if season_match:
        return ("season_episode", int(season_match.group(1)), int(season_match.group(2)))
    m = re.search(r"(?:^|[^\w])E0?(\d{1,4})(?:[^\w]|$)", name, re.I) or re.match(r"0?(\d{1,4})(?:\D|$)", name)
    return ("episode", int(m.group(1))) if m else ("file", name)

results = []
for item in items:
    checked = []
    for candidate in item.get("candidate_dirs") or []:
        path = candidate.get("path") or ""
        exists = os.path.isdir(path)
        seen = set()
        sample = []
        if exists:
            for base, _, files in os.walk(path):
                for fn in sorted(files):
                    if not fn.endswith(".strm"):
                        continue
                    full = os.path.join(base, fn)
                    if not os.path.getsize(full):
                        continue
                    rel = os.path.relpath(full, path)
                    key = episode_key(fn)
                    seen.add(key)
                    if len(sample) < sample_limit:
                        sample.append(rel)
        checked.append({
            "media_type": candidate.get("media_type"),
            "path": path,
            "exists": exists,
            "strm_count": len(seen),
            "sample": sample,
        })
    results.append({"title": item.get("title"), "checked": checked})

print(json.dumps(results, ensure_ascii=False))
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether titles already exist in the NAS STRM library.")
    parser.add_argument("--title", action="append", default=[], help="Title to check. Repeat for multiple titles.")
    parser.add_argument("--media-type", default="", help="Common media type for all titles: movie/tv/anime/variety/documentary")
    parser.add_argument("--json-input", default="", help="Inline JSON array or path to a JSON file with [{title, media_type, strm_path}]")
    parser.add_argument("--sample-limit", type=int, default=5, help="How many sample STRM files to return per match")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print the JSON result")
    return parser.parse_args()


def load_items(args: argparse.Namespace) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for title in args.title:
        text = str(title or "").strip()
        if text:
            items.append({"title": text, "media_type": args.media_type.strip()})

    raw = str(args.json_input or "").strip()
    if raw:
        source = Path(raw)
        if source.exists():
            payload = json.loads(source.read_text(encoding="utf-8"))
        else:
            payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError("--json-input must be a JSON array")
        for item in payload:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "media_type": str(item.get("media_type") or args.media_type or "").strip(),
                    "strm_path": str(item.get("strm_path") or "").strip(),
                }
            )

    if not items:
        raise ValueError("please provide at least one --title or --json-input entry")
    return items


def is_local_nas(config: dict[str, Any]) -> bool:
    if Path(NAS_CANONICAL_PATH).exists():
        return True
    nas = config.get("nas") if isinstance(config, dict) else {}
    host = str(nas.get("host") or "").strip().lower() if isinstance(nas, dict) else ""
    return host in {"127.0.0.1", "localhost"}


def build_payload(items: list[dict[str, Any]], config: dict[str, Any], sample_limit: int) -> dict[str, Any]:
    categories = get_categories(config)
    ordered_types = [key for key in ("tv", "movie", "anime", "variety", "documentary") if key in categories]
    payload_items: list[dict[str, Any]] = []

    for item in items:
        media_type = str(item.get("media_type") or "").strip().lower()
        custom_path = str(item.get("strm_path") or "").strip()
        candidate_types = [media_type] if media_type in categories else ordered_types

        seen_paths = set()
        candidate_dirs = []
        for media_key in candidate_types:
            path = resolve_strm_dir(
                str(item.get("title") or ""),
                media_key,
                config=config,
                custom_path=custom_path,
            )
            if path in seen_paths:
                continue
            seen_paths.add(path)
            candidate_dirs.append({"media_type": media_key, "path": path})

        payload_items.append({"title": item.get("title"), "candidate_dirs": candidate_dirs})

    return {"sample_limit": sample_limit, "items": payload_items}


def run_remote_check(payload: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    text = json.dumps(payload, ensure_ascii=False)
    nas = config.get("nas") if isinstance(config, dict) else {}

    if is_local_nas(config):
        cmd = ["python3", "-c", REMOTE_CHECK]
    else:
        host = str(nas.get("tailscale_ip") or nas.get("host") or "").strip()
        user = str(nas.get("user") or "").strip()
        if not host or not user:
            raise RuntimeError("missing nas host/user in media-config.json")
        encoded = base64.b64encode(REMOTE_CHECK.encode("utf-8")).decode("ascii")
        runner = f"import base64; exec(base64.b64decode('{encoded}').decode('utf-8'))"
        cmd = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "ConnectTimeout=15",
            f"{user}@{host}",
            f"python3 -c {shlex.quote(runner)}",
        ]

    proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=60)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "library check failed").strip()
        raise RuntimeError(detail)
    return json.loads(proc.stdout or "[]")


def summarize(remote_result: list[dict[str, Any]], requested_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requested_by_title = {str(item.get("title")): item for item in requested_items}
    summary: list[dict[str, Any]] = []

    for row in remote_result:
        title = str(row.get("title") or "")
        checked = row.get("checked") or []
        matches = [item for item in checked if item.get("exists") and int(item.get("strm_count") or 0) > 0]
        primary = matches[0] if matches else (checked[0] if checked else {})
        requested = requested_by_title.get(title, {})

        summary.append(
            {
                "title": title,
                "requested_media_type": requested.get("media_type") or "",
                "in_library": bool(matches),
                "strm_dir_exists": bool(primary.get("exists")),
                "strm_count": int(primary.get("strm_count") or 0),
                "checked_path": primary.get("path") or "",
                "matched_media_type": primary.get("media_type") or "",
                "sample": primary.get("sample") or [],
                "matched_dirs": matches,
            }
        )

    return summary


def main() -> int:
    try:
        args = parse_args()
        items = load_items(args)
        config = load_media_config()
        payload = build_payload(items, config, args.sample_limit)
        remote_result = run_remote_check(payload, config)
        result = summarize(remote_result, items)
        indent = 2 if args.pretty else None
        json.dump(result, sys.stdout, ensure_ascii=False, indent=indent)
        sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"检查本地影视库失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
