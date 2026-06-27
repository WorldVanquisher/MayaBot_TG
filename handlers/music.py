# handlers/music.py
# -*- coding: utf-8 -*-
"""
/music 抓歌 handler（起步版）。

流程：
    /music <关键字或URL>
      → 搜索（YouTube/SoundCloud，源由 config.json music.provider 决定）
      → 以 inline 按钮列出结果
      → 用户点选某一首
      → 下载为带元数据+封面的 mp3
      → 以 reply_audio 发回

UI 交互细节（结果展示形式、是否支持切换源、分页等）后续再细化；
此版本结构清晰，便于在此基础上改。
"""
import asyncio
import logging
import tempfile
import shutil
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from utils.music_fetch import (
    search_media,
    download_audio,
    check_environment,
    _load_music_cfg,
)
from utils import music_db

log = logging.getLogger("music")

# Telegram bot 上传上限：普通 bot 50MB。
_TG_AUDIO_LIMIT = 50 * 1024 * 1024

# 搜索结果暂存在 user_data 的键。
_RESULTS_KEY = "music_results"


# ── 回复小工具（与其余 handler 保持一致）────────────────────────────────────

async def _safe_reply(update: Update, text: str, **kwargs):
    try:
        if update.message:
            await update.message.reply_text(text, **kwargs)
        elif update.effective_chat:
            await update.get_bot().send_message(update.effective_chat.id, text, **kwargs)
    except Exception as e:
        log.warning("reply failed: %s", e)


def _fmt_duration(sec) -> str:
    if not sec:
        return "--:--"
    try:
        sec = int(sec)
    except Exception:
        return "--:--"
    return f"{sec // 60}:{sec % 60:02d}"


# ── /music：搜索 ────────────────────────────────────────────────────────────

async def cmd_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await _safe_reply(update, "用法：/music <歌名/关键字 或 链接>")
        return

    # 环境检测：缺 yt-dlp/ffmpeg 时早早给出明确提示。
    env = check_environment()
    if not env["ytdlp"]:
        await _safe_reply(update, "服务端缺少 yt-dlp，无法搜索/下载。")
        return
    if not env["ffmpeg"]:
        await _safe_reply(update, "服务端缺少 ffmpeg，无法转码音频。")
        return

    cfg = _load_music_cfg()
    await _safe_reply(update, f"搜索中（{cfg['provider']}）…")

    try:
        results = await asyncio.to_thread(search_media, query)
    except Exception as e:
        log.error("search failed: %s", e)
        await _safe_reply(update, f"搜索失败：{e}")
        return

    if not results:
        await _safe_reply(update, "没有找到结果。")
        return

    # 记录搜索关键词（推荐信号）；URL 不算关键词。
    if not query.lower().startswith(("http://", "https://")):
        try:
            user = update.effective_user
            if user:
                music_db.record_search(user.id, query)
        except Exception as e:
            log.warning("record_search failed: %s", e)

    # 存结果，callback_data 只带索引（Telegram 限 64 字节，放不下 URL）。
    context.user_data[_RESULTS_KEY] = results

    buttons = []
    for i, r in enumerate(results):
        title = r.get("title") or "(无标题)"
        uploader = r.get("uploader") or ""
        dur = _fmt_duration(r.get("duration"))
        label = f"{i + 1}. {title}"
        if uploader:
            label += f" — {uploader}"
        label += f" [{dur}]"
        # 按钮文字过长会被 Telegram 截断，这里手动限长更可控。
        if len(label) > 60:
            label = label[:57] + "…"
        buttons.append([InlineKeyboardButton(label, callback_data=f"MUSIC_DL:{i}")])

    await _safe_reply(
        update,
        "选择要下载的曲目：",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── 回调：下载并发送 ────────────────────────────────────────────────────────

async def cb_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    try:
        idx = int((query.data or "").split(":", 1)[1])
    except Exception:
        await query.edit_message_text("无效的选择。")
        return

    results = context.user_data.get(_RESULTS_KEY) or []
    if idx < 0 or idx >= len(results):
        await query.edit_message_text("选择已过期，请重新 /music 搜索。")
        return

    item = results[idx]
    title = item.get("title") or "(无标题)"
    url = item.get("url")
    if not url:
        await query.edit_message_text("该结果缺少可下载链接。")
        return

    await query.edit_message_text(f"下载中：{title} …")

    tmp_dir = Path(tempfile.mkdtemp(prefix="mayabot_music_"))
    try:
        info = await asyncio.to_thread(download_audio, url, tmp_dir)
        audio_path: Path = info["path"]

        size = audio_path.stat().st_size
        if size > _TG_AUDIO_LIMIT:
            await query.edit_message_text(
                f"《{info['title']}》文件 {size // (1024*1024)}MB，超过 Telegram 50MB 上限，无法发送。"
            )
            return

        with open(audio_path, "rb") as fh:
            await context.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=fh,
                title=info.get("title") or title,
                performer=info.get("artist") or None,
                duration=int(info["duration"]) if info.get("duration") else None,
                filename=audio_path.name,
            )
        await query.edit_message_text(f"已发送：{info.get('title') or title}")

        # 记录下载历史（推荐信号 + 去重依据）。
        try:
            user = query.from_user
            if user:
                music_db.record_download(
                    user_id=user.id,
                    title=info.get("title") or title,
                    artist=info.get("artist") or "",
                    source="youtube",
                    video_id=item.get("id") or "",
                    url=url,
                    query="",
                )
        except Exception as e:
            log.warning("record_download failed: %s", e)

    except Exception as e:
        log.error("download/send failed: %s", e)
        try:
            await query.edit_message_text(f"下载失败：{e}")
        except Exception:
            await _safe_reply(update, f"下载失败：{e}")
    finally:
        # 清理临时目录。
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── 注册 ───────────────────────────────────────────────────────────────────

def register(app: Application) -> None:
    app.add_handler(CommandHandler("music", cmd_music))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^MUSIC_DL:"))
    log.info("Music handler registered. (/music)")
