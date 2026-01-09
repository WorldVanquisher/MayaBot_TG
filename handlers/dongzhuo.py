# handlers/dongzhuo.py
# -*- coding: utf-8 -*-
import os
import io
import json
import time
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
from PIL import Image, ImageDraw, ImageFont
from utils.storage_tos import TosStorage

log = logging.getLogger("dongzhuo")

ASK_ONE_A, ASK_ONE_B = range(2)
DEBUG_SAVE_IMAGES = False  # disable debug image dumps

storage = TosStorage(prefix="dongzhuo")

# -------------------
# Utils
# -------------------
def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()[:10]

async def _safe_reply(update: Update, text: str):
    try:
        if update.message:
            await update.message.reply_text(text)
        elif update.effective_chat:
            await update.get_bot().send_message(update.effective_chat.id, text)
    except Exception as e:
        log.warning("reply failed: %s", e)

def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


# -------------------
# Bubble drawing on final image
# -------------------
def _draw_bubble_autofit(draw: ImageDraw.ImageDraw, box, text, font_path,
                         max_fs=50, min_fs=22, padding=18, border=3):
    """在最终整图上画【椭圆对白框 + 文本】（单/短行自适应）。box=(x,y,w,h)"""
    x,y,w,h = box
    draw.ellipse([x, y, x+w, y+h], fill=255, outline=0, width=border)
    for fs in range(max_fs, min_fs - 1, -2):
        font = _load_font(font_path, fs)
        bb = draw.textbbox((0,0), text, font=font)
        tw, th = (bb[2]-bb[0], bb[3]-bb[1])
        if tw <= w - 2*padding and th <= h - 2*padding:
            tx = x + (w - tw)//2
            ty = y + (h - th)//2
            draw.text((tx, ty), text, fill=0, font=font)
            return
    font = _load_font(font_path, min_fs)
    bb = draw.textbbox((0,0), text, font=font)
    tw, th = (bb[2]-bb[0], bb[3]-bb[1])
    tx = x + (w - tw)//2
    ty = y + (h - th)//2
    draw.text((tx, ty), text, fill=0, font=font)

def _draw_dialog_bubbles_on_final_image(img: Image.Image, cfg: dict):
    """依据模板几何比例，在最终整图上放置 3 个对白框与台词（更靠角、更小、细边）。"""
    comic_cfg = cfg.get("comic", {}) if isinstance(cfg, dict) else {}
    psize = (comic_cfg.get("panel_size", {}) if isinstance(comic_cfg, dict) else {})
    top_w, top_h = tuple(psize.get("top", [2048, 1024]))
    bot_w, bot_h = tuple(psize.get("bottom", [1024, 1024]))
    gap = 24

    Wt = max(top_w, bot_w * 2 + gap)
    Ht = top_h + gap + bot_h

    W, H = img.size
    sx = W / Wt
    sy = H / Ht

    top_x = (Wt - top_w) // 2
    top_box_t  = (top_x, 0, top_x + top_w, top_h)
    left_box_t = (0, top_h + gap, bot_w, top_h + gap + bot_h)
    right_box_t= (bot_w + gap, top_h + gap, bot_w + gap + bot_w, top_h + gap + bot_h)

    def _scale_box(tb):
        x1, y1, x2, y2 = tb
        return (int(x1*sx), int(y1*sy), int(x2*sx), int(y2*sy))

    top_box   = _scale_box(top_box_t)
    left_box  = _scale_box(left_box_t)
    right_box = _scale_box(right_box_t)

    def _rel_box(panel, rx, ry, rw, rh):
        x1, y1, x2, y2 = panel
        pw, ph = (x2 - x1, y2 - y1)
        bx = x1 + int(pw * rx)
        by = y1 + int(ph * ry)
        bw = int(pw * rw)
        bh = int(ph * rh)
        return (bx, by, bw, bh)

    # 更克制的对白框：更靠角、更小、更细
    b1 = _rel_box(top_box,   rx=0.70, ry=0.06, rw=0.24, rh=0.24)  # 顶格右上
    b2 = _rel_box(left_box,  rx=0.06, ry=0.06, rw=0.38, rh=0.30)  # 左下左上
    b3 = _rel_box(right_box, rx=0.52, ry=0.06, rw=0.44, rh=0.30)  # 右下右上

    draw = ImageDraw.Draw(img)
    font_path = comic_cfg.get("font_path") or "assets/fonts/NotoSansSC-Regular.ttf"

    t1 = "……"
    t2 = "你…你可有何话说？"
    t3 = "再无话说，请速速动手！"

    _draw_bubble_autofit(draw, b1, t1, font_path, max_fs=48, min_fs=22, padding=20, border=3)
    _draw_bubble_autofit(draw, b2, t2, font_path, max_fs=44, min_fs=20, padding=20, border=3)
    _draw_bubble_autofit(draw, b3, t3, font_path, max_fs=44, min_fs=20, padding=20, border=3)

