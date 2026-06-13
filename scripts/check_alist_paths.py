#!/usr/bin/env python3
"""check_alist_paths.py v2.0 - 智能 AList 路径核验脚本 (升级版)

优化点:
1. 自动探测子目录: 如果路径是空文件夹但有子目录，自动进入子目录统计集数 (解决"已刮削"资源 0 文件误报)
2. 防 f-string 语法错误: 绝不嵌套引用，确保 SSH heredoc 执行稳定
3. 优先匹配大文件: 自动排除 .nfo/.jpg 等干扰，只算视频
"""

import json
import urllib.request
import sys
import os

ALIST = "http://127.0.0.1:5678/api/fs/list"
VIDEO_EXTS = ('.mp4', '.mkv', '.avi', '.ts', '.flv', '.wmv', '.mov', '.iso', '.strm')

def check_path(path, label):
    """核验单个路径，支持自动展开子目录"""
    try:
        clean_path = path.rstrip("/")
        payload = json.dumps({"path": clean_path, "page": 1, "per_page": 100}).encode()
        req = urllib.request.Request(ALIST, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
        
        data = result.get("data", {})
        if data is None:
            print(f"FAIL|{label}|路径失效")
            return

        content = data.get("content", []) or []
        files = [i for i in content if i.get("type") == 2]
        
        # 过滤非视频文件
        video_files = []
        for f in files:
            name = f.get("name", "").lower()
            if any(name.endswith(ext) for ext in VIDEO_EXTS):
                video_files.append(f)
        
        total_size = sum(i.get("size", 0) for i in content)
        final_path = clean_path
        is_dir = False

        # 优化：如果当前目录没视频，但有子目录，自动检查第一个子目录
        if len(video_files) == 0:
            sub_dirs = [i for i in content if i.get("type") == 1]
            if sub_dirs:
                # 尝试找 Season 1 或第一个子目录
                target_sub = None
                for sd in sub_dirs:
                    name_lower = sd.get("name", "").lower()
                    if "season" in name_lower:
                        target_sub = sd
                        break
                if not target_sub:
                    target_sub = sub_dirs[0]
                
                # 进入子目录检查
                sub_path = clean_path + "/" + target_sub.get("name")
                payload2 = json.dumps({"path": sub_path, "page": 1, "per_page": 200}).encode()
                req2 = urllib.request.Request(ALIST, data=payload2, headers={"Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req2, timeout=10) as resp2:
                        result2 = json.loads(resp2.read())
                    data2 = result2.get("data", {})
                    if data2:
                        content2 = data2.get("content", []) or []
                        files2 = [i for i in content2 if i.get("type") == 2]
                        video_files = [i for i in files2 if i.get("name", "").lower().endswith(VIDEO_EXTS)]
                        # 重新计算大小 (只算这个目录)
                        total_size = sum(i.get("size", 0) for i in content2)
                        final_path = sub_path
                        is_dir = True
                except Exception as e:
                    pass # 子目录读取失败，保持原样

        count = len(video_files)
        size_gb = total_size / (1024**3)
        
        first_name = ""
        if video_files:
            first_name = video_files[0].get("name", "?")
        
        # 安全打印 (避免 f-string 嵌套错误)
        print(f"OK|{label}|{count} files|{size_gb:.2f} GB|{first_name}|{final_path}")

    except Exception as e:
        print(f"FAIL|{label}|Error: {str(e)}")

def main():
    input_file = "/tmp/media_resources.txt"
    if not os.path.exists(input_file):
        print("Error: /tmp/media_resources.txt not found")
        sys.exit(1)

    seen = set()
    tasks = []
    with open(input_file) as f:
        for line in f:
            parts = line.strip().split("\t")
            if len(parts) >= 2:
                path = parts[0].replace("🏷️", "").strip()
                label = parts[1].strip()
                if path and path not in seen:
                    seen.add(path)
                    tasks.append((path, label))
    
    # 限制任务数
    tasks = tasks[:10]
    
    for path, label in tasks:
        check_path(path, label)

if __name__ == "__main__":
    main()
