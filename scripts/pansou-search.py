#!/usr/bin/env python3
"""pansou-search.py - PanSou 搜索 + 结果展示 (v2.2)

v2.2 (2026-04-13): 
  - ⭐ UC 网盘资源优先展示，无 UC 则展示其他网盘
  - 🔧 修复 SSL 验证问题 (添加 unverified context)
  - 🔧 改进匹配逻辑和错误提示
"""
import json, sys, urllib.request, ssl, os

PANSOU_URL = "https://pansou.example.com:16666"
PANSOU_PRIORITY = ["uc", "quark", "aliyun", "115", "xunlei"]
GBOX_TYPE_MAP = {"uc": 7, "quark": 5, "aliyun": 0, "115": 8, "xunlei": 2}
DEFAULT_ONLY_TYPES = os.environ.get("PANSOU_ONLY_TYPES", "uc").strip()
if str(os.environ.get("PANSOU_INCLUDE_OTHER", "")).lower() in ("1", "true", "yes"):
    DEFAULT_ONLY_TYPES = ""
if str(os.environ.get("PANSOU_UC_ONLY", "")).lower() in ("1", "true", "yes"):
    DEFAULT_ONLY_TYPES = "uc"
ONLY_TYPES = {
    item.strip().lower()
    for item in DEFAULT_ONLY_TYPES.split(",")
    if item.strip()
}
DISPLAY_LIMIT = int(os.environ.get("PANSOU_DISPLAY_LIMIT", "20" if ONLY_TYPES == {"uc"} else "8"))

def canonicalize_title(title):
    if str(os.environ.get("PANSOU_ALLOW_ALIAS_QUERY", "")).lower() in ("1", "true", "yes"):
        return title
    replacements = {
        "权利的游戏": "权力的游戏",
    }
    for wrong, right in replacements.items():
        title = title.replace(wrong, right)
    return title

def main():
    raw_title = sys.argv[1] if len(sys.argv) > 1 else sys.exit("用法: pansou-search.py 片名 [--offset N]")
    title = canonicalize_title(raw_title)
    offset = 1
    if "--offset" in sys.argv:
        idx = sys.argv.index("--offset")
        offset = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 1
    
    url = f"{PANSOU_URL}/api/search"
    body = json.dumps({"kw": title}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    
    try:
        # 忽略 SSL 证书验证 (pansou.example.com 可能是自签证书)
        ssl_context = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=20, context=ssl_context) as resp:
            data = json.loads(resp.read())
        
        code = data.get("code", -1)
        if code != 0:
            print(f"❌ PanSou 返回错误: {data.get('message', 'Unknown')}")
            sys.exit(1)

        merged = data.get("data", {}).get("merged_by_type", {})
        
        # ⭐ 默认只搜索 UC。用户明确允许其它网盘时，可用
        # PANSOU_INCLUDE_OTHER=1 或 PANSOU_ONLY_TYPES=uc,quark 覆盖。
        uc_results = []
        other_results = []
        
        for ptype in PANSOU_PRIORITY:
            if ONLY_TYPES and ptype not in ONLY_TYPES:
                continue
            for item in merged.get(ptype, []):
                note = item.get("note", "")
                t, n = title.lower(), note.lower()
                # 匹配逻辑：包含关系
                if t in n or n in t or t == n:
                    result_item = {
                        "note": note,
                        "url": item.get("url", ""),
                        "password": item.get("password", ""),
                        "source": item.get("source", ""),
                        "pan_type": ptype,
                        "gbox_type": GBOX_TYPE_MAP.get(ptype, -1)
                    }
                    if ptype == "uc":
                        uc_results.append(result_item)
                    else:
                        other_results.append(result_item)
        
        results = uc_results + other_results
        
        if not results:
            print("❌ PanSou 未找到匹配资源 (尝试更换关键词或检查网络)")
            sys.exit(1)
        
        count = min(len(results), DISPLAY_LIMIT)
        print(f"📋 PanSou 找到 {len(results)} 个候选，展示前 {count} 个")
        if raw_title != title:
            print(f"🔎 已规范搜索词: {raw_title} -> {title}")
        if ONLY_TYPES:
            print(f"🎯 已按网盘类型过滤: {', '.join(sorted(ONLY_TYPES)).upper()}")
        if uc_results:
            print(f"⭐ UC 网盘优先，已置顶 {len(uc_results)} 项")
        print("")
        
        for i, r in enumerate(results[:count], offset):
            pwd_text = f"密码: {r['password']}" if r.get("password") else "无密码"
            uc_tag = " ⭐UC" if r['pan_type'] == 'uc' else ""
            print(f"[{i}]{uc_tag} {r['note']}")
            print(f"    {r['pan_type'].upper()} | {r['url'][:80]} | {pwd_text}")
            print("")
        
        with open("/tmp/pansou_results.json", "w") as f:
            json.dump(results[:count], f, ensure_ascii=False, indent=2)

        for stale in ("/tmp/probe_results.json", "/tmp/probe_mounts.json", "/tmp/media_selection_map.json"):
            try:
                os.remove(stale)
            except FileNotFoundError:
                pass
        
        with open("/tmp/media_resources.txt", "a") as f:
            for i, r in enumerate(results[:count], offset):
                pwd = r.get('password', '')
                note = r.get('note', title)
                f.write(f"PANSOU\t{r['pan_type']}\t{r['url']}\t{pwd}\t{note}\n")
        
        print(f"💾 结果已保存到: /tmp/pansou_results.json")
        if str(os.environ.get("AUTO_MEDIA_AUTO_PANSOU_FALLBACK", "")).lower() not in ("1", "true"):
            print(f"⚠️ 请先运行 probe_pansou.py 核验资源，再回复序号选择")

    except urllib.error.URLError as e:
        print(f"❌ PanSou 网络请求失败: {e.reason}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ PanSou 搜索异常: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
