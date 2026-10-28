#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小智AI 弹窗/状态栏渲染（MaixCAM2 640x480）

按参考图 1:1 复刻（参考图为实拍照片，取色/取位已按透视校正后测量）：
    顶部状态条  : 半透明深蓝，左侧时钟+表情，居中状态文字（聆听中…），右侧机器人图标 + 关闭 X
    聊天气泡    : 亮青绿圆角气泡（AI，左对齐）/ 浅色气泡（用户，右对齐），深色文字
    底部提示    : 细字提示唤醒词与操作

输出文件格式（供 libmaixcam2_overlay.so 读取）：
    偏移 0  : u32 帧数 n
    偏移 4  : u32 宽 w
    偏移 8  : u32 高 h
    偏移 12 : n*w*h*4 字节 RGBA
"""
import os
import io
import math
import struct
import time

from PIL import Image, ImageDraw, ImageFont

W, H = 640, 480

FONT_PATH = "/maixapp/share/font/SourceHanSansCN-Regular.otf"

# ---- 配色（参考图实测，已去除实拍偏色） --------------------------------
C_STRIP_BG = (8, 26, 48)          # 顶部状态条底
C_STRIP_LINE = (32, 62, 96)
C_STATE = (140, 234, 255)         # 聆听中… 青蓝
C_CLOCK = (232, 242, 252)
C_FACE = (255, 214, 74)           # 左侧表情（黄）
C_ICON = (236, 244, 252)          # 机器人图标 / X
C_BUBBLE_AI = (47, 232, 196)      # AI 气泡：亮青绿
C_BUBBLE_AI_TX = (6, 42, 40)
C_BUBBLE_USER = (222, 232, 244)   # 用户气泡：浅色
C_BUBBLE_USER_TX = (18, 34, 52)
C_BUBBLE_SYS = (255, 206, 84)
C_BUBBLE_SYS_TX = (58, 40, 4)
C_HINT = (120, 150, 178)
C_ACT = (255, 176, 60)

STRIP_H = 58
MCP_FOOTER_H = 42
BUBBLE_TOP = 174
BUBBLE_GAP = 8
BUBBLE_MAXW = 520
BUBBLE_MINW = 60
BUBBLE_PAD_X = 14
BUBBLE_PAD_Y = 8
BUBBLE_RADIUS = 13
FONT_BUBBLE = 21
LINE_H = 28
MAX_BUBBLES = 4
LEFT_X = 20
RIGHT_X = W - 20

# 顶部条交互热区（640x480；服务将shim原始触摸坐标旋转到此坐标系）
BTN_CLOSE = (604, 12, 28, 32)
BTN_SPEAKER = (556, 12, 34, 32)

_fonts = {}


def font(size):
    f = _fonts.get(size)
    if f is None:
        try:
            f = ImageFont.truetype(FONT_PATH, size)
        except Exception:
            f = ImageFont.load_default()
        _fonts[size] = f
    return f


def text_w(d, s, f):
    if d is not None:
        try:
            return d.textlength(s, font=f)
        except Exception:
            pass
    try:
        return f.getbbox(s)[2]
    except Exception:
        return len(s) * 10


def bold_text(d, xy, s, f, fill, strength=1):
    """SourceHanSans 只有 Regular：用微偏移重绘模拟粗体（参考图中状态文字为粗体）"""
    x, y = xy
    for dx, dy in ((0, 0), (strength, 0), (0, strength), (strength, strength)):
        d.text((x + dx, y + dy), s, font=f, fill=fill)


def draw_robot(d, cx, cy, r, color, width=None):
    """小智机器人脸：圆环 + 两只竖眼"""
    w = width or max(2, int(r * 0.16))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=w)
    ew = max(2, int(r * 0.22))
    eh = max(4, int(r * 0.52))
    gap = max(2, int(r * 0.20))
    top = cy - eh / 2
    for sgn in (-1, 1):
        x0 = cx + sgn * gap - ew / 2
        d.rounded_rectangle([x0, top, x0 + ew, top + eh],
                            radius=ew / 2, fill=color)


def draw_smiley(d, cx, cy, r, color, ink=(90, 62, 0)):
    """左侧黄色表情（参考图左上角笑脸）"""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    er = max(1, int(r * 0.13))
    for sgn in (-1, 1):
        ex = cx + sgn * int(r * 0.34)
        d.ellipse([ex - er, cy - int(r * 0.22) - er,
                   ex + er, cy - int(r * 0.22) + er], fill=ink)
    d.arc([cx - int(r * 0.52), cy - int(r * 0.18),
           cx + int(r * 0.52), cy + int(r * 0.56)],
          20, 160, fill=ink, width=max(1, int(r * 0.13)))


def draw_speaker(d, cx, cy, s, color):
    d.polygon([(cx - s * 0.45, cy - s * 0.18), (cx - s * 0.16, cy - s * 0.18),
               (cx + s * 0.10, cy - s * 0.45), (cx + s * 0.10, cy + s * 0.45),
               (cx - s * 0.16, cy + s * 0.18), (cx - s * 0.45, cy + s * 0.18)],
              fill=color)
    for k in (0.42, 0.66):
        d.arc([cx + s * 0.05 - s * k, cy - s * k,
               cx + s * 0.05 + s * k, cy + s * k], -55, 55,
              fill=color, width=max(1, int(s * 0.09)))


def draw_close(d, cx, cy, r, color, wdt=3):
    k = r * 0.62
    d.line([cx - k, cy - k, cx + k, cy + k], fill=color, width=wdt)
    d.line([cx - k, cy + k, cx + k, cy - k], fill=color, width=wdt)


# 状态 -> (状态文字, 表情心情)
STATE_STYLE = {
    "connecting": ("连接中…", "neutral"),
    "listening": ("聆听中…", "happy"),
    "thinking": ("思考中…", "thinking"),
    "speaking": ("说话中…", "happy"),
    "activation": ("待激活", "neutral"),
}

# 服务器 emotion -> 左侧表情（参考图只有笑脸，其它情绪沿用同一视觉语言）
EMOTION_FACE = {
    "happy": (255, 214, 74),
    "laughing": (255, 214, 74),
    "funny": (255, 214, 74),
    "excited": (255, 200, 90),
    "neutral": (255, 226, 130),
    "sad": (150, 186, 232),
    "angry": (255, 140, 120),
    "crying": (150, 186, 232),
    "loving": (255, 168, 190),
    "surprised": (255, 226, 130),
    "thinking": (196, 214, 236),
    "cool": (170, 210, 240),
}


def wrap(text, f, maxw, d=None):
    """按像素宽度折行（中文逐字，英文尽量不断词）"""
    text = str(text).replace("\r", "").strip()
    if not text:
        return [""]
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if text_w(d, cur + ch, f) > maxw and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur or not lines:
        lines.append(cur)
    return lines


def _blit_bubble(img, d, x0, y0, x1, y1, fill, radius=BUBBLE_RADIUS):
    d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)


def render(path, *, state="idle", detail="", bubbles=(), emotion="",
           wake_word="你好小智", muted=False, clock=True, activation_code="",
           footer=True, photo_jpeg=None, mcp_tool="", imu_sample=None,
           imu_compact=None):
    """渲染一帧到 path（原子替换）。

    state="idle" 且没有临时反馈 -> 全透明（弹窗隐藏）。
    """
    show = (state != "idle" or photo_jpeg is not None or bool(mcp_tool)
            or imu_sample is not None or imu_compact is not None)
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if show:
        d = ImageDraw.Draw(img)

        # ---------------- 顶部状态条 ----------------
        d.rectangle([0, 0, W, STRIP_H], fill=C_STRIP_BG + (205,))
        d.line([(0, STRIP_H), (W, STRIP_H)], fill=C_STRIP_LINE + (255,), width=1)

        # 左：表情 + 时钟
        draw_smiley(d, 33, 29, 17, EMOTION_FACE.get(emotion, C_FACE))
        if clock:
            bold_text(d, (56, 19), time.strftime("%H:%M"), font(19), C_CLOCK)

        # 中：状态文字
        txt, _mood = STATE_STYLE.get(state, (detail or state, "neutral"))
        if state == "activation":
            txt = "待激活"
        fs = font(23)
        tw = text_w(d, txt, fs)
        bold_text(d, ((W - tw) / 2, 17), txt, fs, C_STATE)

        # 右：机器人状态图标 + 关闭 X
        draw_robot(d, 573, 29, 16, C_ICON)
        draw_close(d, BTN_CLOSE[0] + BTN_CLOSE[2] // 2,
                   BTN_CLOSE[1] + BTN_CLOSE[3] // 2, 11, C_ICON, 3)

        # ---------------- 激活态 ----------------
        if state == "activation":
            d.rounded_rectangle([LEFT_X, 84, W - LEFT_X, 150], radius=14,
                                fill=(10, 32, 56, 225))
            d.text((LEFT_X + 18, 96), "请到 xiaozhi.me 控制台添加设备，输入验证码",
                   font=font(17), fill=(200, 224, 244))
            code = (activation_code or "……").replace("", " ").strip()
            fc = font(30)
            cw = text_w(d, code, fc)
            bold_text(d, ((W - cw) / 2, 118), code, fc, C_ACT)
            if mcp_tool or imu_compact is not None:
                _mcp_footer(d, mcp_tool or "self.imu.read", imu_compact)
            else:
                _footer(d, "等待激活…", footer)
            return _save(img, path)

        # The captured JPEG replaces bubbles temporarily, preserving all of it.
        if photo_jpeg is not None:
            top, bottom = STRIP_H + 1, H - MCP_FOOTER_H
            d.rectangle([0, top, W, H], fill=(0, 0, 0, 255))
            with Image.open(io.BytesIO(photo_jpeg)) as source:
                photo = source.convert("RGB")
            photo.thumbnail((W, bottom - top))
            img.paste(photo, ((W - photo.width) // 2,
                              top + (bottom - top - photo.height) // 2))
            if mcp_tool or imu_compact is not None:
                _mcp_footer(d, mcp_tool or "self.imu.read", imu_compact)
            return _save(img, path)

        if imu_sample is not None:
            _imu_panel(d, imu_sample)
            _mcp_footer(d, mcp_tool or "self.imu.read")
            return _save(img, path)

        # ---------------- 聊天气泡 ----------------
        fb = font(FONT_BUBBLE)
        maxw = BUBBLE_MAXW - BUBBLE_PAD_X * 2
        laid = []
        total = 0
        for role, text in list(bubbles)[-MAX_BUBBLES:]:
            lines = wrap(text, fb, maxw, d)
            bh = len(lines) * LINE_H + BUBBLE_PAD_Y * 2
            laid.append((role, lines, bh))
            total += bh + BUBBLE_GAP
        if not laid:
            if detail:
                laid = [("ai", wrap(detail, fb, maxw, d), LINE_H + BUBBLE_PAD_Y * 2)]
                total = laid[0][2]

        y = BUBBLE_TOP
        limit = H - (MCP_FOOTER_H + 8 if mcp_tool or imu_compact is not None
                     else (34 if footer else 8))
        for role, lines, bh in laid:
            if y + bh > limit:
                break
            wmax = max(text_w(d, ln, fb) for ln in lines)
            bw = min(max(wmax + BUBBLE_PAD_X * 2, BUBBLE_MINW), BUBBLE_MAXW)
            if role == "user":
                x1 = RIGHT_X
                x0 = x1 - bw
                fill, tcol = C_BUBBLE_USER, C_BUBBLE_USER_TX
            elif role == "sys":
                x0 = LEFT_X
                x1 = x0 + bw
                fill, tcol = C_BUBBLE_SYS, C_BUBBLE_SYS_TX
            else:
                x0 = LEFT_X
                x1 = x0 + bw
                fill, tcol = C_BUBBLE_AI, C_BUBBLE_AI_TX
            _blit_bubble(img, d, x0, y, x1, y + bh, fill + (240,))
            ty = y + BUBBLE_PAD_Y
            for ln in lines:
                d.text((x0 + BUBBLE_PAD_X, ty), ln, font=fb, fill=tcol)
                ty += LINE_H
            y += bh + BUBBLE_GAP

        if mcp_tool or imu_compact is not None:
            _mcp_footer(d, mcp_tool or "self.imu.read", imu_compact)
        elif footer:
            _footer(d, "说「%s」或长按左上角2秒唤醒 · 点 X 关闭" % wake_word, True)

    return _save(img, path)


def _imu_number(value, spec="+.2f"):
    """Missing/nonfinite sensor readings must never look like measured zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--"
    return format(value, spec) if math.isfinite(value) else "--"


