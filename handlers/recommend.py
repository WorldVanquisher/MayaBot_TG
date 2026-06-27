# handlers/recommend.py
# -*- coding: utf-8 -*-
"""
/randommusic 推荐 handler。

功能（按需求锁定）：
  - /randommusic            手动推送「1 首」歌给调用者（带音频 + 👍/👎 按钮）
  - /randommusic on|off     在当前会话开启/关闭「每日自动推送」
  - /randommusic status     查看当前会话的订阅状态
  - 每日北京时间 08:00       对所有开启自动推送的会话，各推「3 首」（每首独立消息+独立按钮）
  - 反馈：👍 给该艺人加权；👎 撤回该条消息 + 降权
          （反馈只接受推荐目标本人，避免群里他人乱点）

仅使用 YouTube 下载（SoundCloud 暂不接）。不接任何 LLM。
"""
import json
import asyncio
import logging
import tempfile
import shutil
import datetime as dt
from pathlib import Path
from typing import Optional, Dict, Any

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

from utils import music_db, recommend as rec_engine
from utils.music_fetch import download_audio, check_environment

log = logging.getLogger("recommend")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.json"

_TG_AUDIO_LIMIT = 50 * 1024 * 1024  # Telegram 普通 bot 上传上限 50MB

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = None

_PUSH_DEFAULTS = {
    "push_hour": 8,        # 每日推送：北京时间小时
    "push_minute": 0,
    "daily_count": 3,      # 每日自动推送条数
    "manual_count": 1,     # 手动 /randommusic 条数
    "download_gap_sec": 3, # 多首下载之间的间隔（缓解限流）
}


def _push_cfg() -> Dict[str, Any]:
    cfg = dict(_PUSH_DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg.update(data.get("recommend", {}) or {})
    except Exception:
        pass
    return cfg


async def _safe_reply(update: Update, text: str, **kwargs):
    try:
        if update.message:
            await update.message.reply_text(text, **kwargs)
        elif update.effective_chat:
            await update.get_bot().send_message(update.effective_chat.id, text, **kwargs)
    except Exception as e:
        log.warning("reply failed: %s", e)


def _feedback_markup(rec_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👍 喜欢", callback_data=f"MUSIC_FB:like:{rec_id}"),
        InlineKeyboardButton("👎 不喜欢", callback_data=f"MUSIC_FB:dislike:{rec_id}"),
    ]])


# ── 核心：下载一首候选并作为推荐发送 ─────────────────────────────────────────

async def _send_one_recommendation(
    bot, user_id: int, chat_id: int, candidate: Dict[str, Any],
    mention: bool = False,
) -> bool:
    """
    下载候选并以「推荐」形式发到 chat（带 👍/👎 按钮）。
    成功返回 True。失败返回 False（调用方决定是否提示/换下一首）。
    """
    title = candidate.get("title") or "(无标题)"
    artist = candidate.get("artist") or ""
    url = candidate.get("url")
    if not url:
        return False

    # 先登记推荐，拿到 rec_id 作为按钮 callback 数据。
    rec_id = music_db.create_recommendation(
        user_id=user_id, title=title, artist=artist,
        video_id=candidate.get("video_id") or "", url=url,
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="mayabot_rec_"))
    try:
        info = await asyncio.to_thread(download_audio, url, tmp_dir)
        audio_path: Path = info["path"]

        if audio_path.stat().st_size > _TG_AUDIO_LIMIT:
            log.warning("推荐曲目超 50MB，跳过：%s", title)
            return False

        caption_lines = ["🎵 今日推荐" if mention else "🎵 为你推荐"]
        if mention:
            # 仅凭 user_id 即可 @ 到人（客户端会自动显示其名字）。
            caption_lines[0] = f'🎵 <a href="tg://user?id={user_id}">点歌给你</a>'
        caption_lines.append(f"{info.get('title') or title} — {info.get('artist') or artist}")
        caption_lines.append("喜欢点 👍；不喜欢点 👎（将撤回此条并减少同类推荐）")
        caption = "\n".join(caption_lines)

        with open(audio_path, "rb") as fh:
            msg = await bot.send_audio(
                chat_id=chat_id,
                audio=fh,
                title=info.get("title") or title,
                performer=info.get("artist") or artist or None,
                duration=int(info["duration"]) if info.get("duration") else None,
                filename=audio_path.name,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=_feedback_markup(rec_id),
            )
        # 回填消息坐标，供 👎 撤回。
        music_db.set_recommendation_message(rec_id, chat_id, msg.message_id)
        return True

    except Exception as e:
        log.error("推荐下载/发送失败 [%s]: %s", title, e)
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── /randommusic ────────────────────────────────────────────────────────────

