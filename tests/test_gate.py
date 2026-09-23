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


def _by_scene(mapping) -> dict:
    """``{id: effect}`` → ``{id: {scene: effect}}``（扁平写法补上「通用」场景）。

    规则表内部是 ``{id: {scene: effect}}``（场景是 v6 新增的维度），但绝大多数用例
    只要「两个场景一致」的通用规则，写扁平更短。已经是 ``{scene: effect}`` 的原样保留，
    方便写「只对私聊 / 只对群聊」的场景用例。
    """
    out = {}
    for key, val in (mapping or {}).items():
        if isinstance(val, dict):
            out[str(key)] = {str(s): str(e) for s, e in val.items()}
        else:
            out[str(key)] = {"": str(val)}
    return out


def _by_scope(mapping) -> dict:
    """``{scope_type: {id: effect}}`` → 末端补场景层（``command_master`` 用）。"""
    return {str(k): _by_scene(v) for k, v in (mapping or {}).items()}


def _by_cmd(mapping) -> dict:
    """``{cmd: {scope_type: {id: effect}}}`` → 末端补场景层（``command_policy`` 用）。"""
    return {str(k): _by_scope(v) for k, v in (mapping or {}).items()}


def R(**kw) -> G.Rules:
    """构造规则快照（只填本次要用的部分）。

    带「场景」的四张表支持扁平写法（见 :func:`_by_scene` 等），
    也可以直接写 ``{scene: effect}`` 来测「只对某个场景生效」。
    """
    return G.Rules(
        effect_user=_by_scene(kw.get("effect_user")),
        effect_group=_by_scene(kw.get("effect_group")),
        effect_member=_by_scene(kw.get("effect_member")),
        level_effect=kw.get("level_effect") or {},
        level_effect_group=kw.get("level_effect_group") or {},
        subject_level=kw.get("subject_level") or {},
        subject_level_user_group=kw.get("subject_level_user_group") or {},
        limits=kw.get("limits") or {},
        usage=kw.get("usage") or {},
        level_model=kw.get("level_model") or {},
        level_switch=kw.get("level_switch") or {},
        level_switch_enabled=kw.get("level_switch_enabled") or {},
        command_policy=_by_cmd(kw.get("command_policy")),
        command_master=_by_scope(kw.get("command_master")),
        level_command_effect=kw.get("level_command_effect") or {},
        level_command_effect_group=kw.get("level_command_effect_group") or {},
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
    rules = R(level_effect={1: "deny"}, subject_level={"user": {"10001": 1}})
    v = gate.evaluate(G.Subject(sender_id="10001"), rules)
    check(not v.allow and v.layer == "user_level" and v.scope_type == "level", "好友等级 deny 生效")
    # 好友专属 allow 优先于等级 deny
    v = gate.evaluate(G.Subject(sender_id="10001"), R(effect_user=u_allow, **{
        "level_effect": {1: "deny"}, "subject_level": {"user": {"10001": 1}},
    }))
    check(v.allow and v.layer == "user", "好友专属 allow 优先于等级 deny")
    # 等级 effect=inherit → 不截断链条，继续看群规则
    v = gate.evaluate(
        G.Subject(sender_id="10001", group_id="88888"),
        R(effect_group=g_deny, level_effect={1: "inherit"}, subject_level={"user": {"10001": 1}}),
    )
    check(not v.allow and v.layer == "group", "等级 inherit（不管权限）→ 继续看群专属")
    # 群等级
    v = gate.evaluate(
        G.Subject(sender_id="99999", group_id="88888"),
        R(level_effect={7: "deny"}, subject_level={"group": {"88888": 7}}),
    )
    check(not v.allow and v.layer == "group_level", "群等级 deny 生效")
    # 群专属 allow 优先于群等级 deny
    v = gate.evaluate(
        G.Subject(sender_id="99999", group_id="88888"),
        R(effect_group=g_allow, level_effect={7: "deny"}, subject_level={"group": {"88888": 7}}),
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
        # v1.4.0：全局额度按场景取（私聊 = global:private，群聊 = global:group）
        limits={"level": {"2": {"day": lim(500000)}}, "global": {"private": {"day": lim(50000)}}},
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
        R(
            subject_level={"user": {"10001": 1}, "group": {"88888": 2}},
            subject_level_user_group={"10001": 1},
        ),
    )
    check([r.layer for r in refs] == ["member", "user", "user_level", "group", "group_level", "global"],
          f"群聊档位链顺序正确（实得 {[r.layer for r in refs]}）")
    check(refs[0].scope_id == "88888:10001", "群成员层的 scope_id 是「群号:QQ」")
    # v1.4.0：权限链的 scope_id 恒为 *（指令总权限存 global/*），额度键按场景拆分
    check(refs[-1].scope_id == "*" and refs[-1].quota_key == "group",
          "群聊全局层：权限键 * 不变，额度键是 group")
    _p = G.Gate.layers_for(G.Subject(sender_id="10001"), R())[-1]
    check(_p.scope_id == "*" and _p.quota_key == "private",
          "私聊全局层：权限键 * 不变，额度键是 private")
    check([r.layer for r in G.Gate.layers_for(G.Subject(sender_id="10001"), R())] == ["user", "global"],
          "私聊档位链只有好友专属与全局（没有群成员层）")

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

    print("\n[10] 模型路由（等级 → 主 / 备提供商）")
    rules = R(
        level_model={1: {"provider_id": "p-a", "model": "model-x", "fallback_provider_id": "p-b"}},
        subject_level={"user": {"10001": 1}},
    )
    route = G.Gate.resolve_model(G.Subject(sender_id="10001"), rules)
    check(bool(route) and route["provider_id"] == "p-a" and route["layer"] == "user_level", "私聊按好友等级取模型")
    check(G.Gate.resolve_model(G.Subject(sender_id="20002"), rules) is None, "没归级的对象不干预模型")
    # 群聊只看群等级，不回落到发言人的好友等级（否则同一个群会按人切模型）
    grp = R(
        level_model={
            1: {"provider_id": "p-user", "model": "", "fallback_provider_id": ""},
            2: {"provider_id": "p-group", "model": "", "fallback_provider_id": ""},
        },
        subject_level={"user": {"10001": 1}, "group": {"88888": 2}},
    )
    route = G.Gate.resolve_model(G.Subject(sender_id="10001", group_id="88888"), grp)
    check(bool(route) and route["provider_id"] == "p-group" and route["layer"] == "group_level",
          "群聊按群等级取模型（不按发言人）")
    check(G.Gate.resolve_model(G.Subject(sender_id="10001", group_id="77777"), grp) is None,
          "群没归级 → 不干预（不会回落到发言人的好友等级）")
    only_fb = R(
        level_model={1: {"provider_id": "", "model": "", "fallback_provider_id": "p-b"}},
        subject_level={"user": {"10001": 1}},
    )
    route = G.Gate.resolve_model(G.Subject(sender_id="10001"), only_fb)
    check(bool(route), "只配了备用提供商也算配置了模型路由")
    check(G.Gate.pick_provider(route, {"p-b"})["used_fallback"] is True, "主为空 → 直接用备用")
    empty = R(level_model={1: {"provider_id": "", "model": "", "fallback_provider_id": ""}},
              subject_level={"user": {"10001": 1}})
    check(G.Gate.resolve_model(G.Subject(sender_id="10001"), empty) is None, "三个字段都空 → 不干预")

    print("\n[11] 提供商选择与降级")
    route = {"provider_id": "p-a", "fallback_provider_id": "p-b"}
    picked = G.Gate.pick_provider(route, {"p-a", "p-b"})
    check(bool(picked) and picked["provider_id"] == "p-a" and not picked["used_fallback"], "主可用 → 用主")
    picked = G.Gate.pick_provider(route, {"p-b"})
    check(bool(picked) and picked["provider_id"] == "p-b" and picked["used_fallback"],
          "主不可用 → 用备用")
    check(G.Gate.pick_provider(route, set()) is None,
          "都不可用 → 返回 None（绝不设未知 id：AstrBot 会直接放弃本次请求）")
    check(G.Gate.pick_provider({"provider_id": "p-a", "fallback_provider_id": ""}, set()) is None,
          "无备用且主不可用 → 不干预")

    print("\n[12] 提供商熔断")
    c = G.ProviderCircuit(threshold=2, cooldown_sec=300)
    check(not c.note_failure("p-a", now=1000.0), "第 1 次失败不熔断")
    check(c.note_failure("p-a", now=1001.0), "第 2 次失败触发熔断")
    check(c.is_open("p-a", now=1100.0), "熔断期内视为不可用")
    check(not c.is_open("p-a", now=1301.0), "冷却结束自动恢复（再给一次机会）")
    check(not c.is_open("p-a", now=1302.0), "恢复后保持可用")
    check(not c.note_failure("p-b", now=1000.0) and not c.is_open("p-b", now=1001.0), "其它提供商互不影响")
    c.note_failure("p-c", now=1.0)
    c.note_failure("p-c", now=2.0)
    check("p-c" in c.open_ids(now=3.0), "open_ids 列出熔断中的提供商")
    c.note_success("p-c")
    check(not c.is_open("p-c", now=4.0), "成功一次即复位（避免偶发失败累积）")
    off = G.ProviderCircuit(threshold=1, cooldown_sec=0)
    check(not off.note_failure("p-a", now=1.0) and not off.is_open("p-a", now=1.0), "cooldown=0 → 关闭熔断")
    check(isinstance(c.snapshot(now=10.0), dict), "snapshot 可序列化给控制台")
    # 搭配使用：主被熔断 → 自动落到备用（用独立的熔断器避免与上面的时间线耦合）
    route = {"provider_id": "p-a", "fallback_provider_id": "p-b"}
    c2 = G.ProviderCircuit(threshold=1, cooldown_sec=300)
    c2.note_failure("p-a", now=1000.0)
    avail = {"p-a", "p-b"} - c2.open_ids(now=1100.0)
    picked = G.Gate.pick_provider(route, avail)
    check(bool(picked) and picked["provider_id"] == "p-b" and picked["used_fallback"],
          "主提供商熔断后由备用接管")
    avail2 = {"p-a", "p-b"} - c2.open_ids(now=1400.0)
    check(G.Gate.pick_provider(route, avail2)["provider_id"] == "p-a", "冷却结束后主提供商重新被选中")

    print("\n[13] 指令权限（每条指令一份规则）")
    cp = {
        "help": {"global": {"*": "deny"}, "user": {"10001": "allow"}, "group": {"88888": "deny"}},
        "draw": {},
    }
    rules = R(command_policy=cp)
    # evaluate() 只管 LLM，不看指令
    check(gate.evaluate(G.Subject(sender_id="99999"), rules).allow, "指令规则不影响 LLM 判定（两套独立）")
    v = gate.check_command(G.Subject(sender_id="99999"), "help", rules)
    check(not v.allow and v.reason == "command" and v.layer == "global", "某指令全局禁用 → 拦截")
    check(v.command == "help", "Verdict 带回指令名")
    v = gate.check_command(G.Subject(sender_id="10001"), "help", rules)
    check(v.allow and v.layer == "user", "好友显式放行覆盖全局禁用")
    v = gate.check_command(G.Subject(sender_id="10001", group_id="88888"), "help", rules)
    check(v.allow and v.layer == "user", "好友规则优先于群规则（最具体生效）")
    v = gate.check_command(G.Subject(sender_id="99999", group_id="88888"), "help", rules)
    check(not v.allow and v.layer == "group", "群禁用生效")
    check(gate.check_command(G.Subject(sender_id="99999"), "draw", rules).allow, "没有规则的指令互不影响")
    check(gate.check_command(G.Subject(sender_id="99999"), "", rules).allow, "空指令名 → 放行（不误伤）")
    # 默认策略（白名单模式）
    deny_default = make_gate(default_command_effect="deny")
    v = deny_default.check_command(G.Subject(sender_id="99999"), "draw", R())
    check(not v.allow and v.reason == "command", "默认禁止：未配规则的指令被拦")
    check(deny_default.check_command(G.Subject(sender_id="10001"), "help", rules).allow, "白名单模式下显式放行可用")
    # 与 LLM 的默认策略互不影响
    check(make_gate(default_effect="deny").check_command(G.Subject(sender_id="99999"), "draw", R()).allow,
          "LLM 全局默认设成禁止后，指令仍然默认可用（两套默认互不影响）")

    print("\n[14] 指令权限 · 对象级总权限（好友 / 群 / 等级）")
    master = {"group": {"88888": "deny"}, "user": {"10001": "allow"}}
    m_rules = R(
        command_master=master,
        level_command_effect={3: "deny"},
        subject_level={"user": {"10002": 3}},
    )
    v = gate.check_command(G.Subject(sender_id="99999", group_id="88888"), "draw", m_rules)
    check(not v.allow and v.layer == "group", "群级指令总权限=禁止 → 该群任何指令都拦")
    v = gate.check_command(G.Subject(sender_id="10001", group_id="88888"), "draw", m_rules)
    check(v.allow and v.layer == "user", "好友专属「放行」更具体 → 在禁用的群里开白名单成功")
    v = gate.check_command(G.Subject(sender_id="10002"), "draw", m_rules)
    check(not v.allow and v.layer == "user_level", "等级默认指令权限=禁止 → 该等级所有人不能用指令")
    check(gate.check_command(G.Subject(sender_id="10003"), "draw", m_rules).allow, "没归级的其它人不受影响")
    check(gate.check_command(G.Subject(sender_id="10002"), "help", m_rules).allow is False,
          "等级禁止时任何指令都被拦（不是只拦某一条）")

    # 两段式的优先级：对象级 deny 是硬拦截；对象级 allow 之后仍受单条指令规则约束
    both = R(command_master={"group": {"88888": "deny"}}, command_policy={"draw": {"user": {"10003": "deny"}}})
    v = gate.check_command(G.Subject(sender_id="10003"), "draw", both)
    check(not v.allow and v.layer == "user", "单条指令规则照常生效")
    v = gate.check_command(G.Subject(sender_id="99999", group_id="88888"), "draw", both)
    check(not v.allow and v.layer == "group", "对象级禁止优先于单条指令（硬拦截）")
    allowed = R(
        command_master={"user": {"10003": "allow"}},
        command_policy={"draw": {"global": {"*": "deny"}}},
    )
    v = gate.check_command(G.Subject(sender_id="10003"), "draw", allowed)
    check(not v.allow and v.layer == "global", "对象级放行后，单条指令的全局禁止仍然生效")
    check(gate.check_command(G.Subject(sender_id="10003"), "help", allowed).allow, "其它指令不受影响")
    # 等级层不参与单条指令链：等级只管「整体能不能用指令」
    lv_only = R(level_command_effect={1: "deny"}, subject_level={"user": {"10001": 1}},
                command_policy={"draw": {"user": {"10001": "allow"}}})
    v = gate.check_command(G.Subject(sender_id="10001"), "draw", lv_only)
    check(not v.allow and v.layer == "user_level", "等级禁止指令时，单条指令的放行压不过它（需在对象级开白名单）")

    print("\n[15] 场景维度：私聊与群聊分开")
    sc = R(effect_user={"10001": {"private": "deny"}})
    check(not gate.check_permission(G.Subject(sender_id="10001"), sc).allow, "只禁私聊 → 私聊被拒")
    check(
        gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), sc).allow,
        "只禁私聊 → 群里照用（这就是「两个维度」的意义）",
    )
    sc2 = R(effect_user={"10001": {"group": "deny"}})
    check(gate.check_permission(G.Subject(sender_id="10001"), sc2).allow, "只禁群聊 → 私聊不受影响")
    check(
        not gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), sc2).allow,
        "只禁群聊 → 群里被拒",
    )
    sc3 = R(effect_user={"10001": {"": "deny"}})
    check(not gate.check_permission(G.Subject(sender_id="10001"), sc3).allow, "通用规则 → 私聊被拒")
    check(
        not gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), sc3).allow,
        "通用规则 → 群聊也被拒（v5 旧数据的行为不变）",
    )
    sc4 = R(effect_user={"10001": {"": "deny", "group": "allow"}})
    check(not gate.check_permission(G.Subject(sender_id="10001"), sc4).allow, "通用禁止在私聊仍然生效")
    check(
        gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), sc4).allow,
        "群聊专属「放行」覆盖通用「禁止」",
    )

    # 好友等级分场景（v1.2.2 语义）：群聊默认【不使用】好友等级（走群的档位），
    # 只有显式配了「群聊专属等级」才参与判定
    sl = R(
        subject_level={"user": {"10002": 3}, "group": {"88888": 9}},
        subject_level_user_group={"10002": 4},
        level_effect={3: "deny", 4: "allow", 9: "allow"},
    )
    check(not gate.check_permission(G.Subject(sender_id="10002"), sl).allow, "分场景归级：私聊走私聊等级")
    check(
        gate.check_permission(G.Subject(sender_id="10002", group_id="88888"), sl).allow,
        "分场景归级：群聊走群聊专属等级",
    )
    sl2 = R(
        subject_level={"user": {"10002": 3}, "group": {"88888": 9}},
        level_effect={3: "deny", 9: "allow"},
    )
    check(not gate.check_permission(G.Subject(sender_id="10002"), sl2).allow, "私聊仍走好友等级（deny）")
    check(
        gate.check_permission(G.Subject(sender_id="10002", group_id="88888"), sl2).allow,
        "没配群聊专属 → 群聊【不使用】好友等级，走群等级（allow）",
    )

    # 等级键是全局唯一的 level_id（**不按 kind 查表**）：好友的「群聊等级」可以指向
    # 一个**群聊等级**（限额页里 kind=group 的那套），群聊判定必须照常生效 ——
    # 旧的 (kind, id) 键会把这种引用整层查空，界面看着配了、实际静默不生效。
    cross = R(
        level_effect={4: "allow", 6: "deny"},  # 4 = 好友等级；6 = 群聊等级
        subject_level={"user": {"10006": 4}},
        subject_level_user_group={"10006": 6},
    )
    check(
        not gate.check_permission(G.Subject(sender_id="10006", group_id="88888"), cross).allow,
        "好友的群聊等级指向群聊等级 → 群聊按它生效（键是 level_id，与 kind 无关）",
    )
    check(gate.check_permission(G.Subject(sender_id="10006"), cross).allow, "私聊仍走好友等级（互不串）")

    # 好友等级参与群聊判定的前提：配了「群聊专属等级」；场景值没配（inherit）回落主值
    lv = R(
        level_effect={3: "deny"},
        subject_level={"user": {"10002": 3}},
        subject_level_user_group={"10002": 3},
    )
    check(not gate.check_permission(G.Subject(sender_id="10002"), lv).allow, "等级主值=禁止 → 私聊被拒")
    check(
        not gate.check_permission(G.Subject(sender_id="10002", group_id="88888"), lv).allow,
        "配了群聊专属等级 → 群聊参与判定并使用主值",
    )
    lv2 = R(
        level_effect={3: "deny"},
        level_effect_group={3: "allow"},
        subject_level={"user": {"10002": 3}},
        subject_level_user_group={"10002": 3},
    )
    check(not gate.check_permission(G.Subject(sender_id="10002"), lv2).allow, "私聊仍按主值禁止")
    v = gate.check_permission(G.Subject(sender_id="10002", group_id="88888"), lv2)
    check(v.allow and v.layer == "user_level", "群聊专属放行 → 群里可用，且来源仍是好友等级")
    # 真实数据形态（v1.0.0 回归）：reload 出来的「群聊专属值」是字符串 "inherit"，
    # 不是「键不存在」——回落逻辑必须把这两种形态都当作「没配」
    lv3 = R(
        level_effect={3: "deny"},
        level_effect_group={3: "inherit"},
        subject_level={"user": {"10002": 3}},
        subject_level_user_group={"10002": 3},
    )
    check(
        not gate.check_permission(G.Subject(sender_id="10002", group_id="88888"), lv3).allow,
        "群聊专属值=字符串 inherit → 回落主值（此前被当没配而漏拦）",
    )
    lv4 = R(
        level_effect={3: "allow"},
        level_command_effect={3: "deny"},
        level_command_effect_group={3: "inherit"},
        subject_level={"user": {"10002": 3}},
        subject_level_user_group={"10002": 3},
    )
    v = gate.check_command(G.Subject(sender_id="10002", group_id="88888"), "draw", lv4)
    check(not v.allow and v.layer == "user_level", "指令权限的群聊 inherit 同样回落主值")
    lvg = R(level_effect={5: "deny"}, subject_level={"group": {"88888": 5}})
    check(
        not gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), lvg).allow,
        "群等级=禁止 → 群里被拒",
    )
    check(gate.check_permission(G.Subject(sender_id="10001"), lvg).allow, "群等级不影响私聊")

    # 指令权限（对象级总权限、单条指令）同样分场景
    cm = R(command_master={"user": {"10001": {"private": "deny"}}})
    check(not gate.check_command(G.Subject(sender_id="10001"), "draw", cm).allow, "指令：私聊禁止 → 私聊用不了")
    check(
        gate.check_command(G.Subject(sender_id="10001", group_id="88888"), "draw", cm).allow,
        "指令：只禁私聊 → 群里照用",
    )
    lvc = R(
        level_command_effect={4: "deny"},
        level_command_effect_group={4: "allow"},
        subject_level={"user": {"10003": 4}},
    )
    check(
        not gate.check_command(G.Subject(sender_id="10003"), "draw", lvc).allow,
        "等级指令权限：私聊按主值禁止",
    )
    check(
        gate.check_command(G.Subject(sender_id="10003", group_id="88888"), "draw", lvc).allow,
        "等级指令权限：群聊专属放行 → 群里能用指令",
    )
    cp = R(command_policy={"draw": {"user": {"10001": {"group": "deny"}}}})
    check(gate.check_command(G.Subject(sender_id="10001"), "draw", cp).allow, "单条指令：只禁群聊 → 私聊可用")
    check(
        not gate.check_command(G.Subject(sender_id="10001", group_id="88888"), "draw", cp).allow,
        "单条指令：群里被禁",
    )

    print("\n[16] 群成员级管控（member 层）")
    m = R(effect_member={"88888:10001": "deny"})
    v = gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), m)
    check(not v.allow and v.layer == "member", "群成员专属禁止 → 群里被拒，来源是 member 层")
    check(gate.check_permission(G.Subject(sender_id="10001", group_id="99999"), m).allow,
          "只对这个群生效：换个群不受影响")
    check(gate.check_permission(G.Subject(sender_id="10001"), m).allow, "私聊不受群成员规则影响")
    # 成员层最关键的能力：整群禁止，给个别成员放行
    m2 = R(effect_group={"88888": "deny"}, effect_member={"88888:10001": "allow"})
    check(gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), m2).allow,
          "整群禁止 + 该成员放行 → 他能用（成员层比群层更具体）")
    check(not gate.check_permission(G.Subject(sender_id="10002", group_id="88888"), m2).allow,
          "同群其它成员仍被群规则拦下")
    # 也能压过好友级与等级（成员层是最具体的一层）
    m3 = R(
        effect_user={"10001": "deny"},
        level_effect={3: "deny"},
        subject_level={"user": {"10001": 3}},
        effect_member={"88888:10001": "allow"},
    )
    check(gate.check_permission(G.Subject(sender_id="10001", group_id="88888"), m3).allow,
          "成员层压过好友专属与好友等级")
    check(not gate.check_permission(G.Subject(sender_id="10001"), m3).allow,
          "同一套规则在私聊里仍按好友专属禁止")
    # 指令权限同理
    mc = R(
        command_master={"group": {"88888": "deny"}, "member": {"88888:10001": "allow"}},
    )
    check(gate.check_command(G.Subject(sender_id="10001", group_id="88888"), "draw", mc).allow,
          "指令：群禁但该成员放行 → 他能用指令")
    check(not gate.check_command(G.Subject(sender_id="10002", group_id="88888"), "draw", mc).allow,
          "指令：同群其它成员仍被群规则拦下")
    mc2 = R(command_master={"member": {"88888:10001": "deny"}})
    check(not gate.check_command(G.Subject(sender_id="10001", group_id="88888"), "draw", mc2).allow,
          "指令：只禁该成员 → 他被拦")
    check(gate.check_command(G.Subject(sender_id="10001"), "draw", mc2).allow, "指令：私聊不受影响")

    print("\n[17] 按成员的额度（member 维度）")
    mq = R(
        limits={"member": {"88888:10001": {"day": lim(100)}}},
        usage={"member": {"88888:10001": {"day": used(90)}}},
    )
    v = gate.check_quota(G.Subject(sender_id="10001", group_id="88888"), mq)
    check(v.allow and v.layer == "member", "未超限 → 放行，且生效档位是「群成员专属」")
    check(v.limit == 100 and v.used == 90, f"报告的是成员额度（实得 {v.used}/{v.limit}）")
    mq2 = R(
        limits={"member": {"88888:10001": {"day": lim(100)}}},
        usage={"member": {"88888:10001": {"day": used(100)}}},
    )
    v = gate.check_quota(G.Subject(sender_id="10001", group_id="88888"), mq2)
    check(not v.allow and v.layer == "member", "达到上限 → 拦下，来源 member 层")
    check(gate.check_quota(G.Subject(sender_id="10001", group_id="99999"), mq2).allow,
          "同一份成员额度只作用于配置的那个群")
    check(gate.check_quota(G.Subject(sender_id="10002", group_id="88888"), mq2).allow,
          "同群别的成员不受影响")
    mq3 = R(
        limits={
            "member": {"88888:10001": {"day": lim(100)}},
            "user": {"10001": {"day": lim(10)}},
            "group": {"88888": {"day": lim(1)}},
        },
        usage={
            "member": {"88888:10001": {"day": used(50)}},
            "user": {"10001": {"day": used(9999)}},
            "group": {"88888": {"day": used(9999)}},
        },
    )
    v = gate.check_quota(G.Subject(sender_id="10001", group_id="88888"), mq3)
    check(v.allow and v.layer == "member", "成员额度优先于好友与群额度（各层用量互不干扰）")
    v = gate.check_quota(G.Subject(sender_id="10001"), mq3)
    check(not v.allow and v.layer == "user", "私聊里没有成员层 → 仍按好友额度拦下")
    mq4 = R(
        limits={"member": {"88888:10001": {"day": lim(100)}}},
        usage={"user": {"10001": {"day": used(9999)}}},  # 跨群合计很大，但他在这个群里没用过
    )
    check(gate.check_quota(G.Subject(sender_id="10001", group_id="88888"), mq4).allow,
          "member 层取「他在这个群」的用量，不受跨群合计影响")
    # 成员层配了额度后就不再往更粗的层回落（与既有的「占位」语义一致）
    mq5 = R(
        limits={
            "member": {"88888:10001": {"day": lim(100)}},
            "group": {"88888": {"day": lim(10)}},
        },
        usage={
            "member": {"88888:10001": {"day": used(10)}},
            "group": {"88888": {"day": used(9999)}},
        },
    )
    check(gate.check_quota(G.Subject(sender_id="10001", group_id="88888"), mq5).allow,
          "成员层有额度 → 群层的超额不再影响他（档位占位语义）")

    print("\n[16] 指令总权限的成员 / 全局层（v1.0.0：reload 必须装上这两类）")
    m_only = R(command_master={"member": {"88888:10001": "deny"}})
    v = gate.check_command(G.Subject(sender_id="10001", group_id="88888"), "draw", m_only)
    check(not v.allow and v.layer == "member", "成员层指令总权限=禁止 → 只拦他在这个群的指令")
    check(gate.check_command(G.Subject(sender_id="10001"), "draw", m_only).allow,
          "同一人私聊不受成员层规则影响（成员层只在群聊参与）")
    m_level = R(
        subject_level={"user": {"10002": 3}},
        subject_level_user_group={"10002": 3},
        level_command_effect={3: "deny"},
    )
    v = gate.check_command(G.Subject(sender_id="10002", group_id="88888"), "draw", m_level)
    check(not v.allow and v.layer == "user_level", "成员/全局层之外，等级禁止照常生效")
    g_deny = R(command_master={"global": {"*": "deny"}})
    v = gate.check_command(G.Subject(sender_id="10003"), "draw", g_deny)
    check(not v.allow and v.layer == "global", "全局指令总权限=禁止 → 谁都用不了（兜底层）")
    # 全局层的「放行」不是万能通行证：它只解除对象级拦截，第二段（单条指令/默认策略）照走
    g_master = R(command_master={"global": {"*": "allow"}})
    g_gate = make_gate(default_command_effect="deny")
    check(
        not g_gate.check_command(G.Subject(sender_id="10001"), "draw", g_master).allow,
        "全局指令总权限=放行 ≠ 白名单通行证：默认策略=禁止时单条指令仍被拦",
    )
    check(gate.check_command(G.Subject(sender_id="10001"), "draw", g_master).allow,
          "默认策略=放行时，全局放行不改变结果")

    print("\n[18] 可切换模型（/切换模型 的名单归属，v9）")
    sw = R(
        level_switch={1: ("p-a", "p-b"), 2: ("p-c",)},
        subject_level={"user": {"10001": 1}, "group": {"88888": 2}},
    )
    found = G.Gate.model_level_of(G.Subject(sender_id="10001"), sw)
    check(found == ("user", 1, "user_level"), f"私聊按好友等级取名单（实得 {found}）")
    found = G.Gate.model_level_of(G.Subject(sender_id="10001", group_id="88888"), sw)
    check(found == ("group", 2, "group_level"), "群聊按群等级取名单（与模型路由同口径，不按发言人）")
    check(G.Gate.model_level_of(G.Subject(sender_id="10009"), sw) is None, "没归级 → 没有名单（指令会提示未开放）")
    check(
        G.Gate.switch_options(sw, 1) == ("p-a", "p-b"),
        "名单按配置顺序返回（用户看到的序号要稳定，不能每次都变）",
    )
    check(G.Gate.switch_options(sw, 99) == (), "等级没配名单 → 空元组")
    check(G.Gate.switch_options(sw, None) == (), "level_id 为空 / 非法 → 空元组（不抛异常）")
    # 名单与「等级模型路由」互相独立：配了主模型不等于开放切换
    route_only = R(
        level_model={1: {"provider_id": "p-a", "fallback_provider_id": ""}},
        subject_level={"user": {"10001": 1}},
    )
    check(bool(G.Gate.resolve_model(G.Subject(sender_id="10001"), route_only)), "等级模型路由照常解析")

    print("\n[19] 「允许切换模型」开关（v10，默认关 = 只读）")
    check(G.Gate.switch_enabled(R(), 1) is False, "没配过的等级 → 不允许（默认关）")
    check(G.Gate.switch_enabled(R(level_switch_enabled={1: False}), 1) is False, "显式关 → 不允许")
    check(G.Gate.switch_enabled(R(level_switch_enabled={1: True}), 1) is True, "显式开 → 允许")
    check(G.Gate.switch_enabled(R(level_switch_enabled={1: True}), 2) is False, "开关是按等级各管各的")
    check(G.Gate.switch_enabled(R(level_switch_enabled={1: True}), None) is False, "没归级 → 不允许")
    check(G.Gate.switch_enabled(R(level_switch_enabled={1: True}), "什么鬼") is False, "脏 level_id → 不允许（不抛异常）")
    # 开关与名单是两个独立的东西：开了才允许切，名单只决定「能挑哪些」
    both = R(level_switch={1: ("p-a",)}, level_switch_enabled={1: True})
    check(
        G.Gate.switch_enabled(both, 1) and G.Gate.switch_options(both, 1) == ("p-a",),
        "开关与名单互相独立（开着但没配名单 = 用兜底三项）",
    )
    check(
        G.Gate.switch_enabled(R(level_switch={1: ("p-a",)}), 1) is False,
        "只配了名单、没开开关 → 仍然只读（v1.3.3 的旧语义不再生效）",
    )

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
