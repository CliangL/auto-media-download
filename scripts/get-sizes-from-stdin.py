#!/usr/bin/env python3
"""get-sizes-from-stdin.py - 从 stdin 读取路径，批量获取 AList 资源大小

输入格式 (stdin): 路径\t名称
输出格式 (stdout): 路径\t名称\t大小(B)\t类型(file/dir)
"""
import urllib.request
import json
import sys

def human_size(size_bytes):
    """Convert bytes to human readable size"""
    if size_bytes == 0:
        return "dir"
    elif size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes/1024:.1f}KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes/(1024*1024):.1f}MB"
    else:
        return f"{size_bytes/(1024*1024*1024):.2f}GB"

def get_size(path):
    """Get size from AList API"""
    # Try single file first
    try:
        data = json.dumps({"path": path}).encode()
        req = urllib.request.Request('http://127.0.0.1:5678/api/fs/get', data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=8)
        result = json.loads(resp.read())
        if result.get('code') == 200:
            size = result['data']['size']
            return size, 'file' if size > 0 else 'dir'
    except:
        pass
    
    # Try listing directory
    try:
        data = json.dumps({"path": path, "page": 1, "per_page": 1}).encode()
        req = urllib.request.Request('http://127.0.0.1:5678/api/fs/list', data=data, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=8)
        result = json.loads(resp.read())
        if result.get('code') == 200:
            return 0, 'dir'
    except:
        pass
    
    return 0, 'unknown'

def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        
        parts = line.split('\t')
        path = parts[0]
        name = parts[1] if len(parts) > 1 else path.split('/')[-1]
        
        size, item_type = get_size(path)
        size_str = human_size(size)
        
        print(f"{path}\t{name}\t{size_str}\t{size}\t{item_type}")

if __name__ == '__main__':
    main()
