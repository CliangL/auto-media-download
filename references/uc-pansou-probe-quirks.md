# UC / PanSou 探路误判修复记录

## 背景
用户反馈《佳偶天成》PanSou 搜索中 UC 有 3 个资源，其中 2 个有效，`https://drive.uc.cn/s/c48a2752261f4?public=1` 集数最多；但 `probe_pansou.py` 误判为“未找到文件夹”。

## 现场现象
- 手动指定导入 `c48a2752261f4` 后，GBox 返回：`{"success": 1, "failed": 0, "errors": []}`。
- GBox shares 中出现：`/🍓我的UC分享/佳偶天成-c48a27`，`shareId=c48a2752261f4`。
- AList `/🍓我的UC分享` 可见目录：`佳偶天成-c48a27`。
- 修复后 `probe_pansou.py` 可识别：
  - 类型：UC
  - 路径：`/🍓我的UC分享/佳偶天成-c48a27/J 加欧体传 (2026)`
  - 统计：30 个视频文件（用户口径有效正片约 27 集，需注意 4K/重复/花絮等裸文件数可能偏大）
  - 直链样本：`27~4K.mp4`

## 根因
1. GBox/UC 实际落盘名不稳定：可能不用脚本请求的 `clean_name_shareid`，而是 `标题-短ID`、纯标题或 GBox 自己清洗后的名称。
2. UC 无密码链接有时需要三段导入格式里第三段再次传 `share_id`，即 `挂载名<TAB>share_id<TAB>share_id`；只传两段可能返回未确认或找不到目录。
3. 旧 `find_imported_folder()` 只按 `clean_name` 精确/前缀匹配，并且异常分支里缩进错误可能过早 `return None`。

## 固化做法
- `import_share()` 对 UC 无密码链接追加第三段 share_id 作为兜底口令。
- 返回 aliases：`clean_name`、原始标题、`标题-短ID`、`标题_短ID`、短 share_id。
- `find_imported_folder()` 按 aliases 匹配根目录，不只依赖 clean_name。
- 用户手动验证与脚本冲突时，以现场 GBox shares + AList 根目录核实为准，优先修探测逻辑。

## 验证命令
```bash
python3 - <<'PY'
import json
json.dump([{
  'note': '佳偶天成 (2026)',
  'url': 'https://drive.uc.cn/s/c48a2752261f4?public=1',
  'password': '',
  'gbox_type': 7,
}], open('/tmp/pansou_results.json','w'), ensure_ascii=False, indent=2)
PY
PANSOU_PROBE_MAX_CANDIDATES=1 \
PANSOU_PROBE_FOLDER_WAIT=20 \
PANSOU_PROBE_GLOBAL_TIMEOUT=80 \
PANSOU_PROBE_STOP_AFTER_GOOD_COUNT=27 \
python3 $HOME/.openclaw/skills/media/auto-media-download/scripts/probe_pansou.py
```

## 注意
裸视频数不一定等于正片集数。UC 源可能同时含 4K/重复版本，切源前仍要检查集号连续性和是否有重复/花絮。

---

## 2026-05-16 新案例：share_id=162c7bc839ca4 探测失败但并非用户误判

### 现场验证
- 直接用 `probe_pansou.py` 探测 `https://drive.uc.cn/s/162c7bc839ca4`：导入返回 `{'success': 0, 'failed': 0, 'errors': []}`，随后脚本在 `/🍓我的UC分享` 下找不到 `佳偶天成-162c7b / 佳偶天成_162c7b / 佳偶天成`。
- GBox/AList 现场已有多个同名 UC 挂载：
  - `/🍓我的UC分享/佳偶天成`
  - `/🍓我的UC分享/佳偶天成_c48a27`
  - `/🍓我的UC分享/佳偶天成_b4b144`
- 其中 `佳偶天成_b4b144` 可列出子目录 `J 佳偶-@！天成`，递归可数到 23 个视频文件；这证明“脚本曾命中过旧挂载”并非用户凭空质疑。
- 将 `/🍓我的UC分享/佳偶天成` 的 `share_id` 直改为 `162c7bc839ca4` 后，AList 返回 `failed get dir: object not found`；说明这条新 UC 分享在当前 GBox/AList 链路下没有被正确映射为可枚举目录。

