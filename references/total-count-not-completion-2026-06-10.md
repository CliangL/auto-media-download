# 总集数不等于完结状态（2026-06-10）

## 触发场景

新剧/热播剧页面常写：

- `全40集`
- `共40集`
- `首播`
- `每日/每晚更新`

这类文本里的 `全40集` 只是计划总集数或季集数元数据，不能单独作为“已完结”证据。

## 已修复的问题

`decide-tracking.py` 曾把 `全\d+集` 作为 completed_hint，导致《莫离》这种 2026-06-09 刚开播、当前 5/40 的剧被判成“已完结但源不全”，入口脚本随后不写入追剧。

## 规则

1. `已完结` / `完结` / `全集` / `40集全` 才是明确完结信号。
2. `全40集` / `共40集` 只表示总集数。
3. 同一文本出现 `首播`、`开播`、`每日`、`每晚`、`更新中` 等，应标记 ongoing_hint。
4. `handle-selection.sh` 和 `handle-pansou.sh` 对 `needs_recovery` 要二次保护：只有看到明确完结信号才转 completed；否则降级为 ongoing 保留追剧。

## 回归验证

运行：

```bash
cd $HOME/.openclaw/skills/media/auto-media-download
python3 scripts/test_decide_tracking_regressions.py
python3 scripts/decide-tracking.py --title "莫离" --media-type tv --current 5 --note "莫离" --source-path "/🍓我的UC分享/莫离_pluginhuban_48d1a5"
```

期望：

- `全40集 + 首播/每日更新` 的部分资源判定为 `ongoing`
- `已完结，共40集` 的部分资源仍判定为 `needs_recovery`
- 《莫离》当前应为 `ongoing 5/40`
