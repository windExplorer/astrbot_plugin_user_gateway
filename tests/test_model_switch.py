"""``/切换模型`` 的自检：名单组装、当前标记、按序号切换与候选会话。

用法：
    uv run --no-project --with aiosqlite --with pillow python tests/test_model_switch.py

（``--with aiosqlite`` 是因为 ``model_switch`` 会 import ``store`` 取 feature 常量，
而 ``store`` 在导入时就要求 aiosqlite —— 与运行期一致，不做特殊处理。）

为什么要在测试里桩掉 ``astrbot``：``model_switch`` 会 ``from astrbot.api import logger``，
而本仓库的自检刻意**不依赖 AstrBot 环境**（判定内核 / 存储层都是这样跑的）。
这里只塞一个最小 logger 桩，其余全是真代码（真 Gate / 真 Rules / 真渲染）。

覆盖：
- 名单 =「等级配置的名单 + 我的专属模型 + 当前生效的那个」，顺序稳定；
- 当前使用的那个被标记（含「不在名单里但正在用」的情况）；
- 序号切换写的是 ``feature=model_choice`` + 对应场景（群 / 私聊互不影响）；
- 序号 0 = 恢复默认（删掉自己的选择）；
- 序号会话有 TTL、且**绑定发言人**（群里别人发的数字不会误触发）；
- 卡片能渲染出 PNG；渲染不出来时有纯文本兜底。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- 最小 astrbot 桩（必须在 import model_switch 之前） ---
if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("test")  # type: ignore[attr-defined]
    _astrbot.api = _api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

import gate as G  # noqa: E402
import model_card  # noqa: E402
import model_switch as MS  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _failures.append(label)


class FakeStore:
    """只实现本测试用到的存储接口，并把写操作记下来供断言。"""

    def __init__(
        self, levels: dict[int, dict] | None = None, stats: dict | None = None
    ) -> None:
        self.ready = True
        self.levels = levels or {}
        self.policies: list[dict] = []
        self.audits: list[tuple] = []
        # 今日用量统计：None = 读不到（today_of 必须安静地退化成空）
        self.stats = stats

    async def get_level(self, level_id: int) -> dict | None:
        return self.levels.get(int(level_id))

    async def subject_stats(self, subject_type: str, subject_id: str, from_ts: int, to_ts: int):
        if self.stats is None:
            raise RuntimeError("统计不可用（模拟旧库 / 查询失败）")
        return {"totals": dict(self.stats)}

    async def set_policy(self, scope_type, scope_id, effect, feature="llm", note="", scene=""):
        self.policies.append(
            {
                "scope_type": scope_type,
                "scope_id": scope_id,
                "effect": effect,
                "feature": feature,
                "scene": scene,
            }
        )

    async def log_audit(self, actor, action, payload="") -> None:
        self.audits.append((actor, action, payload))


class FakeEvent:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def get_message_str(self) -> str:
        return self._text


class ImageFontStub:
    """给 ``model_card._wrap`` 用的最小字体桩：每个字符固定 10px（不依赖 Pillow）。"""

    size = 20

    def getlength(self, text: str) -> float:
        return 10.0 * len(str(text or ""))


class FakeProvider:
    def __init__(self, pid: str, name: str, model: str) -> None:
        self.provider_config = {"id": pid, "name": name}
        self._model = model

    def get_model(self) -> str:
        return self._model


class FakePlugin:
    """把插件里被 model_switch 用到的部分抽出来（其余不实现，用到就会报错）。"""

    def __init__(
        self,
        *,
        cfg: dict | None = None,
        providers: list[FakeProvider] | None = None,
        default_provider: str = "",
    ) -> None:
        self.cfg = {
            "model_switch_enabled": True,
            "model_switch_timeout_sec": 60,
            "model_card_font": "",
        }
        self.cfg.update(cfg or {})
        self.store = FakeStore()
        self.providers = providers if providers is not None else []
        # AstrBot 的「系统默认」提供商（兜底三项之一）；空 = 拿不到（离线 / 没配）
        self.default_provider = str(default_provider or "")
        self.circuit = G.ProviderCircuit(threshold=2, cooldown_sec=300)
        self.gate = G.Gate(lambda k, d=None: self.cfg.get(k, d))
        self.context = types.SimpleNamespace(get_using_provider_async=self._no_default)
        self.avatars = None
        self.sent: list[str] = []
        self.images: list[bytes] = []
        self.reloads = 0
        # 规则快照（由 main.reload_rules 维护，这里手工给）
        self._model_choice: dict[str, dict[str, str]] = {}
        self._subject_model: dict[str, str] = {}
        self._rules_obj = G.Rules()

    async def _no_default(self, umo: str):
        if not self.default_provider:
            return None
        for p in self.providers:
            if p.provider_config.get("id") == self.default_provider:
                return p
        return FakeProvider(self.default_provider, "系统默认", "sys-default")

    def _cfg(self, key, default=None):
        return self.cfg.get(key, default)

    def _rules(self):
        return self._rules_obj

    def provider_map(self) -> dict:
        out = {}
        for p in self.providers:
            pid = str(p.provider_config.get("id"))
            name = str(p.provider_config.get("name") or pid)
            model = p.get_model()
            out[pid] = {
                "id": pid,
                "name": name,
                "model": model,
                "label": f"{name} · {model}" if model and name != model else name or model or pid,
                "type": "",
                "modalities": [],
            }
        return out

    def provider_info(self, pid: str) -> dict:
        return self.provider_map().get(
            str(pid), {"id": pid, "label": str(pid), "model": "", "name": pid}
        )

    def provider_ids(self) -> set[str]:
        return set(self.provider_map())

    async def reload_rules(self) -> None:
        self.reloads += 1

    async def _send(self, event, text: str) -> None:
        self.sent.append(str(text))

    async def _send_image(self, event, png: bytes) -> bool:
        self.images.append(png)
        return True


def provider_rows() -> list[FakeProvider]:
    return [
        FakeProvider("p-a", "OpenAI", "gpt-4o"),
        FakeProvider("p-b", "Claude", "claude-3-7"),
        FakeProvider("p-c", "本地", "qwen3"),
    ]


def rules(**kw) -> G.Rules:
    return G.Rules(
        subject_level=kw.get("subject_level") or {},
        level_switch=kw.get("level_switch") or {},
        level_switch_enabled=kw.get("level_switch_enabled") or {},
        level_model=kw.get("level_model") or {},
    )


def subject(sender="10001", group="") -> G.Subject:
    return G.Subject(sender_id=sender, group_id=group, umo=f"umo:{group or sender}")


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("\n[1] 名单组装：等级名单 + 专属模型 + 当前使用的")
    plugin = FakePlugin(providers=provider_rows())
    plugin.store.levels = {1: {"id": 1, "kind": "user", "name": "VIP"}}
    plugin._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={1: ("p-a", "p-b")},
        level_switch_enabled={1: True},
        level_model={1: {"provider_id": "p-a", "fallback_provider_id": "p-c"}},
    )
    sw = MS.ModelSwitcher(plugin)
    data = await sw.describe(subject())
    check([o["provider_id"] for o in data["options"]] == ["p-a", "p-b"], "只用等级名单（不掺无关模型）")
    check([o["index"] for o in data["options"]] == [1, 2], "序号从 1 开始且连续")
    check(data["current_id"] == "p-a", "当前模型 = 等级主模型")
    check(data["current_source"] == MS.SOURCE_LEVEL, "来源标注为「分组主模型」")
    check(data["options"][0]["current"] and not data["options"][1]["current"], "只有当前那个被标记")
    check(data["scene"] == "private" and data["level_kind"] == "user", "私聊场景 + 好友等级")
    check(sw.group_label(data) == "VIP（好友等级）", "分组展示名带上等级名")
    check(data["can_switch"] and not data["readonly_reason"], "等级开关打开 → 可切换")

    print("\n[2] 专属模型 / 用户已选的模型都会进名单")
    plugin._subject_model = {"10001": "p-c"}
    data = await sw.describe(subject())
    check([o["provider_id"] for o in data["options"]] == ["p-a", "p-b", "p-c"],
          "专属模型追加在名单之后（不动既有序号）")
    check(data["options"][2]["own"], "专属模型带 own 标记（卡片打「专属模型」标签）")
    check(data["options"][2]["current"] and not data["options"][0]["current"],
          "专属模型就是当前使用的那个（它优先于等级主模型）")
    check(data["current_id"] == "p-c" and data["current_source"] == MS.SOURCE_OWN,
          "专属模型优先于等级主模型（与 route_model 的口径一致）")

    plugin._model_choice = {"10001": {"private": "p-b"}}
    data = await sw.describe(subject())
    check(data["current_id"] == "p-b" and data["current_source"] == MS.SOURCE_CHOICE,
          "用户自己切换的模型优先级最高")
    check([o for o in data["options"] if o["current"]][0]["provider_id"] == "p-b", "当前标记跟着走")

    print("\n[3] 当前模型不在名单里也要列出来（否则卡片上没有一行能标「当前使用」）")
    plugin._model_choice = {"10001": {"private": "p-x"}}
    plugin.providers.append(FakeProvider("p-x", "临时", "temp-model"))
    data = await sw.describe(subject())
    check("p-x" in [o["provider_id"] for o in data["options"]], "当前用的那个被补进名单")
    row = [o for o in data["options"] if o["provider_id"] == "p-x"][0]
    check(row["current"] and "你自己切换的" in row["note"], "并标注来源")

    print("\n[3.1] 用户选的那个当前不可用 → 当前标记落到实际会走的那一层")
    plugin._subject_model = {}
    plugin._model_choice = {"10001": {"private": "p-down"}}  # 未加载的提供商
    data = await sw.describe(subject())
    check(data["current_id"] == "p-a" and data["current_source"] == MS.SOURCE_LEVEL,
          "他选的不可用 → 实际走等级主模型（卡片不能撒谎说当前用的是那个）")
    row = [o for o in data["options"] if o["provider_id"] == "p-down"][0]
    check(row["unavailable"] and not row["current"], "他切过的那个仍然列出来，并标「暂不可用」")

    print("\n[4] 没配名单 → 兜底三项（当前 / 系统默认 / 备用）")
    plugin3 = FakePlugin(providers=provider_rows(), default_provider="p-c")
    plugin3.store.levels = {1: {"id": 1, "kind": "user", "name": "VIP"}}
    plugin3._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={},
        level_switch_enabled={1: True},
        level_model={1: {"provider_id": "p-a", "fallback_provider_id": "p-b"}},
    )
    sw3 = MS.ModelSwitcher(plugin3)
    data = await sw3.describe(subject())
    check(
        [o["provider_id"] for o in data["options"]] == ["p-a", "p-c", "p-b"],
        f"兜底三项：当前生效 / 系统默认 / 备用（实得 {[o['provider_id'] for o in data['options']]}）",
    )
    check(data["options"][0]["current"], "第一项（当前生效）被标成「当前使用」")
    check(data["can_switch"], "没配名单不影响「允许切换」——名单只决定能挑哪些")

    # 没有任何等级模型配置时，当前就是系统默认，不应出现两条一样的
    plugin3b = FakePlugin(providers=provider_rows(), default_provider="p-c")
    plugin3b._rules_obj = rules(
        subject_level={"user": {"10001": 1}}, level_switch_enabled={1: True}
    )
    data = await MS.ModelSwitcher(plugin3b).describe(subject())
    check([o["provider_id"] for o in data["options"]] == ["p-c"],
          "只有系统默认时 → 兜底去重后只剩一条（且是当前使用）")

    print("\n[4.1] 没归级 / 没有任何候选 → 什么都不展示（指令会说清楚）")
    plugin2 = FakePlugin(providers=provider_rows())
    plugin2._rules_obj = rules(subject_level={}, level_switch={})
    data = await MS.ModelSwitcher(plugin2).describe(subject())
    check(data["open"] is False and data["options"] == [], "没归级且没有系统默认 → 没有任何选项")
    check(data["can_switch"] is False, "没归级 → 默认不可切换")
    plugin3c = FakePlugin(providers=provider_rows(), default_provider="p-a")
    plugin3c._rules_obj = rules(subject_level={}, level_switch={})
    data = await MS.ModelSwitcher(plugin3c).describe(subject())
    check([o["provider_id"] for o in data["options"]] == ["p-a"],
          "没归级但有系统默认 → 至少能告诉他现在走的是哪个（只读）")

    print("\n[5] 群聊：按群等级取名单（与模型路由同口径）")
    plugin4 = FakePlugin(providers=provider_rows())
    plugin4.store.levels = {
        2: {"id": 2, "kind": "group", "name": "大群"},
        9: {"id": 9, "kind": "user", "name": "好朋友"},
    }
    plugin4._rules_obj = rules(
        subject_level={"user": {"10001": 9}, "group": {"88888": 2}},
        level_switch={2: ("p-b",), 9: ("p-a",)},
        level_switch_enabled={2: True, 9: True},
    )
    sw4 = MS.ModelSwitcher(plugin4)
    data = await sw4.describe(subject(group="88888"))
    check(data["scene"] == "group" and data["level_kind"] == "group", "群聊场景 + 群等级")
    check([o["provider_id"] for o in data["options"]] == ["p-b"], "用群等级的名单，不用发言人的好友等级")
    data = await sw4.describe(subject())
    check([o["provider_id"] for o in data["options"]] == ["p-a"], "私聊仍用好友等级的名单")

    print("\n[5.1] 只读：等级开关关着（默认）→ 能看不能切")
    plugin5a = FakePlugin(providers=provider_rows(), default_provider="p-c")
    plugin5a.store.levels = {1: {"id": 1, "kind": "user", "name": "VIP"}}
    plugin5a._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={1: ("p-a", "p-b")},  # 配了名单，但开关没开
        level_model={1: {"provider_id": "p-a", "fallback_provider_id": "p-b"}},
    )
    sw5a = MS.ModelSwitcher(plugin5a)
    data = await sw5a.describe(subject())
    check(data["can_switch"] is False, "开关默认关 → 不可切换")
    check(data["readonly_reason"] == MS.REASON_LEVEL_OFF, "原因文案是「所在分组未开放模型切换」")
    check([o["provider_id"] for o in data["options"]] == ["p-a", "p-b"], "只读也要能看到候选列表")
    check("只读" in sw5a.footer_of(data), "卡片底部写明只读原因")
    check("只读" in sw5a.text_list(data, 60), "文本兜底同样写明只读")
    plugin5a.sent.clear()
    await sw5a.handle_command(plugin5a, FakeEvent("切换模型"), subject(), "")
    check(bool(plugin5a.images), "只读时仍然发卡片（需求：没配名单也要展示当前/系统默认/备用）")
    check(sw5a.peek(subject()) is None, "只读**不记序号会话**（此时发的数字不该被这条指令截走）")
    check(await sw5a.handle_index(plugin5a, FakeEvent("1"), subject()) is False, "只读时回数字不拦不答")
    plugin5a.sent.clear()
    await sw5a.handle_command(plugin5a, FakeEvent("切换模型 2"), subject(), "2")
    check(any("只读" in s for s in plugin5a.sent), "只读时 /切换模型 N 直接说明不能切")
    check(not plugin5a.store.policies, "只读时不会写任何库")
    # 开关打开后同样的配置就能切
    plugin5a._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={1: ("p-a", "p-b")},
        level_switch_enabled={1: True},
        level_model={1: {"provider_id": "p-a", "fallback_provider_id": "p-b"}},
    )
    data = await sw5a.describe(subject())
    check(data["can_switch"] is True and not data["readonly_reason"], "开关打开 → 可切换")
    check("回复序号" in sw5a.footer_of(data), "卡片底部改成操作说明")

    print("\n[5.2] 群聊仅管理员可用")
    plugin5b = FakePlugin(providers=provider_rows())
    plugin5b.store.levels = {2: {"id": 2, "kind": "group", "name": "大群"}}
    plugin5b._rules_obj = rules(
        subject_level={"group": {"88888": 2}},
        level_switch={2: ("p-a", "p-b")},
        level_switch_enabled={2: True},
    )
    sw5b = MS.ModelSwitcher(plugin5b)
    member = subject(group="88888")
    data = await sw5b.describe(member)
    check(data["group_restricted"] and data["can_switch"] is False, "群聊里普通成员不可切换")
    check(data["readonly_reason"] == MS.REASON_GROUP_ONLY_ADMIN, "原因文案是「群里只有管理员能切换模型」")
    plugin5b.sent.clear()
    await sw5b.handle_command(plugin5b, FakeEvent("切换模型"), member, "")
    check(plugin5b.sent == [MS.GROUP_REFUSE], "普通成员在群里发指令 → 一句话拒绝，不发卡片")
    check(plugin5b.images == [], "拒绝时不发卡片（需求：群聊仅管理员能用）")
    check(sw5b.peek(member) is None, "拒绝时不留下序号会话")
    # 管理员：豁免「群聊仅管理员」与「等级开关」两条限制
    admin = G.Subject(sender_id="10001", group_id="88888", umo="umo:88888", is_admin=True)
    data = await sw5b.describe(admin)
    check(data["can_switch"] and data["is_admin"], "管理员在群里可以切")
    plugin5b.sent.clear()
    await sw5b.handle_command(plugin5b, FakeEvent("切换模型"), admin, "")
    check(bool(plugin5b.images) and plugin5b.sent == [], "管理员拿到的是可操作的卡片")
    check(sw5b.peek(admin) is not None, "管理员的序号会话已建立")
    res = await sw5b.apply(admin, 1)
    check(res["ok"] and plugin5b.store.policies[-1]["scene"] == "group",
          "管理员切换按群聊场景落库（只影响他自己在群里的请求）")
    # 等级开关关着时，管理员照样能切（「管理员不受影响」）
    plugin5b._rules_obj = rules(
        subject_level={"group": {"88888": 2}}, level_switch={2: ("p-a", "p-b")}
    )
    check((await sw5b.describe(admin))["can_switch"] is True, "开关关着 → 管理员仍然可切")
    check((await sw5b.describe(member))["can_switch"] is False, "开关关着 → 普通成员仍然只能看")
    # 私聊里非管理员仍受开关约束（只读），管理员豁免
    plugin5c = FakePlugin(providers=provider_rows())
    plugin5c.store.levels = {1: {"id": 1, "kind": "user", "name": "VIP"}}
    plugin5c._rules_obj = rules(subject_level={"user": {"10001": 1}}, level_switch={1: ("p-a",)})
    sw5c = MS.ModelSwitcher(plugin5c)
    check((await sw5c.describe(subject()))["can_switch"] is False, "私聊：非管理员只读")
    admin_p = G.Subject(sender_id="10001", umo="umo:10001", is_admin=True)
    check((await sw5c.describe(admin_p))["can_switch"] is True, "私聊：管理员不受开关限制")

    print("\n[6] 按序号切换")
    plugin5 = FakePlugin(providers=provider_rows())
    plugin5._rules_obj = rules(
        subject_level={"user": {"10001": 1}, "group": {"88888": 2}},
        level_switch={1: ("p-a", "p-b"), 2: ("p-c",)},
    )
    sw5 = MS.ModelSwitcher(plugin5)
    data = await sw5.describe(subject())
    sw5.remember(subject(), data["options"], "private")
    res = await sw5.apply(subject(), 2)
    check(res["ok"], "序号 2 切换成功")
    pol = plugin5.store.policies[-1]
    check(
        pol["feature"] == "model_choice" and pol["scope_type"] == "user" and pol["scope_id"] == "10001",
        "写到 feature=model_choice 的用户维度",
    )
    check(pol["effect"] == "p-b" and pol["scene"] == "private", "写的是该模型的提供商 id + 当前场景")
    check(plugin5.reloads == 1, "写库后立刻重建内存规则（无需重启）")
    check(sw5.peek(subject()) is None, "切换成功后待选会话被清掉")
    check("已切换为" in res["message"], "回执里带上结果")

    res = await sw5.apply(subject(), 1)
    check(not res["ok"] and res.get("gone"), "会话已清 → 再回序号提示重新发起")

    print("\n[7] 序号 0 = 恢复默认（只删自己的选择）")
    await sw5.handle_command(plugin5, FakeEvent("切换模型"), subject(), "")
    post = plugin5.images[-1] if plugin5.images else b""
    check(bool(post), "不带参数 → 发卡片图")
    plugin5.images.clear()
    data = await sw5.describe(subject())
    sw5.remember(subject(), data["options"], "private")
    res = await sw5.apply(subject(), 0)
    check(res["ok"] and plugin5.store.policies[-1]["effect"] == "inherit",
          "0 → 写 inherit（等价于删掉该场景的选择）")
    check("恢复" in res["message"], "回执说明已恢复默认")

    print("\n[8] 越界 / 非法参数")
    data = await sw5.describe(subject())
    sw5.remember(subject(), data["options"], "private")
    res = await sw5.apply(subject(), 99)
    check(not res["ok"] and "序号超出范围" in res["message"], "越界序号给出范围提示")
    check(sw5.peek(subject()) is not None, "越界不消耗会话（可以重新输）")
    plugin5.sent.clear()
    await sw5.handle_command(plugin5, FakeEvent("切换模型 abc"), subject(), "abc")
    check(any("序号" in s for s in plugin5.sent), "非数字参数 → 提示正确用法")

    print("\n[9] 纯数字回序号（含「不误伤」与「绑定发言人」）")
    plugin6 = FakePlugin(providers=provider_rows())
    plugin6._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={1: ("p-a", "p-b")},
    )
    sw6 = MS.ModelSwitcher(plugin6)
    check(await sw6.handle_index(plugin6, FakeEvent("2"), subject()) is False,
          "没有待选会话 → 数字消息原样放行（不拦不答）")
    data = await sw6.describe(subject())
    sw6.remember(subject(), data["options"], "private")
    check(await sw6.handle_index(plugin6, FakeEvent("5"), subject("20002")) is False,
          "别人（同会话下不同发言人）发的数字不会命中")
    check(await sw6.handle_index(plugin6, FakeEvent("2"), subject()) is True, "本人回序号 → 已处理")
    check(plugin6.store.policies[-1]["effect"] == "p-b", "按序号切到 p-b")
    check(sw6.peek(subject()) is None, "用过即失效")

    print("\n[10] 会话超时与总开关")
    plugin7 = FakePlugin(providers=provider_rows(), cfg={"model_switch_timeout_sec": 10})
    plugin7.store.levels = {1: {"id": 1, "kind": "user", "name": "VIP"}}
    plugin7._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={1: ("p-a",)},
        level_switch_enabled={1: True},
    )
    sw7 = MS.ModelSwitcher(plugin7)
    check(sw7.ttl() == 10, "TTL 取自配置")
    check(MS.ModelSwitcher(FakePlugin(cfg={"model_switch_timeout_sec": 99999})).ttl() == MS.MAX_TTL,
          "TTL 有上限（防止配错成一周）")
    check(MS.ModelSwitcher(FakePlugin(cfg={"model_switch_timeout_sec": 1})).ttl() == MS.MIN_TTL,
          "TTL 有下限")
    await sw7.handle_command(plugin7, FakeEvent("切换模型"), subject(), "")
    item = sw7.peek(subject())
    check(item is not None and item.get("can_switch") is True, "可切换时才建立序号会话")
    item["expire"] = 0  # 手工过期
    check(sw7.peek(subject()) is None, "过期会话自动清理")
    plugin8 = FakePlugin(providers=provider_rows(), cfg={"model_switch_enabled": False})
    sw8 = MS.ModelSwitcher(plugin8)
    plugin8.sent.clear()
    await sw8.handle_command(plugin8, FakeEvent("切换模型"), subject(), "")
    check(plugin8.sent == [] and plugin8.images == [], "开关关闭 → 指令静默失效")
    check(await sw8.handle_index(plugin8, FakeEvent("1"), subject()) is False, "开关关闭 → 数字也不拦")

    print("\n[11] 卡片渲染（Pillow）")
    check(model_card.find_font_path("") is not None, "能找到字体（插件自带或系统字体）")
    check(model_card.find_font_path("Z:/不存在的字体.ttf") is not None,
          "配置了不存在的字体路径 → 回落到自带 / 系统字体")
    plugin9 = FakePlugin(providers=provider_rows())
    plugin9.store.levels = {1: {"id": 1, "kind": "user", "name": "VIP"}}
    plugin9._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={1: ("p-a", "p-b")},
        level_switch_enabled={1: True},
    )
    sw9 = MS.ModelSwitcher(plugin9)
    data = await sw9.describe(subject())
    png = await sw9.build_card(subject(), data)
    if png is None:
        print("  （当前环境没有 Pillow / 字体，跳过渲染断言）")
    else:
        check(png[:8] == b"\x89PNG\r\n\x1a\n", "渲染出合法 PNG（魔数正确）")
        check(len(png) > 2000, f"卡片不是空白（{len(png)} 字节）")
        from PIL import Image  # noqa: PLC0415

        img = Image.open(__import__("io").BytesIO(png))
        check(img.width == model_card.WIDTH and img.height > 200,
              f"卡片尺寸合理（{img.width}x{img.height}）")
        # 超长模型名不能撑破卡片（走截断）
        long_row = dict(data["options"][0])
        long_row["label"] = "非常长的供应商名字" * 6 + " · " + "model-name" * 6
        png2 = model_card.render_model_card(
            title="模型切换",
            current="当前模型名字" * 8,
            meta="分组：超长名字测试" * 4,
            rows=[long_row, data["options"][1]],
            footer="回复序号即可切换",
        )
        check(bool(png2) and png2[:8] == b"\x89PNG\r\n\x1a\n", "超长文案也能渲染（折行 + 截断）")
    check(model_card.render_model_card(title="x", current="", meta="", rows=[], footer="") is None,
          "没有可选项 → 不渲染（调用方走文本兜底）")

    print("\n[12] 控制台接口（真实 Store 走一遍读写）")
    import tempfile  # noqa: PLC0415
    import webui_api as W  # noqa: E402

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        from store import Store  # noqa: PLC0415

        api_plugin = FakePlugin(providers=provider_rows())
        st = Store(str(Path(tmp) / "api.db"))
        await st.open()
        api_plugin.store = st
        api_plugin.store.levels = {}

        class FakeReq:
            """最小 request 桩（只实现 _q / _payload 用到的那几个方法）。"""

            def __init__(self, body=None, args=None):
                self.args = dict(args or {})
                self._body = body or {}

            async def get_json(self, silent=False):
                return self._body

        async def call(fn, body=None, args=None):
            W.request = FakeReq(body, args)
            return await fn(api_plugin)

        res = await call(
            W.h_set_level,
            {
                "kind": "user",
                "name": "VIP",
                "effect": "allow",
                "provider_id": "p-a",
                "fallback_provider_id": "p-c",
                "switch_providers": ["p-a", "p-b", "p-a"],
            },
        )
        check(res.get("status") == "ok", "POST /levels 保存成功")
        lv_id = int(res["data"]["id"])
        res = await call(W.h_levels, args={"kind": "user"})
        row = res["data"]["items"][0]
        check(row["switch_providers"] == ["p-a", "p-b"], "GET /levels 回传名单（去重后）")
        check(row["switch_enabled"] is False, "没传开关 → 默认关（只读）")
        await call(W.h_set_level, {"id": lv_id, "kind": "user", "name": "VIP", "switch_enabled": True})
        row = (await call(W.h_levels, args={"kind": "user"}))["data"]["items"][0]
        check(row["switch_enabled"] is True, "开关可打开")
        # 缺省 = 沿用原值（部分更新不能把开关/名单重置）
        await call(W.h_set_level, {"id": lv_id, "kind": "user", "name": "VIP"})
        row = (await call(W.h_levels, args={"kind": "user"}))["data"]["items"][0]
        check(row["switch_enabled"] is True, "缺省 switch_enabled → 原值保留（true）")
        check(row["switch_providers"] == ["p-a", "p-b"], "缺省 switch_providers 同样保留")
        await call(W.h_set_level, {"id": lv_id, "kind": "user", "name": "VIP", "switch_enabled": False})
        row = (await call(W.h_levels, args={"kind": "user"}))["data"]["items"][0]
        check(row["switch_enabled"] is False, "可以关回去（只读）")

        # 不传该字段 → 沿用原值（旧前端 / 部分更新不能把名单清空）
        res = await call(
            W.h_set_level,
            {"id": lv_id, "kind": "user", "name": "VIP", "effect": "allow", "provider_id": "p-a"},
        )
        row = (await call(W.h_levels, args={"kind": "user"}))["data"]["items"][0]
        check(row["switch_providers"] == ["p-a", "p-b"], "缺省 switch_providers → 原值保留")
        # 传未加载的提供商 → 过滤掉（写进去用户也切不了）
        await call(
            W.h_set_level,
            {"id": lv_id, "kind": "user", "name": "VIP", "effect": "allow", "switch_providers": ["p-a", "p-zzz"]},
        )
        row = (await call(W.h_levels, args={"kind": "user"}))["data"]["items"][0]
        check(row["switch_providers"] == ["p-a"], "未加载的提供商不进名单（否则是「切了没用」）")
        # 传空数组 = 明确关闭
        await call(W.h_set_level, {"id": lv_id, "kind": "user", "name": "VIP", "effect": "allow", "switch_providers": []})
        row = (await call(W.h_levels, args={"kind": "user"}))["data"]["items"][0]
        check(row["switch_providers"] == [], "空数组 = 不配名单（改用兜底三项）")

        res = await call(W.h_set_subject_model_choice, {"scope_id": "10001", "provider_id": "p-b", "scene": "group"})
        check(res.get("status") == "ok", "POST /subject/model-choice 写入成功")
        mc = await st.effect_map("user", feature="model_choice")
        check(mc == {"10001": {"group": "p-b"}}, f"按指定场景落库（实得 {mc}）")
        res = await call(W.h_set_subject_model_choice, {"scope_id": "10001", "scene": "group", "provider_id": ""})
        check(res.get("status") == "ok", "空 provider_id = 清除")
        check(await st.effect_map("user", feature="model_choice") == {}, "清除后没有残留")
        res = await call(W.h_set_subject_model_choice, {"scope_id": "10001", "provider_id": "p-zzz"})
        check(res.get("status") == "error", "未加载的提供商被拒绝（带 message 而不是 500）")
        res = await call(W.h_set_subject_model_choice, {"scope_id": "10001", "scene": "??", "provider_id": "p-a"})
        check(res.get("status") == "error", "非法场景被拒绝")

        res = await call(W.h_providers)
        data = res["data"]
        check(
            sorted(it["id"] for it in data["items"]) == ["p-a", "p-b", "p-c"],
            "GET /providers 列出全部已加载的提供商",
        )
        check(
            {it["id"]: it["label"] for it in data["items"]}["p-a"] == "OpenAI · gpt-4o",
            "与卡片共用同一份展示名口径（供应商 · 模型）",
        )
        await st.close()

    print("\n[13] 纯文本兜底")
    fake = {
        "current_id": "p-a",
        "can_switch": True,
        "options": [
            {"index": 1, "label": "OpenAI · gpt-4o", "current": True, "own": False, "unavailable": False},
            {"index": 2, "label": "Claude · claude-3-7", "current": False, "own": True, "unavailable": True},
        ],
    }
    text = sw9.text_list(fake, 60)
    check("1. OpenAI · gpt-4o" in text and "2. Claude · claude-3-7" in text, "文本列表带序号与模型名")
    check("回复序号" in text and "60 秒" in text, "文本列表带操作提示与有效期")
    check("当前使用" in text and "专属模型" in text and "暂不可用" in text,
          "文本列表标出「当前使用 / 专属模型 / 暂不可用」")
    check(text.startswith("当前使用：p-a"), "文本列表开头先说明当前模型")
    ro = dict(fake, can_switch=False, readonly_reason=MS.REASON_LEVEL_OFF)
    ro_text = sw9.text_list(ro, 60)
    check("可用模型：" in ro_text and "只读" in ro_text, "只读时文本兜底说明原因，不给操作提示")
    check("回复序号" not in ro_text, "只读时不出现「回复序号」")
    ro_data = dict(fake, can_switch=False, readonly_reason=MS.REASON_GROUP_ONLY_ADMIN)
    check(MS.REASON_GROUP_ONLY_ADMIN in sw9.text_list(ro_data, 60), "群聊拒绝的原因文案可复用")

    print("\n[14] 今日用量（信息条）")
    sw9._plugin.store.stats = None
    check(await sw9.today_of(subject()) == {}, "统计读不到 → 空字典（不抛异常、不影响切换）")
    sw9._plugin.store.stats = {"tok_total": 12345, "calls": 8}
    today = await sw9.today_of(subject())
    check(today == {"tokens": 12345, "calls": 8}, f"今日用量（实得 {today}）")
    sw9._plugin.store.stats = {"tok_total": 0, "calls": 0}
    check(await sw9.today_of(subject()) == {}, "今天完全没用过 → 不显示这一行（不写「0 tokens」）")
    sw9._plugin.store.stats = {"tok_total": "2_500_000.0", "calls": None}
    check(await sw9.today_of(subject()) == {}, "脏值不会炸（宽松转 int 失败即视为 0）")
    check(MS._fmt_num(999) == "999" and MS._fmt_num(1200) == "1.2K" and MS._fmt_num(2_500_000) == "2.50M",
          "数字按控制台口径缩写")

    sw9._plugin.store.stats = {"tok_total": 12345, "calls": 8}
    data = await sw9.describe(subject())
    check(data["today"] == {"tokens": 12345, "calls": 8}, "describe 带上今日用量")
    text = sw9.text_list(data, 60)
    check("今日用量：12.3K tokens · 8 次对话" in text, f"文本兜底带今日用量（实得 {text.splitlines()[1]!r}）")
    # 群聊里统计的是「这个群」的量（与总览 / 列表页同口径）
    grp_calls: list[tuple] = []

    async def _spy(subject_type, subject_id, from_ts, to_ts):
        grp_calls.append((subject_type, subject_id))
        return {"totals": {"tok_total": 100, "calls": 1}}

    sw9._plugin.store.subject_stats = _spy  # type: ignore[assignment]
    await sw9.today_of(subject(group="88888"))
    check(grp_calls and grp_calls[-1] == ("group", "88888"), "群聊按群聚合（不是按发言人）")
    await sw9.today_of(subject())
    check(grp_calls[-1] == ("user", "10001"), "私聊按人聚合")
    sw9._plugin.store.stats = None

    print("\n[15] 配色主题与版面（当前模型在标题区折行，不截断）")
    rows_one = [{"index": 1, "label": "OpenAI · gpt-4o", "current": True}]
    check(set(model_card.THEMES) == {"indigo", "teal", "amber", "rose"}, "内置四套主题")
    check(model_card.theme_colors("teal") is model_card.THEMES["teal"], "主题按名字取到")
    check(model_card.theme_colors("不存在的主题") is model_card.THEMES[model_card.DEFAULT_THEME],
          "主题名写错 → 回落默认（配置坏了也要能出图）")
    check(model_card.theme_colors() is model_card.THEMES[model_card.DEFAULT_THEME], "空主题名 → 默认")
    check(model_card.THEMES[model_card.DEFAULT_THEME]["accent"] is not None, "默认主题有强调色")
    theme_pngs: dict[str, bytes] = {}
    for name in model_card.THEMES:
        png_t = model_card.render_model_card(
            title="模型切换", current="OpenAI · gpt-4o", meta="分组：VIP",
            rows=rows_one, theme=name,
        )
        check(bool(png_t) and png_t[:8] == b"\x89PNG\r\n\x1a\n", f"主题 {name} 能出图")
        if png_t:
            theme_pngs[name] = png_t
    if len(theme_pngs) >= 2:
        check(len(set(theme_pngs.values())) == len(theme_pngs), "不同主题画出来确实不一样")
    # 卡片真用上了配置里的主题（build_card → render_model_card 的透传）
    plugin9.cfg["model_card_theme"] = "teal"
    teal_png = await sw9.build_card(subject(), await sw9.describe(subject()))
    plugin9.cfg["model_card_theme"] = "amber"
    amber_png = await sw9.build_card(subject(), await sw9.describe(subject()))
    check(bool(teal_png) and bool(amber_png) and teal_png != amber_png,
          "改 model_card_theme 配置 → 卡片配色跟着变")
    plugin9.cfg.pop("model_card_theme", None)

    short = model_card.render_model_card(
        title="模型切换", current="OpenAI · gpt-4o", meta="分组：VIP", rows=rows_one,
        footer="回复序号切换",
    )
    long_name = "某供应商名字特别长特别长特别长 · 模型名也一样长特别长特别长特别长特别长"
    long = model_card.render_model_card(
        title="模型切换", current=long_name + long_name, meta="分组：VIP", rows=rows_one,
        footer="回复序号切换",
    )
    if short and long:
        from PIL import Image  # noqa: PLC0415

        h_short = Image.open(__import__("io").BytesIO(short)).height
        h_long = Image.open(__import__("io").BytesIO(long)).height
        check(h_long > h_short, f"当前模型在标题区换到第二行（高度 {h_short} → {h_long}）")
        check(Image.open(__import__("io").BytesIO(long)).height < 900, "折行有上限，不会把卡片撑爆")
    else:
        print("  （当前环境没有 Pillow / 字体，跳过版面断言）")
    check(
        model_card._wrap("一二三四五六七八九十", ImageFontStub(), 100000, max_lines=1)
        == ["一二三四五六七八九十"],
        "折行工具：宽度够时不折",
    )
    wrapped = model_card._wrap("一二三四五六七八九十", ImageFontStub(), 40, max_lines=2)
    check(len(wrapped) == 2 and wrapped[-1].endswith("…"),
          f"折行工具：超出上限时末行收省略号（实得 {wrapped}）")
    check(model_card._wrap("", ImageFontStub(), 40, max_lines=2) == [], "折行工具：空文本 → 空列表")

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  - " + f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
