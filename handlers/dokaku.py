## Legacy Model




# handlers/dokaku.py
# -*- coding: utf-8 -*-
import io
import re
import json
import time
import random
import logging
import hashlib
from pathlib import Path
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from PIL import Image, ImageDraw, ImageFont, ImageOps

# 你已有的工具函数
from utils.image_api import (
    call_openai_image_api, save_bytes, letterbox, compress_image
)

log = logging.getLogger("comic")

# -------- 会话状态 --------
ASK_IMG1, ASK_IMG2 = range(2)

DEBUG_DIR = Path("_debug")
DEBUG_DIR.mkdir(exist_ok=True)

def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()[:10]

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()

async def _safe_reply(update: Update, text: str):
    try:
        if update.message:
            await update.message.reply_text(text)
        elif update.effective_chat:
            await update.get_bot().send_message(update.effective_chat.id, text)
    except Exception as e:
        log.warning("reply failed: %s", e)

# --------- 重试封装 ---------
def gen_with_retry(
    prompt: str,
    size: str,
    ref: bytes | None,
    *,
    model: str,
    quality: str | None,
    timeout: int = 60,
    retries: int = 3,
    backoff_base: float = 0.9,
    backoff_factor: float = 1.6,
    jitter: float = 0.35,
    enforce_change: bool = False,  # True：若与参考图MD5相同，则视为失败重试（仅ref不为空时）
):
    """
    对 call_openai_image_api 的健壮封装：
      - 只对 429/5xx/网络故障重试（指数退避+抖动）
      - 可选 enforce_change: 生成结果与参考图相同则重试
    """
    def _parse_status_from_error(err: Exception) -> int | None:
        msg = str(err)
        m = re.search(r"\[(?:images/(?:edits|generations))\]\s+(\d{3})", msg)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        m2 = re.search(r"HTTP\s+(\d{3})", msg, flags=re.I)
        if m2:
            try:
                return int(m2.group(1))
            except Exception:
                return None
        return None

    def _retryable(err: Exception) -> bool:
        code = _parse_status_from_error(err)
        if code in {429, 500, 502, 503, 504}:
            return True
        s = str(err).lower()
        hints = ("请求失败", "timed out", "timeout", "temporarily", "connection", "reset", "refused")
        return any(h in s for h in hints)

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            out = call_openai_image_api(
                prompt=prompt,
                size=size,
                quality=quality or "",
                ref_image=ref,
                model=model,
                timeout_sec=timeout,
            )
            if enforce_change and ref:
                import hashlib as _hl
                if _hl.md5(out).digest() == _hl.md5(ref).digest():
                    raise RuntimeError("Image identical to reference; treating as failure for retry.")
            if attempt > 1:
                log.info("gen_with_retry: success on attempt %d", attempt)
            return out
        except Exception as e:
            last_err = e
            log.warning("gen_with_retry: attempt %d/%d failed: %s", attempt, retries, e)
            if attempt >= retries or not _retryable(e):
                raise
            sleep_s = backoff_base * (backoff_factor ** (attempt - 1)) + random.random() * jitter
            log.info("gen_with_retry: backing off for %.2fs before retry...", sleep_s)
            time.sleep(sleep_s)
    if last_err:
        raise last_err
    raise RuntimeError("gen_with_retry: unexpected failure without exception.")

