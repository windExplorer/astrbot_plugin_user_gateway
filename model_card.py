"""把「可选模型」渲染成一张卡片图（Pillow）。

为什么单独一个模块：绘图代码量不小、且与插件逻辑无关，混进 ``main.py`` 只会让
闸门那部分更难读；单独成文件也便于脱离 AstrBot 直接跑自检（``tests/test_model_card.py``）。

三条硬约束（改动时保持）：

1. **画不出来绝不能影响功能**：本模块对外只暴露「返回 PNG bytes 或 None」，
   字体找不到 / 图片太大 / Pillow 缺失都返回 ``None``，由调用方退化成纯文本列表。
   权限插件不能因为一张图渲染失败就把用户的指令吃掉。
2. **只依赖 Pillow**：它是 AstrBot 核心依赖（``requirements.txt`` 里的 ``pillow>=11.2.1``），
   随 AstrBot 一起装好，所以这里不新增任何第三方依赖。
3. **字体优先级**：调用方指定（配置项）→ 插件自带 ``assets/fonts`` → 系统常见中文字体。
   全都找不到时**返回 None**——中文渲染成豆腐块比不发图更糟。
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

# 画布与配色（与插件 logo / 控制台的粉紫风格一致）
WIDTH = 760
PAD = 28
RADIUS = 26
HEADER_H = 132
ROW_H = 78
ROW_GAP = 12
FOOTER_H = 64
AVATAR = 76

CARD_BG = (255, 255, 255)
ROW_BG = (247, 246, 251)
ROW_BG_ALT = (252, 251, 255)
HEADER_FROM = (255, 158, 196)
HEADER_TO = (196, 166, 255)
TEXT_DARK = (56, 50, 72)
TEXT_MUTED = (140, 134, 158)
TEXT_ON_HEADER = (255, 255, 255)
TEXT_ON_HEADER_SOFT = (255, 240, 248)
BADGE_BG = (255, 219, 235)
BADGE_TEXT = (214, 60, 126)
ACCENT_CURRENT = (82, 196, 26)
ACCENT_OWN = (250, 140, 22)
ACCENT_WARN = (150, 146, 166)
OUTLINE = (236, 233, 244)

# 字号
F_TITLE = 38
F_SUBTITLE = 23
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


def _gradient(width: int, height: int, top: tuple, bottom: tuple) -> Any:
    """竖向渐变（先画 1×h 再拉伸，比逐行画快得多）。"""
    strip = Image.new("RGB", (1, max(1, height)))
    px = strip.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        px[0, y] = tuple(
            int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)
        )
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
    draw.rounded_rectangle(
        [x0, cy - h // 2, right, cy + h // 2], h // 2, fill=color + (255,)
    )
    draw.text((x0 + 11, cy), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
    return x0


def render_model_card(
    *,
    title: str,
    subtitle: str,
    rows: Iterable[dict[str, Any]],
    footer: str = "",
    avatar: Optional[bytes] = None,
    font_path: str = "",
    width: int = WIDTH,
) -> Optional[bytes]:
    """渲染「模型选择」卡片，返回 PNG 字节；任何失败都返回 ``None``。

    Args:
        title: 卡片标题（如「模型切换」）。
        subtitle: 标题下方一句（如「分组：VIP（好友等级）｜当前：xxx」）。
        rows: 每行 ``{index, label, note, current, own, unavailable}``：
            序号 / 主文案（模型名）/ 次文案 / 是否当前使用 / 是否专属模型 / 是否暂不可用。
        footer: 底部提示（如「回复序号即可切换 · 60 秒内有效」）。
        avatar: 头像字节（可选；取不到时画占位圆）。
        font_path: 指定的字体文件路径（配置项）；为空则自动找。

    Returns:
        PNG bytes，或 ``None``（调用方应退化成纯文本）。
    """
    if Image is None or ImageDraw is None or ImageFont is None:
        return None
    try:
        path = find_font_path(font_path)
        if path is None:
            return None
        fonts = _Fonts(path)

        items = [dict(r) for r in rows]
        if not items:
            return None
        body_h = len(items) * (ROW_H + ROW_GAP) - ROW_GAP
        footer_h = FOOTER_H if footer else 0
        height = HEADER_H + PAD + body_h + (footer_h + 12 if footer_h else 0) + PAD

        img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            [0, 0, width - 1, height - 1], RADIUS, fill=CARD_BG + (255,)
        )

        # ---- 头部：渐变 + 标题 + 头像 ----
        head = _gradient(width, HEADER_H + RADIUS, HEADER_FROM, HEADER_TO).convert("RGBA")
        mask = Image.new("L", (width, HEADER_H + RADIUS), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, width - 1, HEADER_H + RADIUS - 1], RADIUS, fill=255
        )
        img.paste(head, (0, 0), mask)

        text_x = PAD
        ax = width - PAD - AVATAR
        if avatar:
            circ = _circle_avatar(avatar, AVATAR)
            if circ is not None:
                img.alpha_composite(circ, (ax, (HEADER_H - AVATAR) // 2))
                text_x = PAD
        draw.text((text_x, 40), title, font=fonts.get(F_TITLE), fill=TEXT_ON_HEADER, anchor="lm")
        draw.text(
            (text_x, 86),
            _fit(subtitle, fonts.get(F_SUBTITLE), width - PAD * 2 - AVATAR - 16),
            font=fonts.get(F_SUBTITLE),
            fill=TEXT_ON_HEADER_SOFT,
            anchor="lm",
        )

        # ---- 列表 ----
        y = HEADER_H + PAD
        badge_font = fonts.get(F_INDEX)
        label_font = fonts.get(F_LABEL)
        note_font = fonts.get(F_NOTE)
        for i, row in enumerate(items):
            bg = ROW_BG_ALT if i % 2 else ROW_BG
            draw.rounded_rectangle(
                [PAD, y, width - PAD, y + ROW_H], 18, fill=bg + (255,)
            )
            cy = y + ROW_H // 2

            # 序号角标
            bx = PAD + 18
            draw.ellipse([bx, cy - 22, bx + 44, cy + 22], fill=BADGE_BG + (255,))
            draw.text(
                (bx + 22, cy),
                str(row.get("index", i + 1)),
                font=badge_font,
                fill=BADGE_TEXT + (255,),
                anchor="mm",
            )

            # 右侧标签（从右往左排）：「当前使用」永远在最右，最显眼
            tags: list[tuple[str, tuple]] = []
            if row.get("current"):
                tags.append(("当前使用", ACCENT_CURRENT))
            if row.get("own"):
                tags.append(("专属模型", ACCENT_OWN))
            if row.get("unavailable"):
                tags.append(("暂不可用", ACCENT_WARN))
            right = width - PAD - 18
            for text, color in tags:
                right = _tag(draw, fonts, text, right, cy, color) - 8

            label_x = bx + 60
            limit = right - label_x - 12
            note = str(row.get("note") or "")
            label = _fit(str(row.get("label") or ""), label_font, limit)
            if note:
                draw.text(
                    (label_x, cy - 15), label, font=label_font, fill=TEXT_DARK + (255,), anchor="lm"
                )
                draw.text(
                    (label_x, cy + 15),
                    _fit(note, note_font, limit),
                    font=note_font,
                    fill=TEXT_MUTED + (255,),
                    anchor="lm",
                )
            else:
                draw.text(
                    (label_x, cy), label, font=label_font, fill=TEXT_DARK + (255,), anchor="lm"
                )
            y += ROW_H + ROW_GAP

        # ---- 底部提示 ----
        if footer:
            fy = y + 6
            draw.line([PAD, fy, width - PAD, fy], fill=OUTLINE + (255,), width=2)
            draw.text(
                (PAD, fy + FOOTER_H // 2),
                _fit(footer, fonts.get(F_FOOTER), width - PAD * 2),
                font=fonts.get(F_FOOTER),
                fill=TEXT_MUTED + (255,),
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
