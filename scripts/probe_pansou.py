#!/usr/bin/env python3
"""probe_pansou.py v2.0 - 验证网盘资源真实集数和大小 (修复版)
修复内容:
1. 导入路径不用 TempCheck 前缀，直接用清理后的剧名（实测成功率更高）
2. 智能扫描各网盘根目录，不依赖硬编码路径
3. 增加轮询等待机制，网盘扫描慢时自动重试
4. 按文件数量排序展示结果；集数相同优先 UC
"""
import json
import os
import re
import urllib.request
import time
import sys
import subprocess
import threading
import concurrent.futures
from pathlib import Path

from gbox_share_resolver import resolve_share_mount_path
from gbox_share_resolver import fetch_gbox_shares, row_matches_share_id

# Config
CONFIG_FILE = Path(__file__).resolve().parent.parent / "config" / "media-config.json"
try:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        _cfg = json.load(f)
except Exception:
    _cfg = {}

GBOX_URL = (_cfg.get("gbox", {}).get("internal_url") or "http://YOUR_NAS_IP:4567").rstrip("/")
GBOX_USER = _cfg.get("gbox", {}).get("username") or "admin"
GBOX_PASS = _cfg.get("gbox", {}).get("password") or "admin"
_XIAOYA = next((s for s in _cfg.get("sources", []) if s.get("name") == "xiaoya"), {})
ALIST_BASE = (_XIAOYA.get("internal_url") or "http://YOUR_NAS_IP:5678").rstrip("/")
ALIST_URL = f"{ALIST_BASE}/api/fs/list"
ALIST_GET_URL = f"{ALIST_BASE}/api/fs/get"

# 网盘根目录映射
ROOTS_MAP = {
    7: "/🍓我的UC分享",
    5: "/🍊我的夸克分享",
    0: "/🍑我的阿里分享",
    8: "/🏷️我的115分享",
    2: "/🍒我的迅雷分享"
}

DEFAULT_MAX_CANDIDATES = int(os.environ.get("PANSOU_PROBE_MAX_CANDIDATES", "8"))
DEFAULT_FOLDER_WAIT = int(os.environ.get("PANSOU_PROBE_FOLDER_WAIT", "8"))
DEFAULT_GLOBAL_TIMEOUT = int(os.environ.get("PANSOU_PROBE_GLOBAL_TIMEOUT", "75"))
FAST_UNCONFIRMED_UC = str(os.environ.get("PANSOU_FAST_UNCONFIRMED_UC", "1")).lower() in ("1", "true", "yes")
STOP_AFTER_GOOD_COUNT = int(os.environ.get("PANSOU_PROBE_STOP_AFTER_GOOD_COUNT", "20"))
DEEP_CLEANUP_STALE = str(os.environ.get("PANSOU_PROBE_DEEP_CLEANUP_STALE", "")).lower() in ("1", "true", "yes")
AUTO_CLEAN_PROBE_MOUNTS = str(os.environ.get("PANSOU_KEEP_PROBE_MOUNTS", "")).lower() not in ("1", "true", "yes")
VALIDATE_PLAYABLE = str(os.environ.get("PANSOU_VALIDATE_PLAYABLE", "0")).lower() in ("1", "true", "yes")
UC_SHARE_ID_PASSWORD_FALLBACK = str(os.environ.get("PANSOU_UC_SHARE_ID_PASSWORD_FALLBACK", "")).lower() in ("1", "true", "yes")
PROBE_MOUNTS_FILE = "/tmp/probe_mounts.json"
# 探路并发数:GBox 并发太高会限流，保守默认 3 路。
PROBE_CONCURRENCY = int(os.environ.get("PANSOU_PROBE_CONCURRENCY", "3"))
STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "drama-state.json"
GBOX_MOUNT_CLEANUP_SCRIPT = Path(__file__).resolve().parents[3] / "devops" / "gbox-strm-mount-sync" / "scripts" / "gbox-dedupe-mounts.py"
PROMO_KEYWORDS = ("预告", "花絮", "定档", "片花", "花絮", "trailer", "teaser", "preview", "behind")
EPISODE_PATTERNS = (
    r"S\d{1,2}E\d{1,3}",
    r"(?:^|[^\d])E\d{1,3}(?:[^\d]|$)",
    r"(?:^|[^\d])第?\d{1,3}[集话](?:[^\d]|$)",
    r"^\d{1,3}(?:\D|$)",
)

