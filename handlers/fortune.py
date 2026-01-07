# handlers/fortune.py
import logging
import random
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ---- Config / Paths ----
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "fortune.db"

# 时区（按需修改）
try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = None

log = logging.getLogger("fortune")

# ---- DB ----
def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS fortunes (
            user_id     INTEGER,
            date        TEXT,
            score       INTEGER,
            text        TEXT,
            rerolled    INTEGER DEFAULT 0,
            created_at  TEXT,
            PRIMARY KEY (user_id, date)
        );
        """)
        con.commit()

def get_today_record(user_id: int, today: str) -> Optional[Tuple[int, str, int]]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT score, text, rerolled FROM fortunes WHERE user_id=? AND date=?",
            (user_id, today)
        )
        return cur.fetchone()

def save_today_record(user_id: int, today: str, score: int, text: str, rerolled: int = 0) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT OR REPLACE INTO fortunes(user_id, date, score, text, rerolled, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, today, score, text, rerolled, now_iso())
        )
        con.commit()

def update_reroll(user_id: int, today: str, score: int, text: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE fortunes SET score=?, text=?, rerolled=1 WHERE user_id=? AND date=?",
            (score, text, user_id, today)
        )
        con.commit()

# ---- Utils ----
def today_str() -> str:
    if TZ:
        return dt.datetime.now(TZ).date().isoformat()
    return dt.date.today().isoformat()

def now_iso() -> str:
    if TZ:
        return dt.datetime.now(TZ).isoformat(timespec="seconds")
    return dt.datetime.now().isoformat(timespec="seconds")

# ---- Fortune logic ----
ADVICE_GOOD = [
    "宜：凹高分, 推ap! 下一个舞神就是你~",
    "宜：放松心态, 冲刺学业&事业.",
    "宜：放手一搏, 突破极限.",
]
ADVICE_NEUTRAL = [
    "宜：多运动, 多放松",
    "忌：虚无缥缈为自己定大志向.",
    "吃顿好的吧, 平安即是喜乐.",
]
ADVICE_BAD = [
    "人犟损才, 牛犟损力.",
    "放过自己，早点睡，明天会更好.",
    "如果可以的话, 做点没有意义的事情休息一下吧.",
]

def build_fortune(score: int) -> str:
    if score >= 90:
        vibe = "🎉 大吉"
        advice = random.choice(ADVICE_GOOD)
    elif score >= 70:
        vibe = "😊 吉"
        advice = random.choice(ADVICE_GOOD + ADVICE_NEUTRAL)
    elif score >= 40:
        vibe = "😐 平"
        advice = random.choice(ADVICE_NEUTRAL)
    elif score >= 10:
        vibe = "🥲 凶"
        advice = random.choice(ADVICE_NEUTRAL + ADVICE_BAD)
    else:
        vibe = "💀 大凶"
        advice = random.choice(ADVICE_BAD)
    return f"{vibe}\n今天的幸运指数：{score}/100\n{advice}"

def draw_score() -> int:
    return random.randint(0, 100)

# ---- Safe reply helpers ----
async def _safe_reply(update: Update, text: str, **kwargs):
    try:
        if update.message:
            await update.message.reply_text(text, **kwargs)
        elif update.effective_chat:
            await update.get_bot().send_message(update.effective_chat.id, text, **kwargs)
    except Exception as e:
        log.warning("reply failed: %s", e)

async def _safe_edit(query, text: str):
    try:
        await query.edit_message_text(text)
    except Exception as e:
        log.warning("edit failed: %s", e)

# ---- Handlers ----
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_reply(update, "发 /fortune 来抽今日运势（当日保留；若 <10 可重抽一次）。")

async def cmd_fortune(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return
    user_id = user.id
    today = today_str()

    rec = get_today_record(user_id, today)
    if rec is None:
        score = draw_score()
        text = build_fortune(score)
        save_today_record(user_id, today, score, text, rerolled=0)
        rec = (score, text, 0)

    score, text, rerolled = rec
    if score < 10 and rerolled == 0:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("再抽一次（今日仅一次）", callback_data=f"REROLL:{user_id}:{today}")]
        ])
        await _safe_reply(update, text, reply_markup=kb)
    else:
        await _safe_reply(update, text)

async def cb_reroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()

    try:
        _, uid_str, date_str = (query.data or "").split(":")
        uid = int(uid_str)
    except Exception:
        await _safe_edit(query, "无效请求。")
        return

    if query.from_user and query.from_user.id != uid:
        await query.answer("只能本人重抽哦～", show_alert=True)
        return

    rec = get_today_record(uid, date_str)
    if rec is None:
        await _safe_edit(query, "今天还没有抽过，先发 /fortune ～")
        return

    score, text, rerolled = rec
    if not (score < 10 and rerolled == 0):
        await _safe_edit(query, "重抽次数已用完，或不满足条件（<10）。")
        return

    new_score = draw_score()
    new_text  = build_fortune(new_score)
    update_reroll(uid, date_str, new_score, new_text)
    await _safe_edit(query, new_text + "\n（已使用今日重抽）")

# ---- Public API ----
def setup() -> None:
    random.seed()
    init_db()

def register(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("fortune", cmd_fortune))
    app.add_handler(CallbackQueryHandler(cb_reroll, pattern=r"^REROLL:"))
