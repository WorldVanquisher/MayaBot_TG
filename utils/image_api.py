## Legacy Model



# utils/image_api.py
# -*- coding: utf-8 -*-
import os
import io
import json
import base64
import logging
import requests
from pathlib import Path
from typing import Optional

from PIL import Image

log = logging.getLogger("image_api")

OPENAI_URL_GEN   = "https://api.openai.com/v1/images/generations"
OPENAI_URL_EDITS = "https://api.openai.com/v1/images/edits"


# =========================
# Common helpers
# =========================
def _b642bytes(b64: str) -> bytes:
    return base64.b64decode(b64.encode("utf-8"))

def _is_gpt_image(model: str) -> bool:
    m = (model or "").lower()
    return m.startswith("gpt-image-")

def _normalize_size_for_model(model: str, size: str) -> str:
    """
    对不同模型规范化尺寸字符串。
    - dall-e-2 仅支持 256/512/1024 方图；不匹配时默认 1024x1024
    - 其他模型保持原样
    """
    s = (size or "").lower().strip()
    if (model or "").lower() == "dall-e-2":
        allowed = {"256x256", "512x512", "1024x1024"}
        if s not in allowed:
            return "1024x1024"
    return s or "1024x1024"

def save_bytes(path: str | Path, data: bytes) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p