def _imu_panel(d, sample):
    """Draw a snapshot only; acquisition and its display lifetime belong to UI state."""
    d.rounded_rectangle([16, 70, W - 16, 426], radius=14,
                        fill=C_STRIP_BG + (255,), outline=C_STRIP_LINE + (255,))
    d.text((32, 80), "IMU 六轴数据", font=font(24), fill=C_STATE)
    model = wrap(str(sample.get("model") or "型号未知"), font(18), 250, d)[0]
    d.text((350, 86), model, font=font(18), fill=C_CLOCK)
    for left, label, key in ((32, "加速度  m/s²", "acceleration_m_s2"),
                             (336, "角速度  °/s", "angular_velocity_deg_s")):
        d.text((left, 130), label, font=font(20), fill=C_BUBBLE_AI)
        axes = sample.get(key) or {}
        for row, axis in enumerate(("x", "y", "z")):
            y = 168 + row * 57
            d.text((left, y + 9), axis.upper(), font=font(20), fill=C_HINT)
            value = _imu_number(axes.get(axis))
            f = font(34)
            if text_w(d, value, f) > 215:
                value = _imu_number(axes.get(axis), "+.2e")
                f = font(28)
            d.text((left + 34, y), value, font=f, fill=C_CLOCK)
    d.line([(32, 344), (608, 344)], fill=C_STRIP_LINE, width=1)
    temperature = _imu_number(sample.get("temperature_c"), ".1f")
    temp_text = "温度未提供" if temperature == "--" else "温度 %s °C" % temperature
    calibrated = sample.get("gyro_calibrated")
    calibration = ("陀螺仪已校准" if calibrated is True else
                   "陀螺仪未校准" if calibrated is False else "陀螺仪校准状态未知")
    d.text((32, 355), temp_text, font=font(18), fill=C_HINT)
    d.text((336, 355), calibration, font=font(18), fill=C_CLOCK if calibrated is True else C_ACT)
    timestamp = sample.get("timestamp_unix")
    stamp = "--"
    if _imu_number(timestamp) != "--":
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
        except (ValueError, OverflowError, OSError):
            pass
    d.text((32, 389), "采样时间  " + stamp, font=font(16), fill=C_HINT)


