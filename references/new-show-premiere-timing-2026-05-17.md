# 新剧开播当天 decide-tracking.py 搜不到总集数

## 现象

2026-05-17 用户搜《家业》（杨紫、韩东君主演），当天 18:00 在央视八套首播。
- xiaoya 源只搜到首日更新的 4 集（4K）
- `handle-selection.sh` → `decide-tracking.py` 返回 `needs_total`
- 用户质疑脚本有问题
- 约 1 小时后重跑 `decide-tracking.py --title "家业" --media-type tv --shell`
  - 搜狗搜索命中 `html_search:20 [40, 40, 40]`
  - 返回 `DECISION_TOTAL=40, DECISION_STATUS=ongoing`

## 根因

- 该剧当天刚开播，百度/搜狗等中文搜索引擎尚未完成索引
- 平台上（爱奇艺/百度百科/TVMao）已有 40-42 集信息，但搜索引擎未及时收录
- `decide-tracking.py` 使用 baidu/sogou 搜索 + HTML 解析，依赖搜索引擎索引状态
- Brave Search 当时也未命中（可能受地域/搜索配额影响）

## 处理后

手动 rerun `decide-tracking.py` 约 1 小时后成功，total=40，自动写入 `known-totals.json`。

## 教训

- 新剧开播当天（尤其是 18:00-22:00 黄金时段）存在搜索引擎索引延迟窗口
- `decide-tracking.py` 返回 `needs_total` 时，不一定总是脚本故障
- 对于当天开播的新剧，重试间隔 30-60 分钟通常能查到
- 如果用户质疑脚本坏了，先手工 rerun `decide-tracking.py --title "片名" --media-type tv --shell` 确认，再决定是否修脚本
