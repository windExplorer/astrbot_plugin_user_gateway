"""权限与额度的判定内核。

**纯逻辑、零 IO、不依赖 AstrBot** —— 便于单测，也保证闸门热路径极快：
插件在启动时把 ``policy`` / ``llm_quota`` 加载进内存字典，每来一次 LLM 请求只做几次
dict 查表与整数比较。

判定顺序（PRD §4.1）：

    管理员豁免 → 用户级 → 群级 → 全局默认

额度按「对象 × 周期」逐条比对；一个对象同时配了日/月/累计额度时**全部生效**，
命中任一即超限（报告最紧凑的那个周期）。``mode=observe`` 时超限只记录不拦截。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

# 周期顺序：越靠前越"紧凑"，超限时优先报告它
PERIODS: tuple[str, ...] = ("day", "month", "total")

FEATURE_LLM = "llm"

REASON_DISABLED = "disabled"
REASON_ADMIN = "admin"
REASON_PERMISSION = "permission"
REASON_QUOTA = "quota"


@dataclass(frozen=True)
class Subject:
    """一次 LLM 请求的归属对象。"""

    sender_id: str = ""
    group_id: str = ""
    is_admin: bool = False
    umo: str = ""
    platform_id: str = ""


@dataclass
class Verdict:
    """判定结果。``allow=False`` 时插件会拒绝并给出 ``reason``。"""

    allow: bool = True
    reason: str = ""
    # 命中的规则来源
    scope_type: str = ""  # user | group | global
    scope_id: str = ""
    # 额度相关
    period: str = ""
    limit: int = 0
    used: int = 0
    mode: str = ""  # enforce | observe
    # 观察模式：超限但放行（用于灰度与统计）
    observed: bool = False

    @property
    def detail(self) -> str:
        """给日志/审计用的一句话说明。"""
        parts = [f"reason={self.reason or 'ok'}"]
        if self.scope_type:
            parts.append(f"scope={self.scope_type}:{self.scope_id}")
        if self.period:
            parts.append(f"period={self.period}")
            parts.append(f"used={self.used}/{self.limit}")
        if self.mode:
            parts.append(f"mode={self.mode}")
        if self.observed:
            parts.append("observed=1")
        return " ".join(parts)


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


class Gate:
    """判定器。``cfg`` 是一个 ``key -> value`` 的读取函数（带默认值）。"""

    def __init__(self, cfg: Callable[[str, Any], Any]) -> None:
        self._cfg = cfg

    # ------------------------------------------------------------------ #
    # 配置读取（每次都读，便于控制台改配置后立即生效）
    # ------------------------------------------------------------------ #
    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def _guard_enabled(self) -> bool:
        return self._enabled() and bool(self._cfg("llm_guard_enabled", True))

    def _quota_enabled(self) -> bool:
        return self._enabled() and bool(self._cfg("quota_enabled", True))

    def _admin_exempt(self) -> bool:
        return bool(self._cfg("admin_exempt", True))

    def default_effect(self) -> str:
        eff = str(self._cfg("default_effect", "allow") or "allow").strip().lower()
        return eff if eff in ("allow", "deny") else "allow"

    # ------------------------------------------------------------------ #
    # 权限
    # ------------------------------------------------------------------ #
    def resolve_effect(
        self,
        subject: Subject,
        effect_user: Mapping[str, str],
        effect_group: Mapping[str, str],
    ) -> tuple[str, str, str]:
        """按「用户级 > 群级 > 全局默认」解析生效策略。

        Returns:
            ``(effect, scope_type, scope_id)``，effect 为 ``allow`` / ``deny``。
        """
        uid = str(subject.sender_id or "")
        gid = str(subject.group_id or "")
        if uid and uid in effect_user:
            return str(effect_user[uid]), "user", uid
        if gid and gid in effect_group:
            return str(effect_group[gid]), "group", gid
        return self.default_effect(), "global", "*"

    def check_permission(
        self,
        subject: Subject,
        effect_user: Mapping[str, str],
        effect_group: Mapping[str, str],
    ) -> Verdict:
        """只判定权限，不看额度。"""
        effect, scope_type, scope_id = self.resolve_effect(subject, effect_user, effect_group)
        if effect == "deny":
            return Verdict(
                allow=False,
                reason=REASON_PERMISSION,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        return Verdict(allow=True, scope_type=scope_type, scope_id=scope_id)

    # ------------------------------------------------------------------ #
    # 额度
    # ------------------------------------------------------------------ #
    @staticmethod
    def _quota_hit(rows: Optional[Mapping[str, Mapping[str, Any]]]) -> Optional[dict]:
        """在某个对象的额度集合里找出第一条被突破的记录（day > month > total）。"""
        if not rows:
            return None
        for period in PERIODS:
            row = rows.get(period)
            if not row:
                continue
            limit = _as_int(row.get("limit_tokens"), 0)
            if limit <= 0:  # 0 视为未配置/不限
                continue
            used = _as_int(row.get("used_tokens"), 0)
            if used >= limit:
                return {
                    "period": period,
                    "limit": limit,
                    "used": used,
                    "mode": str(row.get("mode") or "enforce"),
                }
        return None

    def check_quota(
        self,
        subject: Subject,
        quotas_user: Mapping[str, Mapping[str, Mapping[str, Any]]],
        quotas_group: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> Verdict:
        """按用户级、群级依次检查额度；超限时 ``mode=observe`` 仍然放行（标记 observed）。

        ``quotas_user`` / ``quotas_group`` 的形状是 ``{scope_id: {period: row}}``。
        """
        uid = str(subject.sender_id or "")
        gid = str(subject.group_id or "")

        for scope_type, scope_id, table in (
            ("user", uid, quotas_user),
            ("group", gid, quotas_group),
        ):
            if not scope_id:
                continue
            hit = self._quota_hit(table.get(scope_id) if table else None)
            if not hit:
                continue
            observe = hit["mode"] == "observe"
            return Verdict(
                allow=observe,  # 观察模式不拦
                reason=REASON_QUOTA,
                scope_type=scope_type,
                scope_id=scope_id,
                period=hit["period"],
                limit=hit["limit"],
                used=hit["used"],
                mode=hit["mode"],
                observed=observe,
            )
        return Verdict(allow=True)

    # ------------------------------------------------------------------ #
    # 总入口
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        subject: Subject,
        effect_user: Optional[Mapping[str, str]] = None,
        effect_group: Optional[Mapping[str, str]] = None,
        quotas_user: Optional[Mapping[str, Mapping[str, Mapping[str, Any]]]] = None,
        quotas_group: Optional[Mapping[str, Mapping[str, Mapping[str, Any]]]] = None,
    ) -> Verdict:
        """完整判定：插件总开关 → 管理员豁免 → 权限 → 额度。

        放行时也会带回**命中的规则来源**（``scope_type``/``scope_id``），
        便于 ``debug_log`` 时看清「为什么放行了」。
        """
        effect_user = effect_user or {}
        effect_group = effect_group or {}
        quotas_user = quotas_user or {}
        quotas_group = quotas_group or {}

        if not self._enabled():
            return Verdict(allow=True, reason=REASON_DISABLED)

        if subject.is_admin and self._admin_exempt():
            return Verdict(allow=True, reason=REASON_ADMIN, scope_type="admin", scope_id="*")

        allowed = Verdict(allow=True, scope_type="global", scope_id="*")

        if self._guard_enabled():
            perm = self.check_permission(subject, effect_user, effect_group)
            if not perm.allow:
                return perm
            allowed = perm  # 保留「用户级 / 群级 / 全局」命中信息

        if self._quota_enabled():
            quota = self.check_quota(subject, quotas_user, quotas_group)
            if not quota.allow:
                return quota
            if quota.observed:  # 超限但观察模式：放行，同时把信息带回去记录
                return quota

        return allowed


class Cooldown:
    """提示冷却：同一 key 在 ttl 秒内只允许触发一次（避免连续被拒时刷屏）。

    内部只存内存，进程重启即清空 —— 对提示场景足够，也不值得持久化。
    """

    def __init__(self, ttl: int = 60) -> None:
        self.ttl = max(0, int(ttl))
        self._last: dict[str, float] = {}

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        """``ttl<=0`` 时恒返回 True（不冷却）。"""
        if self.ttl <= 0:
            return True
        now = now if now is not None else time.time()
        last = self._last.get(key)
        if last is not None and now - last < self.ttl:
            return False
        self._last[key] = now
        return True

    def prune(self, now: Optional[float] = None) -> int:
        """清理过期条目，防止长期运行内存增长；返回清理条数。"""
        if self.ttl <= 0:
            return 0
        now = now if now is not None else time.time()
        stale = [k for k, ts in self._last.items() if now - ts >= self.ttl]
        for k in stale:
            self._last.pop(k, None)
        return len(stale)
