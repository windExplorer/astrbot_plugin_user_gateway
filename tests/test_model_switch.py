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
- 序号切换写的是**专属模型**（``feature=model``：私聊 = 用户、群聊 = 群，互不影响）；
- 序号 0 = 恢复默认（删掉专属模型）；
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
import recall as RC  # noqa: E402

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


class FakeAvatarCache:
    """极简头像缓存桩：记录读 / 抓的调用，可配置「本地有没有」「抓不抓得到」。"""

    def __init__(
        self,
        cached: dict[str, bytes] | None = None,
        fetched: dict[str, bytes] | None = None,
    ) -> None:
        self.cached = dict(cached or {})
        self.fetched = dict(fetched or {})
        self.reads: list[tuple] = []
        self.ensures: list[tuple] = []

    def read_any(self, kind: str, tid: str):
        self.reads.append((kind, tid))
        return self.cached.get(f"{kind}:{tid}")

    async def ensure(self, kind: str, ids, **_kw):
        self.ensures.append((kind, tuple(str(i) for i in ids)))
        out: dict[str, bytes] = {}
        for i in ids:
            got = self.fetched.get(f"{kind}:{i}")
            if got:
                out[str(i)] = got
        return out


class FakeBot:
    """假 aiocqhttp 客户端：发送返回 ``message_id``，撤回记一笔。"""

    def __init__(self, mid: object = "4321", *, with_delete: bool = True) -> None:
        self.calls: list[tuple] = []
        self.deleted: list = []
        self.actions: list[tuple] = []
        self.mid = mid
        if not with_delete:
            return

        async def delete_msg(message_id):
            self.deleted.append(message_id)

        self.delete_msg = delete_msg  # type: ignore[assignment]

    async def send_group_msg(self, **kw):
        self.calls.append(("group", kw))
        return {"status": "ok", "retcode": 0, "data": {"message_id": self.mid}}

    async def send_private_msg(self, **kw):
        self.calls.append(("private", kw))
        return {"retcode": 0, "data": {"message_id": self.mid}}  # 另一层包装，也要能挖出来

    async def call_action(self, action, **params):
        self.actions.append((action, params))
        return {"status": "ok"}


