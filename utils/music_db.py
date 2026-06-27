# utils/music_db.py
# -*- coding: utf-8 -*-
"""
推歌功能的持久化层（框架无关，纯 sqlite）。

风格对齐 handlers/fortune.py：模块内 init_db 建表，函数式读写，
每次操作单独开/关连接。独立数据库文件 music.db（与 fortune.db 分开）。

表：
  user_downloads     —— 用户下载历史（推荐信号 + 去重依据）
  user_searches      —— 用户搜索关键词（推荐信号）
  artist_weights     —— 单张带符号权重的偏好表（正=喜欢，负=不喜欢）
  push_subscriptions —— 自动推送开关（user_id + chat_id 维度）
  recommendations    —— 每条推荐记录（含 chat_id/message_id，供👎撤回与反馈调权）
"""
import sqlite3
import datetime as dt
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "music.db"

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    TZ = None

# 反馈调权幅度
LIKE_DELTA = 2
DISLIKE_DELTA = -3


def now_iso() -> str:
    if TZ:
        return dt.datetime.now(TZ).isoformat(timespec="seconds")
    return dt.datetime.now().isoformat(timespec="seconds")


def _norm_artist(artist: str) -> str:
    """艺人名归一化作为权重表的键（小写去空白）。"""
    return (artist or "").strip().lower()


# ── 建表 ───────────────────────────────────────────────────────────────────

