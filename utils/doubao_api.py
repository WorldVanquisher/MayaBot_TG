# utils/doubao_api.py
# -*- coding: utf-8 -*-
import os
import io
import json
import base64
import logging
import random
import time
from typing import Optional, List, Dict, Any, Union

import requests

log = logging.getLogger("doubao_api")

# -----------------------
# Helpers
# -----------------------
def _b642bytes(b64: str) -> bytes:
    """兼容 dataURL / 纯 base64，两种都能解。"""
    if b64 and ";base64," in b64:
        b64 = b64.split(";base64,", 1)[1]
    return base64.b64decode(b64.encode("utf-8"))

def _bytes2dataurl(img_bytes: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(img_bytes).decode('utf-8')}"

def _download_url(url: str, timeout: int = 60) -> bytes:
    """下载模型返回的图片 URL（支持 TOS 签名链接）。"""
    resp = requests.get(url, timeout=timeout, stream=True)
    if resp.status_code != 200:
        raise RuntimeError(f"[doubao/images] 下载失败 {resp.status_code}: {url[:120]}")
    content = resp.content
    if not content:
        raise RuntimeError("[doubao/images] 下载内容为空")
    return content

def _coerce_refs(ref_images: Optional[List[Union[bytes, Dict[str, Any]]]]) -> List[Dict[str, Any]]:
    """
    统一把 ref_images 转成标准结构：
    - 输入可以是 bytes（默认 role=reference, weight=1.0）
      或 dict: {"image": bytes|dataurl|http-url, "role": "...", "weight": 1.0, "label": "..."}
    - 输出统一为 [{"image": dataurl_or_url, "role": str, "weight": float, "label": str}]
    """
    out: List[Dict[str, Any]] = []
    if not ref_images:
        return out

    for i, item in enumerate(ref_images):
        role = "reference"
        weight = 1.0
        label = f"ref[{i}]"
        image_val: Optional[str] = None  # dataurl 或 http-url

        if isinstance(item, (bytes, bytearray)):
            image_val = _bytes2dataurl(bytes(item))
        elif isinstance(item, dict):
            role = (item.get("role") or "reference")
            weight = float(item.get("weight", 1.0))
            label = item.get("label", label)
            img_val = item.get("image")
            if isinstance(img_val, (bytes, bytearray)):
                image_val = _bytes2dataurl(bytes(img_val))
            elif isinstance(img_val, str):
                # 允许直接传 http(s) URL 或 dataURL
                image_val = img_val
        else:
            raise TypeError(f"ref_images[{i}] 不支持的类型: {type(item)}")

        if not image_val:
            raise ValueError(f"ref_images[{i}] 无法解析为 dataURL/http-url/bytes")

        out.append({
            "image": image_val,
            "role": role,
            "weight": weight,
            "label": label,
        })
    return out

def _request_with_retry(
    method: str, url: str, *,
    headers: Dict[str, str], data: Optional[str],
    timeout: int, retries: int = 3,
    backoff_base: float = 0.9, backoff_factor: float = 1.6, jitter: float = 0.35
) -> requests.Response:
    """
    稳健重试：对 408/409/429/5xx 以及网络异常重试指数退避。
    """
    last_err: Optional[BaseException] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, data=data, timeout=timeout)
            # 部分平台 409/425 也要重试
            if resp.status_code in (408, 409, 425, 429, 500, 502, 503, 504):
                log.warning("[doubao/images] attempt=%d retryable status=%d text=%s",
                            attempt, resp.status_code, resp.text[:200])
                if attempt == retries:
                    return resp
            else:
                return resp
        except Exception as e:
            last_err = e
            log.warning("[doubao/images] attempt=%d request error: %s", attempt, e)

        if attempt < retries:
            sleep_s = backoff_base * (backoff_factor ** (attempt - 1)) + random.random() * jitter
            time.sleep(sleep_s)

    if last_err:
        raise RuntimeError(f"[doubao/images] 请求失败：{last_err}") from last_err
    raise RuntimeError("[doubao/images] 请求失败：未知错误")

