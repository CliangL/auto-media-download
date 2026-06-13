#!/usr/bin/env python3
"""decide-tracking.py - reliable media tracking decision.

For TV/anime, do not decide completion from the selected resource count alone.
The script must find a total episode count from internet/TMDB-style sources;
if it cannot, it returns needs_total so callers avoid writing a false completed state.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import html as html_lib
import json
import os
import re
import shlex
import ssl
import time
import sys
import urllib.parse
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
CONFIG_FILE = SKILL_DIR / "config" / "media-config.json"
KNOWN_TOTALS_FILE = SKILL_DIR / "data" / "known-totals.json"
KNOWN_RELEASE_STATUS_FILE = SKILL_DIR / "data" / "known-release-status.json"
AUTO_FIX_FILE = SCRIPT_DIR / "auto-fix-unknown.sh"
FIXED_SOURCE_TIMEOUT = 8
FIXED_SOURCE_PARSER_VERSION = 7
FIXED_STATUS_TTL_DAYS = {
    "ongoing": 3,
    "completed": 90,  # 已完结剧缓存90天，避免频繁重查导致误判
}
# 完结后源目录无增长的宽限天数，超过此天数列入待核实
COMPLETED_SOURCE_STALE_GRACE_DAYS = 15

UPDATE_RE = re.compile(r"(?:更至|更新至?)\s*第?\s*(\d+)\s*(?:集|话|期)?", re.I)
UPDATE_TAIL_RE = re.compile(r"[._\-\s]*(?:更至|更新至?)\s*(\d+)\s*(?:集|话|期)?$", re.I)
# 检测源路径中是否包含"持续更新"类频道标记（如"同步更新中"、"每日更新"等）
UPDATE_CHANNEL_RE = re.compile(r"(?:同步更新|实时更新|每日更新|更新中|同步更新中|实时同步更新)", re.I)
COMPLETE_PATTERNS = [
    re.compile(r"全\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"(\d{1,4})\s*(?:集|话|期)\s*(?:全|完结|全集|全季)", re.I),
    re.compile(r"共\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"一共\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"总集数\s*[:：]?\s*(\d{1,4})", re.I),
    re.compile(r"集数\s*[:：]?\s*(\d{1,4})", re.I),
    re.compile(r"总期数\s*[:：]?\s*(\d{1,4})", re.I),
    re.compile(r"期数\s*[:：]?\s*(\d{1,4})", re.I),
]
WEAK_EP_RE = re.compile(r"(\d{1,4})\s*(?:集|话|期)", re.I)
CURRENT_CONTEXT_RE = re.compile(r"(?:更新至?|更至?|更新|连载至?|第)\s*$", re.I)
ONGOING_HINT_RE = re.compile(r"更新中|连载|每周|播出中|未完结|更至|更新至?", re.I)
# Completion signals must be explicit.  Phrases like "全40集" are often just
# planned total-count metadata for a currently airing show; do not treat them as
# completed without words like "已完结/完结/全集" or the title-tail "40集全".
COMPLETED_HINT_RE = re.compile(r"已完结|完结|全集|\d+集全", re.I)
UPDATED_TOTAL_PATTERNS = [
    re.compile(r"更新至(?:第)?\s*(\d{1,4})\s*(?:集|话|期)\s*[/／]\s*(?:共|全)\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"更新至(?:第)?\s*(\d{1,4})\s*(?:集|话|期).*?(?:共|全)\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"(?:共|全)\s*(\d{1,4})\s*(?:集|话|期).*?更新至(?:第)?\s*(\d{1,4})\s*(?:集|话|期)", re.I),
]
UPDATED_ONLY_PATTERNS = [
    re.compile(r"更新至(?:第)?\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"最新(?:更新)?(?:至)?(?:第)?\s*(\d{1,4})\s*(?:集|话|期)", re.I),
]
COMPLETE_TOTAL_STATUS_PATTERNS = [
    re.compile(r"(?:已全部更新完毕|全集|全剧终|大结局).*?(?:共|全)?\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"第\s*(\d{1,4})\s*集.*?(?:大结局|完结|全剧终)", re.I),
    re.compile(r"(?:完结|已完结).*?(?:共|全)?\s*(\d{1,4})\s*(?:集|话|期)", re.I),
]
AIRING_HINT_PATTERNS = [
    re.compile(r"(?:今日|正式)?开播", re.I),
    re.compile(r"待新集(?:数)?更新", re.I),
    re.compile(r"首播", re.I),
    re.compile(r"正在热播", re.I),
    re.compile(r"加更", re.I),
    re.compile(r"每周[一二三四五六日天]?", re.I),
    re.compile(r"每晚", re.I),
]
LATEST_PROGRESS_PATTERNS = [
    re.compile(r"最新剧情\s*第\s*(\d{1,4})\s*(?:集|话|期)", re.I),
    re.compile(r"第\s*(\d{1,4})\s*(?:集|话|期)\s*加更", re.I),
    re.compile(r"第\s*(\d{1,4})\s*(?:集|话|期)\s*：", re.I),
]
SCHEDULE_HINT_PATTERNS = [
    re.compile(r"首播日期", re.I),
    re.compile(r"每周[一二三四五六日天]?", re.I),
    re.compile(r"每晚", re.I),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日起", re.I),
    re.compile(r"星期[一二三四五六日天]", re.I),
    re.compile(r"\b\d{1,2}:\d{2}\b", re.I),
    re.compile(r"\b\d{1,2}-\d{1,2}\b", re.I),
    re.compile(r"加更", re.I),
]
OLD_SERIES_YEAR_PATTERNS = [
    re.compile(r"(19\d{2}|20\d{2})\s*年.{0,24}(?:电视剧|网络剧|剧集|首播|播出|出品|发行|上映)", re.I),
    re.compile(r"(?:电视剧|网络剧|剧集|首播|播出|出品|发行|上映).{0,24}(19\d{2}|20\d{2})\s*年", re.I),
    re.compile(r"[（(](19\d{2}|20\d{2})[）)]"),
]
LIVE_AIRING_HINT_RE = re.compile(r"未完结|待新集|正在热播|播出中|连载|更新中|每周|每晚|每日|每天|首播|开播|加更", re.I)

TRUSTED_DOMAINS = {
    "maoyan.com": 0.25,
    "baike.baidu.com": 0.22,
    "douban.com": 0.18,
    "tvmao.com": 0.16,
    "mtime.com": 0.12,
    "wikipedia.org": 0.10,
    "linux.do": 0.12,
    "becmd.com": 0.10,
}

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    )
}

ORIGIN_PRIORITY = {
    "fixed_source_cache": 9,
    "status_override": 9,
    "known_total": 7,
    "douban": 7,
    "tmdb": 6,
    "brave": 5,
    "html_search": 4,
    "resource_title": 3,
    "existing_state_evidence": 2,
    "existing_state_total": 1,
    "provided_total": 0,
}

ORIGIN_CONFIDENCE_BONUS = {
    "fixed_source_cache": 0.06,
    "status_override": 0.06,
    "known_total": 0.04,
    "douban": 0.04,
    "tmdb": 0.035,
    "brave": 0.03,
    "html_search": 0.025,
    "resource_title": 0.0,
    "existing_state_evidence": -0.02,
    "existing_state_total": -0.05,
    "provided_total": -0.08,
}


def _build_source_trace_entry(origin: str, candidates: list[dict[str, Any]] | None) -> str:
    count = len(candidates or [])
    if count <= 0:
        return f"{origin}:0"
    totals: list[str] = []
    for candidate in (candidates or [])[:3]:
        total = parse_int(candidate.get("total"))
        if total:
            totals.append(str(total))
    suffix = ""
    if totals:
        suffix = f" [{', '.join(totals)}]"
    return f"{origin}:{count}{suffix}"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_known_totals() -> dict[str, Any]:
    try:
        return json.loads(KNOWN_TOTALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_known_release_status() -> dict[str, Any]:
    try:
        return json.loads(KNOWN_RELEASE_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_known_release_status(data: dict[str, Any]) -> None:
    KNOWN_RELEASE_STATUS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def existing_state_candidates(title: str, media_type: str) -> list[dict[str, Any]]:
    state_file = SKILL_DIR / "data" / "drama-state.json"
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    dramas = data.get("dramas") if isinstance(data, dict) else None
    if not isinstance(dramas, list):
        return []
    key = clean_for_match(title)
    out: list[dict[str, Any]] = []
    for row in dramas:
        if not isinstance(row, dict):
            continue
        row_title = normalize_title(str(row.get("name") or row.get("title") or ""))
        if not row_title or clean_for_match(row_title) != key:
            continue
        row_type = str(row.get("media_type") or "tv").strip().lower()
        if row_type and media_type and row_type != media_type:
            continue
        status = str(row.get("status") or "").strip().lower()
        ongoing = status == "ongoing"
        completed = status == "completed"
        total = parse_int(row.get("total_episodes"))
        evidence = str(row.get("total_evidence") or "").strip()
        if evidence:
            for candidate in extract_totals_from_text(evidence, "existing-state-evidence"):
                candidate = dict(candidate)
                base_confidence = float(candidate.get("confidence", 0))
                if completed:
                    candidate["confidence"] = max(0.82, min(0.9, base_confidence))
                elif ongoing:
                    candidate["confidence"] = max(0.62, min(0.68, base_confidence))
                else:
                    candidate["confidence"] = max(0.7, min(0.78, base_confidence))
                candidate["source"] = evidence
                candidate["origin"] = "existing_state_evidence"
                out.append(candidate)
        if total:
            confidence = 0.72
            if completed and evidence:
                confidence = 0.84
            elif completed:
                confidence = 0.78
            elif ongoing and evidence:
                confidence = 0.62
            elif ongoing:
                confidence = 0.58
            out.append({
                "total": total,
                "confidence": confidence,
                "source": evidence or "existing-state-total",
                "ongoing_hint": ongoing,
                "completed_hint": completed,
                "origin": "existing_state_total",
                "text": json.dumps({"title": row_title, "status": status, "evidence": evidence}, ensure_ascii=False),
            })
    return out


def strip_backup_suffix(text: str) -> str:
    return re.sub(r"(?:[._\-])?bak(?:cup)?[-_]?\d{6,14}$", "", str(text or ""), flags=re.I).strip()


def strip_year_suffix(text: str) -> str:
    text = re.sub(r"\s*[\(（](?:19|20)\d{2}[^)）]*[\)）]\s*$", "", str(text or "")).strip()
    text = re.sub(r"\s*(?:19|20)\d{2}\s*$", "", text).strip()
    return text


def normalize_title(text: str) -> str:
    text = strip_backup_suffix(text or "")
    m = re.search(r"《([^》]+)》", text)
    if m:
        text = m.group(1)
    text = re.sub(r"^【[^】]*】", "", text)
    text = re.sub(r"^\[[^\]]*\]", "", text)
    text = re.sub(r"^（[^）]*）", "", text)
    text = re.sub(r"\s*[\[【].*?[\]】]\s*$", "", text)
    text = UPDATE_TAIL_RE.sub("", text)
    text = re.sub(r"^[A-Za-z]\s+(?=[\u4e00-\u9fff])", "", text)
    text = re.sub(r"[：:]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return strip_year_suffix(text.strip())


def clean_for_match(text: str) -> str:
    text = strip_backup_suffix(text)
    return re.sub(r"[\s\-_.:：，,。！!？?《》【】\[\]（）()]+", "", (text or "").lower())


def strip_trailing_duplicate_number_key(text: str) -> str:
    return re.sub(r"1$", "", clean_for_match(text or ""))


def source_path_title_aliases(source_path: str) -> list[str]:
    aliases: list[str] = []
    seen: set[str] = set()
    category_words = {
        "电视剧",
        "国产剧",
        "国剧",
        "中国",
        "已刮削",
        "每日更新",
        "同步更新中",
        "电视剧实时同步更新",
        "动漫",
        "综艺",
        "电影",
        "纪录片",
    }
    for raw_part in reversed([p for p in str(source_path or "").split("/") if p.strip()]):
        part = normalize_title(raw_part)
        if not part:
            continue
        if re.fullmatch(r"season\s*\d+|s\d{1,3}", part, re.I):
            continue
        if re.fullmatch(r"\d{4}|19\dX|20\dX|[A-Za-z]|\d+", part, re.I):
            continue
        if "（" in raw_part and "）" in raw_part and not re.search(r"[\u4e00-\u9fff]{2,}", part):
            continue
        compact = re.sub(r"[（(].*?[）)]", "", part).strip()
        compact = re.sub(r"[A-Za-z]$", "", compact).strip()
        if compact in category_words:
            continue
        if not re.search(r"[\u4e00-\u9fff]{2,}", compact):
            continue
        key = clean_for_match(compact)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(compact)
    return aliases


def title_match_keys(title: str) -> set[str]:
    key = clean_for_match(title or "")
    keys = {key} if key else set()
    stripped = strip_trailing_duplicate_number_key(title or "")
    if stripped:
        keys.add(stripped)
    # Xiaoya-style directory/cache names may prepend an initial letter to CJK
    # titles, for example "J家业".  Treat that as the same key only for matching.
    if re.match(r"^[a-z][\u4e00-\u9fff]", key or "", re.I):
        keys.add(key[1:])
    return {item for item in keys if item}


def names_match_title(name: str, title: str) -> bool:
    name_keys = title_match_keys(name)
    title_keys = title_match_keys(title)
    if not name_keys or not title_keys:
        return False
    if name_keys & title_keys:
        return True
    return any(left in right or right in left for left in name_keys for right in title_keys)


def lookup_title_for_source(title: str, note: str, source_path: str) -> str:
    normalized_title = normalize_title(title or note or "")
    title_key = clean_for_match(normalized_title)
    if not title_key:
        return normalized_title
    title_base_key = strip_trailing_duplicate_number_key(normalized_title)
    for alias in source_path_title_aliases(source_path):
        alias_key = clean_for_match(alias)
        if not alias_key:
            continue
        if alias_key == title_key:
            return alias
        if title_base_key and alias_key == title_base_key and title_key != alias_key:
            return alias
    return normalized_title


def parse_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        n = int(str(value).strip())
        return n if n > 0 else None
    except Exception:
        return None


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


def release_years_from_text(text: str) -> list[int]:
    years: list[int] = []
    for pattern in OLD_SERIES_YEAR_PATTERNS:
        for match in pattern.finditer(text or ""):
            year = parse_int(match.group(1))
            if year and 1900 <= year <= 2100:
                years.append(year)
    return years


def historical_release_context(text: str) -> bool:
    years = release_years_from_text(text or "")
    if not years:
        return False
    current_year = datetime.now().year
    newest = max(years)
    oldest = min(years)
    return oldest <= current_year - 2 and newest <= current_year - 1


def live_airing_context(text: str) -> bool:
    text = text or ""
    if not LIVE_AIRING_HINT_RE.search(text):
        return False
    # Old metadata pages frequently contain historical broadcast schedules such as
    # "每晚/每周播出".  Do not treat that as a live update signal years later.
    return not historical_release_context(text)


def release_source_mode(row: dict[str, Any]) -> str:
    mode = str(row.get("source_mode") or "").strip().lower()
    if mode:
        return mode
    updated_at = parse_iso(row.get("updated_at"))
    evidence = str(row.get("evidence") or "").strip().lower()
    if updated_at and evidence.startswith(("http://", "https://")):
        return "auto_fixed_source"
    return "manual_seed"


def fixed_source_parser_version(row: dict[str, Any]) -> int:
    try:
        return int(row.get("parser_version") or 0)
    except Exception:
        return 0


def brave_key() -> str:
    for name in ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY", "BRV_API_KEY", "BRAVE_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    try:
        text = AUTO_FIX_FILE.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r'BRAVE_KEY="([^"]+)"', text)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return ""


def tmdb_key(config: dict[str, Any]) -> str:
    for name in ("TMDB_API_KEY", "THEMOVIEDB_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    val = config.get("tmdb", {}) if isinstance(config.get("tmdb"), dict) else {}
    return str(val.get("api_key") or "").strip()


def request_json(url: str, headers: dict[str, str] | None = None, timeout: int = 12, retries: int = 0) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            ctx = ssl.create_default_context()
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # 经代理访问 TMDB 偶发超时,重试一两次即可
            last_exc = exc
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError("request_json failed")


def request_text(url: str, headers: dict[str, str] | None = None, timeout: int = 12) -> str:
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)
    req = urllib.request.Request(url, headers=merged_headers)
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, "replace")
        except Exception:
            return raw.decode("utf-8", "replace")


def normalize_web_text(text: str) -> str:
    text = html_lib.unescape(text or "")
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", text)
    text = text.replace("\u00a0", " ")
    return text


def tmdb_candidates(title: str, config: dict[str, Any]) -> list[dict[str, Any]]:
    key = tmdb_key(config)
    if not key:
        return []
    base = "https://api.themoviedb.org/3"
    q = urllib.parse.quote(title)
    out: list[dict[str, Any]] = []
    try:
        search = request_json(f"{base}/search/tv?api_key={urllib.parse.quote(key)}&query={q}&language=zh-CN&include_adult=false", retries=2)
    except Exception:
        return out
    target = clean_for_match(title)
    for item in (search.get("results") or [])[:5]:
        name = item.get("name") or item.get("original_name") or ""
        orig = item.get("original_name") or ""
        # 去季号后基名需与目标剧名相等,拒绝"北上"误匹配"北上广不相信眼泪"这类子串命中
        base = clean_for_match(_SEASON_SUFFIX_RE.sub("", name))
        base_orig = clean_for_match(_SEASON_SUFFIX_RE.sub("", orig))
        if target and target not in (base, base_orig, clean_for_match(name)):
            continue
        tid = item.get("id")
        if not tid:
            continue
        try:
            detail = request_json(f"{base}/tv/{tid}?api_key={urllib.parse.quote(key)}&language=zh-CN", retries=2)
        except Exception:
            continue
        total = parse_int(detail.get("number_of_episodes"))
        if not total:
            continue
        status = str(detail.get("status") or "")
        out.append({
            "total": total,
            "confidence": 0.82,
            "source": f"TMDB:{detail.get('name') or name}",
            "origin": "tmdb",
            "ongoing_hint": status.lower() in {"returning series", "in production", "planned"},
            "completed_hint": status.lower() == "ended",
            "text": json.dumps({"name": detail.get("name"), "status": status}, ensure_ascii=False),
        })
    return out


DOUBAN_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
_SEASON_SUFFIX_RE = re.compile(r"\s*第[一二三四五六七八九十百零0-9]+季\s*$")


def douban_candidates(title: str, media_type: str = "tv") -> list[dict[str, Any]]:
    """国产剧/亚洲剧集数最权威且最及时的免费源。

    两步:豆瓣 suggest API 按剧名拿候选条目(精确消歧,能区分同名剧/分季),
    再用移动端 rexxar API 取 ``episodes_count``。豆瓣对国产新剧收录极快,
    集数准确。episodes_count 是计划总集数,本身不证明完结——完结与否交给
    ``decision_from_total`` 用 current vs total 判断。
    """
    if media_type == "movie":
        return []
    out: list[dict[str, Any]] = []
    try:
        raw = request_text(
            "https://movie.douban.com/j/subject_suggest?q=" + urllib.parse.quote(title),
            headers={"Referer": "https://movie.douban.com/", "Accept": "application/json, text/plain, */*"},
            timeout=10,
        )
        items = json.loads(raw)
    except Exception:
        return out
    if not isinstance(items, list):
        return out
    target = clean_for_match(title)
    seen_ids: set[str] = set()
    for it in items[:6]:
        if not isinstance(it, dict) or it.get("type") != "movie":
            continue
        name = str(it.get("title") or "")
        base = _SEASON_SUFFIX_RE.sub("", name)
        cb = clean_for_match(base)
        # 标题消歧:去掉"第X季"后,基名需与目标剧名互相包含,避免命中别的剧
        if target and target not in cb and cb not in target:
            continue
        sid = str(it.get("id") or "").strip()
        if not sid or sid in seen_ids:
            continue
        seen_ids.add(sid)
        try:
            draw = request_text(
                f"https://m.douban.com/rexxar/api/v2/tv/{sid}?for_mobile=1",
                headers={
                    "User-Agent": DOUBAN_MOBILE_UA,
                    "Referer": f"https://m.douban.com/tv/{sid}/",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            detail = json.loads(draw)
        except Exception:
            continue
        if not isinstance(detail, dict):
            continue
        total = parse_int(detail.get("episodes_count"))
        if not total or total > 2000:
            continue
        subtitle = str(detail.get("card_subtitle") or "")
        out.append({
            "total": total,
            "confidence": 0.88,
            "source": f"豆瓣:{detail.get('title') or name}",
            "origin": "douban",
            "ongoing_hint": False,
            "completed_hint": False,
            "text": subtitle[:200],
        })
    return out


def score_domain(url: str) -> float:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    for domain, score in TRUSTED_DOMAINS.items():
        if domain in host:
            return score
    return 0.0


def extract_totals_from_text(text: str, url: str = "") -> list[dict[str, Any]]:
    text = normalize_web_text(text)
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    out: list[dict[str, Any]] = []
    domain_bonus = score_domain(url)
    historical_hint = historical_release_context(text)
    ongoing_hint = live_airing_context(text)
    completed_hint = bool(COMPLETED_HINT_RE.search(text)) or historical_hint

    for pat in COMPLETE_PATTERNS:
        for m in pat.finditer(text):
            n = parse_int(m.group(1))
            if not n or n > 2000:
                continue
            out.append({
                "total": n,
                "confidence": min(0.92, 0.52 + domain_bonus + (0.08 if completed_hint else 0.0)),
                "source": url or "web",
                "origin": "web",
                "ongoing_hint": ongoing_hint,
                "completed_hint": completed_hint,
                "text": text[:500],
            })

    for m in WEAK_EP_RE.finditer(text):
        n = parse_int(m.group(1))
        if not n or n > 2000:
            continue
        prefix = text[max(0, m.start() - 8):m.start()]
        if CURRENT_CONTEXT_RE.search(prefix):
            continue
        # Weak matches need a trusted domain; random streaming sites often expose current/line counts.
        conf = 0.38 + domain_bonus
        if conf >= 0.58:
            out.append({
                "total": n,
                "confidence": min(0.78, conf),
                "source": url or "web-weak",
                "origin": "web",
                "ongoing_hint": ongoing_hint,
                "completed_hint": completed_hint,
                "text": text[:500],
            })
    return out


def strip_html(text: str) -> str:
    text = normalize_web_text(text)
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text or "")
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _is_name_char(char: str) -> bool:
    return bool(re.match(r"[0-9A-Za-z\u4e00-\u9fff·]", char or ""))


def iter_exact_title_matches(text: str, title: str):
    """Yield title matches without treating longer CJK titles as the same show."""
    if not title:
        return
    for match in re.finditer(re.escape(title), text):
        before = text[match.start() - 1] if match.start() > 0 else ""
        after = text[match.end()] if match.end() < len(text) else ""
        if before and _is_name_char(before):
            continue
        if after and _is_name_char(after):
            continue
        yield match


def title_contexts(text: str, title: str, window: int = 180) -> list[str]:
    title = (title or "").strip()
    if not title:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for match in iter_exact_title_matches(text, title):
        start = max(0, match.start() - window)
        end = min(len(text), match.end() + window)
        snippet = text[start:end].strip()
        if snippet and snippet not in seen:
            seen.add(snippet)
            out.append(snippet)
    return out


def html_search_candidates(title: str, media_type: str = "tv") -> list[dict[str, Any]]:
    queries: list[tuple[str, str]] = []
    if media_type == "variety":
        queries.extend([
            ("baidu", f"{title} 综艺 共多少期"),
            ("baidu", f"{title} 期数"),
            ("baidu", f"{title} 百度百科 期数"),
            ("sogou", f"{title} 综艺 共多少期"),
        ])
    else:
        queries.extend([
            ("baidu", f"{title} 一共多少集"),
            ("baidu", f"{title} 电视剧 共多少集"),
            ("baidu", f"{title} 百度百科 集数"),
            ("baidu", f"{title} 爱奇艺 集数"),
            ("sogou", f"{title} 一共多少集"),
        ])

    out: list[dict[str, Any]] = []
    for engine, query in queries:
        if engine == "baidu":
            url = "https://www.baidu.com/s?wd=" + urllib.parse.quote(query)
        else:
            url = "https://www.sogou.com/web?query=" + urllib.parse.quote(query)
        try:
            html = request_text(url, timeout=12)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        page_text = strip_html(html)
        snippets = title_contexts(page_text, title)
        if not snippets and len(page_text) <= 5000:
            snippets = [page_text]
        elif not snippets:
            snippets = [page_text[:5000]]
        for snippet in snippets:
            for candidate in extract_totals_from_text(snippet, url):
                candidate = dict(candidate)
                candidate["confidence"] = min(0.86, float(candidate.get("confidence", 0)) + 0.08)
                candidate["origin"] = "html_search"
                out.append(candidate)
    return out


def fixed_source_domains(media_type: str) -> list[str]:
    if media_type == "variety":
        return [
            "kan.2345.com",
            "baike.baidu.com",
        ]
    if media_type == "anime":
        return [
            "baike.baidu.com",
            "thetvdb.com",
            "wikipedia.org",
        ]
    return [
        "tvmao.com",
        "piaofang.maoyan.com",
        "iq.com",
        "youku.com",
        "mgtv.com",
        "baike.baidu.com",
        "wikipedia.org",
    ]


def brave_result_urls(query: str, *, count: int = 3) -> list[dict[str, str]]:
    key = brave_key()
    if not key:
        return []
    url = (
        "https://api.search.brave.com/res/v1/web/search?q="
        + urllib.parse.quote(query)
        + f"&count={count}&country=CN&search_lang=zh-hans"
    )
    try:
        data = request_json(
            url,
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=FIXED_SOURCE_TIMEOUT,
        )
    except Exception:
        return []
    rows: list[dict[str, str]] = []
    for item in (data.get("web", {}).get("results") or []):
        rows.append(
            {
                "title": str(item.get("title") or ""),
                "description": str(item.get("description") or ""),
                "url": str(item.get("url") or ""),
            }
        )
    return rows


def score_fixed_source_domain(url: str) -> float:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    # 百科类来源（权威性最高）
    if "baike.baidu.com" in host:
        return 0.96
    if "wikipedia.org" in host:
        return 0.95
    # 官方播放平台（一手数据）
    if "iq.com" in host or "youku.com" in host:
        return 0.94
    if "mgtv.com" in host or "hunantv.com" in host:
        return 0.94
    # 第三方聚合器（数据可能滞后，略低于百科和官方平台）
    if "tvmao.com" in host:
        path = urllib.parse.urlparse(url or "").path.lower()
        if path.endswith("/playingtime"):
            return 0.90
        if "/kanju/" in path or "/drama/" in path:
            return 0.93
        return 0.78
    if "piaofang.maoyan.com" in host:
        return 0.93
    # 其他受信来源
    if "thetvdb.com" in host:
        return 0.90
    return 0.84


def extract_fixed_source_status_candidates(
    text: str,
    url: str,
    media_type: str,
    *,
    current: int = 0,
) -> list[dict[str, Any]]:
    clean = strip_html(text)
    if not clean:
        return []
    out: list[dict[str, Any]] = []
    base_conf = score_fixed_source_domain(url)
    historical_hint = historical_release_context(clean)
    schedule_hint = any(p.search(clean) for p in SCHEDULE_HINT_PATTERNS) and not historical_hint
    airing_hint = any(p.search(clean) for p in AIRING_HINT_PATTERNS) and not historical_hint
    completed_hint = bool(COMPLETED_HINT_RE.search(clean))
    ongoing_hint = schedule_hint or airing_hint or live_airing_context(clean)
    latest_current_hint = 0
    for pat in LATEST_PROGRESS_PATTERNS + UPDATED_ONLY_PATTERNS:
        for m in pat.finditer(clean):
            value = parse_int(m.group(1))
            if value and value <= 2000:
                latest_current_hint = max(latest_current_hint, value)
    for pat in UPDATED_TOTAL_PATTERNS:
        for m in pat.finditer(clean):
            a = parse_int(m.group(1))
            b = parse_int(m.group(2))
            if not a or not b:
                continue
            current_hint, total = (a, b) if a <= b else (b, a)
            if total > 2000:
                continue
            if current_hint >= total:
                out.append(
                    {
                        "status": "completed",
                        "total_episodes": total,
                        "current_hint": current_hint,
                        "exact_total": True,
                        "confidence": min(0.99, base_conf + 0.03),
                        "evidence": url,
                        "note": f"固定来源显示已更完（{current_hint}/{total}）",
                    }
                )
                continue
            if historical_hint and not live_airing_context(clean):
                out.append(
                    {
                        "status": "completed",
                        "total_episodes": total,
                        "current_hint": current_hint,
                        "exact_total": True,
                        "confidence": min(0.98, base_conf + 0.02),
                        "evidence": url,
                        "note": f"固定来源为历史剧集，共{total}集",
                    }
                )
                continue
            out.append(
                {
                    "status": "ongoing",
                    "total_episodes": total,
                    "current_hint": current_hint,
                    "exact_total": True,
                    "confidence": min(0.99, base_conf + 0.02),
                    "evidence": url,
                    "note": f"固定来源显示仍在更新（{current_hint}/{total}）",
                }
            )
    for pat in COMPLETE_TOTAL_STATUS_PATTERNS:
        for m in pat.finditer(clean):
            total = parse_int(m.group(1))
            if not total or total > 2000:
                continue
            confidence = min(0.99, base_conf + 0.03)
            if ongoing_hint:
                confidence = max(0.58, confidence - 0.14)
            if latest_current_hint and total > latest_current_hint:
                confidence = max(0.54, confidence - 0.18)
            if current and total > current:
                confidence = max(0.5, confidence - 0.08)
            out.append(
                {
                    "status": "completed",
                    "total_episodes": total,
                    "exact_total": True,
                    "confidence": confidence,
                    "evidence": url,
                    "note": f"固定来源显示已完结，共{total}集",
                }
            )
    total_candidates = [
        parse_int(candidate.get("total"))
        for candidate in extract_totals_from_text(clean, url)
    ]
    filtered_totals = sorted(
        {
            total
            for total in total_candidates
            if total and total <= 2000 and total >= max(current, latest_current_hint or 0, 1)
        }
    )
    if ongoing_hint and latest_current_hint and (
        schedule_hint or not filtered_totals or latest_current_hint < filtered_totals[0]
    ):
        out.append(
            {
                "status": "ongoing",
                "total_episodes": None,
                "current_hint": latest_current_hint,
                "exact_total": False,
                "confidence": min(0.98, base_conf + 0.02),
                "evidence": url,
                "note": f"固定来源显示仍在更新，当前约第{latest_current_hint}集",
            }
        )
    if ongoing_hint and filtered_totals:
        total = filtered_totals[0]
        if not latest_current_hint or total > latest_current_hint:
            out.append(
                {
                    "status": "ongoing",
                    "total_episodes": total,
                    "current_hint": latest_current_hint or None,
                    "exact_total": True,
                    "confidence": min(0.96, base_conf if latest_current_hint else base_conf - 0.06),
                    "evidence": url,
                    "note": f"固定来源显示仍在更新，共{total}集",
                }
            )
    elif completed_hint and filtered_totals and not any(str(item.get("status") or "") == "completed" for item in out):
        total = filtered_totals[0]
        out.append(
            {
                "status": "completed",
                "total_episodes": total,
                "current_hint": latest_current_hint or None,
                "exact_total": True,
                "confidence": min(0.98, base_conf + 0.01),
                "evidence": url,
                "note": f"固定来源显示已完结，共{total}集",
            }
        )
    elif historical_hint and filtered_totals and not out:
        total = filtered_totals[0]
        out.append(
            {
                "status": "completed",
                "total_episodes": total,
                "current_hint": latest_current_hint or None,
                "exact_total": True,
                "confidence": min(0.97, base_conf),
                "evidence": url,
                "note": f"固定来源为历史剧集，共{total}集",
            }
        )
    elif ongoing_hint and not out:
        out.append(
            {
                "status": "ongoing",
                "total_episodes": None,
                "current_hint": latest_current_hint or None,
                "exact_total": False,
                "confidence": min(0.94, base_conf - 0.01),
                "evidence": url,
                "note": "固定来源显示仍在更新",
            }
        )
    return out


def _classify_source(url: str) -> str:
    """按来源类型分类，不依赖特定域名名称。"""
    host = urllib.parse.urlparse(url or "").netloc.lower()
    if "baike.baidu.com" in host or "wikipedia.org" in host:
        return "encyclopedia"
    if any(d in host for d in ("youku.com", "iq.com", "mgtv.com", "hunantv.com",
                                "v.qq.com", "miguvideo.com", "imgo.tv")):
        return "official_platform"
    if any(d in host for d in ("tvmao.com", "maoyan.com", "douban.com", "mtime.com")):
        return "aggregator"
    if "thetvdb.com" in host:
        return "encyclopedia"
    return "other"


def choose_fixed_status_candidate(
    candidates: list[dict[str, Any]],
    *,
    current: int = 0,
) -> dict[str, Any] | None:
    if not candidates:
        return None

    SOURCE_TYPE_BONUS = {
        "encyclopedia": 0.05,       # 百科类来源最可靠
        "official_platform": 0.03,  # 官方播放平台一手数据
        "aggregator": 0.0,          # 第三方聚合器可能有滞后
        "other": -0.02,
    }

    def score(candidate: dict[str, Any]) -> tuple[float, int, int, int, int, int]:
        confidence = float(candidate.get("confidence") or 0.0)
        status = str(candidate.get("status") or "").strip().lower()
        total = parse_int(candidate.get("total_episodes")) or 0
        current_hint = parse_int(candidate.get("current_hint")) or 0
        exact_total = 1 if candidate.get("exact_total") else 0
        evidence = str(candidate.get("evidence") or "")
        ongoing_hint = bool(candidate.get("ongoing_hint"))
        completed_hint = bool(candidate.get("completed_hint"))

        # 来源类型加分
        source_type = _classify_source(evidence)
        confidence += SOURCE_TYPE_BONUS.get(source_type, 0.0)

        if current and total and total < current:
            confidence -= 0.45
        if status == "ongoing":
            if current and current_hint >= current:
                confidence += 0.12
            if current and total and total > current:
                confidence += 0.08
            if total == 0:
                confidence += 0.03
        elif status == "completed":
            if exact_total and total:
                confidence += 0.05
            if current and total and total <= current:
                confidence += 0.04
            if current and total and total > current and total > current * 2:
                confidence -= 0.06
            if completed_hint:
                confidence += 0.02
            if not ongoing_hint:
                confidence += 0.01

        # 候选总数与当前集数一致时加分（该总数更可信）
        current_match_bonus = 0
        if current > 0 and total == current:
            current_match_bonus = 1

        return (
            min(0.99, confidence),
            # 先信任明确完结信号，再看是否有确切总数
            1 if status == "completed" else 0,
            1 if exact_total else 0,
            # 只有真的有在播证据的 ongoing 才能压过 completed
            1 if status == "ongoing" and exact_total and ongoing_hint else 0,
            # 当前集数与候选总数一致，说明该总数可信
            current_match_bonus,
            # 无确切总数的 ongoing 次之
            1 if status == "ongoing" and not exact_total else 0,
            current_hint,
            total,
        )

    return max(candidates, key=score)


def fixed_source_queries(title: str, domain: str, media_type: str) -> list[str]:
    queries = [f'site:{domain} "{title}"']
    if media_type == "variety":
        queries.extend(
            [
                f'site:{domain} "{title}" 每周',
                f'site:{domain} "{title}" 第2季',
                f'site:{domain} "{title}" 共多少期',
            ]
        )
    else:
        queries.extend(
            [
                f'site:{domain} "{title}" 播出时间',
                f'site:{domain} "{title}" 更新至',
                f'site:{domain} "{title}" 共多少集',
            ]
        )
    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query in seen:
            continue
        seen.add(query)
        deduped.append(query)
    return deduped


def fixed_source_snippets(text: str, title: str) -> list[str]:
    clean = strip_html(text)
    if not clean:
        return []
    snippets = title_contexts(clean, title, window=260)
    if snippets:
        return snippets[:3]
    return []


def _baike_page_url_valid(page_url: str, target_title: str) -> bool:
    """验证百度百科 URL 中的条目标题是否与目标剧名匹配。

    百度百科 URL 格式: /item/<url_encoded_title>/<id>
    如果 URL 标题（如"顾德昭"）与目标剧名（如"良陈美锦"）不匹配，
    说明抓到了同名演员/漫画/无关页面的百科，应拒绝。
    """
    if not page_url or not target_title:
        return False
    host = urllib.parse.urlparse(page_url or "").netloc.lower()
    if "baike.baidu.com" not in host and "baike." not in host:
        return True  # 非百科 URL，不做此检查
    path = urllib.parse.urlparse(page_url or "").path
    if "/item/" not in path:
        return True  # 无法解析，放行
    try:
        item_part = path.split("/item/")[1].split("/")[0]
        decoded = urllib.parse.unquote(item_part)
    except Exception:
        return True  # 解码失败，放行
    target_clean = clean_for_match(target_title)
    item_clean = clean_for_match(decoded)
    if not target_clean or not item_clean:
        return True  # 清洗后为空，放行
    if target_clean == item_clean:
        return True
    if target_clean in item_clean or item_clean in target_clean:
        return True
    return False


def _baike_page_media_type_ok(page_html: str, media_type: str) -> bool:
    """检查百度百科页面内容是否属于预期的媒体类型。

    百度百科的 <title> 标签通常包含媒体类型信息，如：
    - 电视剧: "xxx（xxx电视剧）_百度百科" 或 "xxx网络剧"
    - 漫画: "xxx（xxx漫画）_百度百科"
    - 小说: "xxx（xxx小说）_百度百科"

    注意：很多国产剧改编自小说，小说百科页面通常也会包含剧集信息
    （集数、播出时间等），因此仅对完全不同类型的媒体做拒绝。
    """
    if not page_html or not media_type:
        return True  # 无法判断，放行
    if media_type not in ("tv", "variety", "anime"):
        return True  # 仅对已知类型做检查
    import re as _re
    title_m = _re.search(r"<title>(.*?)</title>", page_html, _re.I)
    if not title_m:
        return True  # 找不到 title 标签，放行
    page_title = title_m.group(1)
    # 完全对立的媒体类型（绝不会有剧集信息）
    HARD_REJECT: dict[str, list[str]] = {
        "tv": ["漫画", "动漫", "游戏", "动画"],
        "variety": ["漫画", "动漫", "游戏", "动画", "电影"],
        "anime": ["电视剧", "网络剧", "综艺", "电影"],
    }
    patterns = HARD_REJECT.get(media_type, [])
    for pattern in patterns:
        if pattern in page_title:
            return False
    return True


def fixed_source_url_valid(page_url: str) -> bool:
    parsed = urllib.parse.urlparse(page_url or "")
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    if not host:
        return False
    if "baidu.com" in host and path.startswith("/s"):
        return False
    if "sogou.com" in host and "/web" in path:
        return False
    search_hosts = ("so.youku.com", "so.iqiyi.com", "so.mgtv.com", "search.bilibili.com")
    if any(search_host in host for search_host in search_hosts):
        return False
    if path in {"/search", "/web/search"} or path.startswith("/search/") or path.startswith("/web/search"):
        return False
    if "/search/" in path or "/search?" in path:
        return False
    return True


def fixed_source_title_relevant(title: str, result_title: str) -> bool:
    normalized = normalize_title(strip_html(result_title))
    if not normalized.startswith(title):
        return False
    suffix = normalized[len(title):].strip()
    if not suffix:
        return True
    allowed_prefixes = (
        "_",
        "-",
        "剧情介绍",
        "播出时间",
        "第一季",
        "第二季",
        "第1季",
        "第2季",
        "(",
        "（",
        " ",
        "高清",
        "新古装剧",
        "电视剧",
        "综艺",
        "Episode",
    )
    return suffix.startswith(allowed_prefixes)


def refresh_known_release_status(
    title: str,
    media_type: str,
    note: str,
    source_path: str,
    *,
    current: int = 0,
) -> dict[str, Any] | None:
    target = clean_for_match(title)
    if not target:
        return None
    all_candidates: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    for domain in fixed_source_domains(media_type):
        for query in fixed_source_queries(title, domain, media_type):
            for row in brave_result_urls(query, count=3):
                page_url = row.get("url") or ""
                if not fixed_source_url_valid(page_url):
                    continue
                title_desc = " ".join([row.get("title") or "", row.get("description") or ""]).strip()
                if target not in clean_for_match(title_desc):
                    continue
                if not fixed_source_title_relevant(title, row.get("title") or ""):
                    continue
                # 验证百度百科 URL 条目标题是否匹配目标剧名
                # （防止抓到同名演员/漫画/无关页面的百科）
                if not _baike_page_url_valid(page_url, title):
                    continue
                combined = " ".join([title_desc, page_url]).strip()
                page_key = page_url or combined
                if page_key in seen_pages:
                    continue
                seen_pages.add(page_key)
                texts = [title_desc]
                if page_url:
                    try:
                        fetched = request_text(page_url, timeout=FIXED_SOURCE_TIMEOUT)
                        # 验证抓取到的百科页面内容是否属于预期的媒体类型
                        # （防止搜电视剧搜到漫画/小说百科页面）
                        if not _baike_page_media_type_ok(fetched, media_type):
                            continue
                        texts.extend(fixed_source_snippets(fetched, title))
                    except Exception:
                        pass
                for text in texts:
                    for candidate in extract_fixed_source_status_candidates(
                        text,
                        page_url or domain,
                        media_type,
                        current=current,
                    ):
                        all_candidates.append(candidate)
    best = choose_fixed_status_candidate(all_candidates, current=current)
    if not best:
        return None
    aliases = title_aliases(title, note, source_path)
    canonical = aliases[0] if aliases else title
    data = load_known_release_status()
    if not isinstance(data, dict):
        data = {}
    row = {
        "media_type": media_type,
        "status": str(best.get("status") or ""),
        "total_episodes": best.get("total_episodes"),
        "exact_total": bool(best.get("exact_total")),
        "aliases": [x for x in aliases[1:] if x != canonical],
        "confidence": round(float(best.get("confidence") or 0), 3),
        "evidence": str(best.get("evidence") or ""),
        "note": str(best.get("note") or ""),
        "parser_version": FIXED_SOURCE_PARSER_VERSION,
        "source_mode": "auto_fixed_source",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    data[canonical] = row
    save_known_release_status(data)
    return row


def brave_candidates(title: str, media_type: str = "tv") -> list[dict[str, Any]]:
    key = brave_key()
    if not key:
        return []
    if media_type == "variety":
        queries = [
            f'"{title}" 综艺 共多少期',
            f'"{title}" 综艺 一共多少期',
            f'"{title}" 节目 共多少期',
            f'"{title}" 百度百科 期数',
            f'site:baike.baidu.com "{title}" 期数',
            f'"{title}" 猫眼 期数',
            f'"{title}" "全" "期"',
            f'"{title}" "期数:"',
        ]
    else:
        queries = [
            f'"{title}" 电视剧 总集数',
            f'"{title}" 一共多少集',
            f'"{title}" 共多少集',
            f'"{title}" 猫眼 集数',
            f'site:piaofang.maoyan.com/tv "{title}" 集数',
            f'"{title}" 百度百科 集数',
            f'site:baike.baidu.com "{title}" 集数',
            f'"{title}" "集数:"',
        ]
    out: list[dict[str, Any]] = []
    target = clean_for_match(title)
    for query in queries:
        url = "https://api.search.brave.com/res/v1/web/search?q=" + urllib.parse.quote(query) + "&count=8&country=CN&search_lang=zh-hans"
        try:
            data = request_json(url, headers={"Accept": "application/json", "X-Subscription-Token": key}, timeout=12)
        except Exception:
            continue
        for item in (data.get("web", {}).get("results") or []):
            title_text = item.get("title") or ""
            desc = item.get("description") or ""
            page_url = item.get("url") or ""
            if not fixed_source_url_valid(page_url):
                continue
            combined = f"{title_text} {desc} {page_url}"
            if target and target not in clean_for_match(combined):
                continue
            # 验证百度百科 URL 条目标题是否匹配目标剧名
            # （防止搜索片段提取到同名演员/无关页面的百科数据，如搜"良陈美锦"拿到"顾德昭"百科中的36集）
            if not _baike_page_url_valid(page_url, title):
                continue
            for candidate in extract_totals_from_text(combined, page_url):
                candidate = dict(candidate)
                candidate["origin"] = "brave"
                out.append(candidate)
    return out


def note_candidates(note: str, source_path: str) -> list[dict[str, Any]]:
    text = f"{note or ''} {source_path or ''}"
    out = []
    for m in COMPLETE_PATTERNS[:2]:
        for match in m.finditer(text):
            n = parse_int(match.group(1))
            if n:
                out.append({"total": n, "confidence": 0.74, "source": "resource-title", "origin": "resource_title", "ongoing_hint": False, "completed_hint": True, "text": text[:300]})
    return out


def known_total_candidates(title: str, note: str, source_path: str, media_type: str) -> list[dict[str, Any]]:
    data = load_known_totals()
    if not isinstance(data, dict):
        return []
    haystack = clean_for_match(" ".join(x for x in (title, note, source_path) if x))
    out: list[dict[str, Any]] = []
    for canonical, row in data.items():
        if not isinstance(row, dict):
            continue
        row_type = str(row.get("media_type") or "tv").lower()
        if row_type and media_type and row_type != media_type:
            continue
        names = [canonical] + list(row.get("aliases") or [])
        if not any(
            (clean_for_match(name) and clean_for_match(name) in haystack)
            or names_match_title(str(name or ""), title)
            for name in names
        ):
            continue
        total = parse_int(row.get("total_episodes"))
        if not total:
            continue
        status_hint = str(row.get("status_hint") or "").strip().lower()
        try:
            confidence = float(row.get("confidence"))
        except Exception:
            confidence = 0.0
        if confidence <= 0:
            if status_hint == "completed":
                confidence = 0.78
            elif status_hint == "ongoing":
                confidence = 0.72
            else:
                # Legacy cache rows only record "a total exists"; they should not
                # override fresher web signals about whether a show is still airing.
                confidence = 0.62
        out.append({
            "total": total,
            "confidence": confidence,
            "source": row.get("evidence") or f"local-known-total:{canonical}",
            "origin": "known_total",
            "ongoing_hint": status_hint == "ongoing",
            "completed_hint": status_hint == "completed",
            "text": canonical,
        })
    return out


def known_release_override(
    title: str,
    note: str,
    source_path: str,
    media_type: str,
    current: int = 0,
) -> dict[str, Any] | None:
    data = load_known_release_status()
    if not isinstance(data, dict):
        return None
    haystack = clean_for_match(" ".join(x for x in (title, note, source_path) if x))
    best: dict[str, Any] | None = None
    best_match_len = -1
    for canonical, row in data.items():
        if not isinstance(row, dict):
            continue
        row_type = str(row.get("media_type") or "tv").lower()
        if row_type and media_type and row_type != media_type:
            continue
        names = [canonical] + list(row.get("aliases") or [])
        for name in names:
            key = clean_for_match(name)
            if not key or (key not in haystack and not names_match_title(str(name or ""), title)):
                continue
            if len(key) > best_match_len:
                best = dict(row)
                best["canonical_title"] = canonical
                best_match_len = len(key)
            break
    if best:
        if release_source_mode(best) != "auto_fixed_source":
            return None
        if fixed_source_parser_version(best) != FIXED_SOURCE_PARSER_VERSION:
            return None
        evidence = str(best.get("evidence") or "").strip()
        if evidence.startswith(("http://", "https://")) and not fixed_source_url_valid(evidence):
            return None
        status = str(best.get("status") or "").strip().lower()
        updated_at = parse_iso(best.get("updated_at"))
        ttl_days = FIXED_STATUS_TTL_DAYS.get(status, 7)
        if not updated_at:
            return None
        now = datetime.now().astimezone()
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=now.tzinfo)
        if now - updated_at > timedelta(days=ttl_days):
            return None
        total = parse_int(best.get("total_episodes"))
        if current and total and current > total:
            return None
        # 如果缓存中有 ongoing 状态但没有总集数，强制刷新以便用最新域名评分重新抓取
        if status == "ongoing" and total is None:
            return None
    return best


def title_aliases(title: str, note: str, source_path: str) -> list[str]:
    raw_values = [title, note]
    aliases: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        normalized = normalize_title(value or "")
        if re.fullmatch(r"season\s*\d+", normalized, re.I):
            continue
        if not normalized:
            continue
        key = clean_for_match(normalized)
        if not key or key in seen:
            continue
        seen.add(key)
        aliases.append(normalized)
    return aliases


def save_known_total(
    title: str,
    note: str,
    source_path: str,
    media_type: str,
    total: int | None,
    evidence: str,
    origin: str,
    confidence: float,
    status_hint: str = "",
) -> None:
    total = parse_int(total)
    status_hint = str(status_hint or "").strip().lower()
    min_confidence = 0.64 if status_hint == "ongoing" else 0.78
    if not total or confidence < min_confidence:
        return
    if origin in {"provided_total", "resource_title"}:
        return

    aliases = title_aliases(title, note, source_path)
    if not aliases:
        return

    canonical = aliases[0]
    try:
        data = load_known_totals()
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}

    existing_key = None
    alias_keys = {clean_for_match(alias) for alias in aliases}
    for key, row in data.items():
        if not isinstance(row, dict):
            continue
        names = [key] + list(row.get("aliases") or [])
        if any(clean_for_match(name) in alias_keys or any(names_match_title(str(name or ""), alias) for alias in aliases) for name in names):
            existing_key = key
            break

    if existing_key is not None:
        row = data.get(existing_key)
        if not isinstance(row, dict):
            row = {}
    else:
        row = {}

    merged_aliases: list[str] = []
    seen: set[str] = set()
    for value in aliases + list(row.get("aliases") or []):
        norm = normalize_title(str(value or ""))
        if re.fullmatch(r"season\s*\d+", norm, re.I):
            continue
        if not re.fullmatch(r"[0-9A-Za-z\u4e00-\u9fff·\s]+", norm):
            continue
        key = clean_for_match(norm)
        if not norm or not key or key == clean_for_match(canonical) or key in seen:
            continue
        seen.add(key)
        merged_aliases.append(norm)

    new_row = {
        "total_episodes": total,
        "media_type": media_type or "tv",
        "aliases": merged_aliases,
        "evidence": evidence or f"auto-cached:{canonical} total={total}",
        "confidence": round(confidence, 3),
    }
    if status_hint in {"ongoing", "completed"}:
        # 保护：不允许 ongoing 覆盖已有 completed 状态（仅当已有条目有明确 confidence 评分时）
        # 没有 confidence 的条目可能是旧代码/手动创建的，允许新数据覆盖
        existing_status = str(row.get("status_hint") or "").strip().lower()
        existing_confidence = row.get("confidence")
        if status_hint == "ongoing" and existing_status == "completed" and existing_confidence is not None:
            # 已有 completed 且评分可靠，不降级为 ongoing（防止固定来源误判污染缓存）
            new_row["status_hint"] = "completed"
        else:
            new_row["status_hint"] = status_hint

    if existing_key is not None and existing_key != canonical:
        data.pop(existing_key, None)
    data[canonical] = new_row
    KNOWN_TOTALS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def candidate_origin(candidate: dict[str, Any]) -> str:
    origin = str(candidate.get("origin") or "").strip()
    if origin in ORIGIN_PRIORITY:
        return origin
    source = str(candidate.get("source") or "").lower()
    if source.startswith("tmdb:"):
        return "tmdb"
    if any(domain in source for domain in ("maoyan.com", "baike.baidu.com", "douban.com", "tvmao.com", "mtime.com")):
        return "brave"
    if "baidu.com" in source or "sogou.com" in source:
        return "html_search"
    if source == "resource-title":
        return "resource_title"
    return "web"


def candidate_effective_confidence(candidate: dict[str, Any]) -> float:
    base = float(candidate.get("confidence", 0))
    origin = candidate_origin(candidate)
    bonus = ORIGIN_CONFIDENCE_BONUS.get(origin, 0.0)
    return max(0.0, min(0.98, base + bonus))


def choose_candidate(candidates: list[dict[str, Any]], current: int) -> dict[str, Any] | None:
    if not candidates:
        return None
    # Prefer high confidence and authoritative totals. If several sources disagree,
    # random streaming sites should not beat Maoyan/Baike/Douban-style metadata.
    buckets: dict[int, list[dict[str, Any]]] = {}
    for c in candidates:
        try:
            buckets.setdefault(int(c.get("total", 0)), []).append(c)
        except Exception:
            pass
    collapsed = []
    for total, items in buckets.items():
        best = max(
            items,
            key=lambda c: (
                candidate_effective_confidence(c),
                ORIGIN_PRIORITY.get(candidate_origin(c), 0),
                float(c.get("confidence", 0)),
            ),
        )
        support_bonus = min(0.08, 0.02 * (len(items) - 1))
        best = dict(best)
        best["origin"] = candidate_origin(best)
        best["confidence"] = min(0.96, candidate_effective_confidence(best) + support_bonus)
        best["support_count"] = len(items)
        collapsed.append(best)
    collapsed.sort(
        key=lambda c: (
            int(int(c.get("total", 0)) >= current),
            float(c.get("confidence", 0)),
            ORIGIN_PRIORITY.get(candidate_origin(c), 0),
            int(int(c.get("total", 0)) == current),
            -abs(int(c.get("total", 0)) - current) if current else -int(c.get("total", 0)),
        ),
        reverse=True,
    )
    return collapsed[0] if collapsed else None


def decision_from_total(
    title: str,
    media_type: str,
    current: int,
    total_value: int,
    candidate: dict[str, Any],
    ongoing_hint: bool = False,
) -> dict[str, Any]:
    total: int | None = int(total_value)
    confidence = float(candidate.get("confidence", 0))
    completed_hint = bool(candidate.get("completed_hint"))
    ongoing_hint = bool(candidate.get("ongoing_hint")) or ongoing_hint
    origin = candidate_origin(candidate)

    if current <= 0:
        status = "ongoing"
        reason = f"已查到总集数 {total} 集，但当前资源集数未知，默认追剧"
    elif total < current:
        # web查到总集数小于实际资源集数，分情况处理：
        # - web总集数和实际集数接近（>=85%）→ 大概率是已完结，多余的是花絮/SP
        # - web总集数远小于实际集数 → web数据可能确实有误，清空total继续
        if current > 0 and total / current >= 0.85:
            status = "completed"
            reason = f"集数完整 ({current}/{total})，web总集数{total}与实际{current}接近，多余文件可能为花絮/SP"
            total = total  # 保留web总集数
        else:
            status = "ongoing"
            reason = f"总集数疑似错误：互联网查到 {total} 集，但资源已有 {current} 集，继续追剧"
            total = None
    elif current < total:
        # A total episode count only proves the planned/full count.  It is not
        # completion evidence by itself.  When the source has fewer episodes
        # than the total, only mark it as missing-source if a trusted source
        # explicitly says the show is completed.  Otherwise keep tracking so new
        # episodes can be picked up as the source updates.
        suspicious_large_total = ongoing_hint and total >= max(100, current * 3)
        if suspicious_large_total:
            if media_type == "anime":
                status = "ongoing"
                reason = f"总集数疑似错误：互联网查到 {total} 集，但当前资源为 {current} 集且搜索结果显示仍在更新，继续追剧"
                total = None
            else:
                status = "needs_total"
                reason = f"总集数疑似包含多季/合集口径：互联网查到 {total} 集，但当前资源为 {current} 集，需重新确认总集数"
        elif completed_hint and not ongoing_hint:
            # 明确已完结但源不全，才判定为缺源
            status = "needs_recovery"
            reason = f"已确认完结（共{total}集），当前资源仅 {current} 集，源不全待修复"
        elif ongoing_hint and completed_hint:
            # 同时有更新和完结信号（矛盾），优先信任更新信号继续追剧
            status = "ongoing"
            reason = f"更新中 ({current}/{total}集)，信号矛盾但优先追剧"
        else:
            # 没有明确完结信号时，不再把 current/total 比例当作完结证据。
            status = "ongoing"
            reason = f"未确认完结，当前资源 {current}/{total} 集，继续追剧等待更新"
    else:
        if ongoing_hint and not completed_hint:
            status = "ongoing"
            reason = f"资源/搜索结果显示仍在更新，当前 {current}/{total} 集，继续追剧"
        else:
            status = "completed"
            reason = f"集数完整 ({current}/{total})"

    return {
        "title": title,
        "media_type": media_type,
        "current_episodes": current,
        "total_episodes": total,
        "status": status,
        "track_reason": reason,
        "confidence": round(confidence, 3),
        "evidence": candidate.get("source"),
        "origin": origin,
    }


def decision_from_release_override(
    title: str,
    media_type: str,
    current: int,
    override: dict[str, Any],
) -> dict[str, Any]:
    status = str(override.get("status") or "").strip().lower()
    total = parse_int(override.get("total_episodes"))
    exact_total = bool(override.get("exact_total"))
    evidence = str(override.get("evidence") or f"release-override:{title}").strip()
    note = str(override.get("note") or "").strip()
    confidence = float(override.get("confidence") or 0.99)
    candidate = {
        "total": total or current or 1,
        "confidence": confidence,
        "source": evidence,
        "origin": "fixed_source_cache",
        "ongoing_hint": status == "ongoing",
        "completed_hint": status == "completed",
        "text": title,
    }
    if status == "ongoing":
        if total and exact_total:
            result = decision_from_total(title, media_type, current, total, candidate, ongoing_hint=True)
        else:
            reason = note or "人工校验：仍在更新，继续追剧"
            result = {
                "title": title,
                "media_type": media_type,
                "current_episodes": current,
                "total_episodes": None,
                "status": "ongoing",
                "track_reason": reason,
                "confidence": round(confidence, 3),
                "evidence": evidence,
                "origin": "fixed_source_cache",
            }
        if note:
            result["track_reason"] = note
        return result
    if status == "completed":
        if total:
            result = decision_from_total(title, media_type, current, total, candidate, ongoing_hint=False)
        else:
            result = {
                "title": title,
                "media_type": media_type,
                "current_episodes": current,
                "total_episodes": None,
                "status": "completed",
                "track_reason": note or "人工校验：已完结",
                "confidence": round(confidence, 3),
                "evidence": evidence,
                "origin": "fixed_source_cache",
            }
        if note and result.get("status") == "ongoing":
            result["track_reason"] = note
        return result
    return {
        "title": title,
        "media_type": media_type,
        "current_episodes": current,
        "total_episodes": total,
        "status": "needs_total",
        "track_reason": note or "状态覆盖缺少明确完结/连载结论",
        "confidence": round(confidence, 3),
        "evidence": evidence,
        "origin": "fixed_source_cache",
    }


def cacheable_status_hint(candidate: dict[str, Any], result: dict[str, Any], has_update_marker: bool) -> str:
    status = str(result.get("status") or "").strip().lower()
    if status == "completed":
        return "completed"
    if status == "ongoing":
        if bool(candidate.get("ongoing_hint")) or has_update_marker:
            return "ongoing"
        if result.get("total_episodes") is None:
            return "ongoing"
    return ""


def _correct_ongoing_misjudgment(
    result: dict[str, Any],
    current: int,
    has_update_marker: bool,
) -> dict[str, Any]:
    """Keep an ongoing decision unless there is explicit completion evidence.

    A current/total ratio (32/42, 41/42, etc.) is not completion evidence; it can
    simply mean the platform/source is still airing.  Missing-source recovery is
    decided earlier only when a trusted candidate explicitly carries
    completed_hint=True and ongoing_hint=False.
    """
    return result


def decide(args: argparse.Namespace) -> dict[str, Any]:
    config = load_config()
    media_type = (args.media_type or "tv").strip().lower()
    current = parse_int(args.current) or 0
    provided_total = parse_int(args.provided_total)
    note = args.note or ""
    source_path = args.source_path or ""
    title = lookup_title_for_source(args.title or args.note or "", note, source_path)
    text_context = f"{note} {source_path}"
    has_update_marker = bool(UPDATE_RE.search(text_context) or UPDATE_TAIL_RE.search(text_context) or UPDATE_CHANNEL_RE.search(text_context))

    release_override = known_release_override(title, note, source_path, media_type, current=current)
    if not release_override and current > 0 and media_type != "movie":
        release_override = refresh_known_release_status(
            title,
            media_type,
            note,
            source_path,
            current=current,
        )

    # 校验：如果 release_override 判定为 ongoing，但 known_totals 中有匹配的总集数
    # 且 current >= known_total，说明集数已完整，固定来源的 ongoing 是误判
    # （老剧页面常有"更新至XX集"字样被误匹配为 ongoing）
    if release_override and str(release_override.get("status") or "").lower() == "ongoing":
        known_check = known_total_candidates(title, note, source_path, media_type)
        known_check_best = choose_candidate(known_check, current)
        if known_check_best:
            known_check_total = parse_int(known_check_best.get("total")) or 0
            known_completed = bool(known_check_best.get("completed_hint"))
            known_ongoing = bool(known_check_best.get("ongoing_hint"))
            known_conf = float(known_check_best.get("confidence", 0))
            if known_check_total > 0 and not known_ongoing:
                if current >= known_check_total and (known_completed or known_conf >= 0.70):
                    # 集数完整，忽略 release_override 的 ongoing 误判
                    release_override = None
                elif known_completed and known_conf >= 0.85:
                    # 明确标记为 completed 但集数不全（老剧源残缺），
                    # 忽略 release_override 的 ongoing 误判，走 known_total 逻辑
                    release_override = None

    if release_override:
        result = decision_from_release_override(title, media_type, current, release_override)
        # 当 release_override 判定为 ongoing 但没有总集数时，逐级补充：
        # 1. 先查 known_totals（本地缓存，快）
        # 2. 如果 known_totals 的总数来自低权威来源（聚合器），也联网交叉验证
        # 3. 如果还没有，查 tmdb + brave + html_search（网络搜索，慢但全）
        known_fill: list[dict[str, Any]] = []
        fallback_candidates: list[dict[str, Any]] = []
        if result.get("total_episodes") is None and result.get("status") == "ongoing":
            # 第一级：known_totals 本地缓存
            known_fill = known_total_candidates(title, note, source_path, media_type)
            known_fill_best = choose_candidate(known_fill, current)
            filled_from_known = False
            filled_source_type = ""
            if known_fill_best:
                fill_total = parse_int(known_fill_best.get("total"))
                fill_conf = float(known_fill_best.get("confidence", 0))
                if fill_total and fill_total >= current and fill_conf >= 0.70:
                    result["total_episodes"] = fill_total
                    if current > 0:
                        result["track_reason"] = f"更新中 ({current}/{fill_total}集)"
                    else:
                        result["track_reason"] = f"更新中，共{fill_total}集"
                    if not result.get("evidence") or result.get("evidence") == f"release-override:{title}":
                        result["evidence"] = known_fill_best.get("source") or result.get("evidence")
                    filled_from_known = True
                    filled_source_type = _classify_source(str(known_fill_best.get("source") or ""))

            # 第二级：如果 known_totals 没有，或总数来自低权威来源（聚合器），联网交叉验证
            # 聚合器的数据可能滞后，百科和官方平台的数据更可靠
            need_web_crosscheck = (
                not filled_from_known
                or filled_source_type in ("aggregator", "other")
            )
            if result.get("total_episodes") is None or (
                need_web_crosscheck and media_type != "movie"
            ):
                fallback_candidates.extend(tmdb_candidates(title, config))
                fallback_candidates.extend(brave_candidates(title, media_type))
                fallback_candidates.extend(html_search_candidates(title, media_type))
                fallback_best = choose_candidate(fallback_candidates, current)
                if fallback_best:
                    fb_total = parse_int(fallback_best.get("total"))
                    fb_conf = float(fallback_best.get("confidence", 0))
                    fb_source = str(fallback_best.get("source") or "")
                    fb_source_type = _classify_source(fb_source)
                    # 使用网络搜索结果的条件：
                    # - known_totals 没有找到（result total 仍为 None），且网络结果可靠
                    # - 或网络搜索来源比 known_totals 来源更权威
                    use_web_result = False
                    if result.get("total_episodes") is None:
                        use_web_result = bool(fb_total and fb_total >= current and fb_conf >= 0.58)
                    elif fb_total and fb_total != result.get("total_episodes") and fb_total >= current and fb_conf >= 0.58:
                        SOURCE_RANK = {"encyclopedia": 3, "official_platform": 2, "aggregator": 1, "other": 0}
                        if SOURCE_RANK.get(fb_source_type, 0) > SOURCE_RANK.get(filled_source_type, 0):
                            use_web_result = True
                    if use_web_result:
                        result["total_episodes"] = fb_total
                        if current > 0:
                            result["track_reason"] = f"更新中 ({current}/{fb_total}集)"
                        else:
                            result["track_reason"] = f"更新中，共{fb_total}集"
                        result["evidence"] = fb_source or result.get("evidence")
                        # 回写到 known_totals 缓存
                        save_known_total(
                            title=title,
                            note=note,
                            source_path=source_path,
                            media_type=media_type,
                            total=fb_total,
                            evidence=fb_source,
                            origin=candidate_origin(fallback_best),
                            confidence=fb_conf,
                            status_hint="ongoing",
                        )

        # fill logic 填充完 total_episodes 后，检查网页搜索候选是否
        # 显示"已完结"信号。如果多个来源一致显示已完结（而非"更新中"），
        # 用 decision_from_total 重新判定，覆盖 release_override 的 ongoing 误判。
        # 这解决了中间比例（25%-95%）的剧集无法通过比例校正的问题。
        if result.get("status") == "ongoing" and result.get("total_episodes"):
            fb_total = result["total_episodes"]
            # 从所有已搜集的候选中收集 completed_hint=True 且 ongoing_hint=False 的条目
            completed_signal_candidates: list[dict[str, Any]] = []
            all_check_lists = [known_fill, fallback_candidates]

            # 如果 fill logic 被跳过（total 已由 fixed_source 提供），
            # fallback_candidates 和 known_fill 都为空，需要主动联网搜索来验证完结状态。
            # 固定来源（如 baike）可能滞后（仍显示"更新至14集"但实际已完结）。
            if not fallback_candidates and not known_fill:
                fallback_candidates.extend(brave_candidates(title, media_type))
                fallback_candidates.extend(html_search_candidates(title, media_type))

            for c_list in all_check_lists:
                for c in c_list:
                    c_total = parse_int(c.get("total"))
                    if (
                        bool(c.get("completed_hint"))
                        and not bool(c.get("ongoing_hint"))
                        and c_total
                        and c_total > 0
                        and abs(c_total - fb_total) <= max(2, fb_total * 0.1)
                    ):
                        completed_signal_candidates.append(c)

            # 至少 2 个独立来源确认已完结，才覆盖 fixed_source 的 ongoing 判定
            if len(completed_signal_candidates) >= 2:
                best_completed = max(
                    completed_signal_candidates,
                    key=lambda c: float(c.get("confidence", 0)),
                )
                re_eval = decision_from_total(
                    title, media_type, current, fb_total,
                    best_completed, has_update_marker,
                )
                if re_eval.get("status") != "ongoing":
                    result = re_eval
                    # 保留更详细的 evidence
                    if not result.get("evidence") or result.get("evidence") == f"release-override:{title}":
                        result["evidence"] = best_completed.get("source") or result.get("evidence")
        total_for_cache = result.get("total_episodes")
        if total_for_cache:
            save_known_total(
                title=title,
                note=note,
                source_path=source_path,
                media_type=media_type,
                total=total_for_cache,
                evidence=str(result.get("evidence") or ""),
                origin=str(result.get("origin") or ""),
                confidence=float(result.get("confidence") or 0),
                status_hint=cacheable_status_hint(
                    {
                        "ongoing_hint": result.get("status") == "ongoing",
                        "completed_hint": result.get("status") == "completed",
                    },
                    result,
                    has_update_marker,
                ),
            )
        result = _correct_ongoing_misjudgment(result, current, has_update_marker)
        result["source_trace"] = [
            _build_source_trace_entry("fixed_source_cache", [release_override]),
            f"decision:fixed_source_cache total={result.get('total_episodes') or ''} status={result.get('status') or ''}",
        ]
        return result

    if media_type == "movie":
        return {
            "title": title,
            "media_type": media_type,
            "current_episodes": current or 1,
            "total_episodes": 1,
            "status": "completed",
            "track_reason": "电影，不需要追剧",
            "confidence": 1.0,
            "evidence": "media_type=movie",
            "origin": "movie",
            "source_trace": ["movie:shortcut"],
        }

    known_candidates = known_total_candidates(title, note, source_path, media_type)
    known_best = choose_candidate(known_candidates, current)
    known_total_value = int(known_best["total"]) if known_best and known_best.get("total") else 0
    # 只有当集数确实完整（current>=total）或未知当前集数时才允许短路。
    # 对于 current<total 的情况，即使 known_totals 标记为 completed 也必须
    # 走完整网络搜索验证，防止脏数据（如新剧被错误标记为 completed）导致误判。
    can_short_circuit_known = bool(
        known_best
        and float(known_best.get("confidence", 0)) >= 0.85
        and (current <= 0 or current >= known_total_value)
    )
    if can_short_circuit_known:
        result = decision_from_total(title, media_type, current, int(known_best["total"]), known_best, has_update_marker)
        save_known_total(
            title=title,
            note=note,
            source_path=source_path,
            media_type=media_type,
            total=result.get("total_episodes"),
            evidence=str(result.get("evidence") or ""),
            origin=str(result.get("origin") or ""),
            confidence=float(result.get("confidence") or 0),
            status_hint=cacheable_status_hint(known_best, result, has_update_marker),
        )
        result = _correct_ongoing_misjudgment(result, current, has_update_marker)
        result["source_trace"] = [
            _build_source_trace_entry("known_total", known_candidates),
            "decision:trusted-known-total",
        ]
        return result

    state_candidates = existing_state_candidates(title, media_type)
    note_based_candidates = note_candidates(note, source_path)
    tmdb_result_candidates = tmdb_candidates(title, config)
    douban_result_candidates = douban_candidates(title, media_type)
    brave_result_candidates = brave_candidates(title, media_type)
    html_result_candidates = html_search_candidates(title, media_type)

    source_trace = [
        _build_source_trace_entry("existing_state", state_candidates),
        _build_source_trace_entry("known_total", known_candidates),
        _build_source_trace_entry("resource_title", note_based_candidates),
        _build_source_trace_entry("tmdb", tmdb_result_candidates),
        _build_source_trace_entry("douban", douban_result_candidates),
        _build_source_trace_entry("brave", brave_result_candidates),
        _build_source_trace_entry("html_search", html_result_candidates),
    ]

    candidates: list[dict[str, Any]] = []
    candidates.extend(state_candidates)
    candidates.extend(known_candidates)
    candidates.extend(note_based_candidates)
    candidates.extend(tmdb_result_candidates)
    candidates.extend(douban_result_candidates)
    candidates.extend(brave_result_candidates)
    candidates.extend(html_result_candidates)

    # Model/user-provided totals are only accepted after web-style candidates fail,
    # and never when they are smaller than the selected resource count.
    if provided_total and provided_total >= current and not has_update_marker:
        source_trace.append(f"provided_total:1 [{provided_total}]")
        candidates.append({
            "total": provided_total,
            "confidence": 0.45,
            "source": "provided-total-low-trust",
            "origin": "provided_total",
            "ongoing_hint": False,
            "completed_hint": False,
            "text": "provided by caller",
        })

    best = choose_candidate(candidates, current)
    best_ongoing = choose_candidate([c for c in candidates if bool(c.get("ongoing_hint"))], current)
    if (
        best
        and best_ongoing
        and not bool(best.get("ongoing_hint"))
        and current < int(best.get("total", 0) or 0)
        and float(best_ongoing.get("confidence", 0)) >= 0.6
        and candidate_origin(best_ongoing) != "html_search"
    ):
        ongoing_result = decision_from_total(title, media_type, current, int(best_ongoing["total"]), best_ongoing, has_update_marker)
        if ongoing_result.get("status") == "ongoing":
            best = best_ongoing
    if not best or float(best.get("confidence", 0)) < 0.58:
        if current > 0:
            # 有在播集数但此刻查不到权威总集数(典型:首播当天豆瓣/TMDB 尚未收录)。
            # 先按在播纳入追剧、total 留空,交给 reconcile/cron 每日自动重查补全,
            # 而不是直接 needs_total 卡住入库——总集数迟早查得到,不该阻塞。
            return {
                "title": title,
                "media_type": media_type,
                "current_episodes": current,
                "total_episodes": None,
                "status": "ongoing",
                "track_reason": f"总集数暂未查到，已先纳入追剧（当前 {current} 集），后续自动重查补全",
                "confidence": float(best.get("confidence", 0)) if best else 0.0,
                "evidence": best.get("source") if best else "pending-recheck",
                "origin": candidate_origin(best) if best else "none",
                "total_pending": True,
                "source_trace": source_trace,
            }
        return {
            "title": title,
            "media_type": media_type,
            "current_episodes": current,
            "total_episodes": None,
            "status": "needs_total",
            "track_reason": "未能从互联网确认总集数，且无在播集数可依据，禁止自动判定",
            "confidence": float(best.get("confidence", 0)) if best else 0.0,
            "evidence": best.get("source") if best else "no-total-found",
            "origin": candidate_origin(best) if best else "none",
            "source_trace": source_trace,
        }
    result = decision_from_total(title, media_type, current, int(best["total"]), best, has_update_marker)
    save_known_total(
        title=title,
        note=note,
        source_path=source_path,
        media_type=media_type,
        total=result.get("total_episodes"),
        evidence=str(result.get("evidence") or ""),
        origin=str(result.get("origin") or ""),
        confidence=float(result.get("confidence") or 0),
        status_hint=cacheable_status_hint(best, result, has_update_marker),
    )
    result = _correct_ongoing_misjudgment(result, current, has_update_marker)
    source_trace.append(
        f"decision:{candidate_origin(best)} total={result.get('total_episodes') or ''} status={result.get('status') or ''}"
    )
    result["source_trace"] = source_trace
    return result


def emit_shell(result: dict[str, Any]) -> None:
    pairs = {
        "DECISION_TITLE": result.get("title") or "",
        "DECISION_MEDIA_TYPE": result.get("media_type") or "tv",
        "DECISION_STATUS": result.get("status") or "needs_total",
        "DECISION_TOTAL": "" if result.get("total_episodes") is None else str(result.get("total_episodes")),
        "DECISION_REASON": result.get("track_reason") or "",
        "DECISION_CONFIDENCE": str(result.get("confidence") or 0),
        "DECISION_EVIDENCE": result.get("evidence") or "",
        "DECISION_ORIGIN": result.get("origin") or "",
        "DECISION_SOURCE_TRACE": json.dumps(result.get("source_trace") or [], ensure_ascii=False),
    }
    for key, value in pairs.items():
        print(f"{key}={shlex.quote(str(value))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="")
    ap.add_argument("--media-type", default="tv")
    ap.add_argument("--current", default="0")
    ap.add_argument("--provided-total", default="")
    ap.add_argument("--note", default="")
    ap.add_argument("--source-path", default="")
    ap.add_argument("--shell", action="store_true")
    args = ap.parse_args()
    result = decide(args)
    if args.shell:
        emit_shell(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