def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_downloads (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            title        TEXT,
            artist       TEXT,
            source       TEXT,
            video_id     TEXT,
            url          TEXT,
            query        TEXT,
            downloaded_at TEXT
        );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_dl_user ON user_downloads(user_id);")
        con.execute("CREATE INDEX IF NOT EXISTS idx_dl_user_vid ON user_downloads(user_id, video_id);")

        con.execute("""
        CREATE TABLE IF NOT EXISTS user_searches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            keyword     TEXT,
            searched_at TEXT
        );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_search_user ON user_searches(user_id);")

        # 单张带符号权重的偏好表：weight 正=喜欢、负=不喜欢
        con.execute("""
        CREATE TABLE IF NOT EXISTS artist_weights (
            user_id     INTEGER NOT NULL,
            artist_key  TEXT NOT NULL,
            artist      TEXT,
            weight      INTEGER DEFAULT 0,
            updated_at  TEXT,
            PRIMARY KEY (user_id, artist_key)
        );
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER NOT NULL,
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT,
            updated_at  TEXT,
            PRIMARY KEY (user_id, chat_id)
        );
        """)

        # 每条推荐记录：用于反馈调权 + 👎 时按 message_id 撤回
        con.execute("""
        CREATE TABLE IF NOT EXISTS recommendations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            chat_id     INTEGER,
            message_id  INTEGER,
            title       TEXT,
            artist      TEXT,
            video_id    TEXT,
            url         TEXT,
            feedback    TEXT,
            created_at  TEXT
        );
        """)
        con.commit()


# ── 写入：下载 / 搜索 ────────────────────────────────────────────────────────

def record_download(
    user_id: int, title: str, artist: str, source: str,
    video_id: str = "", url: str = "", query: str = "",
) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO user_downloads(user_id, title, artist, source, video_id, url, query, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, title, artist, source, video_id, url, query, now_iso()),
        )
        con.commit()
    # 用户主动下载视为正反馈信号，给该艺人小幅加权
    if artist:
        bump_artist_weight(user_id, artist, 1)


def record_search(user_id: int, keyword: str) -> None:
    if not (keyword or "").strip():
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO user_searches(user_id, keyword, searched_at) VALUES (?, ?, ?)",
            (user_id, keyword.strip(), now_iso()),
        )
        con.commit()


# ── 读取：推荐信号 ──────────────────────────────────────────────────────────

def get_downloaded_video_ids(user_id: int) -> List[str]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT DISTINCT video_id FROM user_downloads WHERE user_id=? AND video_id<>''",
            (user_id,),
        )
        return [r[0] for r in cur.fetchall()]


def get_downloaded_titles(user_id: int) -> List[str]:
    """已下载曲目的标题（小写），用于无 video_id 时的去重兜底。"""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT DISTINCT lower(title) FROM user_downloads WHERE user_id=? AND title<>''",
            (user_id,),
        )
        return [r[0] for r in cur.fetchall()]


def get_top_artists(user_id: int, limit: int = 10) -> List[Tuple[str, int]]:
    """下载历史中的高频艺人 [(artist, count), ...]。"""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT artist, COUNT(*) c FROM user_downloads "
            "WHERE user_id=? AND artist<>'' GROUP BY lower(artist) ORDER BY c DESC LIMIT ?",
            (user_id, limit),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_recent_searches(user_id: int, limit: int = 10) -> List[str]:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT keyword FROM user_searches WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        return [r[0] for r in cur.fetchall()]


def get_artist_weights(user_id: int) -> Dict[str, int]:
    """返回 {artist_key: weight}，正=喜欢、负=不喜欢。"""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT artist_key, weight FROM artist_weights WHERE user_id=?",
            (user_id,),
        )
        return {r[0]: r[1] for r in cur.fetchall()}


def bump_artist_weight(user_id: int, artist: str, delta: int) -> None:
    """对某艺人权重增减 delta（upsert）。"""
    key = _norm_artist(artist)
    if not key:
        return
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO artist_weights(user_id, artist_key, artist, weight, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, artist_key) DO UPDATE SET "
            "weight = weight + excluded.weight, updated_at = excluded.updated_at",
            (user_id, key, artist.strip(), delta, now_iso()),
        )
        con.commit()


# ── 订阅（自动推送开关）─────────────────────────────────────────────────────

def set_subscription(user_id: int, chat_id: int, enabled: bool) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO push_subscriptions(user_id, chat_id, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, chat_id) DO UPDATE SET "
            "enabled = excluded.enabled, updated_at = excluded.updated_at",
            (user_id, chat_id, 1 if enabled else 0, now_iso(), now_iso()),
        )
        con.commit()


def is_subscribed(user_id: int, chat_id: int) -> bool:
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT enabled FROM push_subscriptions WHERE user_id=? AND chat_id=?",
            (user_id, chat_id),
        )
        row = cur.fetchone()
        return bool(row and row[0])


def get_enabled_subscriptions() -> List[Tuple[int, int]]:
    """所有开启自动推送的 [(user_id, chat_id), ...]，供每日定时任务遍历。"""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT user_id, chat_id FROM push_subscriptions WHERE enabled=1"
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


# ── 推荐记录（反馈 + 撤回）──────────────────────────────────────────────────

def create_recommendation(
    user_id: int, title: str, artist: str, video_id: str = "", url: str = "",
) -> int:
    """登记一条推荐，返回 rec_id（用于 callback_data）。"""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "INSERT INTO recommendations(user_id, title, artist, video_id, url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, title, artist, video_id, url, now_iso()),
        )
        con.commit()
        return int(cur.lastrowid)


def set_recommendation_message(rec_id: int, chat_id: int, message_id: int) -> None:
    """推荐消息发出后回填 chat_id/message_id，供👎撤回。"""
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE recommendations SET chat_id=?, message_id=? WHERE id=?",
            (chat_id, message_id, rec_id),
        )
        con.commit()


def get_recommendation(rec_id: int) -> Optional[Dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        cur = con.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def set_recommendation_feedback(rec_id: int, feedback: str) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE recommendations SET feedback=? WHERE id=?",
            (feedback, rec_id),
        )
        con.commit()


def get_recent_recommended(user_id: int, limit: int = 50) -> Tuple[List[str], List[str]]:
    """
    最近推荐过的曲目，用于避免连续推荐重复。
    返回 (video_ids, titles_lower)。
    """
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT video_id, lower(title) FROM recommendations "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
        vids, titles = [], []
        for vid, title in cur.fetchall():
            if vid:
                vids.append(vid)
            if title:
                titles.append(title)
        return vids, titles
