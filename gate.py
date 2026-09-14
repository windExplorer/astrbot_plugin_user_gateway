"""权限与额度的判定内核。

**纯逻辑、零 IO、不依赖 AstrBot** —— 便于单测，也保证闸门热路径极快：
插件在启动时把规则加载进内存快照（:class:`Rules`），每来一次 LLM 请求只做几次
dict 查表与整数比较。

判定顺序：

    总开关 → 管理员豁免 → 权限 → 额度

权限与额度共用同一套**档位优先级**（越靠前越具体，命中即生效）：

    好友专属 → 好友等级 → 群专属 → 群等级 → 全局默认

两个关键设计（都是 v0.3.0 引入等级时定下的，勿轻易改动）：

1. **额度「最具体的一层生效」**，而不是「所有层都参与」。
   否则「全局每人 5 万」会把「VIP 等级每人 50 万」直接废掉 —— 等级就失去了档位意义。
   命中的那一层内部，日 / 月 / 累计多个周期仍然全部参与（命中任一即超限）。
2. **等级额度是「每个对象各自的上限」**，不是整组合计。
   所以判定时用的一定是**该对象自己的**用量计数（``usage_counter``），
   而不是额度行的已用量（v2 起额度行不再存用量）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

# 周期顺序：越靠前越"紧凑"，超限时优先报告它
PERIODS: tuple[str, ...] = ("day", "month", "total")

FEATURE_LLM = "llm"

REASON_DISABLED = "disabled"
REASON_ADMIN = "admin"
REASON_PERMISSION = "permission"
REASON_QUOTA = "quota"

# 档位名（与 store.LAYERS 对应）
LAYER_USER = "user"
LAYER_USER_LEVEL = "user_level"
LAYER_GROUP = "group"
LAYER_GROUP_LEVEL = "group_level"
LAYER_GLOBAL = "global"

LAYER_LABELS: dict[str, str] = {
    LAYER_USER: "好友专属",
    LAYER_USER_LEVEL: "好友等级",
    LAYER_GROUP: "群专属",
    LAYER_GROUP_LEVEL: "群等级",
    LAYER_GLOBAL: "全局默认",
}


def layer_label(layer: str) -> str:
    """档位的中文名（控制台与日志共用）。"""
    return LAYER_LABELS.get(layer, layer or "未知")


@dataclass(frozen=True)
class Subject:
    """一次 LLM 请求的归属对象。"""

    sender_id: str = ""
    group_id: str = ""
    is_admin: bool = False
    umo: str = ""
    platform_id: str = ""


@dataclass(frozen=True)
class Rules:
    """判定所需的规则快照。插件在 ``reload_rules()`` 里**整体重建**（不做原地修改）。

    Args:
        effect_user: ``{uin: allow|deny}`` —— 好友专属权限。
        effect_group: ``{group_id: allow|deny}`` —— 群专属权限。
        level_effect: ``{(kind, level_id): allow|deny|inherit}`` —— 等级默认权限。
        subject_level: ``{scope_type: {scope_id: level_id}}`` —— 对象归级。
        limits: ``{scope_type: {scope_id: {period: row}}}`` —— 限额规则，
            ``scope_type`` 为 ``user`` / ``group`` / ``level`` / ``global``。
        usage: ``{scope_type: {scope_id: {period: row}}}`` —— 用量计数，
            ``scope_type`` 为 ``user`` / ``group``。
    """

    effect_user: Mapping[str, str] = field(default_factory=dict)
    effect_group: Mapping[str, str] = field(default_factory=dict)
    level_effect: Mapping[Any, str] = field(default_factory=dict)
    subject_level: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    limits: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]] = field(default_factory=dict)
    usage: Mapping[str, Mapping[str, Mapping[str, Mapping[str, Any]]]] = field(default_factory=dict)

    def limits_of(self, scope_type: str, scope_id: str) -> Mapping[str, Mapping[str, Any]]:
        return (self.limits.get(scope_type) or {}).get(str(scope_id)) or {}

    def usage_of(self, scope_type: str, scope_id: str) -> Mapping[str, Mapping[str, Any]]:
        return (self.usage.get(scope_type) or {}).get(str(scope_id)) or {}

    def level_id_of(self, scope_type: str, scope_id: str) -> Optional[int]:
        """对象所属等级 id；未归级返回 None。"""
        got = (self.subject_level.get(scope_type) or {}).get(str(scope_id))
        return int(got) if got else None


@dataclass(frozen=True)
class LayerRef:
    """一个额度档位：去哪找限额、去哪找用量。"""

    layer: str
    scope_type: str  # user | group | level | global（限额所在）
    scope_id: str
    usage_type: str  # user | group（用量所在）
    usage_id: str

    @property
    def label(self) -> str:
        return layer_label(self.layer)


@dataclass
class Verdict:
    """判定结果。``allow=False`` 时插件会拒绝并给出 ``reason``。"""

    allow: bool = True
    reason: str = ""
    # 命中的档位 / 规则来源
    layer: str = ""  # user | user_level | group | group_level | global
    scope_type: str = ""  # user | group | level | global
    scope_id: str = ""
    # 额度相关
    period: str = ""
    limit: int = 0
    used: int = 0
    mode: str = ""  # enforce | observe
    # 观察模式：超限但放行（用于灰度与统计）
    observed: bool = False

    @property
    def layer_label(self) -> str:
        return layer_label(self.layer)

    @property
    def detail(self) -> str:
        """给日志/审计用的一句话说明。"""
        parts = [f"reason={self.reason or 'ok'}"]
        if self.layer:
            parts.append(f"layer={self.layer}({self.layer_label})")
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
    # 档位链
    # ------------------------------------------------------------------ #
    @staticmethod
    def layers_for(subject: Subject, rules: Rules) -> list[LayerRef]:
        """按「从具体到兜底」列出该对象适用的档位。

        私聊：好友专属 → 好友等级 → 全局
        群聊：好友专属 → 好友等级 → 群专属 → 群等级 → 全局
        （群聊里也先看人：这是 v0.2 定的优先级，等级只是插进这条链的中间层）
        """
        uid = str(subject.sender_id or "")
        gid = str(subject.group_id or "")
        out: list[LayerRef] = []

        if uid:
            out.append(LayerRef(LAYER_USER, "user", uid, "user", uid))
            lv = rules.level_id_of("user", uid)
            if lv:
                out.append(LayerRef(LAYER_USER_LEVEL, "level", str(lv), "user", uid))
        if gid:
            out.append(LayerRef(LAYER_GROUP, "group", gid, "group", gid))
            lv = rules.level_id_of("group", gid)
            if lv:
                out.append(LayerRef(LAYER_GROUP_LEVEL, "level", str(lv), "group", gid))
        # 全局：群聊按群用量、私聊按人用量（与 v0.2 的记账口径一致）
        out.append(
            LayerRef(LAYER_GLOBAL, "global", "*", "group" if gid else "user", gid or uid)
        )
        return out

    # ------------------------------------------------------------------ #
    # 权限
    # ------------------------------------------------------------------ #
    def resolve_effect(self, subject: Subject, rules: Rules) -> tuple[str, str, str, str]:
        """按档位链解析生效权限。

        Returns:
            ``(effect, layer, scope_type, scope_id)``；effect 为 ``allow`` / ``deny``。
            ``layer`` 用于告诉管理员「这条规则是哪儿来的」。

        注意：链上的「等级」只有在等级显式设了 allow / deny（非 inherit）时才截断链条，
        否则继续往下找更粗的规则 —— 这样「等级只配额度、不管权限」也能正常工作。
        """
        for ref in self.layers_for(subject, rules):
            if ref.layer == LAYER_USER:
                if ref.scope_id in rules.effect_user:
                    return str(rules.effect_user[ref.scope_id]), ref.layer, ref.scope_type, ref.scope_id
            elif ref.layer == LAYER_GROUP:
                if ref.scope_id in rules.effect_group:
                    return str(rules.effect_group[ref.scope_id]), ref.layer, ref.scope_type, ref.scope_id
            elif ref.layer in (LAYER_USER_LEVEL, LAYER_GROUP_LEVEL):
                kind = "group" if ref.layer == LAYER_GROUP_LEVEL else "user"
                eff = str(rules.level_effect.get((kind, _as_int(ref.scope_id)), "inherit") or "inherit")
                if eff in ("allow", "deny"):
                    return eff, ref.layer, ref.scope_type, ref.scope_id
        return self.default_effect(), LAYER_GLOBAL, "global", "*"

    def check_permission(self, subject: Subject, rules: Rules) -> Verdict:
        """只判定权限，不看额度。"""
        effect, layer, scope_type, scope_id = self.resolve_effect(subject, rules)
        if effect == "deny":
            return Verdict(
                allow=False,
                reason=REASON_PERMISSION,
                layer=layer,
                scope_type=scope_type,
                scope_id=scope_id,
            )
        return Verdict(allow=True, layer=layer, scope_type=scope_type, scope_id=scope_id)

    # ------------------------------------------------------------------ #
    # 额度
    # ------------------------------------------------------------------ #
    @staticmethod
    def quota_hit(
        limits: Mapping[str, Mapping[str, Any]],
        usage: Mapping[str, Mapping[str, Any]],
    ) -> Optional[dict]:
        """在某一层的额度集合里找出第一条被突破的记录（day > month > total）。

        ``used`` 取自**对象自己的用量计数**（v2 起额度行不存用量）。
        """
        for period in PERIODS:
            row = limits.get(period)
            if not row:
                continue
            limit = _as_int(row.get("limit_tokens"), 0)
            if limit <= 0:  # 0 视为未配置/不限
                continue
            used = _as_int((usage.get(period) or {}).get("used_tokens"), 0)
            if used >= limit:
                return {
                    "period": period,
                    "limit": limit,
                    "used": used,
                    "mode": str(row.get("mode") or "enforce"),
                }
        return None

    @staticmethod
    def compact_limit(
        limits: Mapping[str, Mapping[str, Any]],
        usage: Mapping[str, Mapping[str, Any]],
    ) -> dict:
        """该层「最紧凑」的一条额度（day > month > total）。

        与 :meth:`quota_hit` 的区别：未超限时也要能报告「生效的上限是多少」，
        所以这里不判断是否超限，只挑第一条真正配了上限的周期。
        """
        for period in PERIODS:
            row = limits.get(period)
            if not row:
                continue
            limit = _as_int(row.get("limit_tokens"), 0)
            if limit <= 0:  # 0 = 不限，不作为「生效上限」报告
                continue
            return {
                "period": period,
                "limit": limit,
                "used": _as_int((usage.get(period) or {}).get("used_tokens"), 0),
                "mode": str(row.get("mode") or "enforce"),
            }
        return {}

    def check_quota(self, subject: Subject, rules: Rules) -> Verdict:
        """按档位链找**第一个配置了额度的层**，只用该层判定。

        注意「配置了额度的层」是**结构判断**（该层有没有行），不是「有没有上限」：
        某一层配了 ``limit_tokens=0``（明确「不限」）时，它会**占住档位**、更粗的层不再参与。
        这是有意为之 —— 否则「VIP 等级 = 不限」永远会被全局额度先拦掉，等级就失去意义。

        未超限时返回一个带 ``layer`` 与生效上限的「放行」结果，便于控制台/日志说明
        「当前生效的是哪个额度」。``mode=observe`` 超限仍然放行并标记 ``observed``。
        """
        for ref in Gate.layers_for(subject, rules):
            limits = rules.limits_of(ref.scope_type, ref.scope_id)
            if not limits:
                continue
            usage = rules.usage_of(ref.usage_type, ref.usage_id)
            hit = self.quota_hit(limits, usage)
            base = {
                "layer": ref.layer,
                "scope_type": ref.scope_type,
                "scope_id": ref.scope_id,
            }
            if not hit:
                return Verdict(allow=True, **base, **self.compact_limit(limits, usage))
            observe = hit["mode"] == "observe"
            return Verdict(
                allow=observe,  # 观察模式不拦
                reason=REASON_QUOTA,
                period=hit["period"],
                limit=hit["limit"],
                used=hit["used"],
                mode=hit["mode"],
                observed=observe,
                **base,
            )
        return Verdict(allow=True, layer="", scope_type="global", scope_id="*")

    # ------------------------------------------------------------------ #
    # 总入口
    # ------------------------------------------------------------------ #
    def evaluate(self, subject: Subject, rules: Optional[Rules] = None) -> Verdict:
        """完整判定：插件总开关 → 管理员豁免 → 权限 → 额度。

        放行时也会带回**命中的档位**，便于 ``debug_log`` 时看清「为什么放行了」。
        """
        rules = rules or Rules()

        if not self._enabled():
            return Verdict(allow=True, reason=REASON_DISABLED)

        if subject.is_admin and self._admin_exempt():
            return Verdict(allow=True, reason=REASON_ADMIN, layer="admin", scope_type="admin", scope_id="*")

        allowed = Verdict(allow=True, layer=LAYER_GLOBAL, scope_type="global", scope_id="*")

        if self._guard_enabled():
            perm = self.check_permission(subject, rules)
            if not perm.allow:
                return perm
            allowed = perm  # 保留「命中了哪一层权限」的信息

        if self._quota_enabled():
            quota = self.check_quota(subject, rules)
            if not quota.allow or quota.observed:
                return quota
            if quota.layer:  # 有生效额度层且未超限 → 报告它（比权限信息更有用）
                allowed = quota

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
