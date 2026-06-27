# utils/music_fetch.py
# -*- coding: utf-8 -*-
"""
抓歌核心逻辑（框架无关）。

从 xyMusicUpdater 的 yt-dlp 搜索/下载逻辑移植而来，剥离了 Django / Navidrome /
数据库依赖，只保留纯粹的「搜索 → 下载为带元数据的 mp3」能力，供 Telegram
handler（或任何调用方）使用。

抓取的是公开可访问的音频流（YouTube / SoundCloud），与 xyMusicUpdater 同源。
"""
import json
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

# 可选：读取 .env（与项目其余模块保持一致）
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

log = logging.getLogger("music_fetch")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.json"

# provider 标识 → yt-dlp 搜索前缀。
# youtube 走 ytsearch，soundcloud 走 scsearch，二者都抓公开上传流。
_SEARCH_PREFIXES = {
    "youtube": "ytsearch",
    "soundcloud": "scsearch",
}

# 默认配置；config.json 的 "music" 段可覆盖。
_DEFAULTS: Dict[str, Any] = {
    "provider": "youtube",       # 默认搜索源
    "max_results": 5,            # 搜索返回条数
    "audio_format": "mp3",       # 下载音频格式
    "audio_quality": "0",        # 0 = 最佳
    "ytdlp_bin": "yt-dlp",       # yt-dlp 可执行文件（PATH 上）
    "proxy": "",                 # 可选代理，例如 http://127.0.0.1:1080
    "search_timeout": 60,        # 搜索子进程超时（秒）
    "download_timeout": 600,     # 下载子进程超时（秒）
}


# ── 配置 ───────────────────────────────────────────────────────────────────

