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

设计取向（v1.3.6 重画，改动前先读）：**浅色卡片 + 单一强调色**。

    ┌──────────────────────────────────────────────┐
    │ 模型切换                                  ╭──╮│   ← 浅色头部：小标签 + 当前模型（主角，
    │ OpenAI · gpt-4o-2024-11-20-preview        │头││      最多两行，不截断）+ 一行元信息
    │ VIP（好友等级）· 今日 12.3K tokens · 8 次   ╰──╯│
    ├──────────────────────────────────────────────┤
    │  1  OpenAI · gpt-4o              ╭ 当前使用 ╮ │   ← 候选列表：无框线，靠留白分组；
    │  2  Claude · claude-3-7-sonnet   ╰ 专属模型 ╯ │      当前那行淡底 + 左侧色条
    │  3  本地 · qwen3-32b              暂不可用    │
    ├──────────────────────────────────────────────┤
    │ 回复序号切换 · 60 秒内有效 · 0 = 恢复默认      │
    └──────────────────────────────────────────────┘

前两版被吐槽「丑」，教训记在这里：

- **别用大面积高饱和渐变**：视觉上像 PPT 封面，和聊天里的其它内容完全不搭；
  现在头部只是**淡淡一层强调色**（几乎白），靠字号与字重拉开层次。
- **别堆彩色胶囊**：一行三个填充色块就是「廉价」。现在标签是**描边胶囊**，
  只有当前使用的那个用实心强调色。
- **当前模型是主角**：它放在标题区、字号最大、可以折行；分组与今日用量合并成一行灰字，
  不另开信息条（信息条那版又被吐槽「变蠢了」——一屏里三块结构反而没有重点）。
- **颜色只做点缀**：正文/次要文字全是中性灰，强调色只出现在小标签、当前行的色条、
  序号和底部那行的小点上。想换观感只改 ``THEMES`` 里的一个色值。
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
PAD = 30
RADIUS = 24
AVATAR = 64
ROW_H = 64
ROW_GAP = 10
TAG_H = 28
FOOTER_H = 54

# ---------------------------------------------------------------------- 中性色
CARD_BG = (255, 255, 255)
CARD_BORDER = (233, 235, 243)
DIVIDER = (239, 241, 247)
TEXT_DARK = (28, 31, 42)  # 模型名：接近纯黑，保证「主角」的分量
TEXT_BODY = (55, 60, 76)
TEXT_MUTED = (126, 133, 152)
TEXT_SOFT = (164, 170, 186)

# ---------------------------------------------------------------------- 主题
# 只换「强调色」一个值（外加它的浅底），其余全是中性灰 —— 这是上一版最loud的教训：
# 主题一旦连头部渐变、行底色、标签色一起换，就很容易配出廉价感。
THEMES: dict[str, dict[str, tuple]] = {
    "indigo": {"accent": (79, 70, 229), "soft": (242, 243, 255), "tint": (249, 250, 255)},
    "teal": {"accent": (13, 128, 118), "soft": (235, 247, 245), "tint": (248, 252, 251)},
    "amber": {"accent": (176, 86, 12), "soft": (253, 244, 233), "tint": (254, 251, 246)},
    "rose": {"accent": (190, 24, 93), "soft": (253, 240, 246), "tint": (255, 250, 252)},
}
DEFAULT_THEME = "indigo"

# 与主题无关的语义色（标签用）
COLOR_OWN = (180, 118, 12)  # 专属模型（琥珀）
COLOR_WARN = (150, 156, 172)  # 暂不可用（灰）

# ---------------------------------------------------------------------- 字号
F_LABEL = 18  # 顶部小标签「模型切换」
F_CURRENT = 31  # 当前模型（主角）
F_META = 19  # 分组 / 今日用量
F_INDEX = 23
F_NAME = 25
F_NOTE = 19
F_TAG = 17
F_FOOTER = 19

# 断行优先在这些字符处断开（模型名基本是「供应商 · 模型-版本」结构）
_BREAK_CHARS = " ·-/_,|:：，、"


def theme_colors(name: str = "") -> dict[str, tuple]:
    """取主题配色；名字不认识时回落到默认主题（配置写错不该导致卡片画不出来）。"""
    return THEMES.get(str(name or "").strip().lower() or DEFAULT_THEME) or THEMES[DEFAULT_THEME]


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


