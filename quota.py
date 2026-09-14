"""额度相关的纯计算与维护逻辑。

这里只放「不依赖 AstrBot」的部分：周期时间戳、过期判断、token 口径换算与估算。
真正的读写落库在 ``store.py``，插件在 ``main.py`` 里把两者串起来。

额度重置策略（简单可靠优先）：每条额度行带一个 ``reset_at`` 时间戳；
后台维护任务（默认每分钟一次）扫描到 ``now >= reset_at`` 的行就清零并算出下一个 ``reset_at``。
闸门热路径只读内存缓存，不做任何 SQL，也不做时间比较以外的计算。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Optional

PERIODS: tuple[str, ...] = ("day", "month", "total")

# 无 usage 回退估算：中英混排取 3 字符 ≈ 1 token（宁可略高，也不漏记）
CHARS_PER_TOKEN = 3


def next_reset_at(period: str, now: Optional[datetime] = None) -> Optional[int]:
    """下一次重置时间戳（epoch 秒）。

    Args:
        period: ``day`` / ``month`` / ``total``。
        now: 基准时间，默认当前本地时间。

    Returns:
        ``day`` → 次日 0 点；``month`` → 次月 1 日 0 点；``total``（或未知）→ ``None``（不重置）。
    """
    now = now or datetime.now()
    try:
        if period == "day":
            nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return int(nxt.timestamp())
        if period == "month":
            year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
            return int(datetime(year, month, 1, 0, 0, 0).timestamp())
    except Exception:
        return None
    return None


def is_stale(reset_at: Optional[int], now_ts: Optional[int] = None) -> bool:
    """额度行是否已过重置时间（需要清零）。``reset_at`` 为空表示不重置。"""
    if not reset_at:
        return False
    now_ts = int(now_ts if now_ts is not None else time.time())
    return now_ts >= int(reset_at)


def tokens_from_usage(usage: Any, count_cached: bool = True) -> tuple[int, int, int, int]:
    """把 AstrBot 的 ``TokenUsage`` 换算成 ``(in_other, in_cached, out, 计入口径总数)``。

    Args:
        usage: ``astrbot.core.provider.entities.TokenUsage`` 或任何带同名属性的对象；
               为 ``None`` 时返回全 0（调用方应改用 :func:`estimate_tokens`）。
        count_cached: 缓存 token 是否计入额度（PRD 决策 D2，默认计入）。

    Returns:
        ``(输入(非缓存), 输入(缓存), 输出, 用于扣额度的总数)``。
    """
    if usage is None:
        return 0, 0, 0, 0

    def _get(name: str) -> int:
        try:
            return max(0, int(getattr(usage, name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    in_other = _get("input_other")
    in_cached = _get("input_cached")
    out = _get("output")
    # 有些 provider 只给 total / input：兜底把差额算进 in_other
    if in_other == 0 and in_cached == 0:
        total_in = _get("input")
        in_other = max(0, total_in - in_cached)
    total = in_other + out + (in_cached if count_cached else 0)
    return in_other, in_cached, out, total


def estimate_tokens(*texts: Any) -> int:
    """无 usage 时的字符数粗估（用于保证「记账不漏」，会在库里标 ``estimated=1``）。"""
    total_chars = 0
    for t in texts:
        if not t:
            continue
        try:
            total_chars += len(str(t))
        except Exception:
            continue
    if total_chars <= 0:
        return 0
    return max(1, total_chars // CHARS_PER_TOKEN)


def should_warn(used: int, limit: int, ratio: float) -> bool:
    """是否应给管理员发额度预警（``ratio<=0`` 表示关闭预警）。"""
    if ratio <= 0 or limit <= 0:
        return False
    return used >= limit * ratio
