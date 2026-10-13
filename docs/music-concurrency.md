# 音乐任务并发与弱网络配置

`/music` 搜索和下载、手动推荐、每日推荐共享一个后台任务队列。指令接收后立即返回，歌曲任务由 worker 处理；普通 Telegram Update 仍按顺序处理，避免全局用户状态竞态。

配置放在未纳入 Git 的 `config.json` 的 `music` 段。调整后重启容器；运行中的 service 在启动时读取这些参数。

```json
{
  "concurrency": 4,
  "queue_size": 16,
  "audio_format": "mp3",
  "audio_quality": "192K",
  "download_budget_bps": 1048576,
  "max_duration_sec": 1200,
  "max_audio_bytes": 48000000,
  "ffmpeg_threads": 1,
  "search_timeout": 60,
  "download_timeout": 600
}
```

- 最多 4 个音乐任务在处理，最多 16 个等待；每个用户最多一个在途任务。手动任务优先于等待中的每日推荐，已开始的任务不会被抢占。
- 下载速率按 `download_budget_bps / concurrency` 分配给每个 yt-dlp；4 路默认各 256 KiB/s，2 路各 512 KiB/s。并发分片限制为 1。这是 yt-dlp 对媒体下载的近似限速，不包含元数据、封面和其他服务流量，短时突发也可能超过预算。单个任务不会自动借用其他空闲 worker 的份额。
- 所有音频发送共用一个上传槽；新音频不同时争抢上行。上传未做硬性 Mbps 限速，因此单个上传仍可能占用较多上行带宽。出现 `RetryAfter` 时遵循等待时间，最多尝试 3 次；网络超时不盲目重发，避免重复消息。
- 同一歌曲的在途请求合并，发送成功后将 Telegram `file_id` 保存在 `music.db`。以后通过 `file_id` 发送，省去源站下载、转码与文件上传；缓存按 bot ID、歌曲和编码参数隔离。无效文件 ID 会重新下载，聊天权限错误不会触发无意义的重下载。
- MP3 使用 192 kbps；封面优先转为 JPEG 并在发生转换时缩到不超过 512×512。源封面已是 JPEG 时 yt-dlp 可能跳过转换，仍会保留原 JPEG。歌曲元数据由首次下载同时生成，不再单独预取一遍。
- 拒绝直播、未知时长、超过 20 分钟的曲目；源文件和输出采用 48,000,000 字节限制。部分源在下载前没有大小信息，输出仍在发送前检查；最长下载子进程 600 秒。推荐候选搜索可能额外占用最多 5×60 秒，未宣称完整任务有 600 秒总截止时间。
- 子进程以 `nice +10` 运行，ffmpeg 单线程；超时和下载取消会终止整个进程组，然后清理临时文件。
- Compose 限制整个 bot 为 2 核 CPU、1 GiB 内存，停机宽限 45 秒，给共享主机其他服务保留资源。若图像等其他功能确实需要更多内存，可单独调高内存限制，不必增加音乐并发。
- 搜索按钮带独立会话标识；每个用户保留最多 5 组搜索结果、有效期 10 分钟。重启后旧按钮需重新搜索，避免错误匹配到另一首歌。

## 部署与回滚

运行数据库不再纳入 Git 或 Docker 镜像。已有 `music.db` 保留在宿主机 bind mount，新增 `audio_cache` 表在启动时自动创建，不清空下载/推荐历史。全新部署先创建数据库文件，避免 Docker 把不存在的路径创建成目录：

```bash
touch fortune.db music.db
docker compose up --build -d mayabot
```

本次部署前备份保存在宿主机 `_debug/music_upgrade_backup_20261012/`，旧镜像标签为 `mayabot:before-music-concurrency-20261012`。降级并发只需将 `music.concurrency` 改为 `2` 并 `docker compose restart mayabot`。完整代码回滚可使用旧镜像；新增缓存表不会影响旧版读取原有表。

队列是内存队列，服务重启时未完成任务可能取消，不会持久化补发。停机先等待队列最多 30 秒，再取消剩余任务。当前订阅数为 0，因此本次部署未产生每日测试推送。

## 测试

离线回归覆盖 worker 数量、队列溢出、每用户限制、手动优先级、失败后恢复、并发同歌复用、跨 service 缓存、无效文件 ID、上传串行、限流退避与文件回卷、取消/进程组清理、搜索 handler 快速返回、旧按钮正确性、过期按钮，以及真实 PTB 的 multipart/file_id 序列化。离线传输器不访问 Telegram。

```bash
docker run --rm --network none --cpus 2 --memory 1g \
  -v "$PWD":/work:ro -w /work -e PYTHONDONTWRITEBYTECODE=1 \
  -e PTB_TIMEDELTA=1 --entrypoint python mayabot:local \
  -m unittest discover -s tests -p 'test_*.py' -v
```

源站实测脚本从只读历史库选 URL，使用临时独立数据库和下载目录。`SinkBot` 模拟上传，**不向任何聊天发送消息**。遇到明确的时长/直播保护拒绝会记录 `rejected_by_policy` 并补选下一首；其他失败直接使验收失败。

```bash
docker run --rm --cpus 2 --memory 1g --network mayabot_default \
  -v "$PWD":/work:ro -w /work -e PYTHONDONTWRITEBYTECODE=1 \
  -e PYTHONPATH=/work --entrypoint python mayabot:local \
  tests/live_music_probe.py --workers 2 --source-db /work/music.db
```

2 路通过后再执行 `--workers 4`。8 路仅保留为后续显式探测档位，本次不启用、不测试；以后试验保持 CPU、内存和下载总预算不变，比较排队、失败率与吞吐，避免仅看任务数量。

## 观测

日志输出 `music workers=...`、每个任务的 `wait_sec/work_sec`、新文件的 `download_sec/bytes/upload_sec` 与 `cache_hit`。这些指标可用于观察真实 Telegram 上传表现；模拟接收器的上传耗时不能作为实际网络延迟。

本轮验收数字另见 [并发升级验证记录](music-concurrency-validation.md)。