# -------------------
# Handlers
# -------------------
async def cmd_dongzhuo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """一次成图：先发角色A，再发角色B；服务端用 URL 吃参考图；本地灰度+纠偏+对白。"""
    uid = update.effective_user.id if update.effective_user else None
    owner = context.chat_data.get("dongzhuo_owner")
    if owner is not None and owner != uid:
        return ConversationHandler.END
    context.chat_data["dongzhuo_owner"] = uid
    context.user_data.clear()
    await _safe_reply(update, "350234已就位. 请提供[董卓]和[吕布]的图片. \n请先提供[吕布]的图片.\n请最好截图为该人物的半身像, 否则大模型处理可能有点问题. \n随时可 /cancel 退出.")
    return ASK_ONE_A

async def recv_one_a(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return ASK_ONE_A

    tgfile = await update.message.photo[-1].get_file()
    data = await tgfile.download_as_bytearray()
    bts = bytes(data)

    context.user_data["one_A"] = bts
    context.user_data["one_A_url"] = tgfile.file_path

    # 本地 debug 备份
    # (DEBUG_DIR / f"dz_A_{int(time.time())}.jpg").write_bytes(data)

    # 从 telegram file_path 猜后缀（大概率 .jpg）
    ext = Path(tgfile.file_path).suffix or ".jpg"

    try:
        tos_url = storage.upload_bytes(bts, suffix=ext)
        context.user_data["one_A_tos_url"] = tos_url
        log.info("Uploaded one_A to TOS: %s", tos_url)
    except Exception as e:
        log.warning("Upload one_A to TOS failed: %s", e)
        context.user_data["one_A_tos_url"] = None

    await _safe_reply(update, "已收到[吕布]. 请发送[董卓]图片.")
    return ASK_ONE_B


async def recv_one_b(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return ASK_ONE_B

    tgfile = await update.message.photo[-1].get_file()
    data = await tgfile.download_as_bytearray()
    bts = bytes(data)

    context.user_data["one_B"] = bts
    context.user_data["one_B_url"] = tgfile.file_path
    # (DEBUG_DIR / f"dz_B_{int(time.time())}.jpg").write_bytes(data)

    ext = Path(tgfile.file_path).suffix or ".jpg"
    try:
        tos_url = storage.upload_bytes(bts, suffix=ext)
        context.user_data["one_B_tos_url"] = tos_url
        log.info("Uploaded one_B to TOS: %s", tos_url)
    except Exception as e:
        log.warning("Upload one_B to TOS failed: %s", e)
        context.user_data["one_B_tos_url"] = None

    try:
        # 读取配置
        try:
            with open("config.json", "r", encoding="utf-8") as cf:
                cfg = json.load(cf)
        except Exception:
            cfg = {}
        api_cfg = cfg.get("image_api", {}) if isinstance(cfg, dict) else {}
        params  = api_cfg.get("params", {}) if isinstance(api_cfg, dict) else {}

        # Ark 尺寸建议 '2K'；如果你传 '2048x1024'，我们在 doubao_api 里会映射到 '2K'
        gen_size = params.get("size", "2K")

        # —— Telegram 文件直链（PTB v20 的 file_path 已是完整 URL）——
        url_A = context.user_data["one_A_url"]
        url_B = context.user_data["one_B_url"]

        await _safe_reply(update, "往日种种, 你说的可是往日...(合成中)")

        # === 你的中文 PROMPT（保持原样，不作任何修改） ===
        PROMPT = (
            "你是一位专业的漫画绘制师，擅长黑白墨线写实风格的漫画创作，请严格按照以下规则生成画面。\n\n"
            "【核心结构要求】\n"
            "整张图片必须且只能包含三个画格：\n"
            "- 第一行：一个横向大画格\n"
            "- 第二行：两个并排的小画格\n"
            "总画格数 = 3。严禁生成四格漫画、多格漫画或任何额外画格。如果出现第四个画格或额外分割，视为完全错误。\n\n"
            "【角色绑定规则 - 极其重要】\n"
            "第一张参考图的角色 = 角色A；第二张参考图的角色 = 角色B。\n"
            "角色A只能出现在第一格与第二格；角色B只能出现在第三格。\n"
            "严禁角色错位、互换、混用外貌特征。\n\n"
            "【分格内容设定】\n\n"
            "第一格（上方大格）：\n"
            "角色A出镜。角色流着眼泪，嘴巴紧闭，带有幽怨的情绪，正面注视镜头，表情压抑而痛苦。\n\n"
            "第二格（左下格）：\n"
            "角色A出镜。角色悔恨地流泪，带有哭泣的表情，情绪明显更加崩溃，似乎正在询问：“你，你可有何话说？”。\n\n"
            "第三格（右下格）：\n"
            "角色B出镜。角色带着决绝的表情，脖子上方有一根绳子垂下并套住其颈部，他闭上双眼，神情庄重，仿佛正在回复到：“再无话说，请速速动手！”。\n\n"
            "【风格要求】\n"
            "黑白墨线漫画风格，写实，线条干净清晰，使用少量网点阴影，高对比度。\n"
            "禁止输出任何的对话框、文字、和标点符号。\n"
            "背景只能为白色或灰色，禁止任何彩色内容。\n\n"
            "【排版补充说明】\n"
            "画面必须严格遵守三格结构：\n"
            "- 不允许在画格内部再次分割\n"
            "- 不允许出现第四格或隐藏格\n"
            "- 不允许额外边框或杂乱线条\n\n"
        )

        NEG = (
            "four panels, 4 panels, comic strip, manga page with four frames, extra panel, "
            "2x2 grid, multi-frame layout, extra frame, additional panel, split panel"
        )

        # ====== Ark REST：传 URL 列表（用我们统一的 API）======
        from utils.doubao_api import call_doubao_image_api
        ark_endpoint = api_cfg.get("endpoint") or os.getenv("DOUBAO_IMAGE_URL") \
                    or "https://ark.cn-beijing.volces.com/api/v3/images/generations"
        ark_key   = os.getenv("ARK_API_KEY") or api_cfg.get("api_key") or os.getenv("DOUBAO_API_KEY")
        ark_model = (api_cfg.get("model") or os.getenv("DOUBAO_IMAGE_MODEL") or "").strip()
        timeout   = int(api_cfg.get("timeout_sec", 60))

        if not ark_key:
            raise RuntimeError("缺少 ARK_API_KEY / DOUBAO_API_KEY")
        if not ark_model:
            raise RuntimeError("缺少 DOUBAO_IMAGE_MODEL（模型 ID）")

        # 用 bytes 直接当参考图（utils.doubao_api 会自动转成 dataURL）
                # 从 user_data 里拿到刚才上传到 TOS 的两个 URL
        url_A = context.user_data.get("one_A_tos_url")
        url_B = context.user_data.get("one_B_tos_url")
        image_urls = [u for u in (url_A, url_B) if u]

        if len(image_urls) < 2:
            raise RuntimeError("参考图上传对象存储失败，缺少 image_urls")

        out = call_doubao_image_api(
            prompt=PROMPT,
            ref_images=None,      # Ark 模式下不再传 bytes 参考图
            size=gen_size,
            endpoint=ark_endpoint,
            api_key=ark_key,
            model=ark_model,
            extra={
                "ark_mode": True,   # 强制走 Ark 分支
                "image_urls": image_urls,
                "style": "manga",
                "negative_prompt": NEG,
            },
            timeout_sec=timeout,
        )


        # === 本机处理：灰度化 + 版面纠偏 + 对白气泡 ===
        img = Image.open(io.BytesIO(out)).convert("L")
        _draw_dialog_bubbles_on_final_image(img, cfg) # 本地画对白

        # 回图
        bio = io.BytesIO()
        img.save(bio, "JPEG", quality=92)
        bio.name = "dongzhuo_final.jpg"
        bio.seek(0)
        await update.message.reply_photo(photo=bio, caption="真是一对苦命鸳鸯啊.")

    except Exception as e:
        import traceback
        log.error("dongzhuo render failed: %s\n%s", e, traceback.format_exc())
        await _safe_reply(update, f"吃大份去吧. \n渲染出错：{e}")
    finally:
        context.chat_data.pop("dongzhuo_owner", None)

    return ConversationHandler.END

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.chat_data.pop("dongzhuo_owner", None)
    await _safe_reply(update, "已取消. 吃大份去吧.")
    return ConversationHandler.END

def register(app: Application):
    conv = ConversationHandler(
        entry_points=[CommandHandler("dongzhuo", cmd_dongzhuo)],
        states={
            ASK_ONE_A: [MessageHandler(filters.PHOTO, recv_one_a)],
            ASK_ONE_B: [MessageHandler(filters.PHOTO, recv_one_b)],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        name="dongzhuo_one_shot",
        persistent=False,
    )
    app.add_handler(conv)
    log.info("Dongzhuo one-shot handler registered.")