---
name: auto-media-download
description: 自动搜片、下载 STRM、自动追剧的家庭影音管理系统。
version: "6.3.0"
author: "CoPaw (integrated from Hermes)"
commands:
  - name: "/watch <剧名>"
    description: "搜索并添加影视剧到 NAS，自动生成 STRM 并可选开启追剧。"
  - name: "/drama-status"
    description: "查看当前追剧状态列表。"
  - name: "/check-drama"
    description: "强制检查一次所有追剧更新并补全 STRM。"
---

# auto-media-download - 影音自动下载

触发：用户说"我想看XX""想看XX""下载XX""搜一下XX" + 影视名。

## 核心执行流程

严格按顺序，不要跳步：

1. **搜索资源**（默认 xiaoya 优先）：
```bash
AUTO_MEDIA_AUTO_HYPERMS_FALLBACK=1 bash $HOME/.openclaw/skills/media/auto-media-download/scripts/media-download-v2.sh "剧名"
```

2. **展示候选**：直接贴脚本输出的候选摘要，补一句"回复序号选择"。禁止重新整理成长表。

3. **用户选择后生成 STRM**：
```bash
MEDIA_TYPE=tv bash $HOME/.openclaw/skills/media/auto-media-download/scripts/complete-media-selection.sh <序号>
# 电影用 MEDIA_TYPE=movie，动漫用 MEDIA_TYPE=anime
```

4. 脚本完成后只报告关键事实（导入/STRM/集数/状态/目录），不做长篇推理。

## 快速路径（禁止绕过）

- **用户指定 HyperMS/网盘/UC/115**：
```bash
AUTO_MEDIA_FORCE_HYPERMS=1 bash $HOME/.openclaw/skills/media/auto-media-download/scripts/media-download-v2.sh "剧名"
```
  跳过 xiaoya，只从当前消息提取片名，禁止把平台词拼进片名。

- **用户回复纯数字序号**：直接运行 `complete-media-selection.sh <序号>`。禁止在执行前再调用 skill_view、memos_get、todo、read_file、search_files、联网确认或手写 Python。

- **xiaoya 无结果时**：脚本已自动 fallback 到 HyperMS（`AUTO_MEDIA_AUTO_HYPERMS_FALLBACK=1`），禁止先问用户"是否继续搜"。

## 高频错误防护

1. **每次新搜索前必须清理旧临时文件**：`/tmp/media_resources.txt`、`/tmp/media_selection_map.json`、`/tmp/hyperms_selection_map.json`、`/tmp/pansou_results.json`、`/tmp/probe_results.json`。禁止读取旧文件决定候选。
2. **`complete-media-selection.sh` 是选择后的唯一入口**：禁止手动编辑 `drama-state.json`，禁止手写 Python 修改状态，禁止只运行 `sync-drama-state.py push`。
3. **展示序号 ≠ 内部序号**：禁止把展示序号直接传给 `complete-pansou-selection.sh` 或 `handle-selection.sh`；统一用 `complete-media-selection.sh`。
4. **禁止手写临时 Python 探测资源**：必须直接运行 `scripts/probe_pansou.py`，不要 `import probe_pansou`。
5. **追剧状态唯一真值源**：NAS canonical `/vol1/1000/docker/xiaoya/data/drama-state.json`。查看前必须先 `sync-drama-state.py pull`。
6. **资源名里的"更23/更新至23集"只代表当前进度**，不代表总集数。必须联网查总集数后才能判定完结。
7. **GBox 残留要清两层**：探路/导入产生的无效挂载，不能只删网页 `/api/shares`；要同步清 live DB `x_storages`。统一复用 `devops/gbox-strm-mount-sync/scripts/gbox-dedupe-mounts.py`，默认只处理 `5xxx` 自定义挂载并保留 `7xxxx`。
8. **115 分享“文件列表 1 项”只是根层数量**：不能据此判断资源少；若根层是文件夹，必须用 HyperMS `/api/v1/share-links/browse` 递归展开子目录，统计视频集号和清晰度目录后再下结论。

## 关键约束

- **片名保真**：搜索词用用户原始片名，不要拼入平台词（UC/115/网盘）
- **本地库口径**：`NAS 已有` 只指 STRM 目录已存在，xiaoya/AList 源目录不算"已有"
- **总集数判定**：必须走 `scripts/decide-tracking.py` 联网确认，禁止用文件数当总集数
- **禁止浏览器自动化**：总集数/更新状态只走 TMDB/Brave/HTML 搜索链路

## 其他入口

检查本地库：
```bash
python3 scripts/check-library-presence.py --media-type tv --title "剧名"
```

查看追剧列表（先 pull 再看）：
```bash
python3 scripts/sync-drama-state.py pull
python3 scripts/list-drama-state.py
```

追剧校准：
```bash
python3 scripts/reconcile-drama-state.py
```

## 全量排查模式

用户说"全部查完/一次性清完/搞完再让我确认"时：
- 开头输出一次步骤清单，然后连续处理到完成
- 中途禁止发阶段性总结/局部战报
- 只在全部完成后或遇到真正阻塞时才发正文

## 排障参考

遇到边缘问题时按需加载 `references/` 目录：
- `full-skill-backup-20260519.md` — 完整历史规则（别名漏判、PanSou 探路、UC 三层核验等）
- `drama-source-integrity-2026-05-16.md` — 源完整性排查
- `mixed-selection-map-cross-task-contamination-2026-05-16.md` — 映射污染兜底
- `uc-pansou-probe-quirks.md` — UC/PanSou 探路注意事项
- `game-of-thrones-alias-pansou-fallback-2026-05-18.md` — 别名回退搜索
- `new-show-premiere-timing-2026-05-17.md` — 新剧首播总集数滞后