def letterbox(img: Image.Image, target_w: int, target_h: int, fill=255) -> Image.Image:
    """等比缩放后居中贴到指定画布上。"""
    w, h = img.size
    scale = min(target_w / w, target_h / h)
    nw, nh = int(w * scale), int(h * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new(img.mode, (target_w, target_h), fill)
    x = (target_w - nw) // 2
    y = (target_h - nh) // 2
    canvas.paste(img, (x, y))
    return canvas

# ---- 图片压缩：用于最终发送到 Telegram 前减小体积 ----
def compress_image(
    src,                              # PIL.Image.Image | bytes
    fmt: str = "JPEG",                # "JPEG" 或 "WEBP"
    max_w: int = 2048,
    max_h: int = 2048,
    size_cap_kb: int = 800,
    initial_quality: int = 88,
    min_quality: int = 60,
    step: int = 4
) -> bytes:
    """
    压缩图片到指定尺寸与大小上限；返回 bytes。
    - 输入可为 PIL.Image 或 bytes
    - 去除 EXIF / 统一 RGB
    - 逐步降低质量，直到不超过 size_cap_kb
    """
    if isinstance(src, Image.Image):
        img = src.copy()
    else:
        img = Image.open(io.BytesIO(src))

    # 转换色彩空间
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L" and fmt.upper() in ("JPEG", "JPG"):
        img = img.convert("RGB")

    # 等比缩放
    w, h = img.size
    scale = min(max_w / w, max_h / h, 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    fmt = fmt.upper()
    quality = initial_quality
    params = {}

    if fmt in ("JPEG", "JPG"):
        params.update(dict(
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            subsampling=2,  # 4:2:0
        ))
    elif fmt == "WEBP":
        params.update(dict(
            format="WEBP",
            quality=quality,
            method=6,
        ))
    else:
        fmt = "JPEG"
        params.update(dict(
            format="JPEG",
            quality=quality,
            optimize=True,
            progressive=True,
            subsampling=2,
        ))

    def _encode(q: int) -> bytes:
        buf = io.BytesIO()
        p = params.copy()
        p["quality"] = q
        img.save(buf, **p)
        return buf.getvalue()

    out = _encode(quality)
    cap = size_cap_kb * 1024
    while len(out) > cap and quality > min_quality:
        quality = max(min_quality, quality - step)
        out = _encode(quality)
    return out


# =========================
# DALL·E-2 edits 需要：PNG 且 < 4 MB
# =========================
def _resize_long_edge(img: Image.Image, edge: int) -> Image.Image:
    w, h = img.size
    scale = min(edge / w, edge / h, 1.0)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img

def _png_under_4mb(src_bytes: bytes, first_max_edge: int = 1024) -> bytes:
    """
    把任意图片 bytes 转成 PNG，反复下采样直到 < 4MB（DALL·E-2 edits 的硬性要求）。
    """
    im = Image.open(io.BytesIO(src_bytes))
    if im.mode not in ("RGBA", "LA"):
        im = im.convert("RGBA")

    max_edge = first_max_edge
    im_try = _resize_long_edge(im, max_edge)

    def _encode_png(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    out = _encode_png(im_try)
    cap = 4 * 1024 * 1024
    # 若仍超 4MB，则逐步缩 85%
    while len(out) >= cap and max_edge > 256:
        max_edge = int(max_edge * 0.85)
        im_try = _resize_long_edge(im, max_edge)
        out = _encode_png(im_try)
    return out


# =========================
# Main API
# =========================
# utils/image_api.py（仅替换 call_openai_image_api；其他代码保留）
def call_openai_image_api(
    prompt: str,
    size: str,
    quality: str,                 # gpt-image: low/medium/high/auto；dall-e-2：无此参数
    ref_image: Optional[bytes],   # 参考图（可为 None）
    model: str = "dall-e-2",
    timeout_sec: int = 60
) -> bytes:
    """
    统一的图像生成（加强版）：
      - 严格记录 HTTP 状态码/错误体；任何非 200 直接抛错，不再静默回退。
      - 成功时打印一次 INFO 方便你在日志中确认确实命中 API。
    返回 bytes（解码自 b64_json）。
    """
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 OPENAI_API_KEY 环境变量；未能调用 OpenAI Images API")

    headers = {"Authorization": f"Bearer {api_key}"}

    model = (model or "dall-e-2").strip()
    size  = _normalize_size_for_model(model, size)
    is_gpt = _is_gpt_image(model)

    # ---------- 优先：有参考图时尝试 edits ----------
    if ref_image:
        if model.lower() == "dall-e-2":
            # DALL·E-2 edits：必须 PNG 且 < 4MB
            png_bytes = _png_under_4mb(ref_image, first_max_edge=1024)
            files = {"image": ("image.png", io.BytesIO(png_bytes), "image/png")}
            data = {
                "model": model,
                "prompt": prompt,
                "size": size,
                "n": 1,
                "response_format": "b64_json",
            }
        else:
            # gpt-image-*
            files = {"image": ("image.png", io.BytesIO(ref_image), "image/png")}
            data = {"model": model, "prompt": prompt, "size": size, "n": 1}
            if quality:
                data["quality"] = quality

        try:
            r = requests.post(
                OPENAI_URL_EDITS,
                headers=headers,
                data=data,
                files=files,
                timeout=timeout_sec
            )
        except Exception as e:
            raise RuntimeError(f"[images/edits] 请求失败（可能无网络或被拦截）：{e}") from e

        if r.status_code != 200:
            # 直接抛错，不再静默；把前 400 字记录出来
            raise RuntimeError(f"[images/edits] {r.status_code}: {r.text[:400]}")

        try:
            b64 = r.json()["data"][0]["b64_json"]
        except Exception as e:
            raise RuntimeError(f"[images/edits] 解析响应失败：{e}; 原始响应片段={r.text[:200]}") from e

        out = _b642bytes(b64)
        log.info("[images/edits] OK model=%s size=%s bytes=%d", model, size, len(out))
        return out

    # ---------- 无参考图：generations ----------
    payload = {"model": model, "prompt": prompt, "size": size, "n": 1}
    if not is_gpt:
        payload["response_format"] = "b64_json"
    else:
        if quality:
            payload["quality"] = quality

    try:
        r = requests.post(
            OPENAI_URL_GEN,
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps(payload),
            timeout=timeout_sec
        )
    except Exception as e:
        raise RuntimeError(f"[images/generations] 请求失败（可能无网络或被拦截）：{e}") from e

    if r.status_code != 200:
        raise RuntimeError(f"[images/generations] {r.status_code}: {r.text[:400]}")

    try:
        b64 = r.json()["data"][0]["b64_json"]
    except Exception as e:
        raise RuntimeError(f"[images/generations] 解析响应失败：{e}; 原始响应片段={r.text[:200]}") from e

    out = _b642bytes(b64)
    log.info("[images/generations] OK model=%s size=%s bytes=%d", model, size, len(out))
    return out
