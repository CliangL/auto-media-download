# GBox 探路残留清理增强 — 2026-06-12

## 病根（为什么网页里还能看到残留）
探路 `probe_pansou.py` 对每个候选 `import_share` 创建 GBox 挂载，探路结束后
`cleanup_stale_probe_mounts` 清理（`AUTO_CLEAN_PROBE_MOUNTS` 默认开）。但清理只覆盖
**记进 `probe_mounts` 的挂载**，而 `probe_mounts.append` 原来只在「找到 folder」时执行：
- import 成功但「未确认快速跳过」（FAST_UNCONFIRMED_UC）→ 挂载没登记 → 漏清
- import 成功但 `find_imported_folder` 找不到 → 挂载没登记 → 漏清
加上 `delete_gbox_folder` 只按 path 匹配（folder_path 为空时删不掉）、静默失败无重试。

## 改动
1. **`delete_gbox_folder(token, path, share_id=None, retries=2)`**：优先用 share_id
   （`row_matches_share_id`）精确匹配删除——即便 folder_path 未知也能回收；删除失败
   重试 2 次。无 path 无 share_id 时直接返回 0，避免误删。
2. **import 成功立即登记**：探路循环里 `import_share` 一返回 share_id 就 append 一个
   `mount_record`（含 share_id、clean_name、aliases，folder_path 待补），无论后续探路
   成败都能按 share_id 回收。folder_path 找到后回填 `mount_record["folder_path"]`。
3. **`cleanup_stale_probe_mounts` 用 share_id 兜底**：每条 record 用 `share_id` + paths
   删除；命中正式追剧源（protected）整条跳过；folder_path 为空也能按 share_id 删。
4. 空文件夹/不可播放的当场删也传 `share_id` 双保险。

## 安全性
share_id 精确匹配只删本次探路对应的挂载，`selected`（用户选定源）与 `protected`
（drama-state 的 source_path）都跳过，不会误删正式资源。纯增强，不改变正常路径行为。

## 未真实测试
改动需真实探路才能端到端验证（会动 GBox/网盘）。已通过语法 + 逻辑 review。
下次搜片探路后，到 GBox 资源页确认未选候选是否清空；若仍有残留，查 `delete_gbox_folder`
的 share_id 匹配是否命中（`row_matches_share_id` 对该网盘类型的判定）。

## 探路提速（同日已做）
`probe_pansou.py` 主循环从串行改为小并发（`PANSOU_PROBE_CONCURRENCY`，默认 3 路）：
单候选处理提取为闭包 `probe_one`，用 `ThreadPoolExecutor` 并发，`threading.Lock` 保护
`probe_mounts`/`probe_results` 收集，`stop_event` 实现早停（某候选命中 STOP_AFTER_GOOD_COUNT
后，未开始的候选在 probe_one 开头直接返回）。mock 验证：5 候选 1.7s vs 串行 4.0s（2.4x），
线程安全收集无丢失，早停正确跳过未开始候选。GBox 并发限流风险用默认 3 路控制；
**真实探路未测**——下次搜片留意 import 失败率是否上升，如有可调小 `PANSOU_PROBE_CONCURRENCY=2`
或设回 `1`（退化为串行）。原串行版备份在 `probe_pansou.py.bak-pre-parallel-*`。
