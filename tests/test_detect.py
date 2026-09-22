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

    def external_available(self):
        return {"plugin": "astrbot_plugin_model_panel", "api": 2, "trigger": "gateway",
                "probe_concurrency": 3, "probe_timeout": 45}


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
    eq(o[0]["rate"], "今日 99.2%", "成功率带统计范围（否则读不出 2.3% 是哪段时间的）")
    eq(o[0]["health"], "healthy", "健康态落到选项上（决定那行的行底渐变）")
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

    print("[annotate(probe_of)：本次结论必须与历史数字分开]")
    # 用户原话：「测出来是红的，但是有毫秒显示，成功率 2.3%，我也不知道最新检测出的是报错还是通过了」
    panel2 = FakePanel(snapshot={
        "p-a": {"state": "down", "latency_ms": 1180.0, "success_rate": 0.023,
                "last_ts": int(now - 600), "last_probe_ts": int(now)},
        "p-b": {"state": "healthy", "latency_ms": 3000.0, "success_rate": 0.99,
                "last_ts": int(now - 600), "last_probe_ts": int(now)},
        "p-c": {"state": "healthy", "latency_ms": 4300.0, "success_rate": 0.75,
                "last_ts": int(now - 7200)},
    })
    pl4 = FakePlugin(panel=panel2)
    data4 = base_data()
    run(pl4.detector.annotate(data4, {
        "p-a": {"id": "p-a", "ok": False, "latency_ms": None, "error_code": "timeout"},
        "p-b": {"id": "p-b", "ok": True, "latency_ms": 812.0, "error_code": ""},
    }))
    a, b, c = data4["options"]
    eq(a["probe_ok"], False, "失败的模型标成本次失败")
    eq(a["probe_label"], "本次失败", "标签文案（卡片用实心红）")
    eq(a["latency"], "", "★失败的行不留毫秒（旧版会显示上一次成功的 1180ms，正是绕晕人的地方）")
    check(a["note"].startswith("超时"), f"失败原因顶到副标题最前（实得 {a['note']!r}）")
    eq(a["rate"], "今日 2.3%", "成功率照旧给，但标明是今天的窗口")
    eq(a["health"], "down", "状态用对面写回后的值 → 行底变红")
    eq(b["probe_ok"], True, "通过的模型标成本次通过")
    eq(b["latency"], "812ms", "★通过的行显示**本次**延迟（812ms 而不是库里的 3000ms）")
    check(not b["note"].startswith("1 分钟前") and not b["note"].startswith("刚刚"),
          f"本次通过的行不写时间戳（标签已经说明是本次，实得 {b['note']!r}）")
    check("probe_ok" not in c, "这次没测的模型不带本次结论")
    eq(c["latency"], "4300ms", "没测的模型照旧用库里的延迟")
    check(c["note"].startswith("2 小时前"), "没测的模型照旧标数据时间")

    print("[单次上限：默认不限（分组里有多少就测多少）]")
    pl5 = FakePlugin(panel=FakePanel())
    eq(pl5.detector.max_models(), 0, "默认 0 = 不限")
    pl5._settings["detect_max_models"] = 5
    eq(pl5.detector.max_models(), 5, "配了正整数才截断")
    pl5._settings["detect_max_models"] = -3
    eq(pl5.detector.max_models(), 0, "填负数当「不限」，不静默变成只测 1 个")

    print("[估时：按并发轮数算，不是 个数 × 超时]")
    pl6 = FakePlugin(panel=FakePanel())
    eq(pl6.detector.probe_plan(), (3, 45.0), "并发与超时取自对面报的值")
    eq(pl6.detector.eta_text(2), "约 45 秒", "2 个模型 1 轮")
    eq(pl6.detector.eta_text(9), "约 2 分钟", "9 个模型 3 轮（不是 9 × 45s）")
    eq(FakePlugin(panel=None).detector.probe_plan(), (3, 45.0),
       "对面没报（旧版）时用保守默认值，估时间宁可偏大")

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

    print("[卡片：状态靠行底渐变表达（v1.3.14 去掉了行首小圆点）]")
    try:
        import io as _io

        from PIL import Image as _Image

        def _render(health: str):
            row = {"index": 1, "label": "x"}
            if health:
                row["health"] = health
            return MC.render_model_card(title="t", rows=[row], footer="f",
                                        font_path=MC.find_font_path())

        # 与「不带 health」的同一张卡逐像素求差：差异最大的那个像素就是行底色，
        # 不用去猜行画在第几行（头部是渐变，直接扫一列会把头像/头部颜色也扫进来）。
        base_img = _Image.open(_io.BytesIO(_render(""))).convert("RGB")
        base_px = base_img.load()
        x = MC.SHADOW_PAD + MC.PAD + 8  # 行内左侧留白（序号方块还要再往右 10px）

        def tint(health: str):
            im = _Image.open(_io.BytesIO(_render(health))).convert("RGB")
            px = im.load()
            best, best_d = None, -1
            for y in range(im.size[1]):
                d = sum(abs(base_px[x, y][i] - px[x, y][i]) for i in range(3))
                if d > best_d:
                    best_d, best = d, px[x, y]
            check(best_d >= 24, f"「{health}」的行底确实被染色了（与无状态相比差 {best_d}）")
            return best

        t = {h: tint(h) for h in ("healthy", "degraded", "down", "unknown")}
        check(t["healthy"][1] >= t["healthy"][0] + 3, f"正常那行底色偏绿（{t['healthy']}）")
        check(t["degraded"][0] >= t["degraded"][1] + 20,
              f"降级那行底色偏琥珀（{t['degraded']}）")
        check(t["down"][0] >= t["down"][1] + 45, f"故障那行底色偏红（{t['down']}）")
        # 无数据那档是冷灰（蓝 ≥ 绿 ≥ 红），不该跟「正常」混成一个颜色
        check(t["unknown"][2] >= t["unknown"][1] >= t["unknown"][0],
              f"无数据那行底色是冷灰（{t['unknown']}）")
    except ImportError as e:  # pragma: no cover - 没装 Pillow 的环境
        print(f"  skip  没装 Pillow（{e}），跳过版面像素自检")

    print("[整组一起测：默认不截断，配了上限才截断且必须写明]")
    many = base_data(options=[
        {"index": i + 1, "provider_id": f"m{i}", "label": f"模型{i}", "note": ""}
        for i in range(12)
    ])
    p10 = FakePlugin(panel=FakePanel(snapshot={}), data=many)
    p10._rules_obj = G.Rules(level_detect_enabled={1: True})
    flow(p10, Subject())
    eq(len(p10._panel.detect_calls[0]), 12, "12 个模型一次全测（不再被默认 8 截断）")
    meta = " ".join(str(k.get("meta_extra") or "") for k in p10.switcher.built)
    check("正在检测 12 个模型" in meta, f"进度卡报出真实数量（实得 {meta!r}）")
    check("最坏 约 3 分钟" in meta or "最坏 约 2 分钟" in meta,
          f"进度卡带上「最坏等多久」（12 个 / 3 并发 / 超时 45s → 4 轮，实得 {meta!r}）")

    p11 = FakePlugin(cfg={"detect_max_models": 5}, panel=FakePanel(snapshot={}), data=many)
    p11._rules_obj = G.Rules(level_detect_enabled={1: True})
    flow(p11, Subject())
    eq(len(p11._panel.detect_calls[0]), 5, "配了上限就只测前 5 个")
    meta11 = " ".join(str(k.get("meta_extra") or "") for k in p11.switcher.built)
    check("只测前 5 个" in meta11 and "还有 7 个没测" in meta11,
          f"被截断时卡片必须写明（否则会被当成漏测，实得 {meta11!r}）")

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
