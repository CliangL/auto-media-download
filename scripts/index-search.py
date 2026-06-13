#!/usr/bin/env python3
"""index-search.py - 本机索引搜索（通过SSH调用NAS）

搜索 NAS 上的 xiaoya 索引，返回匹配结果。
结果自动保存到 /tmp/media_resources.txt 供后续处理。
格式：路径\t名称
"""
import subprocess
import sys
import re
import os

def main():
    title = sys.argv[1] if len(sys.argv) > 1 else sys.exit("用法: index-search.py 片名")
    
    # SSH 配置
    # SSH 配置：优先读 config，默认 Tailscale（Mac 在办公室用）
    config_dir = os.path.join(os.path.dirname(__file__), '..', 'config')
    config_file = os.path.join(config_dir, 'media-config.json')
    if os.path.exists(config_file):
        import json
        with open(config_file) as f:
            cfg = json.load(f)
        nas_host = cfg.get('nas', {}).get('tailscale_ip', 'YOUR_NAS_IP')
        nas_user = cfg.get('nas', {}).get('user', 'Cliang')
        nas_pass = cfg.get('nas', {}).get('password', '')
    else:
        nas_host = os.environ.get('NAS_HOST', 'YOUR_NAS_IP')
        nas_user = os.environ.get('NAS_USER', 'Cliang')
        nas_pass = os.environ.get('NAS_PASS', 'YOUR_NAS_PASSWORD')
    index_path = '/vol1/1000/docker/xiaoya/data/index.zip'
    
    # ⭐ 优化：媒体路径关键词（用于置顶显示）
    # [修正] 增加 /115, /ISO 等核心库，防止漏搜深层目录资源
    media_keywords = ['电视剧', '电影', '动漫', '纪录片', '综艺', '每日更新', '115', 'ISO']
    # ⭐ 优化：排除路径关键词（用于过滤干扰项）
    exclude_keywords = ['电子书', 'music', '音乐', '.flac', '.mp3', '.mobi', '.epub', '.txt', '教育', '编程', '教程']
    
    print(f"🔍 Searching for: {title}")
    
    # 使用 sshpass + grep 在 NAS 上搜索（避免下载整个 99MB 索引）
    cmd = f'''sshpass -p '{nas_pass}' ssh -o StrictHostKeyChecking=no {nas_user}@{nas_host} "unzip -p {index_path} | grep '{title}'"'''
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
    
    if result.returncode != 0:
        print(f"❌ 索引搜索失败: {result.stderr}")
        sys.exit(1)
    
    # 过滤与评分匹配项
    lines = result.stdout.strip().split('\n')
    matches = []  # List of (score, path, name_in_index)
    
    for line in lines:
        line_lower = line.lower()
        
        # 1. 快速过滤：排除非媒体资源
        if any(kw.lower() in line_lower for kw in exclude_keywords):
            continue
        
        # 2. 解析路径和名称
        if '#' in line:
            parts = line.split('#')
            name_in_index = parts[1].strip() if len(parts) > 1 else ''
            path = parts[0].strip()
        else:
            path = line.strip()
            name_in_index = os.path.basename(line)
        
        if not name_in_index:
            continue
            
        # 3. 标题匹配逻辑
        title_lower = title.lower()
        name_lower = name_in_index.lower()
        
        is_match = False
        if title_lower == name_lower:
            is_match = True
        elif title_lower in name_lower or name_lower in title_lower:
            is_match = True
        elif title_lower in line_lower:
            is_match = True
            
        if not is_match:
            continue
            
        # 4. 计算优先级分数
        score = 0
        # 命中媒体关键词加分
        if any(kw in path for kw in media_keywords):
            score += 10
        # 完美匹配加分
        if title_lower == name_lower:
            score += 5
            
        matches.append((score, path, name_in_index))
    
    # 按分数降序排序
    matches.sort(key=lambda x: x[0], reverse=True)
    
    if not matches:
        print(f"📋 索引搜索: 0 个结果")
        print("===INDEX_COUNT===0")
        with open('/tmp/media_resources.txt', 'w') as f:
            f.write('')
        return
    
    print(f"📋 索引搜索: {len(matches)} 个结果 (已按媒体资源优先级排序)")
    
    with open('/tmp/media_resources.txt', 'w') as f:
        for i, (score, path, name) in enumerate(matches[:15], 1):
            clean_path = path.lstrip('./').lstrip('🏷️')
            f.write(f"{clean_path}\t{name}\n")
            # 打印标记：如果是高分项（媒体资源），显示 ⭐
            tag = " ⭐" if score >= 10 else ""
            print(f"[{i}]{tag} {name}")
            print(f"    {clean_path[:70]}")
    
    print()
    print(f"===INDEX_COUNT==={len(matches)}")
    print(f"💾 结果已保存到 /tmp/media_resources.txt")

if __name__ == '__main__':
    main()
