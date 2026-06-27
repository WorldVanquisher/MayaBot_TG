# MayaBot 推歌功能 — 实施计划（待评审）

> 状态：方案待评审，**尚未动工**。确认后再实现。
> 涉及两个功能：① 每用户下载记录持久化　② 推荐推送（每日 8 点 + /randommusic）

---

## 已查明的现状（影响架构的事实）

- 框架：`python-telegram-bot==22.5`，polling，已 Docker 化（含 yt-dlp + ffmpeg）。
- 现有持久化范式：`handlers/fortune.py` 用 sqlite（`fortune.db`），`setup()` 建表、`register()` 注册。
- **定时调度**：PTB 的 `JobQueue` 需要安装 `python-telegram-bot[job-queue]`（含 APScheduler）。
  当前 requirements 只装了裸包，**JobQueue 可能不可用**（容器没跑没验证到）→ 见决策 A。
- **文本 LLM**：`utils/doubao_api.py` 只有「图像」生成，**没有文本生成 helper**。
  生成推歌评论需要新增一个文本 LLM 调用。`.env` 里有 `OPENAI_API_KEY` 和 `DOUBAO_*` → 见决策 D。
- 下载发送现状：`/music` 下载的歌 `send_audio` 到 `query.message.chat_id`（即群里所有人可见）。

---

## 功能 1：每用户下载记录（持久化）

**目标**：每个用户正常点击下载的歌曲，持久化记录，作为推荐的数据基础。

- 新建 `music.db`（独立于 fortune.db），Docker 里挂卷持久化。
- 表设计：
  - `user_downloads`：`user_id, title, artist, source(youtube/soundcloud), video_id, url, query, downloaded_at`
  - `user_searches`：`user_id, keyword, searched_at`　（搜索关键词也是推荐信号）
  - `user_playlists`：`user_id, playlist_url, added_at`　（用户提供的 YouTube 歌单，推荐信号之一）
- 写入时机：
  - `handlers/music.py` 的 `cb_download` 成功发送后 → 写 `user_downloads`
  - `cmd_music` 触发搜索时 → 写 `user_searches`
- 新建 `utils/music_db.py` 放所有 sqlite 读写（与 fortune 风格一致，框架无关）。

---

## 功能 2：推荐推送（每日 8 点 + /randommusic）

**目标**：综合「下载历史 + 搜索关键词 + YouTube 歌单」三类信号，每天北京时间早 8 点
主动 @ 用户推送 3 首歌并附评论；用户也可 `/randommusic` 主动触发。

### 2.1 推荐候选生成（核心算法）

「商业推歌逻辑」的务实落地（不依赖外部推荐 API，纯 yt-dlp + 历史）：

1. 从用户历史抽取「种子」：高频艺人、近期搜索关键词、歌单里的曲目。
2. 用种子构造候选：
   - 按种子艺人/关键词做 `ytsearch`/`scsearch` 拉候选；
   - （可选）YouTube Mix（`RD<video_id>`）作为「相似曲目」来源。
3. 过滤：去掉用户已下载过的（查 `user_downloads` 去重）。
4. 打分排序：种子匹配度 + 新鲜度（发布时间）+ 随机扰动（避免每天雷同）。
5. 取 Top 3。
- 冷启动（新用户无历史）：回落到一份「热门/精选」默认种子。

### 2.2 评论生成

- 新增 `utils/text_llm.py`：文本 LLM helper（OpenAI 或 Doubao chat，见决策 D）。
- 输入 3 首歌的标题/艺人 + 用户画像摘要 → 输出每首一句中文短评。
- LLM 失败兜底：用模板化文案（不阻断推送）。

### 2.3 推送触发

- **定时**：每天北京时间 08:00 → 见决策 A（JobQueue vs 系统 cron）。
- **手动**：`/randommusic` → 立即对调用者跑同一套推荐逻辑。

### 2.4 推送内容形态　→ 见决策 B（重要）