class FakeRecallEvent:
    """事件桩：只提供 recall 用到的接口。

    ``send`` 里顺手打一次 bot 的发送方法 —— 真机上这一步是适配器干的
    （``aiocqhttp_message_event._dispatch_send``），撤回要抓的 id 就在那儿返回。
    """

    def __init__(self, bot: object = None, platform: str = "aiocqhttp", kind: str = "group") -> None:
        self.bot = bot
        self.platform = platform
        self.kind = kind
        self.sent: list = []

    def get_platform_name(self) -> str:
        return self.platform

    async def send(self, chain) -> None:
        self.sent.append(chain)
        if self.bot is None:
            return
        if self.kind == "group":
            await self.bot.send_group_msg(message=[], group_id=1)  # type: ignore[attr-defined]
        else:
            await self.bot.send_private_msg(message=[], user_id=1)  # type: ignore[attr-defined]


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
            "model_card_recall": True,
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
        self.recall_secs: list[int] = []  # 每次发卡片带上的「多少秒后撤回」
        self.reloads = 0
        # 规则快照（由 main.reload_rules 维护，这里手工给）
        self._subject_model: dict[str, str] = {}
        self._subject_model_group: dict[str, str] = {}
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

    async def _send_image(self, event, png: bytes, *, recall_sec: int = 0) -> bool:
        self.images.append(png)
        self.recall_secs.append(int(recall_sec))
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

    # 群聊场景读的是**群专属模型**（v1.3.12）——跨群不串号的关键：每个群各一份
    plugin._subject_model_group = {"88888": "p-b"}
    gdata = await sw.describe(subject(group="88888"))
    check(gdata["current_id"] == "p-b" and gdata["current_source"] == MS.SOURCE_OWN,
          "群聊场景读的是群专属模型（不是私聊那份、更不是别人的）")
    check(gdata["scene"] == "group", "群聊场景判定正确")

    print("\n[3] 当前模型不在名单里也要列出来（否则卡片上没有一行能标「当前使用」）")
    plugin._subject_model = {"10001": "p-x"}
    plugin.providers.append(FakeProvider("p-x", "临时", "temp-model"))
    data = await sw.describe(subject())
    check("p-x" in [o["provider_id"] for o in data["options"]], "当前用的那个被补进名单")
    row = [o for o in data["options"] if o["provider_id"] == "p-x"][0]
    check(row["current"] and "专属模型" in row["note"], "并标注来源")

    print("\n[3.1] 专属模型当前不可用 → 当前标记落到实际会走的那一层")
    plugin._subject_model = {"10001": "p-down"}  # 未加载的提供商
    data = await sw.describe(subject())
    check(data["current_id"] == "p-a" and data["current_source"] == MS.SOURCE_LEVEL,
          "专属模型不可用 → 实际走等级主模型（卡片不能撒谎说当前用的是那个）")
    row = [o for o in data["options"] if o["provider_id"] == "p-down"][0]
    check(row["unavailable"] and not row["current"], "专属模型仍然列出来，并标「暂不可用」")

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
    gpol = plugin5b.store.policies[-1]
    check(res["ok"] and gpol["scope_type"] == "group" and gpol["scope_id"] == "88888",
          "管理员切换写的是**群专属模型**（只影响这个群，不再按人跨群生效）")
    check(gpol["feature"] == "model" and gpol["effect"] == "p-a",
          "落到 feature=model 的群维度（与控制台「群专属模型」同一份）")
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
        pol["feature"] == "model" and pol["scope_type"] == "user" and pol["scope_id"] == "10001",
        "写到 feature=model 的用户维度（切换 = 改专属模型，与控制台同键）",
    )
    check(pol["effect"] == "p-b" and pol["scene"] == "", "写的是提供商 id（专属模型本身不分场景）")
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
          "0 → 写 inherit（等价于删掉专属模型）")
    check(plugin5.store.policies[-1]["feature"] == "model", "删的是专属模型那把键")
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
        want_w = model_card.WIDTH + model_card.SHADOW_PAD * 2  # 画布 = 卡片本体 + 四周阴影留白
        check(img.width == want_w and img.height > 200,
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
          "什么内容都没有 → 不渲染（没有可画的）")
    check(model_card.render_model_card(title="模型切换", current="p-a", rows=[], footer="已切换") is not None,
          "rows 为空但有头脚 → 渲染（切换成功的回执小卡片就长这样）")

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

        res = await call(W.h_set_subject_model, {"type": "group", "scope_id": "88888", "provider_id": "p-b"})
        check(res.get("status") == "ok", "POST /subject/model（群专属）写入成功")
        gm = await st.effect_map("group", feature="model")
        check(gm == {"88888": {"": "p-b"}}, f"群专属按群维度落库（实得 {gm}）")
        res = await call(W.h_set_subject_model, {"type": "group", "scope_id": "88888", "provider_id": ""})
        check(res.get("status") == "ok", "空 provider_id = 恢复跟随等级配置")
        check(await st.effect_map("group", feature="model") == {}, "清除后没有残留")
        res = await call(W.h_set_subject_model, {"scope_id": "10001", "provider_id": "p-zzz"})
        check(res.get("status") == "error", "未加载的提供商被拒绝（带 message 而不是 500）")
        res = await call(W.h_set_subject_model, {"type": "??", "scope_id": "10001", "provider_id": "p-a"})
        check(res.get("status") == "error", "非法 type 被拒绝")

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

    print("\n[14] 今日用量")
    sw9._plugin.store.stats = None
    check(await sw9.today_of(subject()) == {}, "统计读不到 → 空字典（不抛异常、不影响切换）")
    sw9._plugin.store.stats = {"tok_total": 12345, "calls": 8}
    today = await sw9.today_of(subject())
    check(today == {"tokens": 12345, "calls": 8}, f"今日用量（实得 {today}）")
    sw9._plugin.store.stats = {"tok_total": 0, "calls": 0}
    check(await sw9.today_of(subject()) == {"tokens": 0, "calls": 0},
          "今天没用过也要给 0（藏起来会让用户以为功能没生效）")
    sw9._plugin.store.stats = {"tok_total": "2_500_000.0", "calls": None}
    check(await sw9.today_of(subject()) == {"tokens": 0, "calls": 0},
          "脏值不会炸（宽松转 int 失败即视为 0）")
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
    # v1.3.17 起 8 套：靛蓝（真蓝）与紫罗兰（旧靛蓝的紫）分开，
    # 另加朱红（报错固定色）与落日橙 / 石墨灰（与 model_panel 对齐）。
    check(set(model_card.THEMES) ==
          {"indigo", "violet", "teal", "amber", "sunset", "crimson", "rose", "graphite"},
          "内置八套主题")
    check(model_card.TONE_THEMES["error"] == "crimson"
          and model_card.TONE_THEMES["alert"] == "amber",
          "报错红 / 告警橙是固定的（不跟用户偏好走）")
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

    print("\n[16] 卡片头像：群里用群头像、私聊用对方头像")
    user_png = b"user-avatar-bytes"
    group_png = b"group-avatar-bytes"
    cache = FakeAvatarCache({"user:10001": user_png, "group:88888": group_png})
    sw9._plugin.avatars = cache
    check(await sw9.avatar_for(subject()) == user_png, "私聊用对方头像")
    check(cache.reads[-1] == ("user", "10001"), "私聊读的是 user:10001")
    check(await sw9.avatar_for(subject(group="88888")) == group_png, "群聊用**群头像**")
    check(cache.reads[-1] == ("group", "88888"), "群聊读的是 group:88888（不是发言人的）")
    check(cache.ensures == [], "有缓存时不发起抓取")

    # 本地没缓存 → 现抓一次并落盘（ensure 会写盘），之后不再抓
    fresh = FakeAvatarCache(fetched={"group:88888": group_png})
    sw9._plugin.avatars = fresh
    check(await sw9.avatar_for(subject(group="88888")) == group_png, "没缓存时现抓群头像")
    check(fresh.ensures == [("group", ("88888",))], "抓取只针对群号")
    # 抓不到 → 退回插件 logo（非空），且短时间内不再重试（别让指令回执空等）
    miss = FakeAvatarCache()
    sw9._plugin.avatars = miss
    logo = await sw9.avatar_for(subject(group="77777"))
    check(bool(logo) and logo != group_png, "抓不到 → 用插件 logo 顶上（不留空位）")
    check(len(miss.ensures) == 1, "第一次抓不到会重试一次记录")
    await sw9.avatar_for(subject(group="77777"))
    check(len(miss.ensures) == 1, "短时间内不再重试（负缓存生效）")
    sw9._avatar_miss.clear()
    sw9._plugin.avatars = None
    check(bool(await sw9.avatar_for(subject(group="88888"))), "没有头像缓存对象也能出图（退回 logo）")
    # build_card 走的就是这条路径（真渲染一遍，确认不会因为头像抛异常）
    sw9._plugin.avatars = cache
    sw9._plugin.store.stats = {"tok_total": 1, "calls": 1}
    sw9._plugin._rules_obj = rules(
        subject_level={"user": {"10001": 1}, "group": {"88888": 2}},
        level_switch={2: ("p-a", "p-b")},
        level_switch_enabled={2: True},
        level_model={2: {"provider_id": "p-a", "fallback_provider_id": "p-b"}},
    )
    gdata = await sw9.describe(subject(group="88888"))
    png_card = await sw9.build_card(subject(group="88888"), gdata)
    check(bool(png_card) and png_card[:8] == b"\x89PNG\r\n\x1a\n", "群聊卡片能正常渲染")
    check(cache.reads[-1] == ("group", "88888"), "群聊卡片用的仍是群头像")

    print("\n[17] 三段拼接：头 / 身体 / 脚，圆角只在最外侧")
    if model_card.Image is None:
        print("  （当前环境没有 Pillow，跳过版面断言）")
    else:
        from PIL import Image  # noqa: PLC0415

        m_head = model_card._seg_mask((120, 120), 24, round_top=True)
        check(m_head.getpixel((2, 2)) == 0, "头部遮罩：左上角被切掉")
        check(m_head.getpixel((2, 117)) == 255 and m_head.getpixel((117, 117)) == 255,
              "头部遮罩：下沿两角是直角（与身体对接处不切圆）")
        m_foot = model_card._seg_mask((120, 120), 24, round_top=False)
        check(m_foot.getpixel((2, 2)) == 255 and m_foot.getpixel((60, 2)) == 255,
              "脚部遮罩：上沿是直角（与身体对接处不切圆）")
        check(m_foot.getpixel((2, 117)) == 0, "脚部遮罩：左下角被切掉")

        png_t = model_card.render_model_card(
            title="模型切换", current="OpenAI · gpt-4o", meta="分组：VIP",
            rows=[{"index": 1, "label": "OpenAI · gpt-4o", "current": True},
                  {"index": 2, "label": "Claude · claude-3-7", "unavailable": True}],
            footer="回复序号切换 · 60 秒内有效", theme="indigo",
        )
        check(bool(png_t), "新样板面能渲染")
        img = Image.open(__import__("io").BytesIO(png_t)).convert("RGB")
        sp, pad = model_card.SHADOW_PAD, model_card.PAD
        xc = sp + pad - 10
        col = [img.getpixel((xc, y)) for y in range(sp, img.height - sp)]
        near_white = lambda p: min(p) > 248  # noqa: E731
        try:
            head_bottom = next(i for i, p in enumerate(col) if near_white(p)) + sp
            foot_bottom = max(i for i, p in enumerate(col) if not near_white(p)) + sp
        except ValueError:
            head_bottom = foot_bottom = -1
        check(head_bottom > sp and foot_bottom > head_bottom,
              f"能找到头/身分缝与脚底（{head_bottom} / {foot_bottom}）")
        check(min(img.getpixel((sp + 1, sp + 1))) > 240,
              f"卡外留白是白的（透明区不落成黑块，实得 {img.getpixel((sp + 1, sp + 1))}）")
        # 角上那一两个像素的区别太小（整套配色都很浅），圆角与否在遮罩上断言才靠谱
        cm = model_card._card_mask((120, 120), 24)
        check(cm.getpixel((1, 1)) == 0 and cm.getpixel((118, 118)) == 0, "外轮廓四角都是圆的")
        seam = img.getpixel((sp + 1, max(sp + 1, head_bottom - 3)))
        check(sum(seam) < 720, f"头的下沿是直角（贴边像素仍有色：{seam}）")
        deep = img.getpixel((sp + model_card.RADIUS + 10, max(sp + 1, head_bottom - 14)))
        check(sum(deep) < 720, f"头部背景确实上了主题色（不是「淡到看不出来」：{deep}）")
        foot_top_px = img.getpixel((sp + 1, foot_bottom - model_card.FOOTER_H + 3))
        check(sum(foot_top_px) < 740, f"脚的上沿是直角（贴边处直接是脚底色：{foot_top_px}）")

    print("\n[17.1] 切换成功的回执小卡片（只有头 + 脚，没有候选行）")
    if model_card.Image is None:
        print("  （当前环境没有 Pillow，跳过版面断言）")
    else:
        from PIL import Image as _Img  # noqa: PLC0415

        png_ok = await sw9.build_success_card(subject(group="88888"), {"ok": True, "message": "已切换"})
        check(bool(png_ok) and png_ok[:8] == b"\x89PNG\r\n\x1a\n", "成功回执渲染成 PNG")
        im2 = _Img.open(__import__("io").BytesIO(png_ok)).convert("RGB")
        # 行数 = 0 时卡片高度应明显小于完整卡片（没有候选身体）
        check(im2.height < model_card.SHADOW_PAD * 2 + 400,
              f"回执卡片是紧凑的（高 {im2.height}）")
        # rows=[] 直接渲染也要能出图（回执卡片的底层支撑）
        png_bare = model_card.render_model_card(title="模型切换", rows=[], current="x", footer="f")
        check(bool(png_bare), "rows 为空也能渲染（不再直接返回 None）")

    print("\n[19] 卡片自动撤回（配置 + 抓 message_id + 到点删）")
    check(RC.clamp_delay(0) == 0 and RC.clamp_delay(-3) == 0 and RC.clamp_delay(None) == 0,
          "0 / 负数 / 非法值 → 不撤回")
    check(RC.clamp_delay("60") == 60, "字符串也能解析（配置面板可能给字符串）")
    check(RC.clamp_delay(3) == RC.MIN_DELAY and RC.clamp_delay(999999) == RC.MAX_DELAY,
          "太短 / 太长都夹到合理区间")
    check(RC.message_id_of({"message_id": 123}) == "123", "message_id 在顶层")
    check(RC.message_id_of({"status": "ok", "data": {"message_id": "a9"}}) == "a9",
          "message_id 包在 data 里（不同协议端包装层级不同）")
    check(RC.message_id_of(4321) == "" and RC.message_id_of("ok") == "",
          "裸标量不当 id（可能是状态值，认错就会撤错消息）")
    check(RC.message_id_of(None) == "" and RC.message_id_of({"status": "ok"}) == "",
          "挖不到就返回空串")

    # 撤回时间与「序号有效期」必须是同一个数字（分开配迟早对不上）
    plugin_r = FakePlugin(providers=provider_rows())
    plugin_r.store.levels = {1: {"id": 1, "kind": "user", "name": "VIP"}}
    plugin_r._rules_obj = rules(
        subject_level={"user": {"10001": 1}},
        level_switch={1: ("p-a", "p-b")},
        level_switch_enabled={1: True},
    )
    sw_r = MS.ModelSwitcher(plugin_r)
    check(sw_r.recall_sec() == sw_r.ttl() == 60, "默认：撤回时间 = 有效期 = 60")
    plugin_r.cfg["model_switch_timeout_sec"] = 25
    check(sw_r.ttl() == sw_r.recall_sec() == 25, "改有效期 → 撤回时间跟着变（不会一个 25 一个 60）")
    plugin_r.cfg["model_card_recall"] = False
    check(sw_r.recall_sec() == 0 and sw_r.ttl() == 25, "关掉开关 → 不撤回（有效期照旧 25 秒）")
    check(sw_r.footer_of({"can_switch": True}) == "回复序号切换 · 25 秒内有效 · 0 = 恢复默认",
          "关掉撤回时，卡片底部不会写「自动撤回」")
    plugin_r.cfg["model_card_recall"] = True
    check("25 秒后自动撤回" in sw_r.footer_of({"can_switch": True}),
          f"开了撤回就把时间写在卡片上（实得 {sw_r.footer_of({'can_switch': True})}）")
    check("只读" in sw_r.footer_of({"can_switch": False, "readonly_reason": MS.REASON_LEVEL_OFF}),
          "只读时底部仍是「为什么不能切」，不提撤回")
    plugin_r.cfg["model_switch_timeout_sec"] = 60
    await sw_r.handle_command(plugin_r, FakeEvent("切换模型"), subject(), "")
    check(plugin_r.recall_secs == [60], f"发卡片时把撤回秒数交给发送侧（实得 {plugin_r.recall_secs}）")

    # 抓 message_id → 排撤回 → 到点真删
    bot = FakeBot("4321")
    event = FakeRecallEvent(bot, kind="group")
    rec = RC.Recaller()
    origin_clamp = RC.clamp_delay
    RC.clamp_delay = lambda _v: 0.05  # type: ignore[assignment]
    try:
        await rec.send(event, "chain", 60)
    finally:
        RC.clamp_delay = origin_clamp  # type: ignore[assignment]
    check(len(event.sent) == 1 and bot.calls[-1][0] == "group", "消息照常发出")
    check(rec.scheduled == 1, f"排了一个撤回任务（实得 {rec.scheduled}）")
    check("send_group_msg" not in bot.__dict__, "发送方法已还原（不长期占着补丁）")
    await asyncio.sleep(0.2)
    check(bot.deleted == [4321], f"到点真的撤回了（实得 {bot.deleted}）")
    check(rec.stats()["recalled"] == 1 and rec.stats()["pending"] == 0, "计数与待办都归位")

    # 私聊路径；没有 delete_msg 时退回 call_action
    bot2 = FakeBot("777", with_delete=False)
    event2 = FakeRecallEvent(bot2, kind="private")
    rec2 = RC.Recaller()
    await rec2.send(event2, "chain", 60)
    check(rec2.scheduled == 1, "私聊发送也能抓到 id")
    check(bot2.deleted == [], "还没到点，先不撤")
    await rec2._recall_later(bot2, "777", 0.01)
    check(bot2.actions and bot2.actions[-1][0] == "delete_msg", "没有 delete_msg → 退回 call_action")

    # 平台不支持 / 不排撤回：照发，不排任务
    rec3 = RC.Recaller()
    event3 = FakeRecallEvent(FakeBot(), platform="telegram")
    await rec3.send(event3, "chain", 60)
    check(len(event3.sent) == 1 and rec3.scheduled == 0, "非 QQ 平台：只发不撤")
    await rec3.send(FakeRecallEvent(None), "chain", 60)
    check(rec3.scheduled == 0, "拿不到 bot 也不影响发送")
    # 一段窗口里抓到多个 id（说明还有别人在发）→ 宁可不撤，也不能撤错
    bot4 = FakeBot("1")
    event4 = FakeRecallEvent(bot4)

    async def _two_sends(c):
        await bot4.send_group_msg(message=[], group_id=1)
        await bot4.send_group_msg(message=[], group_id=1)

    rec4 = RC.Recaller()
    await rec4.send(event4, "chain", 60, sender=_two_sends)
    check(rec4.scheduled == 0, "抓到 ≠1 个 id → 不撤（宁可留卡片也不撤错别人的消息）")
    # 发送抛异常时不排撤回，也不吞掉异常（调用方要能退化成文本）
    rec5 = RC.Recaller()

    async def _boom(c):
        raise RuntimeError("send failed")

    try:
        await rec5.send(FakeRecallEvent(FakeBot()), "chain", 60, sender=_boom)
        ok_boom = False
    except RuntimeError:
        ok_boom = True
    check(ok_boom and rec5.scheduled == 0, "发送失败照旧抛给调用方（由它退化成文本）")

    # 插件卸载：待撤回任务全部取消，不会再动已经关掉的连接
    bot6 = FakeBot("9")
    rec6 = RC.Recaller()
    await rec6.send(FakeRecallEvent(bot6), "chain", 60)
    check(rec6.stats()["pending"] == 1, "有任务等着撤回")
    await rec6.close()
    await asyncio.sleep(0.2)
    check(bot6.deleted == [] and rec6.stats()["pending"] == 0, "卸载后不再撤回、任务清空")

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
