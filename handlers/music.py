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
import secrets
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from utils.music_fetch import (
    search_media,
    check_environment,
    _load_music_cfg,
)
from utils import music_db
from utils.music_service import MusicBusy, get_service

log = logging.getLogger("music")

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

    service = get_service(context.application)
    try:
        service.submit(update.effective_user.id,
                       lambda: _search_and_reply(update, context, query))
    except MusicBusy as exc:
        await _safe_reply(update, str(exc))
        return
    await _safe_reply(update, "搜索任务已接收…")


async def _search_and_reply(update, context, query):

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
    now = time.monotonic()
    sessions = context.user_data.setdefault(_RESULTS_KEY, {})
    expired = [key for key, session in sessions.items() if now - session["created"] > 600]
    for key in expired:
        sessions.pop(key, None)
    while len(sessions) >= 5:
        sessions.pop(next(iter(sessions)))
    token = secrets.token_hex(4)
    sessions[token] = {"results": results, "created": now,
                       "chat_id": update.effective_chat.id}

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
        buttons.append([InlineKeyboardButton(label, callback_data=f"MUSIC_DL:{token}:{i}")])

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
        _, token, index = (query.data or "").split(":")
        idx = int(index)
    except Exception:
        await query.edit_message_text("无效的选择。")
        return

    session = (context.user_data.get(_RESULTS_KEY) or {}).get(token)
    if not session or time.monotonic() - session["created"] > 600 or session["chat_id"] != query.message.chat_id:
        await query.edit_message_text("选择已过期，请重新 /music 搜索。")
        return
    results = session["results"]
    if idx < 0 or idx >= len(results):
        await query.edit_message_text("选择已过期，请重新 /music 搜索。")
        return

    item = results[idx]
    title = item.get("title") or "(无标题)"
    url = item.get("url")
    if not url:
        await query.edit_message_text("该结果缺少可下载链接。")
        return

    service = get_service(context.application)
    try:
        service.submit(query.from_user.id,
                       lambda: _download_and_reply(update, context, dict(item)))
    except MusicBusy as exc:
        await _safe_reply(update, str(exc))
        return
    await query.edit_message_text(f"已加入点歌队列：{title} …")


async def _download_and_reply(update, context, item):
    query = update.callback_query
    url = item["url"]
    title = item.get("title") or "(无标题)"
    try:
        _, info = await get_service(context.application).send_audio(
            context.bot, url, chat_id=query.message.chat_id)
        await query.edit_message_text(f"已发送：{info.get('title') or title}")

        # 记录下载历史（推荐信号 + 去重依据）。
        try:
            user = query.from_user
            if user:
                music_db.record_download(
                    user_id=user.id,
                    title=info.get("title") or title,
                    artist=info.get("artist") or "",
                    source=_load_music_cfg()["provider"],
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


# ── 注册 ───────────────────────────────────────────────────────────────────

def register(app: Application) -> None:
    app.add_handler(CommandHandler("music", cmd_music))
    app.add_handler(CallbackQueryHandler(cb_download, pattern=r"^MUSIC_DL:"))
    log.info("Music handler registered. (/music)")