def _mcp_footer(d, name, imu_compact=None):
    d.rectangle([0, H - MCP_FOOTER_H, W, H], fill=C_STRIP_BG + (255,))
    d.line([(0, H - MCP_FOOTER_H), (W, H - MCP_FOOTER_H)],
           fill=C_STRIP_LINE + (255,), width=1)
    text = "MCP: " + name
    for size in range(14 if imu_compact is not None else 16, 9, -1):
        f = font(size)
        if text_w(d, text, f) <= W - 24:
            break
    if imu_compact is None:
        d.text((12, H - 32), text, font=f, fill=C_STATE)
        return
    while text_w(d, text, f) > W - 24:
        text = text[:-2] + "…"
    d.text((12, H - 41), text, font=f, fill=C_STATE)
    groups = []
    for label, key, unit in (("A", "acceleration_m_s2", "m/s²"),
                              ("G", "angular_velocity_deg_s", "°/s")):
        axes = imu_compact.get(key) or {}
        values = []
        for axis in ("x", "y", "z"):
            value = _imu_number(axes.get(axis))
            if len(value) > 8:
                value = _imu_number(axes.get(axis), "+.2e")
            values.append(value)
        groups.append("%s %s %s" % (label, "/".join(values), unit))
    text = "  ".join(groups)
    for size in range(15, 9, -1):
        f = font(size)
        if text_w(d, text, f) <= W - 24:
            break
    d.text((12, H - 22), text, font=f, fill=C_CLOCK)


