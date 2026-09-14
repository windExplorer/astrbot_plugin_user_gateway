"""判定内核与额度计算的自检（纯逻辑，不需要 AstrBot 环境）。

用法：
    uv run --no-project python tests/test_gate.py

覆盖：三级作用域优先级、全局默认策略、管理员豁免、开关、额度超限/观察模式/多周期、
提示冷却、周期重置时间计算、token 口径换算与估算。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gate as G  # noqa: E402
import quota as Q  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _failures.append(label)


def make_gate(**cfg):
    """用给定配置构造 Gate（未给的键走默认值）。"""
    defaults = {
        "enabled": True,
        "llm_guard_enabled": True,
        "quota_enabled": True,
        "admin_exempt": True,
        "default_effect": "allow",
    }
    defaults.update(cfg)
    return G.Gate(lambda k, d=None: defaults.get(k, d))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    u_allow = {"10001": "allow"}
    u_deny = {"10001": "deny"}
    g_deny = {"88888": "deny"}
    g_allow = {"88888": "allow"}

    print("\n[1] 三级作用域优先级")
    gate = make_gate()
    # 私聊：只看用户级
    v = gate.evaluate(G.Subject(sender_id="10001"), u_deny, {})
    check(not v.allow and v.reason == "permission" and v.scope_type == "user", "用户级 deny 生效")
    # 群内：用户级 allow 覆盖群级 deny（用户级优先级更高）
    v = gate.evaluate(G.Subject(sender_id="10001", group_id="88888"), u_allow, g_deny)
    check(v.allow and v.scope_type == "user", "用户级 allow 覆盖群级 deny")
    # 群内：用户无规则 → 群级 deny 生效
    v = gate.evaluate(G.Subject(sender_id="99999", group_id="88888"), u_allow, g_deny)
    check(not v.allow and v.scope_type == "group", "无用户规则时群级 deny 生效")
    # 都没规则 → 全局默认
    gate_deny_default = make_gate(default_effect="deny")
    v = gate_deny_default.evaluate(G.Subject(sender_id="99999", group_id="77777"), {}, {})
    check(not v.allow and v.scope_type == "global", "全局默认 deny（白名单模式）")
    v = gate_deny_default.evaluate(G.Subject(sender_id="10001"), u_allow, {})
    check(v.allow, "白名单模式下显式 allow 放行")

    print("\n[2] 管理员与总开关")
    v = gate.evaluate(G.Subject(sender_id="10001", is_admin=True), u_deny, {})
    check(v.allow and v.reason == "admin", "管理员豁免（即使被显式 deny）")
    v = make_gate(admin_exempt=False).evaluate(G.Subject(sender_id="10001", is_admin=True), u_deny, {})
    check(not v.allow, "关闭豁免后管理员也会被拦")
    v = make_gate(enabled=False).evaluate(G.Subject(sender_id="10001"), u_deny, {})
    check(v.allow and v.reason == "disabled", "总开关关闭 → 全部放行")
    v = make_gate(llm_guard_enabled=False).evaluate(G.Subject(sender_id="10001"), u_deny, {})
    check(v.allow, "仅关闭 LLM 管控 → 权限不拦")
    v = make_gate(quota_enabled=False).evaluate(
        G.Subject(sender_id="10001"), {}, {}, {"10001": {"day": {"limit_tokens": 10, "used_tokens": 99}}}, {},
    )
    check(v.allow, "仅关闭额度 → 额度不拦")

    print("\n[3] 额度判定")
    q_user = {"10001": {"day": {"limit_tokens": 1000, "used_tokens": 1000, "mode": "enforce"}}}
    v = gate.evaluate(G.Subject(sender_id="10001"), {}, {}, q_user, {})
    check(not v.allow and v.reason == "quota" and v.period == "day", "日额度用满 → 拦截")
    check(v.limit == 1000 and v.used == 1000, "Verdict 带回额度详情")
    q_user_ok = {"10001": {"day": {"limit_tokens": 1000, "used_tokens": 999, "mode": "enforce"}}}
    check(gate.evaluate(G.Subject(sender_id="10001"), {}, {}, q_user_ok, {}).allow, "未满 → 放行")
    q_obs = {"10001": {"day": {"limit_tokens": 1000, "used_tokens": 5000, "mode": "observe"}}}
    v = gate.evaluate(G.Subject(sender_id="10001"), {}, {}, q_obs, {})
    check(v.allow and v.observed and v.reason == "quota", "观察模式超限 → 放行但标记 observed")
    q_zero = {"10001": {"day": {"limit_tokens": 0, "used_tokens": 999999, "mode": "enforce"}}}
    check(gate.evaluate(G.Subject(sender_id="10001"), {}, {}, q_zero, {}).allow, "limit=0 视为不限")
    # 多周期：只要有一个超限就拦，并优先报告 day
    q_multi = {"10001": {
        "day": {"limit_tokens": 100, "used_tokens": 50, "mode": "enforce"},
        "month": {"limit_tokens": 100, "used_tokens": 100, "mode": "enforce"},
    }}
    v = gate.evaluate(G.Subject(sender_id="10001"), {}, {}, q_multi, {})
    check(not v.allow and v.period == "month", "多周期：命中超限的那一个（日未满、月已满）")
    # 群额度：用户无额度时看群额度
    q_group = {"88888": {"day": {"limit_tokens": 10, "used_tokens": 10, "mode": "enforce"}}}
    v = gate.evaluate(G.Subject(sender_id="99999", group_id="88888"), {}, {}, {}, q_group)
    check(not v.allow and v.scope_type == "group", "群额度超限拦截群内所有人")
    # 权限优先于额度：权限拒绝时不应报告额度
    v = gate.evaluate(G.Subject(sender_id="10001"), u_deny, {}, q_user, {})
    check(v.reason == "permission", "权限拒绝优先于额度")

    print("\n[4] 提示冷却")
    cd = G.Cooldown(ttl=60)
    check(cd.allow("k", now=100.0), "首次允许")
    check(not cd.allow("k", now=130.0), "冷却期内拒绝")
    check(cd.allow("k", now=161.0), "超时后再次允许")
    check(cd.allow("other", now=130.0), "不同 key 互不影响")
    cd0 = G.Cooldown(ttl=0)
    check(cd0.allow("k", now=1.0) and cd0.allow("k", now=1.0), "ttl=0 → 不冷却")
    cd.prune(now=1000.0)
    check(cd.prune(now=1000.0) == 0, "过期条目已被清空")

    print("\n[5] 周期重置时间")
    base = datetime(2026, 9, 14, 23, 30, 0)
    day_reset = Q.next_reset_at("day", base)
    check(datetime.fromtimestamp(day_reset).day == 15, "日额度重置到次日 0 点")
    check(datetime.fromtimestamp(day_reset).hour == 0, "重置时间为 0 点整")
    month_reset = Q.next_reset_at("month", datetime(2026, 12, 20, 10, 0, 0))
    check(datetime.fromtimestamp(month_reset).month == 1, "12 月 → 次年 1 月")
    check(Q.next_reset_at("total", base) is None, "累计额度不重置")
    check(Q.is_stale(day_reset, day_reset - 1) is False, "未到点 → 不重置")
    check(Q.is_stale(day_reset, day_reset) is True, "到点 → 重置")
    check(Q.is_stale(None, 9999999999) is False, "reset_at 为空 → 永不重置")

    print("\n[6] token 口径")


    class Usage:
        input_other = 100
        input_cached = 50
        output = 30
        input = 150

    io, ic, o, total = Q.tokens_from_usage(Usage(), count_cached=True)
    check((io, ic, o, total) == (100, 50, 30, 180), f"缓存计入 → 100/50/30/180（实得 {io}/{ic}/{o}/{total}）")
    _, _, _, total2 = Q.tokens_from_usage(Usage(), count_cached=False)
    check(total2 == 130, f"缓存不计入 → 130（实得 {total2}）")
    check(Q.tokens_from_usage(None) == (0, 0, 0, 0), "usage 为 None → 全 0")

    class OnlyTotal:
        input_other = 0
        input_cached = 0
        output = 20
        input = 80

    io, ic, o, total = Q.tokens_from_usage(OnlyTotal())
    check(io == 80 and total == 100, f"只给 input 总量时兜底算入 in_other（实得 {io}/{total}）")
    check(Q.estimate_tokens("一二三四五六") == 2, "6 字符 ≈ 2 token")
    check(Q.estimate_tokens("", None) == 0, "空文本 → 0")
    check(Q.should_warn(80, 100, 0.8) and not Q.should_warn(79, 100, 0.8), "80% 预警阈值")
    check(not Q.should_warn(999, 100, 0), "ratio=0 关闭预警")

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  - " + f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