def _load_music_cfg() -> Dict[str, Any]:
    """从 config.json 的 'music' 段读取配置，缺省项用 _DEFAULTS 兜底。"""
    cfg = dict(_DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        cfg.update(data.get("music", {}) or {})
    except Exception as e:
        log.warning("读取 config.json music 段失败，使用默认配置: %s", e)
    return cfg


def _search_prefix(provider: Optional[str]) -> str:
    """provider → yt-dlp 搜索前缀，未知值回落到 ytsearch。"""
    return _SEARCH_PREFIXES.get((provider or "youtube").strip().lower(), "ytsearch")


# ── 环境检测 ────────────────────────────────────────────────────────────────

def check_environment(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    """检测 yt-dlp 与 ffmpeg 是否可用。返回 {'ytdlp': bool, 'ffmpeg': bool}。"""
    cfg = cfg or _load_music_cfg()
    return {
        "ytdlp": shutil.which(cfg["ytdlp_bin"]) is not None,
        "ffmpeg": shutil.which("ffmpeg") is not None,
    }


# ── 搜索 ───────────────────────────────────────────────────────────────────

def search_media(
    query: str,
    limit: Optional[int] = None,
    provider: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    按关键字搜索音频。返回结果列表，每项含:
        id, title, uploader, duration(秒), url, thumbnail

    provider 缺省时用 config.json 的 music.provider。
    传入 http(s) URL 时直接当作单一目标返回（跳过搜索）。
    """
    cfg = _load_music_cfg()
    provider = provider or cfg["provider"]
    limit = int(limit or cfg["max_results"])

    # 直接给 URL：包成单条结果，交给下载阶段解析。
    if query.strip().lower().startswith(("http://", "https://")):
        return [{
            "id": None,
            "title": query.strip(),
            "uploader": "",
            "duration": None,
            "url": query.strip(),
            "thumbnail": None,
        }]

    prefix = _search_prefix(provider)
    cmd = [
        cfg["ytdlp_bin"], "--dump-json", "--flat-playlist",
        f"{prefix}{limit}:{query}",
    ]
    if cfg.get("proxy"):
        cmd += ["--proxy", cfg["proxy"]]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            errors="replace", timeout=int(cfg["search_timeout"]),
        )
    except Exception as e:
        raise RuntimeError(f"搜索失败: {e}")

    results: List[Dict[str, Any]] = []
    for line in result.stdout.splitlines():
        try:
            entry = json.loads(line)
        except Exception:
            continue

        url = entry.get("url")
        if not url and entry.get("id"):
            # YouTube flat 结果常只给 id，补成 watch URL。
            url = f"https://www.youtube.com/watch?v={entry.get('id')}"
        if not url:
            continue

        # 跳过频道/用户/播放列表这类容器条目。
        low = url.lower()
        if entry.get("_type") in ("url", "playlist") and any(
            x in low for x in ("/channel/", "/user/", "/@", "/playlist?list=")
        ):
            continue

        thumb = entry.get("thumbnail")
        if not thumb and entry.get("thumbnails"):
            thumb = entry["thumbnails"][0].get("url")

        results.append({
            "id": entry.get("id"),
            "title": entry.get("title"),
            "uploader": entry.get("uploader") or entry.get("channel") or "",
            "duration": entry.get("duration"),
            "url": url,
            "thumbnail": thumb,
        })

    return results


# ── 下载 ───────────────────────────────────────────────────────────────────

def _is_valid_audio(path: Path) -> bool:
    """用 ffprobe 校验音频文件可读（非零、有时长）。"""
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return res.returncode == 0 and bool(res.stdout.strip())
    except Exception:
        return False


def download_audio(
    url: str,
    dest_dir: Path,
    provider: Optional[str] = None,
) -> Dict[str, Any]:
    """
    把单个 URL 下载为带元数据 + 封面的音频文件。

    返回:
        {
            "path": Path,           # 下载好的音频文件
            "title": str,           # 标题（来自 yt-dlp 元数据）
            "artist": str,          # 艺人/上传者（uploader 兜底）
            "duration": float|None, # 时长（秒）
            "thumbnail": str|None,  # 封面 URL（便于单独发送）
        }

    失败时抛出 RuntimeError。
    """
    cfg = _load_music_cfg()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # 先抓单条元数据（非 flat），拿到 title/uploader/duration/thumbnail，
    # 也用于把 uploader 作为 artist 兜底（SoundCloud / YouTube 常缺规范 artist）。
    meta: Dict[str, Any] = {}
    try:
        meta_cmd = [cfg["ytdlp_bin"], "--dump-single-json", "--no-playlist", url]
        if cfg.get("proxy"):
            meta_cmd += ["--proxy", cfg["proxy"]]
        mres = subprocess.run(
            meta_cmd, capture_output=True, text=True,
            errors="replace", timeout=int(cfg["search_timeout"]),
        )
        if mres.returncode == 0 and mres.stdout.strip():
            meta = json.loads(mres.stdout)
    except Exception as e:
        log.warning("元数据预取失败（不影响下载）: %s", e)

    audio_fmt = cfg["audio_format"]
    output_tpl = str(dest_dir / "%(title)s.%(ext)s")
    cmd = [
        cfg["ytdlp_bin"], "--no-playlist",
        "-x", "--audio-format", audio_fmt,
        "--audio-quality", str(cfg["audio_quality"]),
        "--no-mtime", "--no-overwrites", "--no-part",
        "--add-metadata", "--embed-thumbnail",
        "--output", output_tpl,
    ]
    if cfg.get("proxy"):
        cmd += ["--proxy", cfg["proxy"]]
    cmd.append(url)

    before = set(dest_dir.iterdir())
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True,
            errors="replace", timeout=int(cfg["download_timeout"]),
        )
    except Exception as e:
        _cleanup_partials(dest_dir, before)
        raise RuntimeError(f"下载执行失败: {e}")

    if res.returncode != 0:
        _cleanup_partials(dest_dir, before)
        err = (res.stderr or "").strip().splitlines()
        msg = err[-1] if err else "未知错误"
        raise RuntimeError(f"yt-dlp 下载失败: {msg}")

    # 找出新生成的目标格式文件。
    new_files = [
        f for f in (set(dest_dir.iterdir()) - before)
        if f.suffix.lower() == f".{audio_fmt}"
    ]
    valid = [f for f in new_files if _is_valid_audio(f)]
    # 清理校验失败的残留。
    for f in new_files:
        if f not in valid:
            f.unlink(missing_ok=True)

    if not valid:
        raise RuntimeError("下载完成但未得到有效音频文件")

    path = valid[0]
    uploader = meta.get("uploader") or meta.get("channel") or ""
    title = meta.get("title") or path.stem
    # artist：优先元数据 artist，其次 uploader 兜底。
    artist = meta.get("artist") or uploader

    return {
        "path": path,
        "title": title,
        "artist": artist,
        "duration": meta.get("duration"),
        "thumbnail": meta.get("thumbnail"),
    }


def _cleanup_partials(dest_dir: Path, before: set) -> None:
    """删除下载失败时产生的部分文件（.part/.ytdl/.tmp/.temp.）。"""
    partial_suffixes = {".part", ".ytdl", ".tmp"}
    for f in (set(dest_dir.iterdir()) - before):
        if f.suffix in partial_suffixes or ".temp." in f.name:
            try:
                f.unlink()
            except Exception:
                pass