def _footer(d, text, enabled):
    if not enabled:
        return
    f = font(15)
    tw = text_w(d, text, f)
    d.text(((W - tw) / 2, H - 26), text, font=f, fill=C_HINT)


def _save(img, path):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fp:
        fp.write(struct.pack("<III", 1, W, H))
        fp.write(img.tobytes())
    os.replace(tmp, path)
    return img


def render_status_icon(path, kind="idle"):
    """顶栏小图标（38x38）：idle 白 / conn 绿 / off 灰"""
    col = {"idle": (255, 255, 255), "conn": (120, 235, 150),
           "off": (110, 118, 132)}.get(kind, (255, 255, 255))
    img = Image.new("RGBA", (38, 38), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    draw_robot(d, 19, 19, 17, col + (255,))
    tmp = path + ".tmp"
    with open(tmp, "wb") as fp:
        fp.write(struct.pack("<III", 1, 38, 38))
        fp.write(img.tobytes())
    os.replace(tmp, path)


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/popup_preview.png"
    st = sys.argv[2] if len(sys.argv) > 2 else "listening"
    render("/tmp/_prev.rgba", state=st, detail="聆听中",
           bubbles=[("ai", "哈喽～我在的，今天过得怎么样呀？")],
           emotion="happy", wake_word="你好小智")
    with open("/tmp/_prev.rgba", "rb") as f:
        f.read(12)
        im = Image.frombytes("RGBA", (W, H), f.read(W * H * 4))
    im.save(out)
    print("saved", out)
