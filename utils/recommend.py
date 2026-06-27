# utils/recommend.py
# -*- coding: utf-8 -*-
"""
推荐算法（框架无关，仅 YouTube）。

务实落地「商业推歌」思路，不依赖外部推荐 API：
  种子(下载高频艺人 + 近期搜索词 + 正权艺人)
    → ytsearch 拉候选
    → 去掉已下载 / 强负权(不喜欢)艺人
    → 打分(种子权重 + 偏好权重 + 随机扰动)
    → 取 TopN

注意：SoundCloud 暂不使用（等稳定 handler），这里固定 provider=youtube。
为规避 yt-dlp 限流，限制种子数与每种子候选数。
"""
import json
import random
import logging
from pathlib import Path
from typing import List, Dict, Any, Tuple

from utils.music_fetch import search_media
from utils import music_db

log = logging.getLogger("recommend")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.json"

_DEFAULTS: Dict[str, Any] = {
    # 冷启动 / 无历史时使用的默认种子
    "default_seeds": ["lofi hip hop", "j-pop hits", "anime opening"],
    "max_seeds": 5,            # 单次最多用多少个种子（控制 yt-dlp 调用次数）
    "candidates_per_seed": 5,  # 每个种子拉多少候选
    "dislike_threshold": -3,   # 艺人权重 <= 此值视为「不喜欢」，从候选中剔除
    # 打分权重
    "w_seed": 1.0,             # 种子自身重要度的系数
    "w_pref": 0.5,             # 候选艺人偏好权重的系数
    "jitter": 1.5,             # 随机扰动幅度（避免每天雷同）
    "max_duration_sec": 1200,  # 超过此时长的候选视为合辑/长混音，过滤掉
}


def _load_cfg() -> Dict[str, Any]:
    cfg = dict(_DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg.update(data.get("recommend", {}) or {})
    except Exception as e:
        log.warning("读取 config.json recommend 段失败，使用默认: %s", e)
    return cfg


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def build_seeds(user_id: int, cfg: Dict[str, Any]) -> List[Tuple[str, float]]:
    """
    构造 [(seed_text, seed_weight), ...]。
    种子来源：下载高频艺人、正权艺人、近期搜索词。无历史则回落默认种子。
    """
    seeds: List[Tuple[str, float]] = []
    seen = set()

    weights = music_db.get_artist_weights(user_id)  # {artist_key: weight}

    def _add(text: str, weight: float):
        key = _norm(text)
        if not key or key in seen:
            return
        seen.add(key)
        seeds.append((text.strip(), weight))

    # 1) 下载高频艺人（次数越多权重越高），叠加偏好权重
    for artist, count in music_db.get_top_artists(user_id, limit=cfg["max_seeds"]):
        akey = _norm(artist)
        w = float(count) + max(0.0, weights.get(akey, 0))
        _add(artist, w)

    # 2) 正权艺人（喜欢过、但未必下载量大）
    for akey, w in weights.items():
        if w > 0:
            _add(akey, float(w))

    # 3) 近期搜索词（弱信号）
    for kw in music_db.get_recent_searches(user_id, limit=cfg["max_seeds"]):
        _add(kw, 1.0)

    # 冷启动：无任何历史 → 默认种子
    if not seeds:
        for s in cfg["default_seeds"]:
            _add(s, 1.0)

    # 取权重最高的前 max_seeds 个
    seeds.sort(key=lambda x: x[1], reverse=True)
    return seeds[: int(cfg["max_seeds"])]


def recommend(user_id: int, n: int = 3) -> List[Dict[str, Any]]:
    """
    为用户生成 n 条推荐。返回候选列表，每项含:
        title, artist, video_id, url, thumbnail, score
    生成不出来时返回空列表（调用方应有兜底提示）。
    """
    cfg = _load_cfg()
    weights = music_db.get_artist_weights(user_id)
    dislike_th = int(cfg["dislike_threshold"])

    downloaded_vids = set(music_db.get_downloaded_video_ids(user_id))
    downloaded_titles = set(music_db.get_downloaded_titles(user_id))

    # 排除最近已经推荐过的曲目，避免连续 /randommusic 出同一首。
    recent_vids, recent_titles = music_db.get_recent_recommended(user_id, limit=50)
    downloaded_vids |= set(recent_vids)
    downloaded_titles |= set(recent_titles)

    seeds = build_seeds(user_id, cfg)
    if not seeds:
        return []

    candidates: List[Dict[str, Any]] = []
    seen_keys = set()  # 候选内部去重（video_id 或标题）

    for seed_text, seed_w in seeds:
        try:
            results = search_media(
                seed_text,
                limit=int(cfg["candidates_per_seed"]),
                provider="youtube",
            )
        except Exception as e:
            log.warning("种子搜索失败，跳过 [%s]: %s", seed_text, e)
            continue

        for r in results:
            vid = r.get("id") or ""
            title = r.get("title") or ""
            uploader = r.get("uploader") or ""
            if not title or not r.get("url"):
                continue

            # 去重键
            dkey = vid or _norm(title)
            if dkey in seen_keys:
                continue

            # 过滤：已下载过
            if vid and vid in downloaded_vids:
                continue
            if _norm(title) in downloaded_titles:
                continue

            # 过滤：不喜欢的艺人
            akey = _norm(uploader)
            pref = weights.get(akey, 0)
            if pref <= dislike_th:
                continue

            # 过滤：超长（合辑/长混音）
            dur = r.get("duration")
            if dur and cfg.get("max_duration_sec") and dur > cfg["max_duration_sec"]:
                continue

            seen_keys.add(dkey)

            # 打分：种子重要度 + 偏好权重 + 随机扰动
            score = (
                seed_w * float(cfg["w_seed"])
                + max(0, pref) * float(cfg["w_pref"])
                + random.uniform(0, float(cfg["jitter"]))
            )
            candidates.append({
                "title": title,
                "artist": uploader,
                "video_id": vid,
                "url": r.get("url"),
                "thumbnail": r.get("thumbnail"),
                "duration": dur,
                "score": score,
            })

    if not candidates:
        return []

    # 加权随机抽样（不放回）：分数越高被选中概率越大，但不固定取最高分，
    # 这样候选池相同时，重复调用也会得到不同结果。
    picked: List[Dict[str, Any]] = []
    pool = list(candidates)
    k = min(n, len(pool))
    for _ in range(k):
        total = sum(max(0.01, c["score"]) for c in pool)
        r = random.uniform(0, total)
        upto = 0.0
        chosen_idx = len(pool) - 1
        for i, c in enumerate(pool):
            upto += max(0.01, c["score"])
            if upto >= r:
                chosen_idx = i
                break
        picked.append(pool.pop(chosen_idx))

    return picked
