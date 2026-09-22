"""``/切换模型检测`` 的自检：两道冷却、跨插件取值、卡片注解与「没装就明说」。

用法：
    uv run --no-project --python 3.12 --with pillow python tests/test_detect.py

为什么专门测这个：这条指令的**功能全部挂在另一个插件上**，而它的三种失败方式
都很难在运行的 bot 里复现 ——
  1) 没装 / 装了旧版 model_panel：必须明确提示去装，不能静默不响应、也不能抛 AttributeError；
  2) 两道冷却（人 / 模型）算错：用户以为「点了没反应」，管理员以为「冷却坏了」；
  3) 注解错位：延迟取自 A 模型、时间取自 B 模型，图上看着很正常，只有对数据才发现。

覆盖：
- ``panel()``：没注册 / star_cls 为空 / 旧版没有 external_detect → 一律 None（= 不可用）；
- ``annotate()``：健康点 / 延迟 / 成功率 / 数据时间落到选项上，且时间**前置**在 note 里；
- 「同一模型 N 分钟内不重复测」按 **last_probe_ts** 切分（真实对话不算「刚测过」）；
- 指令冷却按 (会话, 人) 记，管理员豁免，时长可配；
- ``handle_command``：没装 → 安装提示；分组开关关 → 明确拒绝；冷却内 → 报还剩几分钟；
- ``_run``：把选中的 pid 原样交给对方，并把最新数字渲染进卡片。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# --- 最小 astrbot 桩（必须在 import detect 之前） ---
if "astrbot" not in sys.modules:
    _astrbot = types.ModuleType("astrbot")
    _api = types.ModuleType("astrbot.api")
    _api.logger = logging.getLogger("test")  # type: ignore[attr-defined]
    _astrbot.api = _api  # type: ignore[attr-defined]
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

import detect as D  # noqa: E402
import gate as G  # noqa: E402
import model_card as MC  # noqa: E402

_failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  ✓ " if cond else "  ✗ ") + label)
    if not cond:
        _failures.append(label)


def eq(actual, expected, label: str) -> None:
    ok = actual == expected
    check(ok, label if ok else f"{label}（期望 {expected!r}，实得 {actual!r}）")


class Subject:
    def __init__(self, sender="10001", group="", admin=False, umo="aiocqchat:FriendMessage:10001"):
        self.sender_id = sender
        self.group_id = group
        self.is_admin = admin
        self.umo = umo
        self.platform_id = "aiocqhttp"


class FakePanel:
    """对方插件实例：只实现联动用到的两个接口，并把调用记下来。"""

    def __init__(self, snapshot=None, detect_res=None):
        self.snapshot = snapshot or {}
        self.detect_res = detect_res or {"ok": True, "results": []}
        self.snapshot_calls: list = []
        self.detect_calls: list = []

    async def external_snapshot(self, provider_ids=None, days=0.0):
        self.snapshot_calls.append((list(provider_ids or []), days))
        items = {k: v for k, v in self.snapshot.items()
                 if provider_ids is None or k in set(provider_ids)}
        return {"ok": True, "items": items, "missing": [], "live_available": True}

    async def external_detect(self, provider_ids, timeout=0.0):
        self.detect_calls.append(list(provider_ids or []))
        return self.detect_res


class OldPanel:
    """旧版 model_panel：装是装了，但没有 external_detect（联动必须判成不可用）。"""

    async def external_snapshot(self, provider_ids=None, days=0.0):
        return {"ok": True, "items": {}}


class FakeSwitcher:
    """只实现 detect.py 用到的那几个方法（/切换模型 本身另有自检）。"""

    def __init__(self, data):
        self.data = data
        self.built: list[dict] = []
        self.remembered: list = []
        self.ttl_calls = 0

    async def describe(self, subject):
        return dict(self.data)

    def group_label(self, data):
        return str(data.get("level_name") or "默认")

    def footer_of(self, data):
        return "回复序号切换 · 60 秒内有效 · 0 = 恢复默认"

    def recall_sec(self):
        return 60

    def ttl(self):
        self.ttl_calls += 1
        return 60

    def text_list(self, data, ttl):
        return "文本兜底"

    def remember(self, subject, options, scene, **kw):
        self.remembered.append((list(options), scene, kw))

    async def build_card(self, subject, data, **kw):
        self.built.append(dict(kw))
        # 用真渲染器出图：卡片能不能画出来本身也是要测的
        return MC.render_model_card(
            title=str(kw.get("title") or "模型切换"),
            current="OpenAI · gpt-4o",
            meta="分组：VIP",
            rows=list(kw.get("rows") or data.get("options") or []),
            footer=str(kw.get("footer") or "回复序号切换"),
        )


class FakeStore:
    ready = True


class FakePlugin:
    def __init__(self, cfg=None, panel=None, data=None):
        self._settings = dict(cfg or {})
        self.store = FakeStore()
        self.gate = G.Gate(self._cfg)
        self.detector = D.ModelDetector(self)
        self.switcher = FakeSwitcher(data or {})
        self.sent: list[str] = []
        self.images: list[bytes] = []
        self.recall_secs: list[int] = []
        self._panel = panel
        self.context = types.SimpleNamespace(get_registered_star=self._get_star)
        self._rules_obj = G.Rules()

    def _get_star(self, name):
        if self._panel is None:
            return None
        return types.SimpleNamespace(star_cls=self._panel, name=name)

    def _cfg(self, key, default=None):
        return self._settings.get(key, default)

    def _rules(self):
        return self._rules_obj

    async def _send(self, event, text):
        self.sent.append(str(text))

    async def _send_image(self, event, png, *, recall_sec=0):
        self.images.append(png)
        self.recall_secs.append(int(recall_sec))
        return True


def options_rows():
    return [
        {"index": 1, "provider_id": "p-a", "label": "OpenAI · gpt-4o", "note": "当前使用", "current": True},
        {"index": 2, "provider_id": "p-b", "label": "Claude · claude-3-7", "note": ""},
        {"index": 3, "provider_id": "p-c", "label": "本地 · qwen3", "note": ""},
    ]


def base_data(**over):
    data = {
        "options": options_rows(),
        "current_id": "p-a",
        "level_id": 1,
        "level_name": "VIP",
        "can_switch": True,
        "group_restricted": False,
        "is_admin": False,
        "open": True,
        "scene": "private",
    }
    data.update(over)
    return data


def run(coro):
    return asyncio.run(coro)


def flow(plugin, subject, data=None):
    """跑一次 ``handle_command``，并把它开的后台任务也跑完。

    自检要的是**确定性**，不是调度：把 ``create_task`` 换成收集器，回执发完再手动 await，
    这样断言「检测交给后台」和「跑完发出结果卡」都能在同一段同步代码里检查。
    """
    async def inner():
        created: list = []
        orig = asyncio.create_task

        def fake_task(coro):
            created.append(coro)
            return types.SimpleNamespace(cancel=lambda: None)

        asyncio.create_task = fake_task
        try:
            await plugin.detector.handle_command(plugin, object(), subject)
        finally:
            asyncio.create_task = orig
        for coro in created:
            await coro
        return len(created)

    return asyncio.run(inner())


def main() -> int:
    now = time.time()

    print("[格式化]")
    eq(D._fmt_ms(812.0), "812ms", "毫秒原样给")
    eq(D._fmt_ms(105000), "1分45秒", "超过 1 分钟才折成分钟")
    eq(D._fmt_ms(None), "", "没有数据 → 空串（整组指标不画，而不是显示 -）")
    eq(D._fmt_rate(None), "", "没有样本 → 空串（不显示 100%）")
    eq(D._fmt_rate(0.992), "99.2%", "成功率按百分比")
    eq(D._fmt_ago(int(now - 5), now), "刚刚", "5 秒前")
    eq(D._fmt_ago(int(now - 180), now), "3 分钟前", "3 分钟前")
    eq(D._fmt_ago(0, now), "", "没有时间戳 → 空串")

    print("[panel()：三种「不可用」都要判成 None]")
    p = FakePlugin(panel=None)
    check(p.detector.panel() is None, "没注册 model_panel → None")
    p2 = FakePlugin(panel=OldPanel())
    check(p2.detector.panel() is None, "装了但没有 external_detect（旧版）→ None（不能等调用时抛）")
    p3 = FakePlugin(panel=FakePanel())
    check(p3.detector.panel() is p3._panel, "接口齐全 → 拿到实例")

    print("[annotate()：健康点 / 延迟 / 成功率 / 数据时间]")
    panel = FakePanel(snapshot={
        "p-a": {"state": "healthy", "latency_ms": 812.0, "success_rate": 0.992,
                "last_ts": int(now - 300), "last_probe_ts": int(now - 3600)},
        "p-b": {"state": "down", "latency_ms": None, "success_rate": 0.0,
                "last_ts": int(now - 7200), "last_probe_ts": int(now - 60)},
        "p-c": {"state": "unknown", "latency_ms": None, "success_rate": None,
                "last_ts": 0, "last_probe_ts": 0},
    })
    pl = FakePlugin(panel=panel)
    data = base_data()
    run(pl.detector.annotate(data))
    o = data["options"]
    eq(o[0]["latency"], "812ms", "延迟落到选项上")
    eq(o[0]["rate"], "99.2%", "成功率落到选项上")
    eq(o[0]["health"], "healthy", "健康态落到选项上（决定行首色点）")
    check(o[0]["note"].startswith("5 分钟前"), f"数据时间**前置**在 note 里（实得 {o[0]['note']!r}）")
    eq(o[1]["latency"], "", "对方拿不到延迟 → 空串，卡片整组不画指标")
    check(o[1]["note"].startswith("2 小时前"), "失败过的模型同样标出数据时间")
    eq(o[2]["note"], "", "一条记录都没有 → 不写时间（不编「刚刚」）")

    print("[annotate()：没有 model_panel 时安静跳过]")
    pl2 = FakePlugin(panel=None)
    data2 = base_data()
    run(pl2.detector.annotate(data2))
    check(all("latency" not in opt for opt in data2["options"]),
          "拿不到数据时不写指标（卡片照常出，只是少一列数字）")

    print("[模型级冷却：按对方的 last_probe_ts 切分]")
    det = pl.detector
    todo, skipped = det._split(data["options"], panel.snapshot)
    eq([o["provider_id"] for o in todo], ["p-a", "p-c"],
       "刚测过的（p-b）不进本次待测表，1 小时前测过的（p-a）照测")
    eq([o["provider_id"] for o in skipped], ["p-b"], "p-b 在冷却内（60 秒前测过）")
    check(skipped[0].get("probed_ago"), "被跳过的行带上「上次测于」的时间说法")
    # 真实对话更新的是 last_ts 而不是 last_probe_ts：对话多不等于刚测过
    only_chat = {"p-a": {"last_ts": int(now - 10), "last_probe_ts": 0}}
    todo2, _skipped2 = det._split(options_rows(), only_chat)
    eq(len(todo2), 3, "只有真实对话记录（last_probe_ts 为空）→ 不算「刚测过」，照测")

    print("[指令冷却：按 (会话, 人) 记，管理员豁免，时长可配]")
    pl3 = FakePlugin(cfg={"detect_command_cooldown_min": 30}, panel=FakePanel())
    s = Subject()
    eq(pl3.detector.cooldown_left(s), 0, "没测过 → 0（可以测）")
    pl3.detector.mark(s)
    check(pl3.detector.cooldown_left(s) >= 29, "刚测完 → 约 30 分钟（向上取整）")
    other = Subject(sender="10002")
    eq(pl3.detector.cooldown_left(other), 0, "冷却按人记：别人不受影响")
    pl3._settings["detect_command_cooldown_min"] = 0
    eq(pl3.detector.cooldown_left(s), 0, "配成 0 → 关闭指令冷却")

    print("[handle_command：三种拦截都给出可行动的说法]")
    p4 = FakePlugin(panel=None, data=base_data())
    eq(flow(p4, Subject()), 0, "没装 model_panel → 不进流程")
    check(p4.sent and "astrbot_plugin_model_panel" in p4.sent[0],
          "没装 model_panel → 提示去装（并带仓库地址）")
    check(not p4.images, "没装时不发任何卡片")

    p5 = FakePlugin(panel=FakePanel(), data=base_data())
    p5._rules_obj = G.Rules(level_detect_enabled={1: False})
    flow(p5, Subject())
    check(p5.sent and "没有开放" in p5.sent[0], "分组开关关着 → 明确拒绝")
    check(not p5.images, "被拒绝时不发卡片")

    p5b = FakePlugin(panel=FakePanel(), data=base_data(is_admin=True))
    p5b._rules_obj = G.Rules(level_detect_enabled={})  # 一个分组都没开
    flow(p5b, Subject(admin=True))
    check(bool(p5b.images), "管理员豁免分组开关（不受「默认关」限制）")

    print("[handle_command：冷却内不动手]")
    p6 = FakePlugin(cfg={"detect_command_cooldown_min": 30}, panel=FakePanel(), data=base_data())
    p6._rules_obj = G.Rules(level_detect_enabled={1: True})
    sub6 = Subject()
    p6.detector.mark(sub6)
    eq(flow(p6, sub6), 0, "冷却内不排后台任务")
    check(p6.sent and "冷却" in p6.sent[0] and "分钟" in p6.sent[0],
          "冷却内 → 告诉用户还剩几分钟")
    check(not p6.images, "冷却内不发「检测中」卡片")

    print("[handle_command：正常流程先回「检测中」再后台跑]")
    panel7 = FakePanel(snapshot={})
    p7 = FakePlugin(panel=panel7, data=base_data())
    p7._rules_obj = G.Rules(level_detect_enabled={1: True})
    sub7 = Subject()
    eq(flow(p7, sub7), 1, "回执只发一张卡，检测交给后台任务")
    eq(panel7.detect_calls, [["p-a", "p-b", "p-c"]], "把卡片上那几个模型原样交给对方检测")
    eq(len(p7.images), 2, "先「检测中」、跑完再发一张最新卡")
    check(p7.switcher.remembered, "结果卡发出后重新挂上选单（看完就能直接回序号切换）")
    eq(p7.recall_secs[-1], 60, "结果卡按「切换选择等待时长」自动撤回（与普通卡片一致）")

    print("[模型冷却：全都刚测过时不花钱，直接说明]")
    p7b = FakePlugin(panel=FakePanel(snapshot={
        str(o["provider_id"]): {"last_probe_ts": int(now - 30)} for o in options_rows()
    }), data=base_data())
    p7b._rules_obj = G.Rules(level_detect_enabled={1: True})
    eq(flow(p7b, Subject()), 0, "全在模型冷却内 → 不排任务")
    check(p7b.sent and "刚测过" in p7b.sent[0], "说明「刚测过、不重复打」（省额度）")
    check(not p7b.images, "不花钱时也不发卡片")

    print("[_run：对方说忙 / 失败时，别把「没测成」说成「测完了」]")
    busy = FakePanel(detect_res={"ok": False, "busy": True, "items": {}, "results": []})
    p8 = FakePlugin(panel=busy, data=base_data())
    run(p8.detector._run(p8, object(), Subject(), pids=["p-a"]))
    check(p8.sent and "在跑" in p8.sent[0], "对方忙 → 让用户稍后再试（不排队）")
    bad = FakePanel(detect_res={"ok": False, "error": "数据库炸了", "results": []})
    p9 = FakePlugin(panel=bad, data=base_data())
    run(p9.detector._run(p9, object(), Subject(), pids=["p-a"]))
    check(p9.sent and "数据库炸了" in p9.sent[0], "对方报错 → 把原因原样带出来")

    print("[卡片：指标与健康点的版面]")
    rows = [
        {"index": 1, "label": "OpenAI · gpt-4o", "note": "1 分钟前 · 当前使用", "current": True,
         "latency": "812ms", "rate": "99.2%", "health": "healthy"},
        {"index": 2, "label": "Claude · claude-3-7", "note": "5 分钟前",
         "latency": "1分45秒", "rate": "80.0%", "health": "degraded"},
        {"index": 3, "label": "本地 · qwen3", "unavailable": True, "note": "", "health": "unknown"},
    ]
    png = MC.render_model_card(title="模型切换", current="OpenAI · gpt-4o",
                               meta="分组：VIP · 检测完成：2 正常 / 1 异常",
                               rows=rows, footer="回复序号切换 · 60 秒内有效")
    check(bool(png), "带指标的卡片渲染得出来（PNG 非空）")
    png2 = MC.render_model_card(title="模型切换", current="OpenAI · gpt-4o", meta="分组：VIP",
                                rows=rows, footer="回复序号切换")
    check(bool(png2), "指标整组缺失时也渲染得出来（没装对面插件的情形）")

    print()
    if _failures:
        print(f"失败 {len(_failures)} 项：")
        for f in _failures:
            print("  - " + f)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
