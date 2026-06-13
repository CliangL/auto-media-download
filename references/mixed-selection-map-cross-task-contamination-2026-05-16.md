# mixed selection map 污染导致误选别的片名（2026-05-16）

## 现象
在《佳偶天成》切源任务中，先跑了 `pansou-search.py` + `probe_pansou.py`，随后直接执行：

```bash
MEDIA_TYPE=tv bash scripts/complete-pansou-selection.sh 1
```

结果并没有导入《佳偶天成》的 UC 结果，而是输出：

```text
✅ 展示序号 1 映射为 Xiaoya/AList 第 1 项
片名: 黑夜告白 | 类型: tv
```

说明 `complete-pansou-selection.sh` 优先读取 `/tmp/media_selection_map.json`，而该文件里残留的是**别的搜索任务**生成的 mixed 列表。

## 现场证据
`/tmp/media_selection_map.json` 当时内容：

```json
[
  {
    "kind": "xiaoya",
    "resource_index": 1,
    "title": "黑夜告白",
    "display_index": 1
  },
  {
    "kind": "pansou",
    "probe_index": 1,
    "title": "佳偶天成",
    "display_index": 2
  }
]
```

因此：
- 展示序号 `1` → 被映射到 `黑夜告白`
- 《佳偶天成》实际是 `display_index=2`

但这并不是当前用户眼里看到的“本次候选序号”，而是**旧任务遗留映射**。

## 正确处理
当目标是**直接执行当前 probe 结果**，且已确认 `/tmp/probe_results.json` 就是本次任务时：

```bash
AUTO_MEDIA_IGNORE_SELECTION_MAP=1 MEDIA_TYPE=tv bash scripts/complete-pansou-selection.sh 1
```

这样脚本会跳过 `/tmp/media_selection_map.json`，直接按 `/tmp/probe_results.json` 的 probe 序号取第 1 条。

## 适用条件
只在下面条件同时满足时使用这个兜底：
1. 你明确知道当前任务要吃的是 `/tmp/probe_results.json`
2. 你确认 `probe_results` 里的第 N 条就是用户授权要执行的结果
3. 你已经发现 `/tmp/media_selection_map.json` 是旧任务残留或跨任务污染

## 不要这样做
- 不要在**未核对 map 内容**时盲目执行 `complete-pansou-selection.sh 1`
- 不要把“展示序号”和“probe 序号”默认当成一回事
- 不要在 mixed 列表污染后继续向用户汇报“已按 1 号执行”，实际却导入了别的剧

## 推荐排查顺序
1. `cat /tmp/media_selection_map.json`
2. `cat /tmp/probe_results.json`
3. 对比 `title/display_index/probe_index`
4. 若 map 污染：用 `AUTO_MEDIA_IGNORE_SELECTION_MAP=1`
5. 执行后再核验 `drama-state.json`、STRM 目录、最终 source_path

## 本次任务最终正确结果
- 目标剧：`佳偶天成`
- 正确 UC 源：`https://drive.uc.cn/s/b4b144a82f924`
- 最终挂载路径：`/🍓我的UC分享/电视剧/佳偶天成/J 佳偶-@！天成`
- 资源实际视频数：`23`
