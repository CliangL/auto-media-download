#!/usr/bin/env python3
"""gen-strm.py - 通过 xiaoya AList API 递归列出视频并生成 STRM 文件

v2.3 (2026-04-20):
  - 修复综艺误判为电视剧，补充 variety 一等类型支持。
  - 综艺 STRM 改为保留原视频名，不再生成错误的 SxxExx 命名。
  - 综艺默认不按“年份前缀”去重，避免 2025-xx-xx 文件被错误折叠成一集。
"""
import json, urllib.request, urllib.parse, os, sys, re, shutil, time

from strm_layout import category_dir, resolve_strm_dir

# 从环境变量读取参数
source = os.environ.get('SOURCE_PATH', '')
prefix = os.environ.get('STRM_URL_PREFIX', 'http://YOUR_NAS_LAN_IP:5678/d')
name = os.environ.get('NAME', '')
api = os.environ.get('ALIST_API', 'http://localhost:5678/api/fs/list')
api_get = os.environ.get('ALIST_GET_API', api.rsplit('/', 1)[0] + '/get')
force_type = os.environ.get('MEDIA_TYPE', '')  # movie/tv/anime/variety，外部强制指定
prune_stale = os.environ.get('PRUNE_STALE_STRM', '1').lower() not in ('0', 'false', 'no')
validate_playable = os.environ.get('VALIDATE_STRM_PLAYABLE', '0').lower() in ('1', 'true', 'yes')

video_exts = ('.mp4', '.mkv', '.avi', '.ts', '.flv', '.wmv', '.mov')
promo_keywords = ('预告', '花絮', '定档', '片花', 'trailer', 'teaser', 'preview', 'behind')

def build_stream_url(url_path):
    """Build a player-safe /d URL while preserving path separators."""
    clean = str(url_path or '').lstrip('/')
    encoded = urllib.parse.quote(clean, safe='/')
    return f'{prefix.rstrip("/")}/{encoded}'

def list_videos(path, depth=0):
    """递归列出目录下的视频文件（支持 API 分页）"""
    vids = []
    if depth > 5:
        return vids
    try:
        page = 1
        per_page = 500
        while True:
            data = json.dumps({'page': page, 'per_page': per_page, 'path': path}).encode()
            req = urllib.request.Request(api, data=data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())
            data_obj = result.get('data') or {}
            items = data_obj.get('content') or []
            total_count = data_obj.get('total', 0)
            if not items:
                break
            for item in items:
                if item.get('is_dir') or item.get('type') == 1:
                    vids.extend(list_videos(path + '/' + item['name'], depth + 1))
                elif item.get('name', '').lower().endswith(video_exts):
                    if should_skip_video(item.get('name', '')):
                        continue
                    vids.append((path, item['name']))
            # 检查是否需要继续翻页
            if page * per_page >= total_count:
                break
            page += 1
    except Exception as e:
        print(f'  ⚠️ 无法列出 {path}: {e}', file=sys.stderr)
    return vids

def playable_check(path, vname):
    """Optional slow fs/get check for playback incidents, disabled by default."""
    full_path = f'{path.rstrip("/")}/{vname}'
    try:
        data = json.dumps({'path': full_path}).encode()
        req = urllib.request.Request(api_get, data=data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
    except Exception as exc:
        return False, str(exc)
    if result.get('code') != 200:
        return False, str(result.get('message') or result)
    data_obj = result.get('data') or {}
    if data_obj.get('raw_url') or data_obj.get('url') or data_obj.get('sign'):
        return True, ''
    return False, str(result)[:180]

def extract_season_episode(path, vname):
    """Extract season/episode from filename, falling back to parent folder."""
    season = None
    episode = None

    joined = f'{path}/{vname}'
    m = re.search(r'S(\d+)\s*E(\d+)', joined, re.I)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r'(?:Season|S)[\s._-]*(\d{1,2})', path, re.I)
    if m:
        season = int(m.group(1))
    else:
        m = re.search(r'第\s*(\d{1,2})\s*季', path)
        if m:
            season = int(m.group(1))

    m = re.search(r'(?:^|[^\w])E(\d{1,3})(?:[^\w]|$)', vname, re.I)
    if m:
        episode = int(m.group(1))
    else:
        m = re.search(r'(?:第)?(\d{1,3})[集话]', vname)
        if m:
            episode = int(m.group(1))
        else:
            m = re.match(r'(\d{1,3})(?:\D|$)', vname)
            if m:
                episode = int(m.group(1))

    return season, episode

def extract_episode(vname):
    """从文件名提取集数"""
    m = re.search(r'S\d+E(\d+)', vname, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r'E(\d+)', vname, re.I)
    if m:
        return int(m.group(1))
    m = re.match(r'(\d+)', vname)
    if m:
        return int(m.group(1))
    return None

def should_skip_video(vname):
    """过滤宣传/预告类视频，避免追剧集数虚高。"""
    name_l = (vname or '').lower()
    if not any(k in name_l for k in promo_keywords):
        return False
    return extract_episode(vname) is None

