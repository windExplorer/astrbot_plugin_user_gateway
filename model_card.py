"""把「可选模型」渲染成一张卡片图（Pillow）。

为什么单独一个模块：绘图代码量不小、且与插件逻辑无关，混进 ``main.py`` 只会让
闸门那部分更难读；单独成文件也便于脱离 AstrBot 直接跑自检（``tests/test_model_switch.py``）。

三条硬约束（改动时保持）：

1. **画不出来绝不能影响功能**：本模块对外只暴露「返回 PNG bytes 或 None」，
   字体找不到 / 图片太大 / Pillow 缺失都返回 ``None``，由调用方退化成纯文本列表。
   权限插件不能因为一张图渲染失败就把用户的指令吃掉。
2. **只依赖 Pillow**：它是 AstrBot 核心依赖（``requirements.txt`` 里的 ``pillow>=11.2.1``），
   随 AstrBot 一起装好，所以这里不新增任何第三方依赖。
3. **字体优先级**：调用方指定（配置项）→ 插件自带 ``assets/fonts`` → 系统常见中文字体。
   全都找不到时**返回 None**——中文渲染成豆腐块比不发图更糟。

版面（v1.3.5 起）：

    ┌──── 渐变头部：标题 + 分组 ────────────┬─ 头像 ─┐
    ├──── 信息条：当前使用 / 今日用量 ──────────────┤
    ├──── 候选列表（序号 + 模型名 + 标签）──────────┤
    └──── 底部提示 ─────────────────────────────┘

「当前使用」单独占一行、**允许折行（最多两行）**：默认模型名（``供应商 · 模型``）动辄二三十个
字符，塞在头部副标题里必然被截断——而这一行恰恰是用户最需要看全的。
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Iterable, Optional

try:  # Pillow 随 AstrBot 安装；缺失时整个模块退化为「不渲染」
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - 仅在没有 Pillow 的环境
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore

ASSETS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

# 系统字体候选（按「中文覆盖好 + 常见」排序）。Windows / macOS / Linux 各来几份，
# 找不到就用插件自带字体；两者都没有才会走到「不渲染」。
SYSTEM_FONT_CANDIDATES: tuple[str, ...] = (
    # Windows
    "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux（Debian/Ubuntu/Arch 常见路径）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wenquanyi/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)

# ---------------------------------------------------------------------- 版面
WIDTH = 760
PAD = 26
RADIUS = 22
HEADER_H = 116
AVATAR = 72
INFO_PAD_Y = 13
INFO_LABEL_W = 104
ROW_H = 76
ROW_GAP = 12
FOOTER_H = 62
GAP = 14  # 各大块之间的间距

# ---------------------------------------------------------------------- 配色
# 三套主题，结构一致（改色只动这里）。默认 indigo：靛蓝 → 天蓝的头部渐变 + 近白底，
# 这是三套里最"稳"的一套（正文全是中性色，只有标签带一点彩色）。
#
# 为什么做成主题而不是写死：配色是最主观的一环，用户对「好看」的意见可能反复变化；
# 留一个配置项（`model_card_theme`）比每次改代码 + 发包便宜得多。
THEMES: dict[str, dict[str, tuple]] = {
    "indigo": {
        "header_from": (124, 92, 255),
        "header_to": (72, 140, 255),
        "text_on_header": (255, 255, 255),
        "text_on_header_soft": (226, 232, 255),
        "info_bg": (247, 248, 252),
        "info_line": (233, 236, 246),
        "info_label": (138, 145, 168),
        "info_value": (40, 46, 64),
        "row_bg": (251, 252, 255),
        "row_current_bg": (240, 250, 245),
        "row_border": (234, 238, 248),
        "row_border_current": (198, 235, 213),
        "badge_bg": (237, 240, 255),
        "badge_text": (84, 90, 214),
        "text_dark": (44, 50, 68),
        "text_muted": (140, 147, 168),
        "accent_current": (16, 165, 110),
        "accent_own": (232, 146, 16),
        "accent_warn": (146, 154, 174),
        "outline": (232, 236, 246),
    },
    "mint": {
        "header_from": (13, 148, 136),
        "header_to": (52, 199, 123),
        "text_on_header": (255, 255, 255),
        "text_on_header_soft": (213, 245, 232),
        "info_bg": (246, 251, 249),
        "info_line": (226, 240, 235),
        "info_label": (128, 152, 145),
        "info_value": (38, 58, 52),
        "row_bg": (250, 253, 252),
        "row_current_bg": (237, 249, 243),
        "row_border": (228, 240, 236),
        "row_border_current": (186, 226, 206),
        "badge_bg": (228, 246, 241),
        "badge_text": (12, 122, 106),
        "text_dark": (40, 54, 50),
        "text_muted": (134, 156, 150),
        "accent_current": (16, 150, 118),
        "accent_own": (232, 146, 16),
        "accent_warn": (144, 158, 154),
        "outline": (226, 239, 235),
    },
    "sunset": {
        "header_from": (255, 131, 100),
        "header_to": (255, 176, 72),
        "text_on_header": (255, 255, 255),
        "text_on_header_soft": (255, 235, 220),
        "info_bg": (253, 249, 246),
        "info_line": (245, 233, 226),
        "info_label": (162, 138, 124),
        "info_value": (62, 46, 40),
        "row_bg": (254, 252, 251),
        "row_current_bg": (247, 250, 243),
        "row_border": (243, 234, 228),
        "row_border_current": (205, 231, 199),
        "badge_bg": (255, 238, 230),
        "badge_text": (214, 92, 54),
        "text_dark": (58, 46, 42),
        "text_muted": (162, 144, 134),
        "accent_current": (16, 165, 110),
        "accent_own": (217, 119, 6),
        "accent_warn": (154, 148, 144),
        "outline": (243, 234, 228),
    },
}
DEFAULT_THEME = "indigo"

CARD_BG = (255, 255, 255)  # 卡片底色三套主题共用


def theme_colors(name: str = "") -> dict[str, tuple]:
    """取主题配色；名字不认识时回落到默认主题（配置写错不该导致卡片画不出来）。"""
    return THEMES.get(str(name or "").strip().lower() or DEFAULT_THEME) or THEMES[DEFAULT_THEME]

# ---------------------------------------------------------------------- 字号
F_TITLE = 36
F_SUBTITLE = 22
F_INFO_LABEL = 22
F_INFO_VALUE = 26
F_INDEX = 26
F_LABEL = 27
F_NOTE = 21
F_TAG = 20
F_FOOTER = 22


def find_font_path(prefer: str = "") -> Optional[Path]:
    """按「配置指定 → 插件自带 → 系统候选」找一份可用的字体文件；都没有返回 None。"""
    want = str(prefer or "").strip()
    if want:
        p = Path(want)
        if p.is_file():
            return p
    bundled = sorted(ASSETS_DIR.glob("*.woff2")) + sorted(ASSETS_DIR.glob("*.ttf")) + sorted(
        ASSETS_DIR.glob("*.otf")
    )
    for p in bundled:
        if p.is_file():
            return p
    for raw in SYSTEM_FONT_CANDIDATES:
        p = Path(raw)
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


class _Fonts:
    """字体缓存（同一次渲染里每个字号只加载一次）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[int, Any] = {}

    def get(self, size: int) -> Any:
        font = self._cache.get(size)
        if font is None:
            font = ImageFont.truetype(str(self.path), size)
            self._cache[size] = font
        return font