def _map_size_for_ark(size: str) -> str:
    """
    Ark 常用：'1K'/'2K'/'4K'（或具体枚举）。
    若传入 '2048x1024' 这类带 'x' 的尺寸，回退为 '2K'，避免 400。
    """
    s = (size or "").strip()
    if "x" in s.lower():
        return "2K"
    return s or "2K"

def _is_ark_endpoint(endpoint: str, extra: Optional[Dict[str, Any]]) -> bool:
    """判断是否使用 Ark REST 形态。"""
    if extra and extra.get("ark_mode") is True:
        return True
    return "/api/v3" in (endpoint or "")

# -----------------------
# Main API
# -----------------------
def call_doubao_image_api(
    prompt: str,
    ref_images: Optional[List[Union[bytes, Dict[str, Any]]]] = None,
    size: str = "1024x1024",
    *,
    endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
    timeout_sec: int = 60,
) -> bytes:
    """
    Doubao / 方舟 图像生成：文本 + 多参考图 -> 单张图片 bytes

    环境变量（可选）：
        DOUBAO_IMAGE_URL / DOUBAO_API_KEY / DOUBAO_IMAGE_MODEL
        （Ark 别名也支持：ARK_API_KEY）

    模式：
    1) Ark REST（如 https://ark.cn-beijing.volces.com/api/v3/images/generations ）：
       - 仅接受 URL 列表：payload = {"model","prompt","image":[url...],"size":"2K", ...}
       - 触发方式：extra.ark_mode=True，或 endpoint 含 "/api/v3"
    2) 通用/旧模式（dataURL/base64）：
       - 兼容 image_prompts / reference_images / images 三路

    响应解析：
       - 优先取 data[0].url / images[0].url 并下载
       - 否则取 b64_image/image_base64/image/base64
    """
    endpoint = (endpoint or os.getenv("DOUBAO_IMAGE_URL", "")).strip()
    api_key  = (api_key  or os.getenv("DOUBAO_API_KEY") or os.getenv("ARK_API_KEY") or "").strip()
    model    = (model    or os.getenv("DOUBAO_IMAGE_MODEL", "")).strip()

    if not endpoint:
        raise RuntimeError("缺少豆包 endpoint（DOUBAO_IMAGE_URL）")
    if not api_key:
        raise RuntimeError("缺少豆包 API Key（DOUBAO_API_KEY / ARK_API_KEY）")
    if not model:
        raise RuntimeError("缺少豆包模型名（DOUBAO_IMAGE_MODEL）")

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    # ---------- Ark REST 模式 ----------
    if _is_ark_endpoint(endpoint, extra):
        # 取 URL 列表：优先 extra.image_urls；否则从 ref_images 里筛 http(s) URL
        image_urls: List[str] = []
        if extra and isinstance(extra.get("image_urls"), list):
            image_urls = [u for u in extra["image_urls"] if isinstance(u, str)]
        if not image_urls and ref_images:
            for it in _coerce_refs(ref_images):
                val = it.get("image", "")
                if isinstance(val, str) and val.startswith("http"):
                    image_urls.append(val)
        if not image_urls:
            raise RuntimeError("Ark 模式需要 image URL 列表：请传 extra.image_urls 或在 ref_images 中放 http(s) URL")

        payload: Dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "image": image_urls,
            "size": _map_size_for_ark(size),
            # 以下参数与官方一致，可在 extra 中覆盖
            "sequential_image_generation": (extra or {}).get("sequential_image_generation", "disabled"),
            "response_format": (extra or {}).get("response_format", "url"),
            "watermark": bool((extra or {}).get("watermark", False)),
        }
        # 允许透传其他 Ark 可识别字段（如 seed 等）
        for k in ("seed", "strength", "guidance_scale", "style"):
            if extra and k in extra:
                payload[k] = extra[k]

        resp = _request_with_retry(
            "POST", endpoint, headers=headers, data=json.dumps(payload),
            timeout=timeout_sec, retries=3
        )
        if resp.status_code != 200:
            raise RuntimeError(f"[doubao/images] {resp.status_code}: {resp.text[:500]}")

        try:
            js = resp.json()
        except Exception as e:
            raise RuntimeError(f"[doubao/images] 响应非 JSON：{e}; 片段={resp.text[:200]}") from e

        # Ark 通常为 {"data":[{"url": "..."}]}
        if isinstance(js, dict):
            if "data" in js and isinstance(js["data"], list) and js["data"]:
                item = js["data"][0]
                if isinstance(item, dict):
                    if "url" in item and isinstance(item["url"], str):
                        return _download_url(item["url"], timeout=timeout_sec)
                    for key in ("b64_image", "image_base64", "image"):
                        if key in item and isinstance(item[key], str):
                            return _b642bytes(item[key])
            if "images" in js and isinstance(js["images"], list) and js["images"]:
                item = js["images"][0]
                if isinstance(item, dict):
                    if "url" in item and isinstance(item["url"], str):
                        return _download_url(item["url"], timeout=timeout_sec)
                    for key in ("base64", "b64_image", "image"):
                        if key in item and isinstance(item[key], str):
                            return _b642bytes(item[key])
            if "url" in js and isinstance(js["url"], str):
                return _download_url(js["url"], timeout=timeout_sec)
            if "image" in js and isinstance(js["image"], str):
                return _b642bytes(js["image"])

        raise RuntimeError(f"[doubao/images] 未找到图片字段；原始响应片段={json.dumps(js)[:600]}")

    # ---------- 通用/旧模式（base64/dataURL 等） ----------
    imgs = _coerce_refs(ref_images)
    payload: Dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "size": size,
        "n": 1,
    }
    if imgs:
        payload["image_prompts"] = [
            {"image": it["image"], "role": it.get("role", "reference"), "weight": it.get("weight", 1.0)}
            for it in imgs
        ]
        payload["reference_images"] = [
            {"image": it["image"], "role": it.get("role", "reference"), "weight": it.get("weight", 1.0)}
            for it in imgs
        ]
        payload["images"] = [
            {"type": "input", "image": it["image"], "role": it.get("role", "reference")}
            for it in imgs
        ]
    if extra:
        payload.update(extra)

    resp = _request_with_retry(
        "POST", endpoint, headers=headers, data=json.dumps(payload),
        timeout=timeout_sec, retries=3
    )
    if resp.status_code != 200:
        raise RuntimeError(f"[doubao/images] {resp.status_code}: {resp.text[:500]}")

    try:
        js = resp.json()
    except Exception as e:
        raise RuntimeError(f"[doubao/images] 响应非 JSON：{e}; 片段={resp.text[:200]}") from e

    # 兼容多种返回结构：优先 URL（更省流/快），其次 base64
    def _extract_image(jsobj: Any) -> Optional[bytes]:
        if not isinstance(jsobj, dict):
            return None
        if "data" in jsobj and isinstance(jsobj["data"], list) and jsobj["data"]:
            item = jsobj["data"][0]
            if isinstance(item, dict):
                if "url" in item and isinstance(item["url"], str):
                    return _download_url(item["url"], timeout=timeout_sec)
                for key in ("b64_image", "image_base64", "image"):
                    if key in item and isinstance(item[key], str):
                        return _b642bytes(item[key])
        if "images" in jsobj and isinstance(jsobj["images"], list) and jsobj["images"]:
            item = jsobj["images"][0]
            if isinstance(item, dict):
                if "url" in item and isinstance(item["url"], str):
                    return _download_url(item["url"], timeout=timeout_sec)
                for key in ("base64", "b64_image", "image"):
                    if key in item and isinstance(item[key], str):
                        return _b642bytes(item[key])
        if "url" in jsobj and isinstance(jsobj["url"], str):
            return _download_url(jsobj["url"], timeout=timeout_sec)
        if "image" in jsobj and isinstance(jsobj["image"], str):
            return _b642bytes(jsobj["image"])
        return None

    img_bytes = _extract_image(js)
    if not img_bytes:
        raise RuntimeError(f"[doubao/images] 未找到图片字段；原始响应片段={json.dumps(js)[:600]}")
    log.info("[doubao/images] OK model=%s size=%s bytes=%d refs=%d", model, size, len(img_bytes), len(imgs))
    return img_bytes