def detect_quality(vname):
    """检测画质等级，返回数字（越大越好）"""
    vname_lower = vname.lower()
    if '4k' in vname_lower or '2160p' in vname_lower:
        return 40
    if '1080p' in vname_lower or 'bluray' in vname_lower or '蓝光' in vname_lower:
        return 30
    if '720p' in vname_lower:
        return 20
    return 10  # 默认

def infer_media_type(source_path, forced):
    forced = (forced or '').strip().lower()
    if forced in ('movie', 'tv', 'anime', 'variety', 'documentary'):
        return forced

    text = source_path or ''
    if '综艺' in text:
        return 'variety'
    if '动漫' in text:
        return 'anime'
    if '纪录片' in text:
        return 'documentary'
    return ''

def safe_stem(vname):
    stem, _ = os.path.splitext(vname)
    stem = re.sub(r'[\\/:*?"<>|]+', '_', stem).strip()
    return stem or 'video'

def dedupe_videos(videos, media_type=''):
    """多版本去重：同一季同一集只保留最高画质版本"""
    if media_type == 'variety':
        seen = set()
        ordered = []
        for path, vname in videos:
            key = f'{path}/{vname}'
            if key in seen:
                continue
            seen.add(key)
            ordered.append((path, vname))
        return ordered

    ep_map = {}  # {集数: (path, vname, quality)}
    for path, vname in videos:
        season, ep = extract_season_episode(path, vname)
        quality = detect_quality(vname)
        # Multi-season shows must not collapse S01E01 and S08E01 into one item.
        key = (season or 1, ep) if ep else f'{path}/{vname}'
        if key not in ep_map or quality > ep_map[key][2]:
            ep_map[key] = (path, vname, quality)
    return [(p, v) for p, v, q in sorted(ep_map.values(), key=lambda x: x[0])]