def _fit(text: str, font: Any, limit: float, tail: str = "…") -> str:
    """按像素宽度截断文本（中文逐字宽度不同，不能按字符数截）。"""
    text = str(text or "")
    if limit <= 0:
        return ""
    if font.getlength(text) <= limit:
        return text
    while text and font.getlength(text + tail) > limit:
        text = text[:-1]
    return (text + tail) if text else ""


# 适合断行的字符：模型名基本是「供应商 · 模型-版本」这种结构，在这些地方断开比
# 从单词中间劈开（``preview-very-lo / ng-name``）好看得多。
_BREAK_CHARS = " ·-/_,|:：，、"


def _last_break(text: str) -> int:
    """找一个适合断行的位置，返回**断点后的下标**（0 = 没找到，只能硬断）。

    要求断点至少落在行的 35% 之后，避免第一行只剩一两个字。
    """
    n = len(text)
    if n < 2:
        return 0
    floor = max(1, int(n * 0.35))
    for i in range(n - 1, -1, -1):
        if text[i] in _BREAK_CHARS and i >= floor:
            return i + 1
    return 0


def _wrap(text: str, font: Any, limit: float, max_lines: int = 2, tail: str = "…") -> list[str]:
    """按像素宽度折行，最多 ``max_lines`` 行（最后一行超出的部分用省略号收尾）。

    逐字符累加（中文没有词边界），但**优先在分隔符处断开**；``limit<=0`` 时返回空列表。
    """
    text = " ".join(str(text or "").split())  # 折叠空白，避免出现空行
    if not text or limit <= 0 or max_lines <= 0:
        return []
    lines: list[str] = []
    cur = ""
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if font.getlength(cur + ch) <= limit:
            cur += ch
            idx += 1
            continue
        cut = _last_break(cur)
        if cut:  # 在分隔符处断行（不消费 ch，它继续参与下一行）
            lines.append(cur[:cut])
            cur = cur[cut:]
        else:  # 没有可断点 → 硬断
            lines.append(cur)
            cur = ""
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines:
        lines.append(cur)
        return lines
    rest = cur + text[idx:]
    if rest:
        lines[-1] = _fit(lines[-1] + rest, font, limit, tail=tail)
    return lines


