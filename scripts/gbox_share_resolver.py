#!/usr/bin/env python3
"""Resolve imported GBox share paths via share_id instead of guessed titles."""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from typing import Iterable


ROOT_PREFIXES = ("🍓我的UC分享", "🍊我的夸克分享", "🍑我的阿里分享", "🏷️我的115分享", "🍒我的迅雷分享")


def normalize_mount_path(path: str | None) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    return "/" + text.strip("/")


def mount_leaf(path: str | None) -> str:
    return normalize_mount_path(path).rstrip("/").split("/")[-1]


def row_payload(row: dict) -> str:
    try:
        return json.dumps(row, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(row)


def iter_aliases(aliases: Iterable[str] | None) -> list[str]:
    seen: set[str] = set()
    items: list[str] = []
    for raw in aliases or ():
        value = str(raw or "").strip()
        if value and value not in seen:
            seen.add(value)
            items.append(value)
    return items


def row_matches_share_id(row: dict, share_id: str | None) -> bool:
    target = str(share_id or "").strip()
    if not target:
        return False
    payload = row_payload(row)
    if target in payload:
        return True
    addition = row.get("addition")
    if isinstance(addition, str) and target in addition:
        return True
    return False


def fetch_gbox_shares(gbox_url: str, token: str, max_pages: int = 5, size: int = 200) -> list[dict]:
    headers = {"X-ACCESS-TOKEN": token}
    rows: list[dict] = []
    for page in range(max_pages):
        req = urllib.request.Request(f"{gbox_url.rstrip('/')}/api/shares?page={page}&size={size}", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        content = data.get("content") or []
        rows.extend(content)
        if data.get("last", True) or not content:
            break
    return rows


def candidate_score(row: dict, share_id: str | None, root_path: str | None, aliases: Iterable[str] | None) -> int:
    path = normalize_mount_path(row.get("path"))
    if not path:
        return -1

    score = 0
    if row_matches_share_id(row, share_id):
        score += 1000

    wanted_root = normalize_mount_path(root_path)
    if wanted_root and path.startswith(wanted_root.rstrip("/") + "/"):
        score += 120

    leaf = mount_leaf(path)
    payload = row_payload(row)
    for alias in iter_aliases(aliases):
        if leaf == alias:
            score += 400
        elif leaf.startswith(alias):
            score += 220
        elif alias in leaf:
            score += 120
        elif alias in payload:
            score += 40

    return score


def pick_mount_path(rows: Iterable[dict], share_id: str | None, root_path: str | None, aliases: Iterable[str] | None) -> str | None:
    best_path = None
    best_score = -1
    for row in rows:
        score = candidate_score(row, share_id, root_path, aliases)
        if score > best_score:
            best_score = score
            best_path = normalize_mount_path(row.get("path"))
    return best_path if best_score >= 1000 or best_score >= 220 else None


def alist_path_ready(alist_base: str, path: str) -> bool:
    payload = json.dumps({"path": normalize_mount_path(path), "page": 1, "per_page": 1}).encode()
    req = urllib.request.Request(
        f"{alist_base.rstrip('/')}/api/fs/list",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    return data.get("code") == 200


def resolve_share_mount_path(
    gbox_url: str,
    token: str,
    alist_base: str,
    root_path: str,
    share_id: str | None,
    aliases: Iterable[str] | None = None,
    max_wait: int = 40,
    poll_interval: int = 3,
) -> str | None:
    start = time.time()
    last_guess = None
    while True:
        try:
            rows = fetch_gbox_shares(gbox_url, token)
            last_guess = pick_mount_path(rows, share_id, root_path, aliases)
            if last_guess and alist_path_ready(alist_base, last_guess):
                return last_guess
        except Exception:
            pass

        if time.time() - start >= max_wait:
            # A name/alias hit is not enough: stale same-title mounts can remain
            # in G-Box while AList returns code != 200. Treat those as unresolved.
            return None
        time.sleep(poll_interval)


def _cli() -> int:
    parser = argparse.ArgumentParser(description="Resolve GBox mounted share path by share_id.")
    parser.add_argument("--gbox-url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--alist-base", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--share-id", required=True)
    parser.add_argument("--alias", action="append", default=[])
    parser.add_argument("--wait", type=int, default=40)
    parser.add_argument("--poll", type=int, default=3)
    args = parser.parse_args()

    path = resolve_share_mount_path(
        gbox_url=args.gbox_url,
        token=args.token,
        alist_base=args.alist_base,
        root_path=args.root,
        share_id=args.share_id,
        aliases=args.alias,
        max_wait=args.wait,
        poll_interval=args.poll,
    )
    if path:
        print(path)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
