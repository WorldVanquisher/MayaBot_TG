# make_layout.py
from PIL import Image, ImageDraw

def make_layout(
    path="tri_panel_layout.png",
    top=(2048, 1024),
    bottom=(1024, 1024),
    gap=24,
    border=8
):
    """
    生成一个标准的三格漫画空模板:
    - 上方一格 (top)
    - 下方左右两格 (bottom, bottom)
    """
    top_w, top_h = top
    bot_w, bot_h = bottom

    # 计算画布总大小
    W = max(top_w, bot_w * 2 + gap)
    H = top_h + gap + bot_h

    # 创建白底画布
    img = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(img)

    # 上格（居中）
    top_x = (W - top_w) // 2
    top_box = (top_x, 0, top_x + top_w, top_h)

    # 下格（左右）
    left_box  = (0, top_h + gap, bot_w, top_h + gap + bot_h)
    right_box = (bot_w + gap, top_h + gap, bot_w + gap + bot_w, top_h + gap + bot_h)

    # 画出三格黑框
    for box in (top_box, left_box, right_box):
        draw.rectangle(box, outline=0, width=border)

    img.save(path)
    print(f"✅ 已保存模板: {path} ({W}x{H})")

if __name__ == "__main__":
    make_layout()
