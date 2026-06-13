# auto-media-download

[English](#english) | [中文](#中文)

一个给 AI agent（Hermes / OpenClaw / Claude Code 等支持 Skill 的运行时）使用的**影视自动下载 + 追剧管理** Skill。你只要对 agent 说一句"我想看《XX》"，它就会自动搜索资源、在 NAS 上生成 STRM、判断总集数并加入追剧列表，每天定时检查更新。

> An **auto media download + drama-tracking** Skill for AI agents. Say "I want to watch *X*" and the agent searches sources, generates STRM files on your NAS, figures out the total episode count, and tracks the show for daily updates.

---

## 中文

### 它能做什么

- **自然语言下载**：对 agent 说"我想看/下载/搜一下 XX"，自动完成搜索 → 选源 → 生成 STRM → 入库。
- **多源搜索**：优先 Xiaoya/AList 索引，自动回退到 PanSou（网盘聚合）+ g-box 探路验证真实集数。
- **总集数自动判定**：豆瓣（国产剧最权威）+ TMDB（海外剧，可选 key）+ 百度/搜狗兜底，多源交叉；查不到的新剧先按"在播"入库，由定时任务自动重查补全。
- **自动追剧**：每天定时检查在追剧集的更新，补全缺集，完结后自动移出列表。
- **防冒名**：拒绝"同名不同人/翻唱"的假资源混入。

### 环境要求

这个 Skill 编排的是**你自己的 NAS 媒体栈**，不是云服务。需要：

1. **一台 NAS**，可 SSH 登录（脚本通过 SSH 在 NAS 上执行下载/生成 STRM）。
2. NAS 上的 **Docker 容器**（缺哪个 `install.sh` 会检测并提示，agent 可协助部署）：
   - **Xiaoya / AList**（必需）：影视资源索引，暴露 `/api/fs/list` 和 `/d` 直链（默认端口 5678）。
   - **g-box**（必需）：网盘（UC/夸克/115 等）挂载与探路，暴露 `/api/...`（默认 4567）。
   - **PanSou**（可选）：网盘资源搜索聚合（默认 8080）。
   - **Emby / Navidrome 等播放端**（可选）：消费生成的 STRM。
3. **运行 agent 的机器**上需要：`python3` `jq` `curl` `ssh`（可选 `sshpass`）。`install.sh` 会检测，授权后可用 `--install-deps` 自动装。
4. （可选）**TMDB API Key**：海外剧总集数更准。免费注册 https://www.themoviedb.org/settings/api 。不填也能用（国产剧靠豆瓣）。

### 安装

```bash
# 1. 克隆到 agent 的 skills 目录（路径取决于你的运行时）
#    Hermes/OpenClaw 通常是 ~/.hermes/skills/media/ 或 ~/.openclaw/skills/
git clone https://github.com/CliangL/auto-media-download.git

# 2. 运行安装脚本：自动检测 NAS 容器和端口，只问必填项
cd auto-media-download
bash install.sh
```

`install.sh` 会：
- 检测本机依赖（python3/jq/curl/ssh）；
- 让你输入 **NAS SSH 地址 + 密码**（唯一必填的敏感项）；
- **SSH 到 NAS 自动 `docker ps`**，识别 Xiaoya/g-box/PanSou 容器和端口，自动填入配置；
- 只在检测不到时才追问地址，其余自动；
- 把真实配置写进 `config/media-config.json`（已被 `.gitignore` 排除，**不会进 git**）；
- 生成环境检测报告 `install-plan.md`，缺容器时指导 agent 部署。

重新配置：`bash install.sh --reconfigure`。

### 使用

安装后直接对 agent 说话即可，无需记命令：

- "我想看《狂飙》" / "下载流浪地球2" / "搜一下庆余年" → 自动搜索并展示候选，回复序号即下载。
- "查看追剧列表" / "强制检查追剧更新"。

### 配置：哪些自动、哪些要你填

| 项 | 来源 |
|---|---|
| Xiaoya/g-box/PanSou 地址端口 | **自动**（SSH 探测 NAS 容器） |
| 依赖检测/安装 | **自动**（授权后 `--install-deps`） |
| 追剧状态/历史 | **自动**（首次建空文件） |
| NAS SSH 密码 | **需你填**（敏感，只存本地 config） |
| g-box 密码 | 需你填（默认 admin/admin 可回车跳过） |
| TMDB API Key | 可选（不填用豆瓣） |

### 已知限制

- 依赖 Xiaoya/AList + g-box 这套特定 NAS 架构；没有等价资源源时无法工作。
- 免费网盘源受版权波动：全平台 VIP 独占的资源可能下不到（脚本会如实报告，不下假货）。
- 同名多版本/多季剧（如"三体"腾讯版 vs 其他），刚开始追、集数少时可能选错版本，随追剧进展自动校准。

---

## English

### What it does

- **Natural-language downloads**: tell the agent "I want to watch *X*" — it searches, picks a source, generates STRM on the NAS, and adds it to your library.
- **Multi-source search**: Xiaoya/AList index first, falling back to PanSou + g-box probing to verify real episode counts.
- **Automatic total-episode detection**: Douban (best for Chinese dramas) + TMDB (overseas, optional key) + search-engine fallback, cross-checked. New shows with no data yet are added as "airing" and auto-rechecked by the cron job.
- **Drama tracking**: daily checks for new episodes of tracked shows, fills gaps, auto-archives finished shows.
- **Counterfeit guard**: rejects same-title-different-artist / cover-version fakes.

### Requirements

This Skill orchestrates **your own NAS media stack**, not a cloud service. You need:

1. **A NAS reachable via SSH** (scripts run downloads/STRM generation on the NAS over SSH).
2. **Docker containers on the NAS** (`install.sh` detects what's missing; the agent can help deploy):
   - **Xiaoya / AList** (required): media index, exposes `/api/fs/list` and `/d` direct links (default port 5678).
   - **g-box** (required): cloud-drive (UC/Quark/115…) mounting & probing, exposes `/api/...` (default 4567).
   - **PanSou** (optional): cloud-drive search aggregator (default 8080).
   - **Emby / Navidrome etc.** (optional): consume the generated STRM.
3. On the **machine running the agent**: `python3` `jq` `curl` `ssh` (optional `sshpass`). `install.sh` checks these and can auto-install with `--install-deps`.
4. (Optional) **TMDB API Key** for more accurate overseas episode counts — free at https://www.themoviedb.org/settings/api . Works without it (Chinese dramas use Douban).

### Install

```bash
# 1. Clone into your agent's skills directory
git clone https://github.com/CliangL/auto-media-download.git
cd auto-media-download

# 2. Run the installer — auto-detects NAS containers/ports, asks only for required secrets
bash install.sh
```

`install.sh` detects local deps, asks for **NAS SSH address + password** (the only required secret), then **SSHes into the NAS and runs `docker ps`** to auto-discover Xiaoya/g-box/PanSou ports. It writes the real config to `config/media-config.json` (git-ignored — **never committed**) and produces `install-plan.md` to guide container deployment if needed. Reconfigure with `bash install.sh --reconfigure`.

### Usage

Just talk to the agent: "I want to watch *Breaking Bad*", "download *X*", "show my tracked dramas", "force-check drama updates".

### Auto vs. manual config

| Item | Source |
|---|---|
| Xiaoya/g-box/PanSou URLs & ports | **Auto** (SSH probe) |
| Dependency detect/install | **Auto** (`--install-deps`) |
| Tracking state/history | **Auto** (created empty) |
| NAS SSH password | **You provide** (local config only) |
| g-box password | You provide (default admin/admin) |
| TMDB API Key | Optional (Douban otherwise) |

### Known limitations

- Requires the Xiaoya/AList + g-box NAS architecture; won't work without equivalent sources.
- Free cloud-drive sources fluctuate with copyright; platform-exclusive VIP titles may be unavailable (the script reports honestly and never downloads fakes).
- Same-title multi-version/multi-season shows may pick the wrong version early on (few local episodes), self-correcting as tracking progresses.

---

**安全提示 / Security**: 真实密码、内网 IP、API key 只存在本地 `config/media-config.json`（已被 `.gitignore` 排除）。请勿手动提交该文件。Real secrets live only in the git-ignored `config/media-config.json` — never commit it.
