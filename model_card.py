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

版面（v1.3.8 重画，借 `astrbot_plugin_box` 的卡片风格）：**头 + 身体 + 脚**三段拼接。

    ╭────────────────────────────────────────────╮  ← 头：主题色**纵向渐变**
    │ 模型切换                                ╭──╮│     上两角圆角、**下沿是直的**
    │ OpenAI · gpt-4o                         │头││     当前模型（主角，最多两行）
    │ VIP（好友等级）· 今日 12.3K tokens · 8 次 ╰──╯│     头像挂白圈，垂直居中
    ├────────────────────────────────────────────┤  ← 直边对接（不带圆角）
    │ ╭────────────────────────────────────────╮ │  ← 身体：白底，候选行是浅灰圆角块
    │ │ ① OpenAI · gpt-4o          ⟨当前使用⟩ │ │
    │ │ ② Claude · claude-3-7                  │ │
    │ ╰────────────────────────────────────────╯ │
    ├────────────────────────────────────────────┤  ← 直边对接
    │ ● 回复序号切换 · 60 秒内有效 · 0 = 恢复默认 │  ← 脚：浅色底，下两角圆角
    ╰────────────────────────────────────────────╯

三条设计约束（改版面之前先读，都是被用户打过回的）：

- **三段之间必须是直边**：头的下沿、脚的上沿都是**直角**，只有最外侧那圈的上下四角
  是圆角（头看上两角、脚看下两角）。整卡一个圆角矩形再往里头贴色块，
  接缝处会出现"两段圆弧互啃"的缺口，很难看。
- **头部要有颜色**，别只是"淡到看不出来"的一层白：用户明确说过「太淡了」。
  纵深靠 ``top → bottom`` 的渐变拉开（借 box 的做法），文字用同色系深色保证对比度。
- **强调色只出现在少数几处**（序号块、当前行、当前标签、脚上的小圆点），
  其余全是中性灰 —— 一行塞三个彩色胶囊就会显得廉价。

