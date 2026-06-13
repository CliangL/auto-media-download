#!/usr/bin/env python3
"""Shared STRM library layout helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR.parent / "config" / "media-config.json"
DEFAULT_STRM_BASE = "/vol1/1000/docker/xiaoya/strm/C-每日更新"
DEFAULT_CATEGORIES = {
    "movie": "电影",
    "tv": "电视剧",
    "anime": "动漫",
    "variety": "综艺",
    "documentary": "纪录片",
}


def load_media_config() -> dict[str, Any]:
    try:
        with CONFIG_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def get_categories(config: dict[str, Any] | None = None) -> dict[str, str]:
    doc = config if isinstance(config, dict) else load_media_config()
    storage = doc.get("storage") if isinstance(doc, dict) else {}
    raw = storage.get("categories") if isinstance(storage, dict) else {}

    categories = dict(DEFAULT_CATEGORIES)
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key and value:
                categories[str(key)] = str(value)

    if "variety" not in categories:
        categories["variety"] = DEFAULT_CATEGORIES["variety"]
    if "documentary" not in categories:
        categories["documentary"] = DEFAULT_CATEGORIES["documentary"]
    return categories


def get_strm_base(config: dict[str, Any] | None = None) -> str:
    doc = config if isinstance(config, dict) else load_media_config()
    storage = doc.get("storage") if isinstance(doc, dict) else {}
    nas = doc.get("nas") if isinstance(doc, dict) else {}

    for candidate in (
        storage.get("base_dir") if isinstance(storage, dict) else None,
        nas.get("strm_base_dir") if isinstance(nas, dict) else None,
        DEFAULT_STRM_BASE,
    ):
        text = str(candidate or "").strip()
        if text:
            return text.rstrip("/")
    return DEFAULT_STRM_BASE


def category_dir(media_type: str, config: dict[str, Any] | None = None) -> str:
    media_key = str(media_type or "tv").strip().lower() or "tv"
    categories = get_categories(config)
    return categories.get(media_key, categories.get("tv", DEFAULT_CATEGORIES["tv"]))


def resolve_strm_dir(
    title: str,
    media_type: str = "tv",
    *,
    config: dict[str, Any] | None = None,
    custom_path: str = "",
) -> str:
    base_dir = get_strm_base(config)
    custom = str(custom_path or "").strip()
    if custom:
        if custom.startswith(base_dir):
            return custom.rstrip("/")
        if custom.startswith("C-每日更新/"):
            return str(Path(base_dir).parent / custom).rstrip("/")
        return str((Path(base_dir) / custom).resolve()).rstrip("/")
    return f"{base_dir}/{category_dir(media_type, config)}/{title}".rstrip("/")
