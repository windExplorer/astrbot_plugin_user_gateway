"""判定内核与额度计算的自检（纯逻辑，不需要 AstrBot 环境）。

用法：
    uv run --no-project python tests/test_gate.py

覆盖：档位优先级（好友专属 → 好友等级 → 群专属 → 群等级 → 全局）、
额度「最具体的一层生效」、等级默认权限、观察模式、多周期、管理员豁免、开关、
提示冷却、周期重置时间、token 口径换算与估算。
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


def R(**kw) -> G.Rules:
    """构造规则快照（只填本次要用的部分）。"""
    return G.Rules(
        effect_user=kw.get("effect_user") or {},
        effect_group=kw.get("effect_group") or {},
        level_effect=kw.get("level_effect") or {},
        subject_level=kw.get("subject_level") or {},
        limits=kw.get("limits") or {},
        usage=kw.get("usage") or {},
    )


def lim(limit: int, mode: str = "enforce") -> dict:
    return {"limit_tokens": limit, "mode": mode}


def used(n: int) -> dict:
    return {"used_tokens": n}


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    u_allow = {"10001": "allow"}
    u_deny = {"10001": "deny"}
    g_deny = {"88888": "deny"}
    g_allow = {"88888": "allow"}
    gate = make_gate()

    print("\n[1] 档位优先级（权限）")
    v = gate.evaluate(G.Subject(sender_id="10001"), R(effect_user=u_deny))
    check(not v.allow and v.reason == "permission" and v.layer == "user", "好友专属 deny 生效")
    # 群内：好友专属 allow 覆盖群专属 deny（好友级优先级更高）
    v = gate.evaluate(G.Subject(sender_id="10001", group_id="88888"), R(effect_user=u_allow, effect_group=g_deny))
    check(v.allow and v.layer == "user", "好友专属 allow 覆盖群专属 deny")
    v = gate.evaluate(G.Subject(sender_id="99999", group_id="88888"), R(effect_user=u_allow, effect_group=g_deny))
    check(not v.allow and v.layer == "group", "无好友规则时群专属 deny 生效")
    v = make_gate(default_effect="deny").evaluate(G.Subject(sender_id="99999", group_id="77777"), R())
    check(not v.allow and v.layer == "global", "全局默认 deny（白名单模式）")
    check(make_gate(default_effect="deny").evaluate(G.Subject(sender_id="10001"), R(effect_user=u_allow)).allow,
          "白名单模式下显式 allow 放行")

    print("\n[2] 等级默认权限")
    # 好友归级 → 等级 effect=deny 生效
    rules = R(level_effect={("user", 1): "deny"}, subject_level={"user": {"10001": 1}})
    v = gate.evaluate(G.Subject(sender_id="10001"), rules)
    check(not v.allow and v.layer == "user_level" and v.scope_type == "level", "好友等级 deny 生效")
    # 好友专属 allow 优先于等级 deny
    v = gate.evaluate(G.Subject(sender_id="10001"), R(effect_user=u_allow, **{
        "level_effect": {("user", 1): "deny"}, "subject_level": {"user": {"10001": 1}},
    }))
    check(v.allow and v.layer == "user", "好友专属 allow 优先于等级 deny")
    # 等级 effect=inherit → 不截断链条，继续看群规则
    v = gate.evaluate(
        G.Subject(sender_id="10001", group_id="88888"),
        R(effect_group=g_deny, level_effect={("user", 1): "inherit"}, subject_level={"user": {"10001": 1}}),
    )
    check(not v.allow and v.layer == "group", "等级 inherit（不管权限）→ 继续看群专属")
    # 群等级
    v = gate.evaluate(
        G.Subject(sender_id="99999", group_id="88888"),
        R(level_effect={("group", 7): "deny"}, subject_level={"group": {"88888": 7}}),
    )
    check(not v.allow and v.layer == "group_level", "群等级 deny 生效")
    # 群专属 allow 优先于群等级 deny
    v = gate.evaluate(
        G.Subject(sender_id="99999", group_id="88888"),
        R(effect_group=g_allow, level_effect={("group", 7): "deny"}, subject_level={"group": {"88888": 7}}),
    )
    check(v.allow and v.layer == "group", "群专属 allow 优先于群等级 deny")

    print("\n[3] 管理员与总开关")
    v = gate.evaluate(G.Subject(sender_id="10001", is_admin=True), R(effect_user=u_deny))
    check(v.allow and v.reason == "admin", "管理员豁免（即使被显式 deny）")
    v = make_gate(admin_exempt=False).evaluate(G.Subject(sender_id="10001", is_admin=True), R(effect_user=u_deny))
    check(not v.allow, "关闭豁免后管理员也会被拦")
    v = make_gate(enabled=False).evaluate(G.Subject(sender_id="10001"), R(effect_user=u_deny))
    check(v.allow and v.reason == "disabled", "总开关关闭 → 全部放行")
    check(make_gate(llm_guard_enabled=False).evaluate(G.Subject(sender_id="10001"), R(effect_user=u_deny)).allow,
          "仅关闭 LLM 管控 → 权限不拦")
    v = make_gate(quota_enabled=False).evaluate(
        G.Subject(sender_id="10001"),
        R(limits={"user": {"10001": {"day": lim(10)}}}, usage={"user": {"10001": {"day": used(99)}}}),
    )
    check(v.allow, "仅关闭额度 → 额度不拦")

    print("\n[4] 额度：最具体的一层生效")
    q_user = {"user": {"10001": {"day": lim(1000)}}}
    v = gate.evaluate(G.Subject(sender_id="10001"), R(limits=q_user, usage={"user": {"10001": {"day": used(1000)}}}))
    check(not v.allow and v.reason == "quota" and v.period == "day" and v.layer == "user", "好友专属日额度用满 → 拦截")
    check(v.limit == 1000 and v.used == 1000, "Verdict 带回额度详情")
    v = gate.evaluate(G.Subject(sender_id="10001"), R(limits=q_user, usage={"user": {"10001": {"day": used(999)}}}))
    check(v.allow and v.layer == "user", "未满 → 放行并报告生效档位")
    # 观察模式
    v = gate.evaluate(
        G.Subject(sender_id="10001"),
        R(limits={"user": {"10001": {"day": lim(1000, "observe")}}}, usage={"user": {"10001": {"day": used(5000)}}}),
    )
    check(v.allow and v.observed and v.reason == "quota", "观察模式超限 → 放行但标记 observed")
    # limit=0 视为不限
    v = gate.evaluate(
        G.Subject(sender_id="10001"),
        R(limits={"user": {"10001": {"day": lim(0)}}}, usage={"user": {"10001": {"day": used(999999)}}}),
    )
    check(v.allow and v.layer == "user" and v.limit == 0, "limit=0 = 明确「不限」：占住档位但不拦（更粗的层不会再生效）")
    # 多周期：命中任一即拦，报告最紧凑的周期
    v = gate.evaluate(
        G.Subject(sender_id="10001"),
        R(
            limits={"user": {"10001": {"day": lim(100), "month": lim(100)}}},
            usage={"user": {"10001": {"day": used(50), "month": used(100)}}},
        ),
    )
    check(not v.allow and v.period == "month", "多周期：报告真正超限的那个（日未满、月已满）")
    # 群额度：好友无额度时看群额度
    v = gate.evaluate(
        G.Subject(sender_id="99999", group_id="88888"),
        R(limits={"group": {"88888": {"day": lim(10)}}}, usage={"group": {"88888": {"day": used(10)}}}),
    )
    check(not v.allow and v.layer == "group", "群额度超限拦截群内所有人")
    # 权限优先于额度
    v = gate.evaluate(
        G.Subject(sender_id="10001"),
        R(effect_user=u_deny, limits=q_user, usage={"user": {"10001": {"day": used(1000)}}}),
    )
    check(v.reason == "permission", "权限拒绝优先于额度")

    print("\n[5] 等级额度：更具体的层覆盖全局（等级的意义所在）")
    # 全局 5 万，VIP 等级 50 万 → VIP 用户用到 10 万仍然放行（全局不参与）
    two_layer = R(
        limits={"level": {"2": {"day": lim(500000)}}, "global": {"*": {"day": lim(50000)}}},
        usage={"user": {"10001": {"day": used(100000)}}},
        subject_level={"user": {"10001": 2}},
    )
    v = gate.evaluate(G.Subject(sender_id="10001"), two_layer)
    check(v.allow and v.layer == "user_level", "VIP 等级额度生效，全局额度不参与（否则等级形同虚设）")
    check(v.limit == 500000, "报告的是等级额度")
    # 同一份全局额度，普通用户（无等级）用到 10 万 → 被拦
    v = gate.evaluate(G.Subject(sender_id="20002"), R(limits=two_layer.limits, usage={"user": {"20002": {"day": used(100000)}}}))
    check(not v.allow and v.layer == "global", "无等级用户按全局额度拦截")
    # 好友专属额度又优先于等级额度
    v = gate.evaluate(
        G.Subject(sender_id="10001"),
        R(
            limits={"user": {"10001": {"day": lim(100)}}, "level": {"2": {"day": lim(500000)}}},
            usage={"user": {"10001": {"day": used(500)}}},
            subject_level={"user": {"10001": 2}},
        ),
    )
    check(not v.allow and v.layer == "user", "好友专属额度优先于等级额度")
    # 群等级：群归级后按群等级额度算
    v = gate.evaluate(
        G.Subject(sender_id="99999", group_id="88888"),
        R(
            limits={"level": {"3": {"day": lim(1000)}}},
            usage={"group": {"88888": {"day": used(2000)}}},
            subject_level={"group": {"88888": 3}},
        ),
    )
    check(not v.allow and v.layer == "group_level" and v.scope_type == "level", "群等级额度生效")
    check(v.used == 2000, "等级额度的用量取自「该对象自己的」计数，而不是等级合计")

    print("\n[6] 档位链与额度上限的显示口径")
    refs = G.Gate.layers_for(
        G.Subject(sender_id="10001", group_id="88888"),
        R(subject_level={"user": {"10001": 1}, "group": {"88888": 2}}),
    )
    check([r.layer for r in refs] == ["user", "user_level", "group", "group_level", "global"],
          f"群聊档位链顺序正确（实得 {[r.layer for r in refs]}）")
    check([r.layer for r in G.Gate.layers_for(G.Subject(sender_id="10001"), R())] == ["user", "global"],
          "私聊档位链只有好友专属与全局")

    print("\n[7] 提示冷却")
    cd = G.Cooldown(ttl=60)
    check(cd.allow("k", now=100.0), "首次允许")
    check(not cd.allow("k", now=130.0), "冷却期内拒绝")
    check(cd.allow("k", now=161.0), "超时后再次允许")
    check(cd.allow("other", now=130.0), "不同 key 互不影响")
    cd0 = G.Cooldown(ttl=0)
    check(cd0.allow("k", now=1.0) and cd0.allow("k", now=1.0), "ttl=0 → 不冷却")
    cd.prune(now=1000.0)
    check(cd.prune(now=1000.0) == 0, "过期条目已被清空")

    print("\n[8] 周期重置时间")
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

    print("\n[9] token 口径")

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
