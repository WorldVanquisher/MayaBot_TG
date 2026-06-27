# bot.py
import os
import logging
from telegram.ext import ApplicationBuilder

# === Handlers ===
# legacy 版本（仍可用）：/dokaku
from handlers.dokaku import register as dokaku_register
# 新版本（支持豆包多参考图）：/dongzhuo
from handlers.dongzhuo import register as dongzhuo_register
# 签到/运势
from handlers.fortune import setup as fortune_setup, register as fortune_register
# 抓歌：/music（YouTube/SoundCloud 搜索下载）
from handlers.music import register as music_register
# 推荐推送：/randommusic（每日定时 + 手动）
from handlers.recommend import setup as recommend_setup, register as recommend_register


# 可选：读取 .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Token 支持两种变量名
TOKEN = (os.getenv("TG_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError("未设置 TG_BOT_TOKEN / BOT_TOKEN，请在 .env 或环境变量中配置。")

# 更详细的日志（必要时可改为 logging.DEBUG）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

def main():
    # 可能需要的初始化（DB 等）
    fortune_setup()
    recommend_setup()   # 初始化 music.db（下载历史/偏好/订阅/推荐记录）

    app = ApplicationBuilder().token(TOKEN).build()

    # 注册各模块
    fortune_register(app)   # /start /fortune
    dokaku_register(app)    # /dokaku（旧版）
    dongzhuo_register(app)  # /dongzhuo（新版，豆包优先）
    music_register(app)     # /music（抓歌）
    recommend_register(app) # /randommusic（推荐推送 + 每日定时）

    print("✅ Bot running.")
    print("Commands: /start /fortune /dokaku /dongzhuo /music /randommusic")
    app.run_polling(allowed_updates=["message", "edited_message", "callback_query"])

if __name__ == "__main__":
    main()