def looks_like_episode(name):
    text = str(name or "")
    low = text.lower()
    if any(k in low for k in PROMO_KEYWORDS):
        return any(re_search(pattern, text) for pattern in EPISODE_PATTERNS)
    return True

def re_search(pattern, text):
    import re
    return re.search(pattern, text, re.I)

def login_gbox():
    data = json.dumps({"username": GBOX_USER, "password": GBOX_PASS}).encode()
    req = urllib.request.Request(f"{GBOX_URL}/api/accounts/login", data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            token = result.get("token")
            if not token:
                print(f"  ⚠️ GBox 登录响应无 token: {result}")
                return None
            return token
    except Exception as e:
        print(f"  ❌ GBox 登录失败: {e}")
        return None

def snapshot_share_ids(token):
    """Return the share IDs visible in GBox before/after an import attempt."""
    try:
        rows = fetch_gbox_shares(GBOX_URL, token)
    except Exception:
        return set()
    ids = set()
    for row in rows:
        try:
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
        except Exception:
            payload = str(row)
        for match in re.findall(r"[0-9a-f]{12,}", payload, flags=re.I):
            ids.add(match.lower())
    return ids

def import_share(token, link, password, gbox_type, share_name):
    """导入网盘分享，返回 (share_id, clean_name, aliases)。"""
    gbox_type = normalize_gbox_type(gbox_type, link)
    try:
        share_id = link.split("/s/")[1].split("?")[0]
    except:
        return None, None, []
    
    # 清理名称：GBox import 会按空白拆字段，挂载名必须不含空格/TAB。
    clean_name = ""
    for c in share_name:
        if c.isspace():
            clean_name += "_"
        elif c.isalnum() or c in "-_":
            clean_name += c
        elif c in ".·":
            clean_name += "_"
    clean_name = clean_name.strip()[:32]
    clean_name = re.sub(r"_+", "_", clean_name).strip("_")
    if not clean_name:
        clean_name = "probe_" + share_id[:8]
    base_name = clean_name
    # 同名剧集常常有多个分享链接；挂载名必须带 share_id，避免后续候选
    # 反复命中第一个同名挂载而没有真正核验。
    clean_name = f"{clean_name}_{share_id[:6]}"[:40]
    # UC/GBox 有时会忽略请求的挂载名，按标题或“标题-短ID”落盘；
    # 探测目录必须同时匹配这些别名，不能只找 clean_name。
    aliases = [clean_name, base_name, f"{base_name}-{share_id[:6]}", f"{base_name}_{share_id[:6]}", share_id[:6]]
    
    # GBox 对带密码分享要求三段 TAB 格式：挂载名<TAB>share_id<TAB>密码。
    # 用 "password=..." 会假成功，但 AList 报 guest missing pwd_id/stoken。
    # 无密码 UC 默认不要把 share_id 当 password；这会让部分分享生成记录但
    # AList object not found。仅在显式调试开关下才尝试该兜底。
    content_str = f"{clean_name}\t{share_id}"
    if password:
        content_str += f"\t{password}"
    elif gbox_type == 7 and UC_SHARE_ID_PASSWORD_FALLBACK:
        content_str += f"\t{share_id}"
        
    body = json.dumps({"type": gbox_type, "content": content_str}, ensure_ascii=False).encode()
    req = urllib.request.Request(f"{GBOX_URL}/api/import-shares-with-result", data=body, headers={"X-ACCESS-TOKEN": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            res = json.loads(resp.read())
            if res.get("success"):
                return share_id, clean_name, aliases
            errors = res.get("errors") or []
            failed = res.get("failed", 0)
            if not errors and not failed:
                print(f"  ⚠️ 导入未确认，尝试查找既有目录: {res}")
                return share_id, clean_name, aliases
            print(f"  ❌ 导入失败: {res}")
            return None, None, []
    except Exception as e:
        print(f"  ❌ 导入异常: {e}")
        return None, None, []

def alist_list(path, page=1, per_page=200):
    """调用 AList API 列出目录"""
    payload = json.dumps({"path": path, "page": page, "per_page": per_page}).encode()
    req = urllib.request.Request(ALIST_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())

def alist_get(path):
    """获取单个文件直链；能列目录不代表能播放，必须额外验证。"""
    payload = json.dumps({"path": path}).encode()
    req = urllib.request.Request(ALIST_GET_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        return json.loads(resp.read())

def find_imported_folder(root_path, clean_name, max_wait=40, poll_interval=5, aliases=None, share_id=None, token=None):
    """在网盘根目录下查找刚导入的文件夹，带轮询等待。

    先立即查一次；很多资源已经导入过，固定 sleep 会让探路白慢几十秒。
    UC/GBox 可能不按请求挂载名落盘，需按 aliases 和 GBox shares 记录兜底。
    """
    if token:
        try:
            resolved = resolve_share_mount_path(
                gbox_url=GBOX_URL,
                token=token,
                alist_base=ALIST_BASE,
                root_path=root_path,
                share_id=share_id,
                aliases=aliases or [clean_name],
                max_wait=max_wait,
                poll_interval=poll_interval,
            )
            if resolved:
                return resolved
        except Exception:
            pass

    aliases = [str(x) for x in (aliases or [clean_name]) if str(x or "").strip()]
    short_id = str(share_id or "")[:6]
    if short_id:
        aliases.append(short_id)

    def matches(name):
        name = str(name or "")
        for alias in aliases:
            if alias and (name == alias or name.startswith(alias) or alias in name):
                return True
        return False

    start = time.time()
    while True:
        try:
            res = alist_list(root_path, page=1, per_page=200)
            if res.get("code") != 200:
                time.sleep(poll_interval)
                continue
                
            items = res.get("data", {}).get("content", []) or []
            # 按修改时间倒序
            items.sort(key=lambda x: x.get("modified", ""), reverse=True)
            
            dirs = []
            for item in items:
                if item.get("is_dir") or item.get("type") == 1:
                    dirs.append(item.get("name", ""))

            # 先匹配精确目录/完整前缀/短 ID 别名，避免只按 clean_name 漏掉 UC 实际落盘名。
            for name in dirs:
                if matches(name):
                    return f"{root_path}/{name}"

            # 没找到，继续等
            if time.time() - start >= max_wait:
                break
            time.sleep(poll_interval)
        except Exception:
            if time.time() - start >= max_wait:
                break
            time.sleep(poll_interval)

    return None

def normalize_gbox_type(gbox_type, url=""):
    """Accept both numeric GBox types and loose provider strings from older prompts."""
    if isinstance(gbox_type, int):
        return gbox_type
    text = str(gbox_type or "").lower()
    url_l = str(url or "").lower()
    if "quark" in text or "quark" in url_l or "pan.quark.cn" in url_l:
        return 5
    if "aliyun" in text or "ali" in text or "alipan" in url_l or "aliyundrive" in url_l:
        return 0
    if "115" in text or "115.com" in url_l:
        return 8
    if "xunlei" in text or "xunlei" in url_l:
        return 2
    return 7

def cleanup_gbox_mount_records(paths, storages_only=False):
    unique = []
    seen = set()
    for raw in paths:
        path = str(raw or "").strip()
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    if not unique or not GBOX_MOUNT_CLEANUP_SCRIPT.exists():
        return

    cmd = [sys.executable, str(GBOX_MOUNT_CLEANUP_SCRIPT), "--apply"]
    if storages_only:
        cmd.append("--storages-only")
    for path in unique:
        cmd.extend(["--path", path])

    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=240,
            check=False,
        )
        if proc.returncode != 0:
            print("    ⚠️ 双面清理失败，数据库残留可能还在")
    except Exception as e:
        print(f"    ⚠️ 双面清理异常: {e}")

def delete_gbox_folder(token, path, share_id=None, retries=2):
    """删除 GBox share 记录（清理探路垃圾）。

    优先用 share_id 精确匹配（即使 folder_path 未知/探路失败也能回收挂载），
    回退按 path 匹配；删除失败会重试，避免单次网络抖动留下残留。
    返回删除条数。
    """
    roots = ("🍓我的UC分享", "🍊我的夸克分享", "🍑我的阿里分享", "🏷️我的115分享", "🍒我的迅雷分享")

    def strip_root(value):
        parts = str(value or "").strip("/").split("/")
        if parts and parts[0].startswith(roots):
            parts = parts[1:]
        return "/".join(part for part in parts if part)

    targets = set()
    if path:
        targets.update({path, "/" + str(path).strip("/")})
        base = strip_root(path)
        if base:
            targets.add(base)
            targets.add("/" + base)
            for root in roots:
                targets.add(f"/{root}/{base}")
    sid = str(share_id or "").lower()
    if not targets and not sid:
        return 0  # 无任何匹配依据，避免误删

    for attempt in range(retries + 1):
        try:
            deleted = 0
            headers = {"X-ACCESS-TOKEN": token}
            for page in range(0, 5):
                req = urllib.request.Request(f"{GBOX_URL}/api/shares?page={page}&size=200", headers=headers)
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read())
                for row in data.get("content") or []:
                    rid = row.get("id")
                    if rid is None:
                        continue
                    matched = (sid and row_matches_share_id(row, sid)) or (row.get("path") in targets)
                    if matched:
                        delete_req = urllib.request.Request(f"{GBOX_URL}/api/shares/{rid}", headers=headers, method="DELETE")
                        with urllib.request.urlopen(delete_req, timeout=8):
                            deleted += 1
                if data.get("last", True):
                    break
            if deleted:
                print(f"    🗑️ 已清理无效资源: {path or share_id}")
            return deleted
        except Exception as e:
            if attempt < retries:
                time.sleep(0.6 * (attempt + 1))
                continue
            print(f"    ⚠️ 清理异常: {e}")
            return 0

def cleanup_stale_probe_mounts(token):
    """清理上一次探路残留的未选挂载，避免 GBox 资源页越积越多。"""
    try:
        with open(PROBE_MOUNTS_FILE, "r", encoding="utf-8") as f:
            rows = json.load(f)
    except Exception:
        rows = []

    if not rows:
        return

    protected = set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        for drama in state.get("dramas") or []:
            source_path = str(drama.get("source_path") or "").strip()
            if source_path:
                protected.add(source_path.rstrip("/"))
    except Exception:
        pass

    print("🧹 清理上次探路残留挂载...")
    seen = set()
    for row in rows:
        if row.get("selected"):
            continue
        sid = row.get("share_id")
        raw_paths = [(row.get(k) or "").strip() for k in ("video_path", "folder_path")]
        raw_paths = [p for p in raw_paths if p]
        # 命中正式追剧源则整条跳过，避免误删
        if any(p.rstrip("/") in protected for p in raw_paths):
            continue
        primary = next((p for p in raw_paths if p not in seen), "")
        # 即使没有可用 path（探路中途失败），只要有 share_id 也能精确回收挂载
        if not primary and not sid:
            continue
        if primary:
            seen.add(primary)
        delete_gbox_folder(token, primary, share_id=sid)
    if seen:
        cleanup_gbox_mount_records(sorted(seen), storages_only=True)

    try:
        os.remove(PROBE_MOUNTS_FILE)
    except Exception:
        pass

    if not DEEP_CLEANUP_STALE:
        return

    # 兜底清理：删除明显属于探路临时挂载、且当前已失效、又未被追剧状态引用的残留项。
    # 这一步会遍历 GBox shares + AList，默认关闭，避免每次探路先慢一轮。
    try:
        headers = {"X-ACCESS-TOKEN": token}
        deep_cleaned = set()
        for page in range(0, 5):
            req = urllib.request.Request(f"{GBOX_URL}/api/shares?page={page}&size=200", headers=headers)
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            rows = data.get("content") or []
            for row in rows:
                path = str(row.get("path") or "").strip()
                if not path or path.rstrip("/") in protected:
                    continue
                leaf = path.strip("/").split("/")[-1]
                if not (
                    re.search(r"_[0-9a-f]{6}$", leaf, re.I)
                    or re.search(r"(?:更至?|更新至?)\d+", leaf, re.I)
                ):
                    continue
                try:
                    res = alist_list(path, page=1, per_page=1)
                    if res.get("code") == 200:
                        continue
                except Exception:
                    pass
                delete_gbox_folder(token, path)
                deep_cleaned.add(path)
            if data.get("last", True):
                break
        if deep_cleaned:
            cleanup_gbox_mount_records(sorted(deep_cleaned), storages_only=True)
    except Exception:
        pass

def _common_video_dir(paths):
    dirs = []
    for path in paths:
        text = str(path or "").strip()
        if not text:
            continue
        dirs.append(text.rsplit("/", 1)[0] if "/" in text else text)
    if not dirs:
        return None
    common = os.path.commonpath(dirs)
    return common or None

def count_videos(folder_path, depth=0, max_depth=5, sample_limit=3):
    """递归统计视频文件数和总大小，返回 (count, size, common_video_dir, samples)。"""
    if depth > max_depth:
        return 0, 0, None, []
    try:
        res = alist_list(folder_path, page=1, per_page=1000)
        if res.get("code") != 200:
            return 0, 0, None, []

        data = res.get("data") or {}
        items = data.get("content") or []
        video_exts = (".mp4", ".mkv", ".avi", ".ts", ".flv", ".wmv", ".mov", ".m2ts", ".rmvb")

        video_count = 0
        total_size = 0
        samples = []
        video_paths = []
        subdirs = []
        for item in items:
            name = item.get("name", "")
            name_l = name.lower()
            is_dir = item.get("is_dir") or item.get("type") == 1
            if is_dir:
                subdirs.append(name)
            elif any(name_l.endswith(ext) for ext in video_exts):
                if not looks_like_episode(name):
                    continue
                video_count += 1
                total_size += int(item.get("size") or 0)
                video_paths.append(folder_path.rstrip("/") + "/" + name)
                if len(samples) < sample_limit:
                    samples.append(folder_path.rstrip("/") + "/" + name)

        for sub in subdirs:
            child = folder_path.rstrip("/") + "/" + sub
            c, s, d, child_samples = count_videos(child, depth + 1, max_depth, sample_limit)
            video_count += c
            total_size += s
            if d:
                video_paths.append(d)
            if len(samples) < sample_limit:
                samples.extend(child_samples[:sample_limit - len(samples)])

        return video_count, total_size, _common_video_dir(video_paths), samples
    except Exception as e:
        print(f"    ❌ 统计失败: {e}")
        return 0, 0, None, []

def validate_playable(samples):
    """Optional slow fs/get check for playback incidents, disabled by default."""
    last_error = ""
    playable_count = 0
    first_sample = None
    for sample in samples:
        try:
            res = alist_get(sample)
        except Exception as e:
            last_error = str(e)
            continue

        if res.get("code") != 200:
            last_error = str(res.get("message") or res.get("msg") or res)
            continue

        data = res.get("data") or {}
        if data.get("raw_url") or data.get("url") or data.get("sign"):
            playable_count += 1
            if first_sample is None:
                first_sample = sample
            continue

        last_error = str(res)

    return playable_count, first_sample, last_error

def main():
    # 加载 pansou 结果
    try:
        with open("/tmp/pansou_results.json", "r") as f:
            results = json.load(f)
    except:
        print("❌ 找不到 /tmp/pansou_results.json")
        return
    
    start_time = time.time()

    # 去重，默认最多查 4 个；命中可用大集数资源后会提前停止。
    to_check = []
    seen_urls = set()
    for r in results:
        if r["url"] not in seen_urls and len(to_check) < DEFAULT_MAX_CANDIDATES:
            to_check.append(r)
            seen_urls.add(r["url"])
    
    if not to_check:
        print("⚠️ 没有需要核验的资源")
        return
    
    # 登录 GBox
    token = login_gbox()
    if not token:
        print("❌ GBox 登录失败")
        return

    cleanup_stale_probe_mounts(token)
    
    print(f"🔍 正在核验 {len(to_check)} 个资源的真实内容...\n")

    probe_results = []
    probe_mounts = []
    
    _collect_lock = threading.Lock()
    stop_event = threading.Event()

    def probe_one(r):
        # 早停 / 全局超时已触发则快速返回，不再发起新的导入。
        if stop_event.is_set() or time.time() - start_time >= DEFAULT_GLOBAL_TIMEOUT:
            return

        note = r.get("note", "")[:30]
        url = r.get("url", "")
        pwd = r.get("password", "")
        gbox_type = normalize_gbox_type(r.get("gbox_type", 7), url)

        print(f"📝 检查: {note}...")
        before_share_ids = snapshot_share_ids(token) if (FAST_UNCONFIRMED_UC and gbox_type == 7) else set()

        # 导入
        share_id, clean_name, aliases = import_share(token, url, pwd, gbox_type, note)
        if not share_id:
            print("  ⏭️ 跳过\n")
            return

        # 导入一旦返回 share_id，GBox 可能已异步创建挂载。立即登记待清理项，
        # 即便后续“未确认快速跳过”或“找不到文件夹”，残留也能按 share_id 回收，
        # 不会留在资源页。folder_path 待找到后补全。
        root = ROOTS_MAP.get(gbox_type, "/🍓我的UC分享")
        mount_record = {
            "note": r.get("note", ""),
            "url": url,
            "share_id": share_id,
            "root": root,
            "clean_name": clean_name,
            "aliases": aliases or [],
            "folder_path": "",
            "selected": False,
        }
        with _collect_lock:
            probe_mounts.append(mount_record)

        # 查找导入的文件夹
        print(f"  ⏳ 等待网盘扫描...")
        if FAST_UNCONFIRMED_UC and gbox_type == 7:
            try:
                rows_after = fetch_gbox_shares(GBOX_URL, token)
                target = str(share_id or "").lower()
                has_target_row = any(row_matches_share_id(row, target) for row in rows_after)
                after_share_ids = set()
                for row in rows_after:
                    try:
                        payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
                    except Exception:
                        payload = str(row)
                    for match in re.findall(r"[0-9a-f]{12,}", payload, flags=re.I):
                        after_share_ids.add(match.lower())
                if not has_target_row and target not in after_share_ids - before_share_ids:
                    print("  ⚠️ GBox 未出现本次 share_id 的新挂载记录，快速跳过")
                    print()
                    return
            except Exception as exc:
                print(f"  ⚠️ GBox shares 快速核验失败，继续常规扫描: {exc}")
        remaining = max(3, int(DEFAULT_GLOBAL_TIMEOUT - (time.time() - start_time)))
        folder_wait = min(DEFAULT_FOLDER_WAIT, remaining)
        folder_path = find_imported_folder(
            root,
            clean_name,
            max_wait=folder_wait,
            poll_interval=3,
            aliases=aliases,
            share_id=share_id,
            token=token,
        )
        if folder_path:
            mount_record["folder_path"] = folder_path

        if not folder_path:
            print(f"  ⚠️ 未找到文件夹 (在 {root} 中)\n")
            return

        # 递归统计视频
        count, size, video_path, samples = count_videos(folder_path, sample_limit=1000)
        size_gb = size / (1024**3)

        if count > 0:
            sample_path = samples[0] if samples else ""
            if VALIDATE_PLAYABLE:
                playable_count, sample_path, play_error = validate_playable(samples)
                if playable_count <= 0:
                    print(f"  ⚠️ 找到 {count} 个视频文件，但无法获取播放直链，清理后跳过")
                    if play_error:
                        print(f"     原因: {play_error[:160]}")
                    delete_gbox_folder(token, folder_path, share_id=share_id)
                    print()
                    return

                if playable_count < count:
                    print(f"  ⚠️ 找到 {count} 个视频文件，但仅 {playable_count} 个可播放")
                count = playable_count
                print(f"  ✅ 可播放 {count} 集, 大小 {size_gb:.2f}GB")
                print(f"  ▶️ 直链验证通过: {sample_path}")
            else:
                print(f"  ✅ 可枚举 {count} 个视频文件, 大小 {size_gb:.2f}GB")
                if sample_path:
                    print(f"  📄 样本文件: {sample_path}")
            if video_path and video_path != folder_path:
                print(f"  📂 视频目录: {video_path}")
            print()
            with _collect_lock:
                probe_results.append({
                    "note": r.get("note", ""),
                    "url": url,
                    "share_id": share_id,
                    "count": count,
                    "size_gb": round(size_gb, 2),
                    "type": "UC" if gbox_type == 7 else "夸克" if gbox_type == 5 else "其他",
                    "gbox_type": gbox_type,
                    "folder_path": folder_path,
                    "video_path": video_path or folder_path,
                    "playable_sample": sample_path if VALIDATE_PLAYABLE else "",
                    "sample_video": sample_path,
                })
                mount_record["video_path"] = video_path or folder_path
                mount_record["count"] = count
                mount_record["playable_sample"] = sample_path if VALIDATE_PLAYABLE else ""
                mount_record["sample_video"] = sample_path
                if count >= STOP_AFTER_GOOD_COUNT:
                    print(f"  🎯 已找到 {count} 集可用资源，提前停止继续探测")
                    stop_event.set()
        else:
            print(f"  ⚠️ 文件夹为空，清理后跳过")
            # 自动清理空文件夹，避免垃圾堆积
            delete_gbox_folder(token, folder_path, share_id=share_id)

    # 小并发探路:GBox 并发太高会限流，默认 3 路（PANSOU_PROBE_CONCURRENCY 可调）。
    # 早停用 stop_event:某候选命中足够集数后，未开始的候选在 probe_one 开头直接返回。
    _max_workers = max(1, min(PROBE_CONCURRENCY, len(to_check)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=_max_workers) as _executor:
        _futures = [_executor.submit(probe_one, r) for r in to_check]
        for _fut in concurrent.futures.as_completed(_futures):
            try:
                _fut.result()
            except Exception as _exc:
                print(f"  ⚠️ 候选探路异常: {_exc}")
    
    # 按视频目录去重，再按集数/网盘优先级/大小降序排序展示。
    # 剧集仍以集数优先；电影等集数相同时遵守 UC 优先。
    unique = []
    seen_paths = set()
    for res in probe_results:
        key = res.get("video_path") or res.get("folder_path") or res.get("url")
        if key in seen_paths:
            continue
        seen_paths.add(key)
        unique.append(res)
    probe_results = unique
    probe_results.sort(
        key=lambda x: (
            x["count"],
            1 if int(x.get("gbox_type") or -1) == 7 else 0,
            x["size_gb"],
        ),
        reverse=True,
    )

    print("=" * 50)
    print("📊 核验结果汇总 (按集数排序，集数相同优先 UC):")
    for i, res in enumerate(probe_results, 1):
        print(f"{i}. [{res['type']}] {res['note']} → {res['count']}集 / {res['size_gb']}GB")
        if res.get("video_path"):
            print(f"   路径: {res['video_path']}")
    print("=" * 50)

    # 保存结果供后续使用
    with open("/tmp/probe_results.json", "w") as f:
        json.dump(probe_results, f, ensure_ascii=False, indent=2)
    with open(PROBE_MOUNTS_FILE, "w") as f:
        json.dump(probe_mounts, f, ensure_ascii=False, indent=2)

    print("\n✅ 探路完成，结果已保存至 /tmp/probe_results.json")
    if AUTO_CLEAN_PROBE_MOUNTS and probe_mounts:
        print("🧹 自动清理本次探路 GBox 临时挂载（候选信息已保留，选择后会重新导入最终源）...")
        cleanup_stale_probe_mounts(token)
    try:
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "build-selection-map.py")],
            check=False,
            timeout=10,
        )
    except Exception as e:
        print(f"⚠️ 统一选择映射生成失败: {e}")

if __name__ == "__main__":
    main()