### 结论
- 问题不只是“数错 20 多集”，而是 `probe_pansou.py` 在 **同名多挂载 + 导入未确认 + UC 实际落盘名/令牌状态不稳定** 时会误把旧挂载当作新链接结果。
- 用户手动在盘搜/UC 客户端看到“链接里有完整剧集”，不代表当前 GBox/AList 挂载链路已经能列出相同内容；要把“源链接内容”和“当前挂载可枚举内容”分开核对。

### 后续修复方向
- 探测逻辑需要优先按 `share_id` 绑定挂载，不够时再回退名称别名。
- 导入返回 `success=0, failed=0` 时，不能直接认定失败；应通过 GBox shares API 检查记录是否新增或复用了旧挂载，禁止直连 SQLite 判断或修改。
- 对 UCShare 挂载需要单独识别 `share_id/share_pwd/ShareToken/root_folder_id` 异常，不要只扫 AList 根目录名。

## 注意
裸视频数不一定等于正片集数。UC 源可能同时含 4K/重复版本，切源前仍要检查集号连续性和是否有重复/花絮。

---

## UC分享源替换（历史记录：G-Box数据库直改法已废弃）

### 背景（2026-05-09）
用户明确提供新的UC分享链接要求替换失效源时，曾尝试直接修改G-Box数据库。该做法现在只作为历史记录保留，不能作为默认执行流程。

### 当前结论（2026-05-18修正）
- **禁止手写 SQL 直改 G-Box 数据库**：容易被 shell quoting、SQLite 保留字、旧同名挂载污染，且无法保证分享真的被 G-Box 正确导入。
- **禁止因为用户给了 UC 链接就重启 `g-box` / `xy-emd` / `strange_hypatia`**：标准 import API 导入分享后应通过 G-Box shares 和 AList API 验证落盘；失败时停止，不做服务重启。
- 正确路径是：构造当前任务专用 `/tmp/pansou_results.json` 或 `/tmp/probe_results.json` → 运行 `complete-pansou-selection.sh` / `handle-pansou.sh` → 核对 `import + shares + AList` → 递归找到视频 → 生成 STRM。
- UC 无密码链接必须使用三段导入格式：`挂载名<TAB>share_id<TAB>share_id`。

### 架构说明
- **主AList** (strange_hypatia, 端口5678)：不直接支持UCShare驱动，其数据库无法添加UCShare存储
- **G-Box AList** (g-box容器)：专门管理网盘分享，数据库路径 `/vol1/1000/docker/xiaoya/gbox-alist-data/data.db`
- UC分享挂载通过G-Box管理，主AList通过某种代理/联邦机制读取G-Box的挂载

### 旧操作步骤（仅历史摘要，不提供可执行命令）

历史上曾尝试过“定位旧挂载 -> 直接更新 G-Box SQLite 里的 share_id -> 重启容器”的做法。该做法已经废弃，原因是容易被同名旧挂载、shell quoting、SQLite 保留字和 G-Box 内部状态污染；新的执行路径必须使用 GBox import/shares API 和 AList API 交叉验证。

**重启容器（废弃步骤，不要执行）**：
旧记录曾建议在直改数据库后重启相关容器。这个动作已经废弃；当前流程不应提供或执行重启命令。

4. **验证新源可访问**：
```bash
ssh YOUR_USERNAME@YOUR_NAS_IP "
curl -s 'http://127.0.0.1:5678/api/fs/list' -H 'Content-Type: application/json' -d '{"path":"/🍓我的UC分享/佳偶天成_新短ID","page":1,"per_page":50}'
"
```

### 历史关键点
- Share ID提取：UC链接 `https://drive.uc.cn/s/c48a2752261f4?public=1` 中ID为 `c48a2752261f4`
- Mount path短ID：通常取share_id前6位，如 `/🍓我的UC分享/佳偶天成_c48a27`
- 主AList数据库**不支持**直接添加UCShare存储，尝试会报错（如缺少enable_sign列或驱动不支持）
- “改完数据库后重启容器生效”是旧直改 DB 思路下的历史判断；当前标准流程不依赖这个动作，不能在导入/验证失败时重启容器。

### 验证STRM更新
更新源后需重新生成STRM文件：
```bash
python3 $HOME/.openclaw/skills/media/auto-media-download/scripts/gen-strm.py \
  --source-path "/🍓我的UC分享/佳偶天成_c48a27" \
  --strm-path "/vol1/1000/docker/xiaoya/strm/C-每日更新/电视剧/佳偶天成/Season 1" \
  --media-type tv
```