实现上先拼「卡片本身」（不含阴影），再整体贴到画布上：
渐变头 / 渐变脚是**带 alpha 的图层 + 遮罩**贴进去的，最后整卡套一层全局圆角遮罩，
这样接缝天然是直的，外轮廓天然是圆的。
"""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any, Iterable, Optional

try:  # Pillow 随 AstrBot 安装；缺失时整个模块退化为「不渲染」
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except Exception:  # pragma: no cover - 仅在没有 Pillow 的环境
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFilter = None  # type: ignore
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
WIDTH = 880  # 卡片本体宽度（不含下面的阴影留白）
PAD = 40  # 卡片内左右边距
RADIUS = 30  # 外轮廓圆角（只有最外侧四角用它）
AVATAR = 120  # 头像直径（v1.3.9 从 92 调大：小头像在群里一眼认不出来）
AVATAR_RING = 9  # 头像外的白圈（叠在渐变头上，像 box 那张卡）
HEAD_PAD = 30  # 头部内容与头部上下沿的最小留白
ROW_H = 76
ROW_GAP = 12
ROW_RADIUS = 20
TAG_H = 32
FOOTER_H = 64
BADGE = 40  # 行首序号方块的边长
SHADOW_PAD = 26  # 卡片四周的阴影留白（画布会比卡片大这么多）

# ---------------------------------------------------------------------- 中性色
CARD_BG = (255, 255, 255)
ROW_BG = (247, 248, 252)  # 非当前选项的行底
BADGE_BG = (238, 241, 249)  # 非当前选项的序号块
TEXT_DARK = (30, 33, 44)  # 模型名：接近纯黑，保证「主角」的分量
TEXT_BODY = (56, 62, 78)
TEXT_MUTED = (126, 133, 152)
TEXT_SOFT = (167, 173, 189)

# ---------------------------------------------------------------------- 主题
# 每套主题给足一组色（头部渐变的两端、同色系深色文字、强调色、行底、脚底、描边）。
# 只换「强调色」是不够的 —— 头部必须真的有色，这是用户明确提过的（「太淡了」）。
THEMES: dict[str, dict[str, tuple]] = {
    "indigo": {
        "top": (232, 234, 255),
        "bottom": (160, 170, 248),
        "ink": (42, 38, 94),
        "sub": (78, 74, 138),
        "accent": (79, 70, 229),
        "soft": (238, 240, 255),
        "footer": (243, 244, 255),
        "border": (206, 210, 243),
    },
    "teal": {
        "top": (222, 246, 240),
        "bottom": (142, 214, 199),
        "ink": (18, 70, 64),
        "sub": (46, 110, 102),
        "accent": (13, 128, 118),
        "soft": (233, 247, 244),
        "footer": (238, 250, 247),
        "border": (192, 227, 218),
    },
    "amber": {
        "top": (255, 241, 217),
        "bottom": (249, 203, 132),
        "ink": (92, 58, 12),
        "sub": (140, 96, 30),
        "accent": (186, 96, 12),
        "soft": (253, 243, 229),
        "footer": (254, 247, 237),
        "border": (240, 213, 174),
    },
    "rose": {
        "top": (255, 229, 241),
        "bottom": (249, 164, 202),
        "ink": (108, 30, 64),
        "sub": (162, 70, 108),
        "accent": (193, 24, 93),
        "soft": (253, 238, 245),
        "footer": (255, 244, 249),
        "border": (243, 202, 222),
    },
}
DEFAULT_THEME = "indigo"

# 与主题无关的语义色（标签用）
COLOR_OWN = (178, 112, 10)  # 专属模型（琥珀）
COLOR_WARN = (150, 156, 172)  # 暂不可用（灰）

# ---------------------------------------------------------------------- 字号
F_LABEL = 20  # 顶部小标签「模型切换」
F_CURRENT = 38  # 当前模型（主角）
F_META = 21  # 分组 / 今日用量
F_INDEX = 24  # 行首序号
F_NAME = 29
F_NOTE = 20
F_TAG = 19
F_FOOTER = 20

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


def _mix(a: tuple, b: tuple, t: float) -> tuple:
    """两色线性插值（``t=0`` 取 a，``t=1`` 取 b）。"""
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


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
        ImageDraw.Draw(out).ellipse([0, 0, total - 1, total - 1], fill=ring_color + (255,))
        out.alpha_composite(core, (ring, ring))
        return out
    except Exception:
        return None


def _gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Any:
    """纵向渐变图层（``top`` → ``bottom``）。"""
    w, h = size
    layer = Image.new("RGBA", (w, max(1, h)))
    draw = ImageDraw.Draw(layer)
    for row in range(max(1, h)):
        t = row / max(1, h - 1)
        draw.line([(0, row), (w, row)], fill=_mix(top, bottom, t) + (255,))
    return layer


def _seg_mask(size: tuple[int, int], radius: int, *, round_top: bool) -> Any:
    """"头的下沿 / 脚的上沿必须是直角" —— 圆角矩形 + 一块方角补丁。

    ``round_top=True`` 时上两角圆、下沿直（头部用）；False 反之（脚部用）。
    """
    w, h = size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, w - 1, h - 1], radius, fill=255)
    if round_top:
        draw.rectangle([0, radius, w, h], fill=255)  # 抹掉下方的圆角
    else:
        draw.rectangle([0, 0, w, h - radius], fill=255)  # 抹掉上方的圆角
    return mask


def _card_mask(size: tuple[int, int], radius: int) -> Any:
    """整卡的外轮廓（四角都圆）。"""
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size[0] - 1, size[1] - 1], radius, fill=255)
    return mask


def _pill(draw: Any, fonts: _Fonts, text: str, right: int, cy: int, color: tuple, filled: bool) -> int:
    """右对齐画一个小胶囊标签，返回它的左边界（供下一个标签接着排）。

    ``filled=False`` 时是**描边胶囊**（透明底 + 彩色边与字）—— 一行里并排两三个也不会显得吵。
    """
    font = fonts.get(F_TAG)
    w = int(font.getlength(text)) + 28
    x0 = right - w
    box = [x0, cy - TAG_H // 2, right, cy + TAG_H // 2]
    if filled:
        draw.rounded_rectangle(box, TAG_H // 2, fill=color + (255,))
        draw.text((x0 + 14, cy), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
    else:
        draw.rounded_rectangle(box, TAG_H // 2, fill=CARD_BG + (255,), outline=color + (255,), width=2)
        draw.text((x0 + 14, cy), text, font=font, fill=color + (255,), anchor="lm")
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
        current: **当前使用的模型名**（头部的主角，最多两行，不截断）；空则不画这一行。
        meta: 头部下方一行小字（如「VIP（好友等级）· 今日 12.3K tokens · 8 次对话」）。
        footer: 底部提示（如「回复序号切换 · 60 秒内有效 · 0 = 恢复默认」）。
        avatar: 头像字节（可选；取不到时留空位）。
        font_path: 指定的字体文件路径（配置项）；为空则自动找。
        theme: 主题名（``indigo`` / ``teal`` / ``amber`` / ``rose``）；不认识则用默认主题。
        width: 卡片本体宽度（默认 :data:`WIDTH`）。

    Returns:
        PNG bytes，或 ``None``（调用方应退化成纯文本）。
    """
    if Image is None or ImageDraw is None or ImageFont is None or ImageFilter is None:
        return None
    try:
        c = theme_colors(theme)
        accent, soft = c["accent"], c["soft"]
        path = find_font_path(font_path)
        if path is None:
            return None
        fonts = _Fonts(path)

        items = [dict(r) for r in rows]
        if not items:
            return None

        # ---- 先算尺寸（头部高度取决于当前模型折了几行） ----
        avatar_gap = (AVATAR + AVATAR_RING * 2 + 22) if avatar else 0
        head_text_limit = width - PAD * 2 - avatar_gap
        cur_lines = _header_lines(fonts, current, head_text_limit)
        f_label, f_cur, f_meta = fonts.get(F_LABEL), fonts.get(F_CURRENT), fonts.get(F_META)
        label_h = sum(f_label.getmetrics())
        cur_lh = int(sum(f_cur.getmetrics()) * 1.14)
        meta_h = sum(f_meta.getmetrics())

        # 头部高度与下面「一段一段往下排」的增量严格一致（否则底部留白会被吃掉）；
        # 正文块与头像是**各自垂直居中**的：头像比文字高时，文字不会缩在顶上。
        content_h = label_h + len(cur_lines) * (14 + cur_lh) + (12 + meta_h if meta else 0)
        head_h = max(content_h + HEAD_PAD * 2, AVATAR + AVATAR_RING * 2 + 48)

        body_h = 26 + len(items) * ROW_H + (len(items) - 1) * ROW_GAP + 26
        card_h = head_h + body_h + (FOOTER_H if footer else 0)
        canvas_w = width + SHADOW_PAD * 2
        canvas_h = card_h + SHADOW_PAD * 2

        # ---- 1) 卡片本体：白底 → 渐变头 → 渐变脚 → 整体套圆角 ----
        card = Image.new("RGBA", (width, card_h), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(card)
        cdraw.rectangle([0, 0, width, card_h], fill=CARD_BG + (255,))
        card.paste(
            _gradient((width, head_h), c["top"], c["bottom"]), (0, 0),
            _seg_mask((width, head_h), RADIUS, round_top=True),
        )
        if footer:
            fy = card_h - FOOTER_H
            card.paste(
                Image.new("RGBA", (width, FOOTER_H), c["footer"] + (255,)), (0, fy),
                _seg_mask((width, FOOTER_H), RADIUS, round_top=False),
            )
        card.putalpha(_card_mask((width, card_h), RADIUS))

        # ---- 2) 贴到画布上，先铺一层柔和投影（聊天气泡里有一点纵深更好看） ----
        img = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).rounded_rectangle(
            [SHADOW_PAD, SHADOW_PAD + 10, SHADOW_PAD + width, SHADOW_PAD + card_h + 10],
            RADIUS, fill=_mix(c["ink"], (255, 255, 255), 0.25) + (78,),
        )
        img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(18)))
        ox, oy = SHADOW_PAD, SHADOW_PAD
        img.alpha_composite(card, (ox, oy))
        draw = ImageDraw.Draw(img)

        # ---- 3) 头部内容：小标签 + 当前模型（主角）+ 元信息 + 头像 ----
        y = oy + max(HEAD_PAD, (head_h - content_h) // 2)
        draw.text((ox + PAD, y), str(title or ""), font=f_label, fill=c["sub"] + (255,))
        y += label_h
        for line in cur_lines:
            y += 14
            draw.text((ox + PAD, y), line, font=f_cur, fill=c["ink"] + (255,))
            y += cur_lh
        if meta:
            y += 12
            draw.text(
                (ox + PAD, y),
                _fit(meta, f_meta, head_text_limit),
                font=f_meta, fill=c["sub"] + (255,),
            )
        if avatar:
            circ = _circle(avatar, AVATAR, ring=AVATAR_RING, ring_color=CARD_BG)
            if circ is not None:
                side = circ.width
                img.alpha_composite(
                    circ,
                    (ox + width - PAD - side, oy + (head_h - side) // 2),
                )

        # ---- 4) 候选列表：浅灰圆角行块 + 序号方块 + 右对齐标签 ----
        y = oy + head_h + 26
        name_font, note_font, idx_font = fonts.get(F_NAME), fonts.get(F_NOTE), fonts.get(F_INDEX)
        row_w = width - PAD * 2
        for i, row in enumerate(items):
            is_current = bool(row.get("current"))
            dead = bool(row.get("unavailable"))
            draw.rounded_rectangle(
                [ox + PAD, y, ox + PAD + row_w, y + ROW_H],
                ROW_RADIUS,
                fill=(soft if is_current else ROW_BG) + (255,),
            )
            cy = y + ROW_H // 2

            # 序号方块（当前用强调色实心，其余浅灰）—— 比一串裸数字好认得多
            bx = ox + PAD + 18
            badge_color = accent if is_current else BADGE_BG
            draw.rounded_rectangle(
                [bx, cy - BADGE // 2, bx + BADGE, cy + BADGE // 2], 12, fill=badge_color + (255,)
            )
            draw.text(
                (bx + BADGE // 2, cy),
                str(row.get("index", i + 1)),
                font=idx_font,
                fill=((255, 255, 255) if is_current else TEXT_BODY) + (255,),
                anchor="mm",
            )

            # 右侧标签（从右往左排）：「当前使用」实心，其余描边
            tags: list[tuple[str, tuple, bool]] = []
            if is_current:
                tags.append(("当前使用", accent, True))
            if row.get("own"):
                tags.append(("专属模型", COLOR_OWN, False))
            if dead:
                tags.append(("暂不可用", COLOR_WARN, False))
            right = ox + width - PAD - 18
            for text, color, filled in tags:
                right = _pill(draw, fonts, text, right, cy, color, filled) - 10

            label_x = bx + BADGE + 18
            limit = right - label_x - 16
            note = str(row.get("note") or "")
            label = _fit(str(row.get("label") or ""), name_font, limit)
            if note:
                draw.text((label_x, cy - 14), label, font=name_font,
                          fill=(TEXT_SOFT if dead else TEXT_DARK) + (255,), anchor="lm")
                draw.text((label_x, cy + 16), _fit(note, note_font, limit),
                          font=note_font, fill=TEXT_MUTED + (255,), anchor="lm")
            else:
                draw.text((label_x, cy), label, font=name_font,
                          fill=(TEXT_SOFT if dead else TEXT_BODY) + (255,), anchor="lm")
            y += ROW_H + ROW_GAP

        # ---- 5) 脚部：提示 + 品牌（提示长到放不下品牌时就只留提示） ----
        if footer:
            fy = oy + card_h - FOOTER_H
            f_footer = fonts.get(F_FOOTER)
            brand = "用户网关 · 模型切换"
            brand_font = fonts.get(F_TAG)
            avail = width - PAD * 2
            brand_w = int(brand_font.getlength(brand))
            show_brand = bool(footer) and int(f_footer.getlength(footer)) + brand_w + 40 <= avail
            hint = _fit(footer, f_footer, avail - (brand_w + 40 if show_brand else 0))
            cy = fy + FOOTER_H // 2
            draw.ellipse(
                [ox + PAD, cy - 5, ox + PAD + 10, cy + 5], fill=accent + (255,)
            )
            draw.text(
                (ox + PAD + 22, cy), hint, font=f_footer,
                fill=_mix(accent, (255, 255, 255), 0.25) + (255,), anchor="lm",
            )
            if show_brand:
                draw.text(
                    (ox + width - PAD, cy), brand, font=brand_font,
                    fill=_mix(accent, (255, 255, 255), 0.5) + (255,), anchor="rm",
                )

        # ---- 6) 描边（最后画，压在色块之上） ----
        draw.rounded_rectangle(
            [ox, oy, ox + width - 1, oy + card_h - 1],
            RADIUS, outline=c["border"] + (255,), width=2,
        )

        # ---- 7) 压成白底 RGB 再存 ----
        # 直接 convert("RGB") 会把卡外那块**透明**区域变成纯黑（阴影也就成了黑边），
        # 而透明度在聊天客户端里的表现各家不一致，所以这里统一摊到白底上。
        flat = Image.alpha_composite(
            Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255)), img
        )
        out = io.BytesIO()
        flat.convert("RGB").save(out, format="PNG", optimize=True)
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
