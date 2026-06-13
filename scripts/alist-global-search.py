#!/usr/bin/env python3
"""
alist-global-search.py
功能：AList 全盘递归搜索 (替代 index.zip)
特点：自动发现所有挂载盘，深度搜索，防卡顿
"""
import requests
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

# 配置
ALIST_URL = "http://YOUR_NAS_LAN_IP:5678"
MAX_DEPTH = 4      # 最大递归深度 (防止死循环)
SEARCH_TIMEOUT = 15 # 总搜索超时 (秒)

def get_all_roots():
    """获取 AList 所有的根目录 (存储源)"""
    try:
        resp = requests.post(f"{ALIST_URL}/api/fs/list", json={"path": "/", "page": 1, "per_page": 100}, timeout=5).json()
        if resp.get('code') == 200:
            # 过滤出所有文件夹作为根目录
            return [f"/{item['name']}" for item in resp['data']['content'] if item.get('is_dir')]
    except:
        pass
    # 如果获取失败，回退到常用目录
    return ["/115", "/我的115分享", "/阿里云盘", "/夸克", "/本地", "/每日更新"]

def search_recursive(path, keyword, depth=0):
    """递归搜索指定路径"""
    if depth > MAX_DEPTH:
        return []
    
    results = []
    try:
        payload = {"path": path, "page": 1, "per_page": 2000}
        resp = requests.post(f"{ALIST_URL}/api/fs/list", json=payload, timeout=8).json()
        
        if resp.get('code') != 200:
            return []
            
        items = resp.get('data', {}).get('content', [])
        if not items:
            return []

        for item in items:
            name = item.get('name', '')
            is_dir = item.get('is_dir', False)
            full_path = f"{path}/{name}"

            # 匹配逻辑
            if keyword.lower() in name.lower():
                # 视频文件判定
                if not is_dir and name.lower().endswith(('.mp4', '.mkv', '.iso', '.ts', '.avi', '.wmv', '.flv', '.rmvb', '.m2ts')):
                    size_mb = round(item.get('size', 0) / 1024 / 1024, 1)
                    results.append({
                        'path': full_path,
                        'name': name,
                        'size': f"{size_mb}MB",
                        'type': 'VIDEO'
                    })
            
            # 文件夹递归
            if is_dir:
                sub_results = search_recursive(full_path, keyword, depth + 1)
                results.extend(sub_results)
                
    except Exception:
        pass
    return results

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 alist-global-search.py <keyword>")
        sys.exit(1)
    
    keyword = sys.argv[1]
    print(f"🔍 启动全盘搜索：{keyword}")
    
    roots = get_all_roots()
    print(f"✅ 发现 {len(roots)} 个存储源，开始扫描...")
    
    all_results = []
    start_time = time.time()
    
    # 对每个根目录进行递归搜索
    for root in roots:
        if time.time() - start_time > SEARCH_TIMEOUT:
            print("⚠️ 搜索超时，已返回当前结果")
            break
        res = search_recursive(root, keyword)
        all_results.extend(res)

    # 去重 & 排序 (精确匹配优先)
    seen = set()
    unique_results = []
    
    def sort_key(x):
        # 1. 是否精确匹配文件名 (权重最高)
        exact_match = 1 if x['name'].lower().replace(' ', '') == keyword.lower().replace(' ', '') else 0
        # 2. 文件大小 (大的通常画质好，优先展示)
        try:
            size_val = float(x['size'].replace('MB',''))
        except:
            size_val = 0
        return (-exact_match, -size_val)

    for r in all_results:
        if r['path'] not in seen:
            seen.add(r['path'])
            unique_results.append(r)
            
    unique_results.sort(key=sort_key)

    # 打印结果 (Markdown 友好格式)
    print(f"\n📋 找到 {len(unique_results)} 个结果:")
    for i, r in enumerate(unique_results[:15], 1):
        # 截断路径
        short_path = r['path']
        if len(short_path) > 60:
            short_path = "..." + short_path[-57:]
        
        print(f"{i}. {r['name']} | {r['size']} | {short_path}")

    # 保存到临时文件供后续脚本使用
    with open('/tmp/media_resources.txt', 'w', encoding='utf-8') as f:
        for r in unique_results[:15]:
            f.write(f"{r['path']}\t{r['name']}\n")

if __name__ == '__main__':
    main()