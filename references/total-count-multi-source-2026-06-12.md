# 总集数判定多源增强 — 2026-06-12

## 背景（病根）
原 `decide-tracking.py` 名义上有 6 个数据源，但 **TMDB / Brave 都没配 key 而失效**，实际只靠
`html_search`（百度/搜狗网页解析）一条腿。新剧首次入库无缓存时，独腿 + 新剧数据滞后 →
经常 `needs_total` 卡住，或被脆弱的网页解析判错。

## 改动
1. **新增豆瓣源** `douban_candidates()`（国产剧/亚洲剧最权威、最及时，免费无 key）：
   - `movie.douban.com/j/subject_suggest` 按剧名拿候选 id（精确消歧，能区分分季）
   - `m.douban.com/rexxar/api/v2/tv/{id}`（带 iPhone UA + Referer）取 `episodes_count`
   - `origin=douban`，`ORIGIN_PRIORITY=7`，`confidence=0.88`。`completed_hint=False`，
     完结与否交给 `decision_from_total` 用 current vs total 判断。
2. **TMDB 接通**：key 写入 `config/media-config.json` 的 `tmdb.api_key`。
   - 标题匹配**收紧**：去季号基名需 `== target`（或原名/完整名相等），拒绝
     "北上" 子串误匹配 "北上广不相信眼泪"。`_SEASON_SUFFIX_RE` 通用季号正则。
   - `request_json` 加 `retries` 参数，TMDB search/detail 用 `retries=2` 抗代理偶发超时。
3. **新剧兜底**：`decide()` 查不到权威总集数但 `current>0` 时，返回
   `status=ongoing` + `total_episodes=None` + `total_pending=True`，**先入库追剧**，
   由 `reconcile-drama-state.py`（cron 每日 2 次）自动重查补全，而不再 `needs_total` 阻塞。
   `current<=0`（无在播集数可依据）才 `needs_total`。
4. **清理脏缓存**：`known-totals.json` 里之前 TMDB 子串误匹配存入的错误值
   （如 `北上 -> 44, evidence=TMDB:北上广不相信眼泪`）。清理规则：evidence 剧名去季号 != key。

## 已知限制
**同名多版本/多季剧**（三体腾讯版30 / 其他版本；权游各季10）：`decide-tracking` 只有
剧名+current，无法知道用户下载的是哪个版本/季。current 小时 `choose_candidate` 的
`-abs(total-current)` 会偏向接近 current 的候选，可能选到非主流版本集数。随追剧
current 增长会自我校准。根治需引入 `source_path` 版本/季消歧。

## 验证
回归测试通过。翘楚 24 / 莫离 40 / 庆余年 46 completed / 北上 38（清脏缓存后）。
