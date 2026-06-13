#!/usr/bin/env python3
"""Cleanup and verify temporary UC direct-link regression artifacts.

This script is intentionally narrow:
- Uses only the GBox HTTP shares API for mount cleanup.
- Uses NAS SSH only for the temporary STRM test directory.
- Never reads or writes GBox SQLite tables and never restarts containers.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR.parent / "config" / "media-config.json"

DEFAULT_SHARE_PATH = "/🍓我的UC分享/电视剧/HermesTest_GOT_9a3bd0"
DEFAULT_SHARE_ID = "9a3bd0cc0d944"
DEFAULT_NAS_STRM_DIR = "/vol1/1000/docker/xiaoya/strm/C-每日更新/Hermes回归测试/UC"


def load_config() -> dict[str, Any]:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def request_json(url: str, *, data: dict[str, Any] | None = None, headers: dict[str, str] | None = None, method: str | None = None, timeout: int = 10) -> Any:
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        url,
        data=raw,
        headers={
            **({"Content-Type": "application/json"} if data is not None else {}),
            **(headers or {}),
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if not body:
        return None
    return json.loads(body)


def gbox_login(cfg: dict[str, Any]) -> tuple[str, str]:
    gbox = cfg.get("gbox") or {}
    base = (gbox.get("internal_url") or "http://YOUR_NAS_LAN_IP:4567").rstrip("/")
    token = request_json(
        f"{base}/api/accounts/login",
        data={"username": gbox.get("username") or "admin", "password": gbox.get("password") or "admin"},
        timeout=8,
    ).get("token")
    if not token:
        raise RuntimeError("GBox login response did not include token")
    return base, token


def list_shares(base: str, token: str) -> list[dict[str, Any]]:
    headers = {"X-ACCESS-TOKEN": token}
    shares: list[dict[str, Any]] = []
    page = 0
    while True:
        data = request_json(f"{base}/api/shares?page={page}&size=200", headers=headers, timeout=10)
        content = data.get("content") or []
        shares.extend([row for row in content if isinstance(row, dict)])
        if data.get("last", True) or not content:
            break
        page += 1
    return shares


def matching_shares(shares: list[dict[str, Any]], share_path: str, share_id: str) -> list[dict[str, Any]]:
    result = []
    for row in shares:
        path = str(row.get("path") or "")
        row_share_id = str(row.get("shareId") or row.get("share_id") or "")
        if path == share_path or (share_id and row_share_id == share_id):
            result.append(row)
    return result


def delete_shares(base: str, token: str, rows: list[dict[str, Any]]) -> int:
    headers = {"X-ACCESS-TOKEN": token}
    deleted = 0
    for row in rows:
        share_pk = row.get("id")
        if share_pk is None:
            continue
        req = urllib.request.Request(f"{base}/api/shares/{share_pk}", headers=headers, method="DELETE")
        try:
            urllib.request.urlopen(req, timeout=8).read()
            deleted += 1
        except Exception as exc:
            print(f"GBOX_DELETE_ERROR id={share_pk} error={type(exc).__name__}", file=sys.stderr)
    return deleted


def build_ssh_cmd(cfg: dict[str, Any]) -> list[str]:
    nas = cfg.get("nas") or {}
    host = nas.get("host") or nas.get("tailscale_ip") or nas.get("ssh_host")
    user = nas.get("user")
    if not host or not user:
        raise RuntimeError("NAS host/user missing from media-config.json")
    base = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=15", f"{user}@{host}"]
    probe = subprocess.run(base + ["echo ok"], text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode == 0:
        return base
    password = nas.get("password") or ""
    if not password:
        return base
    return ["sshpass", "-p", password, *base]


def ssh_capture(ssh_cmd: list[str], remote: str) -> str:
    proc = subprocess.run([*ssh_cmd, remote], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=45)
    return proc.stdout.strip()


def main() -> int:
    cfg = load_config()
    share_path = os.environ.get("UC_CLEANUP_SHARE_PATH", DEFAULT_SHARE_PATH)
    share_id = os.environ.get("UC_CLEANUP_SHARE_ID", DEFAULT_SHARE_ID)
    nas_strm_dir = os.environ.get("UC_CLEANUP_NAS_STRM_DIR", DEFAULT_NAS_STRM_DIR)

    print("UC_DIRECT_CLEANUP_CHECK=1")
    print(f"TARGET_SHARE_PATH={share_path}")
    print(f"TARGET_SHARE_ID={share_id}")
    print(f"TARGET_NAS_STRM_DIR={nas_strm_dir}")

    base, token = gbox_login(cfg)
    before = matching_shares(list_shares(base, token), share_path, share_id)
    print(f"GBOX_MATCHES_BEFORE={len(before)}")
    deleted = delete_shares(base, token, before)
    print(f"GBOX_DELETED={deleted}")
    after = matching_shares(list_shares(base, token), share_path, share_id)
    print(f"GBOX_MATCHES_AFTER={len(after)}")

    ssh_cmd = build_ssh_cmd(cfg)
    quoted_dir = shlex.quote(nas_strm_dir)
    remote = (
        f"if [ -e {quoted_dir} ]; then "
        f"echo NAS_DIR_EXISTED_BEFORE=1; rm -rf -- {quoted_dir}; "
        "else echo NAS_DIR_EXISTED_BEFORE=0; fi; "
        f"if [ -e {quoted_dir} ]; then echo NAS_DIR_EXISTS_AFTER=1; else echo NAS_DIR_EXISTS_AFTER=0; fi"
    )
    nas_output = ssh_capture(ssh_cmd, remote)
    print(nas_output)

    gbox_clean = len(after) == 0
    nas_clean = "NAS_DIR_EXISTS_AFTER=0" in nas_output
    print("SAFETY_NO_SQLITE3=1")
    print("SAFETY_NO_X_STORAGES=1")
    print("SAFETY_NO_DOCKER_RESTART=1")
    print(f"CLEANUP_OK={1 if gbox_clean and nas_clean else 0}")
    return 0 if gbox_clean and nas_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