def _gradient(width: int, height: int, top: tuple, bottom: tuple) -> Any:
    """竖向渐变（先画 1×h 再拉伸，比逐行画快得多）。"""
    strip = Image.new("RGB", (1, max(1, height)))
    px = strip.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
    return strip.resize((width, max(1, height)), Image.BILINEAR)


def _circle_avatar(data: bytes, size: int) -> Optional[Any]:
    """把头像字节裁剪成圆形小图（失败返回 None，调用方画个占位圆）。"""
    try:
        src = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    try:
        side = min(src.size)
        left = (src.width - side) // 2
        top = (src.height - side) // 2
        src = src.crop((left, top, left + side, top + side)).resize(
            (size, size), Image.LANCZOS
        )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(src, (0, 0), mask)
        return out
    except Exception:
        return None


def _tag(draw: Any, fonts: _Fonts, text: str, right: int, cy: int, color: tuple) -> int:
    """在 ``right`` 处右对齐画一个标签，返回它的左边界（供下一个标签接着排）。"""
    font = fonts.get(F_TAG)
    w = int(font.getlength(text)) + 22
    h = 30
    x0 = right - w
    draw.rounded_rectangle([x0, cy - h // 2, right, cy + h // 2], h // 2, fill=color + (255,))
    draw.text((x0 + 11, cy), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
    return x0


def _info_block_height(fonts: _Fonts, info: list[dict[str, Any]], width: int) -> tuple[int, list[list[str]]]:
    """算出信息条的高度与每行的折行结果（先算后画，避免两遍逻辑不一致）。"""
    value_font = fonts.get(F_INFO_VALUE)
    # 值从「左内边距 + 标签宽」处开始，右侧留同样的内边距
    limit = width - (PAD + 18 + INFO_LABEL_W) - (PAD + 18)
    line_h = int(value_font.size * 1.34)
    wrapped: list[list[str]] = []
    height = 0
    for item in info:
        lines = _wrap(str(item.get("value") or ""), value_font, limit, max_lines=2) or [""]
        wrapped.append(lines)
        height += INFO_PAD_Y * 2 + len(lines) * line_h
    return height, wrapped


def render_model_card(
    *,
    title: str,
    subtitle: str,
    rows: Iterable[dict[str, Any]],
    footer: str = "",
    info: Optional[Iterable[dict[str, Any]]] = None,
    avatar: Optional[bytes] = None,
    font_path: str = "",
    theme: str = DEFAULT_THEME,
    width: int = WIDTH,
) -> Optional[bytes]:
    """渲染「模型选择」卡片，返回 PNG 字节；任何失败都返回 ``None``。

    Args:
        title: 卡片标题（如「模型切换」）。
        subtitle: 标题下方一句（如「分组：VIP（好友等级）」）。
        rows: 每行 ``{index, label, note, current, own, unavailable}``：
            序号 / 主文案（模型名）/ 次文案 / 是否当前使用 / 是否专属模型 / 是否暂不可用。
        footer: 底部提示（如「回复序号切换 · 60 秒内有效 · 0 = 恢复默认」）。
        info: 头部下方信息条，``[{label, value}, ...]``（如「当前使用 / 今日用量」）；
            ``value`` 会自动折行（最多两行），所以长模型名不会被截掉。
        avatar: 头像字节（可选；取不到时画占位圆）。
        font_path: 指定的字体文件路径（配置项）；为空则自动找。
        theme: 配色主题名（``indigo`` / ``mint`` / ``sunset``）；不认识则用默认主题。

    Returns:
        PNG bytes，或 ``None``（调用方应退化成纯文本）。
    """
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    try:
        c = theme_colors(theme)
        path = find_font_path(font_path)
        if path is None:
            return None
        fonts = _Fonts(path)

        items = [dict(r) for r in rows]
        if not items:
            return None
        info_items = [dict(i) for i in (info or []) if str(i.get("value") or "").strip()]
        info_h, info_lines = _info_block_height(fonts, info_items, width)

        body_h = len(items) * (ROW_H + ROW_GAP) - ROW_GAP
        footer_h = FOOTER_H if footer else 0
        height = (
            HEADER_H
            + (GAP + info_h if info_items else 0)
            + GAP
            + body_h
            + (GAP + footer_h if footer_h else 0)
            + PAD
        )

        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle([0, 0, width - 1, height - 1], RADIUS, fill=CARD_BG + (255,))

        # ---- 头部：渐变 + 标题 + 分组（+ 头像） ----
        head = _gradient(width, HEADER_H + RADIUS, c["header_from"], c["header_to"]).convert("RGBA")
        mask = Image.new("L", (width, HEADER_H + RADIUS), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, HEADER_H + RADIUS - 1], RADIUS, fill=255
        )
        img.paste(head, (0, 0), mask)

        text_limit = width - PAD * 2 - (AVATAR + 18 if avatar else 0)
        draw.text((PAD, 44), _fit(title, fonts.get(F_TITLE), text_limit),
                  font=fonts.get(F_TITLE), fill=c["text_on_header"], anchor="lm")
        draw.text((PAD, 84), _fit(subtitle, fonts.get(F_SUBTITLE), text_limit),
                  font=fonts.get(F_SUBTITLE), fill=c["text_on_header_soft"], anchor="lm")
        if avatar:
            circ = _circle_avatar(avatar, AVATAR)
            if circ is not None:
                img.alpha_composite(circ, (width - PAD - AVATAR, (HEADER_H - AVATAR) // 2))

        y = HEADER_H

        # ---- 信息条：当前使用 / 今日用量 ----
        if info_items:
            y += GAP
            draw.rounded_rectangle(
                [PAD, y, width - PAD, y + info_h], 16, fill=c["info_bg"] + (255,)
            )
            label_font = fonts.get(F_INFO_LABEL)
            value_font = fonts.get(F_INFO_VALUE)
            line_h = int(value_font.size * 1.34)
            row_y = y
            for idx, (item, lines) in enumerate(zip(info_items, info_lines)):
                if idx:  # 行间细分隔线（只在内侧，不贴边）
                    draw.line(
                        [PAD + 18, row_y, width - PAD - 18, row_y],
                        fill=c["info_line"] + (255,),
                        width=1,
                    )
                block_h = INFO_PAD_Y * 2 + len(lines) * line_h
                cy = row_y + block_h // 2
                draw.text((PAD + 18, cy), str(item.get("label") or ""),
                          font=label_font, fill=c["info_label"] + (255,), anchor="lm")
                ty = cy - (len(lines) * line_h) // 2 + line_h // 2
                for line in lines:
                    draw.text((PAD + 18 + INFO_LABEL_W, ty), line,
                              font=value_font, fill=c["info_value"] + (255,), anchor="lm")
                    ty += line_h
                row_y += block_h
            y += info_h

        # ---- 候选列表 ----
        y += GAP
        badge_font = fonts.get(F_INDEX)
        label_font = fonts.get(F_LABEL)
        note_font = fonts.get(F_NOTE)
        for i, row in enumerate(items):
            is_current = bool(row.get("current"))
            bg = c["row_current_bg"] if is_current else c["row_bg"]
            border = c["row_border_current"] if is_current else c["row_border"]
            draw.rounded_rectangle(
                [PAD, y, width - PAD, y + ROW_H], 18,
                fill=bg + (255,), outline=border + (255,), width=1,
            )
            cy = y + ROW_H // 2

            # 序号角标
            bx = PAD + 18
            draw.ellipse([bx, cy - 22, bx + 44, cy + 22], fill=c["badge_bg"] + (255,))
            draw.text((bx + 22, cy), str(row.get("index", i + 1)),
                      font=badge_font, fill=c["badge_text"] + (255,), anchor="mm")

            # 右侧标签（从右往左排）：「当前使用」永远在最右，最显眼
            tags: list[tuple[str, tuple]] = []
            if is_current:
                tags.append(("当前使用", c["accent_current"]))
            if row.get("own"):
                tags.append(("专属模型", c["accent_own"]))
            if row.get("unavailable"):
                tags.append(("暂不可用", c["accent_warn"]))
            right = width - PAD - 18
            for text, color in tags:
                right = _tag(draw, fonts, text, right, cy, color) - 8

            label_x = bx + 60
            limit = right - label_x - 12
            note = str(row.get("note") or "")
            label = _fit(str(row.get("label") or ""), label_font, limit)
            if note:
                draw.text((label_x, cy - 15), label, font=label_font,
                          fill=c["text_dark"] + (255,), anchor="lm")
                draw.text((label_x, cy + 15), _fit(note, note_font, limit),
                          font=note_font, fill=c["text_muted"] + (255,), anchor="lm")
            else:
                draw.text((label_x, cy), label, font=label_font,
                          fill=c["text_dark"] + (255,), anchor="lm")
            y += ROW_H + ROW_GAP

        # ---- 底部提示 ----
        if footer:
            fy = y - ROW_GAP + GAP
            draw.line([PAD, fy, width - PAD, fy], fill=c["outline"] + (255,), width=1)
            draw.text(
                (PAD, fy + FOOTER_H // 2),
                _fit(footer, fonts.get(F_FOOTER), width - PAD * 2),
                font=fonts.get(F_FOOTER),
                fill=c["text_muted"] + (255,),
                anchor="lm",
            )

        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        # 画图失败绝不能影响功能（调用方会退化成文本）
        return None


def assets_info() -> dict[str, Any]:
    """自带字体资源概况（控制台「运行信息」展示用；没有也不影响功能）。"""
    try:
        files = [p.name for p in sorted(ASSETS_DIR.glob("*")) if p.is_file()]
    except Exception:
        files = []
    path = find_font_path()
    return {
        "dir": str(ASSETS_DIR),
        "files": files,
        "resolved": os.path.basename(str(path)) if path else "",
    }
