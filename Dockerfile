# mayabot —— Telegram bot（python-telegram-bot, polling 模式）
# 单进程，无 web 端口；含抓歌功能（yt-dlp + ffmpeg）。

FROM python:3.12-slim

# 系统依赖：
#   ffmpeg   —— yt-dlp 抽取/转码音频、嵌入封面所需
#   tzdata   —— fortune.py 使用 Asia/Shanghai 时区，slim 镜像默认无 tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

# 默认时区（可被 compose 的 TZ 环境变量覆盖）
ENV TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖（利用 Docker 层缓存：代码变动不会触发重装依赖）
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码。
# .env / config.json / fortune.db 已在 .dockerignore 中排除，
# 运行时通过挂载注入，绝不打进镜像。
COPY . .

# 运行时生成目录（图像 handler 会写 _debug）
RUN mkdir -p _debug _out

# polling bot，无需 EXPOSE 端口
CMD ["python", "bot.py"]
