# 开发日志：推歌推荐功能（每用户记录 + 推荐推送）

> 日期：2026-06-26
> 范围：MayaBot 新增「每用户下载记录持久化」+「推荐推送（每日 8 点 + /randommusic）」
> 依据：`todo.md` 计划 + 用户「人工评价」段锁定的决策

---

## 锁定的决策（来自 todo.md 人工评价）

| 项 | 决定 |
|---|---|
| 下载来源 | **仅 YouTube**。SoundCloud 暂不接（等专门的稳定 handler） |
| 评论生成 | **不接任何 LLM**（已有 API 均为出图方向） |
| 推送形态 | 带音频推到**群聊**，每条音频下挂 👍/👎 反馈按钮 |
| 反馈逻辑 | 👍 → 该艺人加权；👎 → **撤回该消息** + 降权 |
| 偏好存储 | **单张带符号权重表**（正=喜欢，负=不喜欢），非两张表 |
| 手动推送 | `/randommusic` 只推 **1 首** |
| 自动推送 | 每日北京时间 8 点推 **3 首**，可由用户自由开关 |
| 调度方式 | （自行决定）PTB 原生 **JobQueue**（加 `[job-queue]` extra） |

---

## 新增 / 修改的文件

### 新增
- **`utils/music_db.py`** — 持久化层（独立 `music.db`，sqlite，框架无关）
  - `user_downloads`：下载历史（推荐信号 + 去重依据）
  - `user_searches`：搜索关键词（推荐信号）
  - `artist_weights`：单张带符号权重的偏好表（正=喜欢 / 负=不喜欢）
  - `push_subscriptions`：每日自动推送的开关（user_id + chat_id）
  - `recommendations`：每条推荐记录（含 chat_id/message_id，供 👎 撤回与反馈调权）
- **`utils/recommend.py`** — 推荐算法（仅 YouTube）
  - 种子：下载高频艺人 + 正权艺人 + 近期搜索词；冷启动回落 config 默认种子
  - 候选：`ytsearch` 拉取 → 去已下载 → 剔除「不喜欢」艺人（权重 ≤ 阈值）→ 过滤超长
  - 打分：种子重要度 + 偏好权重 + 随机扰动（避免每天雷同）→ 取 TopN
- **`handlers/recommend.py`** — `/randommusic` + 反馈回调 + 每日定时任务
  - `/randommusic`：手动推 1 首
  - `/randommusic on|off|status`：每日自动推送订阅开关 / 查询
  - 👍/👎 回调：仅推荐目标本人可操作；👎 删除消息 + 降权
  - `_daily_push_job`：每日 8 点遍历订阅会话，各推 3 首（每首独立消息+按钮，下载间隔缓解限流）

### 修改
- **`handlers/music.py`** — 下载成功写 `user_downloads`，搜索时写 `user_searches`（复用 music_db）
- **`bot.py`** — 导入并注册 recommend handler，启动时 `recommend_setup()` 建库
- **`requirements.txt`** — `python-telegram-bot` → `python-telegram-bot[job-queue]`（含 APScheduler）
- **`docker-compose.yml`** — 挂载 `./music.db:/app/music.db` 持久化
- **`config.json`** — 新增 `recommend` 段（种子、打分权重、推送时间/条数等）

---

## 关键设计点

- **偏好用单张带符号权重表**：`weight` 正负即喜欢/不喜欢，`bump_artist_weight` 用 upsert 累加；
  下载本身也视为弱正反馈（+1），👍 +2，👎 -3（可在 music_db 顶部常量调）。
- **👎 能精确撤回**：推荐发送前先 `create_recommendation` 拿 rec_id 放进 callback_data，
  发送后 `set_recommendation_message` 回填 message_id；👎 时据此 `message.delete()`。
- **反馈鉴权**：回调校验 `from_user.id == 推荐目标 user_id`，防群里他人乱点污染口味。
- **定时任务无消息上下文**：靠 `push_subscriptions` 持久化的 chat_id 才知道推到哪、@ 谁。
- **限流缓解**：每日推送多首之间 `download_gap_sec` 间隔；种子数与每种子候选数都受限。

---

## 验证

在本机 Docker 重建并启动后确认（2026-06-26）：

- ✅ 镜像构建成功，`apscheduler` 随 `[job-queue]` extra 一并安装
- ✅ 容器健康运行（`Up`），`Application started`
- ✅ handler 注册：`Music handler registered (/music)`、`Recommend handler registered (/randommusic)`
- ✅ 每日推送调度生效：`每日推送已调度：08:00 (Asia/Shanghai)`
- ✅ `music.db` 挂载正常（文件非目录），5 张业务表齐全：
  `artist_weights / push_subscriptions / recommendations / user_downloads / user_searches`
- ✅ 容器内 import 链通过（`utils.recommend` / `utils.music_db`）
- 各 .py 文件均通过 `py_compile` 语法校验；`config.json` 合法

> 说明：未做端到端真实推送测试（需真实 Telegram 群 + 用户交互）。
> 逻辑与接线已验证；首次实测建议：私聊或群里 `/music` 听几首 → `/randommusic` 看推荐 →
> 点 👍/👎 验证加权与撤回 → `/randommusic on` 验证每日订阅。

---

## 命令速查

| 命令 | 作用 |
|---|---|
| `/music <关键字/链接>` | 搜索并下载（写入下载/搜索历史） |
| `/randommusic` | 立即推荐 **1 首**（带 👍/👎） |
| `/randommusic on` | 开启本会话每日 8 点自动推送（3 首） |
| `/randommusic off` | 关闭本会话自动推送 |
| `/randommusic status` | 查看本会话订阅状态 |

## 后续 / 待办

- SoundCloud 源：等稳定下载 handler 后，把 recommend/ music 的 provider 放开。
- 推荐质量：当前为「种子搜索 + 权重打分」，可观察实际效果后再调 `config.json` 的权重与种子。
- 反馈调权幅度：`utils/music_db.py` 顶部 `LIKE_DELTA / DISLIKE_DELTA` 可按体感调整。
