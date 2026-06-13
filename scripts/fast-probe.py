#!/usr/bin/env python3
"""
fast-probe.py v1.0 - 极速并发探路脚本
替代慢速的浏览器核验。直接调用 GBox API 并行导入前 N 个链接，统计真实文件数。
"""
import json, subprocess, threading, sys, os
from urllib.request import Request, urlopen

CONFIG_FILE = os.path.join(os.path.dirname(__file__), '..', 'config', 'media-config.json')
PANSOU_FILE = "/tmp/pansou_results.json"

with open(CONFIG_FILE) as f: cfg = json.load(f)
GBOX_URL = cfg['gbox']['internal_url']
GBOX_USER = cfg['gbox']['username']
GBOX_PASS = cfg['gbox']['password']
SSH_CMD = f"sshpass -p '{cfg['nas']['password']}' ssh -o StrictHostKeyChecking=no {cfg['nas']['user']}@{cfg['nas']['tailscale_ip']}"

def login_gbox():
    data = json.dumps({"username": GBOX_USER, "password": GBOX_PASS}).encode()
    req = Request(f"{GBOX_URL}/api/accounts/login", data=data, headers={"Content-Type": "application/json"})
    return json.loads(urlopen(req, timeout=5).read())["token"]

def probe_link(idx, item, token):
    """尝试导入并统计文件数"""
    url = item["url"]
    share_id = url.split("/s/")[-1].split("?")[0]
    note = item["note"]
    pan_type = item["pan_type"]
    gbox_type = item.get("gbox_type", 5)
    pw = item.get("password", "")

    # 构造导入路径
    folder_map = {"uc": "🍓我的UC分享", "quark": "🍊我的夸克分享", "aliyun": "🍑我的阿里分享", "xunlei": "🍒我的迅雷分享"}
    share_root = folder_map.get(pan_type, "Unknown")
    mount_path = f"探路测试/{idx}_{note[:10]}"
    
    # 1. 尝试导入
    pw_part = f', "password": "{pw}"' if pw else ""
    body = json.dumps({"type": gbox_type, "content": f"{mount_path}\t{share_id}{pw_part}"}).encode()
    req = Request(f"{GBOX_URL}/api/import-shares-with-result", data=body, headers={"X-ACCESS-TOKEN": token, "Content-Type": "application/json"})
    
    try:
        res = json.loads(urlopen(req, timeout=10).read())
        if not res.get("success"):
            return {"idx": idx, "status": "❌ 导入失败", "ep_count": 0, "size": "-"}
    except Exception as e:
        return {"idx": idx, "status": f"❌ 超时 ({str(e)[:10]})", "ep_count": 0, "size": "-"}

    # 2. 等待同步并统计
    import time; time.sleep(2)
    
    # 通过 SSH 调用 AList 统计文件
    cmd = f"""python3 -c "
import urllib.request, json
api = 'http://127.0.0.1:5678/api/fs/list'
data = json.dumps({{'path':'/{share_root}/{mount_path}', 'page':1, 'per_page':0}}).encode()
req = urllib.request.Request(api, data=data, headers={{'Content-Type':'application/json'}})
with urllib.request.urlopen(req, timeout=5) as r:
    res = json.loads(r.read())
count = len(res.get('data',{{}}).get('content',[]))
total_size = res.get('data',{{}}).get('total',0)
print(f'{{count}} {{total_size}}')
" """
    
    try:
        proc = subprocess.run(f"{SSH_CMD} '{cmd}'", shell=True, capture_output=True, text=True, timeout=8)
        if proc.stdout.strip():
            count, size_bytes = proc.stdout.strip().split()
            count = int(count)
            size = f"{int(size_bytes)/1024/1024/1024:.1f}GB" if int(size_bytes) > 1024**3 else f"{int(size_bytes)/1024/1024:.0f}MB"
            return {"idx": idx, "status": "✅ 可下载", "ep_count": count, "size": size, "note": note}
        return {"idx": idx, "status": "❌ 同步为空", "ep_count": 0, "size": "-"}
    except:
        return {"idx": idx, "status": "❌ 统计失败", "ep_count": 0, "size": "-"}

def main():
    print("🚀 启动极速并发探路...")
    with open(PANSOU_FILE) as f: items = json.load(f)
    
    top_items = items[:5]
    token = login_gbox()
    results = []
    threads = []

    def run_probe(i, item):
        res = probe_link(i+1, item, token)
        results.append(res)

    for i, item in enumerate(top_items):
        t = threading.Thread(target=run_probe, args=(i, item))
        t.start()
        threads.append(t)

    for t in threads: t.join(timeout=12)

    # 排序：优先可下载且文件多的
    results.sort(key=lambda x: x.get("ep_count", 0), reverse=True)
    
    print(f"\n🎬 【探路结果】 (耗时 {12}s, 实际可能更短)")
    for i, r in enumerate(results):
        mark = "🔥推荐" if i==0 and r["ep_count"] > 0 else ""
        print(f"{i+1}. [{r['idx']}] {r['note'][:20]:<20} | 📺{r['ep_count']} | 📦{r['size']} | {r['status']} {mark}")

if __name__ == "__main__":
    main()