# --------- 漫画气泡 ---------
def draw_speech_bubble(
    img, box, text, font_path,
    max_font_size=52, min_font_size=22,
    padding=22, tail=None, border=5, line_spacing=1.18
):
    draw = ImageDraw.Draw(img)
    x, y, w, h = box
    ellipse_box = [x, y, x + w, y + h]
    draw.ellipse(ellipse_box, fill="white", outline="black", width=border)
    if tail:
        draw.polygon(tail, fill="white", outline="black")

    def can_fit(fs):
        font = _load_font(font_path, fs)
        content_w = w - 2 * padding
        lines, cur = [], ""
        for ch in text:
            test = cur + ch
            bb = draw.textbbox((0, 0), test, font=font)
            if bb[2] - bb[0] <= content_w:
                cur = test
            else:
                if cur: lines.append(cur)
                cur = ch
        if cur: lines.append(cur)
        line_h = draw.textbbox((0, 0), "字", font=font)[3]
        total_h = int(len(lines) * line_h * line_spacing)
        return total_h <= (h - 2 * padding), lines, font, line_h

    chosen = None
    for fs in range(max_font_size, min_font_size - 1, -2):
        ok, lines, font, line_h = can_fit(fs)
        if ok:
            chosen = (lines, font, line_h); break
    if not chosen:
        _, lines, font, line_h = can_fit(min_font_size)
    else:
        lines, font, line_h = chosen

    content_h = int(len(lines) * line_h * line_spacing)
    cy = y + (h - content_h) // 2
    for i, ln in enumerate(lines):
        bb = draw.textbbox((0, 0), ln, font=font)
        tw = bb[2] - bb[0]
        tx = x + (w - tw) // 2
        ty = cy + int(i * line_h * line_spacing)
        draw.text((tx, ty), ln, fill="black", font=font)