def _circle(data: bytes, size: int, ring: int = 0, ring_color: tuple = CARD_BG) -> Optional[Any]:
    """把头像字节裁成圆形（可带一圈描边）；失败返回 None（调用方不画头像）。"""
    try:
        src = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    try:
        side = min(src.size)
        left = (src.width - side) // 2
        top = (src.height - side) // 2
        src = src.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
        core = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        core.paste(src, (0, 0), mask)
        if ring <= 0:
            return core
        total = size + ring * 2
        out = Image.new("RGBA", (total, total), (0, 0, 0, 0))
        ImageDraw.Draw(out).ellipse(
            [0, 0, total - 1, total - 1], fill=ring_color + (255,)
        )
        out.alpha_composite(core, (ring, ring))
        return out
    except Exception:
        return None


def _pill(
    draw: Any, fonts: _Fonts, text: str, right: int, cy: int, color: tuple, filled: bool
) -> int:
    """右对齐画一个小胶囊标签，返回它的左边界（供下一个标签接着排）。

    ``filled=False`` 时是**描边胶囊**（透明底 + 彩色边与字）—— 一行里并排两三个也不会显得吵。
    """
    font = fonts.get(F_TAG)
    w = int(font.getlength(text)) + 24
    x0 = right - w
    box = [x0, cy - TAG_H // 2, right, cy + TAG_H // 2]
    if filled:
        draw.rounded_rectangle(box, TAG_H // 2, fill=color + (255,))
        draw.text((x0 + 12, cy), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
    else:
        draw.rounded_rectangle(box, TAG_H // 2, outline=color + (255,), width=1)
        draw.text((x0 + 12, cy), text, font=font, fill=color + (255,), anchor="lm")
    return x0


def _header_lines(fonts: _Fonts, current: str, limit: float) -> list[str]:
    """当前模型那一行（最多两行）。空字符串 → 空列表（调用方不画这一行）。"""
    text = " ".join(str(current or "").split())
    if not text:
        return []
    return _wrap(text, fonts.get(F_CURRENT), limit, max_lines=2) or []


def render_model_card(
    *,
    title: str,
    rows: Iterable[dict[str, Any]],
    current: str = "",
    meta: str = "",
    footer: str = "",
    avatar: Optional[bytes] = None,
    font_path: str = "",
    theme: str = DEFAULT_THEME,
    width: int = WIDTH,
) -> Optional[bytes]:
    """渲染「模型选择」卡片，返回 PNG 字节；任何失败都返回 ``None``。

    Args:
        title: 顶部小标签（如「模型切换」）。
        rows: 每行 ``{index, label, note, current, own, unavailable}``：
            序号 / 模型名 / 次文案 / 是否当前使用 / 是否专属模型 / 是否暂不可用。
        current: **当前使用的模型名**（标题区的主角，最多两行，不截断）；空则不画这一行。
        meta: 标题区下方一行灰字（如「VIP（好友等级）· 今日 12.3K tokens · 8 次对话」）。
        footer: 底部提示（如「回复序号切换 · 60 秒内有效 · 0 = 恢复默认」）。
        avatar: 头像字节（可选；取不到时留空位）。
        font_path: 指定的字体文件路径（配置项）；为空则自动找。
        theme: 主题名（``indigo`` / ``teal`` / ``amber`` / ``rose``）；不认识则用默认主题。

    Returns:
        PNG bytes，或 ``None``（调用方应退化成纯文本）。
    """
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    try:
        c = theme_colors(theme)
        accent, soft, tint = c["accent"], c["soft"], c["tint"]
        path = find_font_path(font_path)
        if path is None:
            return None
        fonts = _Fonts(path)

        items = [dict(r) for r in rows]
        if not items:
            return None

        # ---- 先算尺寸（头部高度取决于当前模型折了几行） ----
        head_text_limit = width - PAD * 2 - (AVATAR + 18 if avatar else 0)
        cur_lines = _header_lines(fonts, current, head_text_limit)
        head_h = 26 + 26  # 上边距 + 小标签
        if cur_lines:
            head_h += 10 + len(cur_lines) * 42
        if meta:
            head_h += (10 if cur_lines else 4) + 26
        head_h += 24  # 下边距

        body_h = len(items) * ROW_H + (len(items) - 1) * ROW_GAP
        height = head_h + 22 + body_h + (22 + FOOTER_H if footer else 0) + PAD

        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [0, 0, width - 1, height - 1], RADIUS, fill=CARD_BG + (255,)
        )

        # ---- 头部：一层极淡的主题色 + 小标签 + 当前模型（主角）+ 元信息 + 头像 ----
        head_mask = Image.new("L", (width, head_h + RADIUS), 0)
        ImageDraw.Draw(head_mask).rounded_rectangle(
            [0, 0, width - 1, head_h + RADIUS - 1], RADIUS, fill=255
        )
        img.paste(Image.new("RGBA", (width, head_h + RADIUS), tint + (255,)), (0, 0), head_mask)

        y = 26
        draw.text((PAD, y), str(title or ""), font=fonts.get(F_LABEL),
                  fill=TEXT_MUTED + (255,), anchor="la")
        y += 26
        for line in cur_lines:
            draw.text((PAD, y), line, font=fonts.get(F_CURRENT),
                      fill=TEXT_DARK + (255,), anchor="la")
            y += 42
        if meta:
            y += 10 if cur_lines else 4
            draw.text((PAD, y), _fit(meta, fonts.get(F_META), width - PAD * 2),
                      font=fonts.get(F_META), fill=TEXT_MUTED + (255,), anchor="la")
        if avatar:
            circ = _circle(avatar, AVATAR, ring=3, ring_color=CARD_BG)
            if circ is not None:
                img.alpha_composite(circ, (width - PAD - AVATAR, (head_h - AVATAR) // 2))

        # 头部与列表之间的分隔线（头部是淡色块，需要一条线收口）
        draw.line([0, head_h, width - 1, head_h], fill=DIVIDER + (255,), width=1)

        # ---- 候选列表：无框线，靠留白分组；当前那行淡底 + 左侧色条 ----
        y = head_h + 22
        name_font = fonts.get(F_NAME)
        note_font = fonts.get(F_NOTE)
        idx_font = fonts.get(F_INDEX)
        for i, row in enumerate(items):
            is_current = bool(row.get("current"))
            if is_current:
                draw.rounded_rectangle(
                    [PAD - 12, y, width - PAD + 12, y + ROW_H], 14, fill=soft + (255,)
                )
                draw.rounded_rectangle(
                    [PAD - 12, y + 12, PAD - 8, y + ROW_H - 12], 2, fill=accent + (255,)
                )
            cy = y + ROW_H // 2

            # 序号（强调色小数字，不用色块，避免"糖果"感）
            draw.text((PAD + 8, cy), str(row.get("index", i + 1)), font=idx_font,
                      fill=(accent if is_current else TEXT_SOFT) + (255,), anchor="lm")

            # 右侧标签（从右往左排）：「当前使用」实心，其余描边
            tags: list[tuple[str, tuple, bool]] = []
            if is_current:
                tags.append(("当前使用", accent, True))
            if row.get("own"):
                tags.append(("专属模型", COLOR_OWN, False))
            if row.get("unavailable"):
                tags.append(("暂不可用", COLOR_WARN, False))
            right = width - PAD - 6
            for text, color, filled in tags:
                right = _pill(draw, fonts, text, right, cy, color, filled) - 8

            label_x = PAD + 44
            limit = right - label_x - 14
            note = str(row.get("note") or "")
            label = _fit(str(row.get("label") or ""), name_font, limit)
            if note:
                draw.text((label_x, cy - 13), label, font=name_font,
                          fill=TEXT_DARK + (255,), anchor="lm")
                draw.text((label_x, cy + 14), _fit(note, note_font, limit),
                          font=note_font, fill=TEXT_MUTED + (255,), anchor="lm")
            else:
                draw.text((label_x, cy), label, font=name_font,
                          fill=TEXT_BODY + (255,), anchor="lm")
            y += ROW_H + ROW_GAP

        # ---- 底部提示 ----
        if footer:
            fy = y - ROW_GAP + 22
            draw.line([PAD, fy, width - PAD, fy], fill=DIVIDER + (255,), width=1)
            draw.ellipse(
                [PAD, fy + FOOTER_H // 2 - 3, PAD + 6, fy + FOOTER_H // 2 + 3],
                fill=accent + (255,),
            )
            draw.text(
                (PAD + 16, fy + FOOTER_H // 2),
                _fit(footer, fonts.get(F_FOOTER), width - PAD * 2 - 16),
                font=fonts.get(F_FOOTER),
                fill=TEXT_MUTED + (255,),
                anchor="lm",
            )

        # 卡片描边（放在最后画，保证压在色块之上）
        draw.rounded_rectangle(
            [0, 0, width - 1, height - 1], RADIUS, outline=CARD_BORDER + (255,), width=1
        )

        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG", optimize=True)
        return out.getvalue()
    except Exception:
        # 画图失败绝不能影响功能（调用方会退化成纯文本）
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