def main():
    if not source or not name:
        print('❌ 缺少参数')
        sys.exit(1)

    media_hint = infer_media_type(source, force_type)

    # ⭐ 处理单文件（ISO / MKV / MP4 等）：直接生成 STRM，不走递归列表
    if source.lower().endswith(('.iso', '.mkv', '.mp4', '.avi', '.ts', '.flv', '.wmv', '.mov')):
        ext = source.lower().rsplit('.', 1)[-1]
        print(f'📄 检测到单文件 ({ext.upper()}): {os.path.basename(source)}')
        url = build_stream_url(source)

        media_type = media_hint or 'movie'
        base_dir = resolve_strm_dir(name, media_type, custom_path=os.environ.get('STRM_CUSTOM_PATH', ''))
        strm_file = f'{name}.strm'
        print(f'📀 类型: {category_dir(media_type)}')
            
        os.makedirs(base_dir, exist_ok=True)
        with open(f'{base_dir}/{strm_file}', 'w') as f:
            f.write(url)
        print('✅ STRM 已生成')
        print('===STRM_TOTAL===1')
        print('===STRM_COUNT===1')
        sys.exit(0)

    # ⭐ 优化：根目录预检查，区分"目录为空"与"API报错"
    try:
        data = json.dumps({'page': 1, 'per_page': 1, 'path': source}).encode()
        req = urllib.request.Request(api, data=data, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
        
        code = result.get('code', 0)
        if code != 200:
            msg = result.get('message', 'Unknown Error')
            print(f'❌ AList 路径无法访问 (HTTP Code: {code})')
            print(f'   🔍 错误详情：{msg}')
            print(f'   💡 提示：请在网页端确认该路径是否有效，或尝试选择其他资源')
            print('===STRM_TOTAL===0')
            print('===STRM_COUNT===0')
            sys.exit(1)
    except Exception as e:
        print(f'❌ AList 连接测试失败: {e}')
        print('===STRM_TOTAL===0')
        print('===STRM_COUNT===0')
        sys.exit(1)

    all_videos = sorted(list_videos(source))
    total_count = len(all_videos)
    print(f'📋 找到 {total_count} 个视频文件')

    # 多版本去重
    videos = dedupe_videos(all_videos, media_hint)
    deduped_count = len(videos)
    if total_count != deduped_count:
        print(f'⚡ 多版本去重: {total_count} → {deduped_count} 集')

    if validate_playable:
        playable = []
        skipped = []
        for path, vname in videos:
            ok, reason = playable_check(path, vname)
            if ok:
                playable.append((path, vname))
            else:
                skipped.append((vname, reason))
        if skipped:
            print(f'🧪 直链校验: 可播放 {len(playable)} / {len(videos)}，跳过 {len(skipped)} 个失效文件')
            for vname, reason in skipped[:8]:
                print(f'  ⚠️ 跳过不可播放: {vname[:50]} | {reason[:120]}')
            if len(skipped) > 8:
                print(f'  ... 还有 {len(skipped) - 8} 个不可播放文件')
        videos = playable
    count = deduped_count
    count = len(videos)

    if count == 0:
        print('❌ 未找到视频文件')
        print('===STRM_TOTAL===0')
        print('===STRM_COUNT===0')
        sys.exit(1)

    # 判断类型：外部传入 > 文件名推断
    if media_hint == 'movie':
        print('🎬 类型: 电影（联网判定）')
        print('===MEDIA_TYPE===:movie')
        media_type = 'movie'
    elif media_hint in ('anime', 'tv', 'variety', 'documentary'):
        label = category_dir(media_hint)
        emoji = '📺'
        print(f'{emoji} 类型: {label}（联网判定）')
        print(f'===MEDIA_TYPE==={media_hint}')
        media_type = media_hint
    elif any(re.search(r'S\d+E\d+', vname, re.I) for _, vname in videos):
        print('📺 类型: 电视剧（文件名含集数编号）')
        print('===MEDIA_TYPE===:tv')
        media_type = 'tv'
    elif count == 1:
        print('🎬 类型: 电影')
        print('===MEDIA_TYPE===:movie')
        media_type = 'movie'
    else:
        print('📺 类型: 电视剧（多文件）')
        print('===MEDIA_TYPE===:tv')
        media_type = 'tv'

    # 基础目录
    base_dir = resolve_strm_dir(name, media_type, custom_path=os.environ.get('STRM_CUSTOM_PATH', ''))

    print(f'📁 目录: {base_dir}')
    print()
    print('📝 生成 STRM 文件...')

    newly_generated = 0  # ⭐ 新增计数器：只统计新生成的
    generated_paths = set()
    for i, (path, vname) in enumerate(videos, 1):
        # 计算相对路径
        rel_path = path[len(source):].lstrip('/')
        if rel_path:
            url_path = f'{source}/{rel_path}/{vname}'
        else:
            url_path = f'{source}/{vname}'
        # 去掉前导 /
        url_path = url_path.lstrip('/')

        if media_type == 'movie':
            strm_dir = base_dir
            strm_file = f'{name}.strm'
            print(f'  ✅ 电影: {vname[:60]}')
        elif media_type == 'variety':
            strm_dir = f'{base_dir}/{rel_path}' if rel_path else base_dir
            strm_file = f'{safe_stem(vname)}.strm'
            print(f'  ✅ 综艺: {vname[:60]}')
        else:
            # 提取 Season 和 Episode
            sn = '01'
            ep = ''
            season_num, episode_num = extract_season_episode(path, vname)
            if season_num:
                sn = str(season_num)
            if episode_num:
                ep = str(episode_num)
            if not ep:
                ep = str(i)
            ep = str(int(ep))
            strm_dir = f'{base_dir}/Season {int(sn)}'
            strm_file = f'{name} - S{int(sn):02d}E{int(ep):02d}.strm'
            print(f'  ✅ E{int(ep):02d}: {vname[:60]}')

        strm_path = f'{strm_dir}/{strm_file}'
        generated_paths.add(os.path.abspath(strm_path))
        url = build_stream_url(url_path)
        # ⭐ 增量模式：已有 STRM 且内容一致才跳过；源路径变更时必须覆写死链
        if os.path.exists(strm_path):
            try:
                old_url = open(strm_path, 'r', encoding='utf-8', errors='ignore').read().strip()
            except Exception:
                old_url = ''
            if old_url == url:
                if media_type in ('movie', 'variety'):
                    print(f'  ⏭️ 已存在，跳过: {strm_file}')
                else:
                    print(f'  ⏭️ 已存在，跳过 E{int(ep):02d}')
                continue
            if media_type in ('movie', 'variety'):
                print(f'  ♻️ 发现旧链接，重写: {strm_file}')
            else:
                print(f'  ♻️ 发现旧链接，重写 E{int(ep):02d}')

        os.makedirs(strm_dir, exist_ok=True)
        with open(strm_path, 'w') as f:
            f.write(url)
        newly_generated += 1

    if prune_stale and media_type in ('tv', 'anime', 'documentary') and os.path.isdir(base_dir):
        stale_files = []
        for root, _, filenames in os.walk(base_dir):
            for filename in filenames:
                if not filename.endswith('.strm'):
                    continue
                path = os.path.abspath(os.path.join(root, filename))
                if path not in generated_paths:
                    stale_files.append(path)
        if stale_files:
            backup_dir = os.path.join(
                os.path.dirname(base_dir),
                '.stale-strm-backups',
                f'{os.path.basename(base_dir)}-{time.strftime("%Y%m%d_%H%M%S")}',
            )
            for path in stale_files:
                rel = os.path.relpath(path, base_dir)
                dest = os.path.join(backup_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(path, dest)
            print(f'  🧹 已备份移走 {len(stale_files)} 个本次源未生成的旧 STRM')
            print(f'     备份: {backup_dir}')

    newly_generated = newly_generated if 'newly_generated' in dir() else count
    print()
    if newly_generated < count:
        print(f'✅ 新增 {newly_generated} 个 STRM | 跳过 {count - newly_generated} 个已有文件')
    print(f'===STRM_TOTAL==={count}')
    print(f'===STRM_COUNT==={newly_generated}')

if __name__ == '__main__':
    main()