async def cmd_randommusic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    arg = (context.args[0].strip().lower() if context.args else "")

    # 子命令：订阅开关 / 状态
    if arg in ("on", "off"):
        music_db.set_subscription(user.id, chat.id, arg == "on")
        await _safe_reply(
            update,
            "已开启每日自动推送（北京时间早 8 点）。" if arg == "on"
            else "已关闭本会话的每日自动推送。",
        )
        return
    if arg == "status":
        on = music_db.is_subscribed(user.id, chat.id)
        await _safe_reply(update, f"本会话每日自动推送：{'开启' if on else '关闭'}")
        return

    # 无参数：立即推送 1 首
    env = check_environment()
    if not env["ytdlp"] or not env["ffmpeg"]:
        await _safe_reply(update, "服务端缺少 yt-dlp / ffmpeg，无法推荐。")
        return

    await _safe_reply(update, "正在为你挑一首…")
    try:
        cands = await asyncio.to_thread(rec_engine.recommend, user.id, 1)
    except Exception as e:
        log.error("recommend failed: %s", e)
        await _safe_reply(update, f"推荐失败：{e}")
        return

    if not cands:
        await _safe_reply(update, "暂时没有合适的推荐，先用 /music 听几首让我了解你的口味吧～")
        return

    ok = await _send_one_recommendation(
        context.bot, user.id, chat.id, cands[0], mention=False
    )
    if not ok:
        await _safe_reply(update, "这首下载失败了，稍后再试试 /randommusic。")


# ── 反馈回调：👍 加权 / 👎 撤回+降权 ────────────────────────────────────────

async def cb_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    try:
        _, action, rec_id_str = (query.data or "").split(":")
        rec_id = int(rec_id_str)
    except Exception:
        await query.answer("无效的反馈。")
        return

    rec = music_db.get_recommendation(rec_id)
    if not rec:
        await query.answer("该推荐已过期。")
        return

    # 反馈只接受推荐目标本人（防群里他人乱点影响别人口味）。
    if query.from_user and query.from_user.id != rec["user_id"]:
        await query.answer("这不是给你的推荐哦～", show_alert=True)
        return

    artist = rec.get("artist") or ""

    if action == "like":
        if artist:
            music_db.bump_artist_weight(rec["user_id"], artist, music_db.LIKE_DELTA)
        music_db.set_recommendation_feedback(rec_id, "like")
        await query.answer("已记下你的喜欢 👍")
        try:
            # 去掉按钮，避免重复反馈。
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    elif action == "dislike":
        if artist:
            music_db.bump_artist_weight(rec["user_id"], artist, music_db.DISLIKE_DELTA)
        music_db.set_recommendation_feedback(rec_id, "dislike")
        await query.answer("已撤回，并减少同类推荐")
        # 撤回（删除）该条推荐消息。
        try:
            await query.message.delete()
        except Exception as e:
            log.warning("撤回消息失败: %s", e)
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
    else:
        await query.answer()


# ── 每日定时推送 ────────────────────────────────────────────────────────────

async def _daily_push_job(context: ContextTypes.DEFAULT_TYPE):
    cfg = _push_cfg()
    count = int(cfg["daily_count"])
    gap = float(cfg["download_gap_sec"])

    subs = music_db.get_enabled_subscriptions()
    if not subs:
        log.info("每日推送：无订阅，跳过。")
        return
    log.info("每日推送：%d 个订阅会话", len(subs))

    for user_id, chat_id in subs:
        try:
            cands = await asyncio.to_thread(rec_engine.recommend, user_id, count)
        except Exception as e:
            log.warning("每日推送 recommend 失败 user=%s: %s", user_id, e)
            continue
        if not cands:
            continue
        for cand in cands:
            await _send_one_recommendation(
                context.bot, user_id, chat_id, cand, mention=True
            )
            await asyncio.sleep(gap)  # 间隔下载，缓解限流


# ── 注册 ───────────────────────────────────────────────────────────────────

def setup() -> None:
    music_db.init_db()


def register(app: Application) -> None:
    app.add_handler(CommandHandler("randommusic", cmd_randommusic))
    app.add_handler(CallbackQueryHandler(cb_feedback, pattern=r"^MUSIC_FB:"))

    # 调度每日推送（需要 python-telegram-bot[job-queue]）。
    cfg = _push_cfg()
    jq = getattr(app, "job_queue", None)
    if jq is None:
        log.warning("JobQueue 不可用（未安装 [job-queue] extra），每日自动推送已禁用；/randommusic 仍可用。")
    else:
        run_time = dt.time(
            hour=int(cfg["push_hour"]), minute=int(cfg["push_minute"]),
            tzinfo=TZ,
        )
        jq.run_daily(_daily_push_job, time=run_time, name="daily_music_push")
        log.info("每日推送已调度：%02d:%02d (Asia/Shanghai)", cfg["push_hour"], cfg["push_minute"])

    log.info("Recommend handler registered. (/randommusic)")