候选：
- (B1) 只发「文字推荐 + 链接 + 评论」，**不下载音频**（轻、快、无 429 风险）。
- (B2) 发「文字推荐 + 评论」+ 同时下载并发送 3 个音频文件（重、慢、有 SoundCloud 429 风险，
  我们之前已实际撞到过 429）。

### 2.5 推送目标 / 订阅　→ 见决策 C

- 每日推送需要知道「推给谁、推到哪个 chat」。@ 用户必须在群里。
- 需要一张订阅表 `push_subscriptions`：`user_id, chat_id, enabled, created_at`。
- 用户用某命令（如 `/randommusic on`）在某个群订阅，定时任务遍历订阅推送。

---

## 需要你拍板的决策

- **决策 A（调度方式）**：
  - A1）加 `python-telegram-bot[job-queue]` 依赖，用 PTB 原生 JobQueue 定时（推荐，集成最干净）。
  - A2）用系统 cron / 单独的 asyncio 定时循环（不加依赖，但要自己管时区与重启恢复）。

- **决策 B（推送是否带音频文件）**：
  - B1）仅文字 + 链接 + 评论（推荐：轻量、规避 429、Telegram 50MB 限制无关）。
  - B2）文字 + 评论 + 3 个音频文件（体验完整但重，且有限流/超限风险）。

- **决策 C（推送目标）**：
  - C1）群里 @ 用户（需订阅表记录 user_id + 群 chat_id）。
  - C2）私聊推送（需用户先和 bot 建立私聊；群内不打扰他人）。

- **决策 D（评论用哪个 LLM）**：
  - D1）OpenAI chat（`OPENAI_API_KEY` 已有）。
  - D2）Doubao/方舟 文本模型（需文本 endpoint/模型名，当前 .env 只有图像模型）。

- **决策 E（推荐信号权重）**：三类信号（下载/搜索/歌单）默认等权，还是你想偏重某一类？

---

## 预计文件改动（确认后执行）

- 新增 `utils/music_db.py`　—— 每用户记录 + 订阅的 sqlite 读写
- 新增 `utils/recommend.py`　—— 候选生成 + 打分排序
- 新增 `utils/text_llm.py`　—— 评论生成（决策 D）
- 改 `handlers/music.py`　—— 下载/搜索时写库
- 新增 `handlers/recommend.py`　—— `/randommusic` + 每日定时任务 + 订阅命令
- 改 `bot.py`　—— 注册新 handler + 启动定时任务
- 改 `requirements.txt`　—— （若选 A1）加 job-queue extra
- 改 `docker-compose.yml`　—— 挂载 `music.db` 持久化
- 改 `config.json`　—— 推荐相关配置（每日推送时间、条数、信号权重等）

---

## 风险与注意

- **yt-dlp 限流**：之前实测 SoundCloud 会 429。若选 B2，每日为多用户下载会放大风险 → 需限速/错峰/缓存。
- **首次推送的 chat 来源**：定时任务不在任何消息上下文里，必须靠订阅表持久化 chat_id。
- **时区**：北京时间 8 点，沿用 fortune 的 `Asia/Shanghai`；Docker 已装 tzdata。
- **隐私**：每用户历史属个人数据，库文件不进镜像、挂卷持久化；不跨用户泄露。

---

## 人工评价

- **Soundcloud**: 实测下来暂时不要使用soundcloud下载,等待一个专门的可以稳定下载soundcloud音乐源的handler下来再处理, 先用可以保证成功的ytb
- 关于评论: 无需添加任何的llm评论, 此前添加的所有api都是在出图方向上进行的
- 关于推送: 携带音频推送至群聊, 然后在音频下面写入用户对于该推荐是否满意的选择支, 如果满意在数据库里面增加这种类型乐曲的权重, 否则在群聊里撤回此消息, 然后减少这种歌曲的权重(或者你可以写两个数据库一个是用户感兴趣or不感兴趣的数据库). 同时要注意的一个点是在用户手动调用handler去主动推送的时候请只推送一首歌. 同时用户可以选择自由开启自动推送的设置.
- 关于剩下的内容: 自行调度, 写完后补充开发日志, 使用md文档说明你干了什么