# -------- /dokaku --------
async def cmd_dokaku(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    owner = context.chat_data.get("dokaku_owner")
    if owner is not None and owner != uid:
        return ConversationHandler.END

    context.chat_data["dokaku_owner"] = uid
    context.user_data.clear()
    await _safe_reply(update, "请提供两位苦命鸳鸯的图片.\n请先提供吕布的图片.\n随时可 /cancel 退出.")
    return ASK_IMG1

# ---- 第一张图 ----
async def recv_img1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    owner = context.chat_data.get("dokaku_owner")
    if owner is not None and uid != owner:
        return ASK_IMG1
    if not update.message or not update.message.photo:
        return ASK_IMG1

    tgfile = await update.message.photo[-1].get_file()
    uniq = tgfile.file_unique_id
    data = await tgfile.download_as_bytearray()
    context.user_data["img1"] = bytes(data)
    context.user_data["img1_id"] = uniq

    ts = int(time.time())
    p = DEBUG_DIR / f"img1_{uniq}_{ts}.jpg"
    with open(p, "wb") as f:
        f.write(data)
    log.info("[ASK_IMG1] id=%s md5=%s saved=%s size=%d", uniq, _md5(data), p, len(data))
    await _safe_reply(update, "等待董卓中.")
    return ASK_IMG2

# ---- 第二张图 ----
async def recv_img2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    owner = context.chat_data.get("dokaku_owner")
    if owner is not None and uid != owner:
        return ASK_IMG2
    if not update.message or not update.message.photo:
        return ASK_IMG2

    tgfile = await update.message.photo[-1].get_file()
    uniq = tgfile.file_unique_id
    data = await tgfile.download_as_bytearray()
    context.user_data["img2"] = bytes(data)
    context.user_data["img2_id"] = uniq

    ts = int(time.time())
    Path("_debug").mkdir(exist_ok=True)
    with open(Path("_debug") / f"img2_{uniq}_{ts}.jpg", "wb") as f:
        f.write(data)

    try:
        with open("config.json", "r", encoding="utf-8") as cf:
            cfg = json.load(cf)
        api_cfg = cfg.get("image_api", {})
        comic_cfg = cfg.get("comic", {})

        top_w, top_h = comic_cfg.get("panel_size", {}).get("top", [1536, 1024])
        bot_w, bot_h = comic_cfg.get("panel_size", {}).get("bottom", [1024, 1024])
        size_top = f"{top_w}x{top_h}"
        size_bot = f"{bot_w}x{bot_h}"

        # —— 强制先用 dall-e-2，确保能打通 —— #
        model = (api_cfg.get("model") or "dall-e-2").strip()
        if model.lower().startswith("gpt-image-"):
            log.warning("配置 model=%s 可能不可用；建议暂时改为 'dall-e-2'", model)
        quality = api_cfg.get("quality", "standard")
        timeout = int(api_cfg.get("timeout_sec", 60))

        # 仅文本 prompts（安全、黑白漫画风）
        p1 = (
            "Close-up, head-and-shoulders portrait, misty eyes with subtle tears, "
            "mouth gently closed, facing camera; black-and-white manga ink style, "
            "clean linework, high contrast, screentone; no text, no speech bubbles."
        )
        p2 = (
            "Close-up, crying expression, mouth slightly open as if asking a question, "
            "gazing right; black-and-white manga ink style, screentone, clean lines; "
            "no text, no speech bubbles."
        )
        p3 = (
            "Close-up, solemn and resolute expression, eyes gently closed; "
            "a ceremonial decorative collar resting on shoulders (fashion accessory); "
            "facing left; black-and-white manga ink style, strong contrast, clean linework; "
            "no text, no speech bubbles."
        )

        refA = context.user_data["img1"]
        refB = context.user_data["img2"]

        # —— 是否使用参考图（默认 False，先保证 usage > 0 且能出新图） —— #
        use_reference = False  # 改成 True 即启用 edits+透明mask 的风格迁移尝试
        def _maybe_ref(x): return x if use_reference else None

        await _safe_reply(update, "两张图片已收到。\n开始AI渲染（约数秒）…")

        # 生成
        b1 = gen_with_retry(p1, size_top, _maybe_ref(refA),
                            model=model, quality=quality, timeout=timeout, retries=3)
        b2 = gen_with_retry(p2, size_bot, _maybe_ref(refA),
                            model=model, quality=quality, timeout=timeout, retries=3)
        b3 = gen_with_retry(p3, size_bot, _maybe_ref(refB),
                            model=model, quality=quality, timeout=timeout, retries=3)

        # MD5 对比（便于你在日志里确认是否真的是新图）
        log.info("p1 md5=%s (refA=%s) size=%d", _md5(b1), _md5(refA), len(b1))
        log.info("p2 md5=%s (refA=%s) size=%d", _md5(b2), _md5(refA), len(b2))
        log.info("p3 md5=%s (refB=%s) size=%d", _md5(b3), _md5(refB), len(b3))

        # 调试落盘
        save_bytes(Path("_debug/p1.jpg"), b1)
        save_bytes(Path("_debug/p2.jpg"), b2)
        save_bytes(Path("_debug/p3.jpg"), b3)

        # 打开为 PIL 并 letterbox
        img1 = letterbox(Image.open(io.BytesIO(b1)).convert("L"), top_w, top_h, 255)
        img2 = letterbox(Image.open(io.BytesIO(b2)).convert("L"), bot_w, bot_h, 255)
        img3 = letterbox(Image.open(io.BytesIO(b3)).convert("L"), bot_w, bot_h, 255)

        # 拼图
        gap = 24
        panel_border = 8
        canvas_w = max(top_w, bot_w * 2 + gap)
        canvas_h = top_h + bot_h + gap
        canvas = Image.new("L", (canvas_w, canvas_h), 255)
        draw = ImageDraw.Draw(canvas)

        top_x = (canvas_w - top_w) // 2
        top_box  = (top_x, 0, top_x + top_w, top_h)
        left_box = (0, top_h + gap, bot_w, top_h + gap + bot_h)
        right_box= (bot_w + gap, top_h + gap, bot_w + gap + bot_w, top_h + gap + bot_h)

        canvas.paste(img1, (top_x, 0))
        canvas.paste(img2, (0, top_h + gap))
        canvas.paste(img3, (bot_w + gap, top_h + gap))

        for bx in (top_box, left_box, right_box):
            draw.rectangle(bx, outline="black", width=panel_border)

        # 字体
        font_path = None
        for p in [
            "assets/fonts/NotoSansSC-Medium.ttf",
            "assets/fonts/NotoSansSC-SemiBold.ttf",
            "assets/fonts/NotoSansSC-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        ]:
            if Path(p).exists():
                font_path = p; break
        if font_path is None:
            font_path = "assets/fonts/NotoSansSC-Regular.ttf"

        # 对白
        text1, text2, text3 = "……", "你…你可有何话说？", "再无话说，请速速动手！"

        # 气泡
        b1_box = (top_x + int(top_w*0.60), int(top_h*0.10), int(top_w*0.33), int(top_h*0.22))
        tail1 = (b1_box[0] + b1_box[2]//2, b1_box[1] + b1_box[3] - 4,
                 b1_box[0] + b1_box[2]//2 - 18, b1_box[1] + b1_box[3] + 34,
                 b1_box[0] + b1_box[2]//2 + 6,  b1_box[1] + b1_box[3] + 12)

        b2_box = (int(bot_w*0.06), top_h + gap + int(bot_h*0.06), int(bot_w*0.45), int(bot_h*0.28))
        tail2 = (b2_box[0] + b2_box[2] - 10, b2_box[1] + b2_box[3] - 6,
                 b2_box[0] + b2_box[2] + 24, b2_box[1] + b2_box[3] + 26,
                 b2_box[0] + b2_box[2] - 6,  b2_box[1] + b2_box[3] + 10)

        b3_box = ((bot_w + gap) + int(bot_w*0.10), top_h + gap + int(bot_h*0.08),
                  int(bot_w*0.60), int(bot_h*0.32))
        tail3 = (b3_box[0] + 16, b3_box[1] + b3_box[3] - 6,
                 b3_box[0] - 16, b3_box[1] + b3_box[3] + 24,
                 b3_box[0] + 6,  b3_box[1] + b3_box[3] + 12)

        draw_speech_bubble(canvas, b1_box, text1, font_path, max_font_size=56, min_font_size=26, tail=tail1)
        draw_speech_bubble(canvas, b2_box, text2, font_path, max_font_size=50, min_font_size=24, tail=tail2)
        draw_speech_bubble(canvas, b3_box, text3, font_path, max_font_size=50, min_font_size=24, tail=tail3)

        # 压缩 & 回图
        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, "JPEG", quality=95)
        compressed = compress_image(buf.getvalue(), fmt="JPEG", max_w=1600, max_h=1600,
                                    size_cap_kb=700, initial_quality=88, min_quality=62, step=4)

        bio = io.BytesIO(compressed)
        bio.name = "dokaku.jpg"
        bio.seek(0)
        await update.message.reply_photo(photo=bio, caption="真是一对苦命鸳鸯啊.")

    except Exception as e:
        import traceback
        log.error("Render failed: %s\n%s", e, traceback.format_exc())
        await _safe_reply(update, f"吃大份去吧. 渲染时出错：{e}")
    finally:
        context.chat_data.pop("dokaku_owner", None)

    return ConversationHandler.END

# ---- 非图片时提示 ----
async def need_photo_img1(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    owner = context.chat_data.get("dokaku_owner")
    if owner is not None and uid != owner:
        return ASK_IMG1
    await _safe_reply(update, "请提供吕布的[图片]. \n随时可 /cancel 退出。")
    return ASK_IMG1

async def need_photo_img2(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    owner = context.chat_data.get("dokaku_owner")
    if owner is not None and uid != owner:
        return ASK_IMG2
    await _safe_reply(update, "还差董卓的[图片]. \n随时可 /cancel 退出。")
    return ASK_IMG2

# ---- 取消 ----
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.chat_data.pop("dokaku_owner", None)
    await _safe_reply(update, "已取消, 吃大份去吧.")
    return ConversationHandler.END

# ---- 注册 ----
def register(app: Application):
    conv = ConversationHandler(
        entry_points=[CommandHandler("dokaku", cmd_dokaku)],
        states={
            ASK_IMG1: [
                MessageHandler(filters.PHOTO, recv_img1),
                MessageHandler(~(filters.PHOTO | filters.COMMAND), need_photo_img1),
            ],
            ASK_IMG2: [
                MessageHandler(filters.PHOTO, recv_img2),
                MessageHandler(~(filters.PHOTO | filters.COMMAND), need_photo_img2),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="dokaku_conv",
        persistent=False,
    )
    app.add_handler(conv)
    log.info("Comic ConversationHandler registered.")
