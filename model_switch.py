"""用户自助切换模型（``/切换模型`` 指令 + 回序号选择）。

功能形状（与 PRD / CHANGELOG 对应）：

1. 管理员在**等级**里打开「允许切换模型」开关（``quota_level.switch_enabled``，**默认关**），
   并可另配一份「可切换模型」名单（``switch_providers``）；
2. 用户发 ``/切换模型``：按他所在的分组（私聊 = 好友等级、群聊 = 群等级）列出候选模型，
   渲染成卡片图（Pillow，见 :mod:`model_card` 的说明）；
3. 开关打开时用户回一个序号即切换（**0 = 恢复默认**）；开关关闭时**只读**：
   卡片照发、能看清自己在用什么，但切不了。

几个刻意的设计（改动前请先读）：

- **默认只读**：`switch_enabled` 默认关。放开自助切换是管理员显式的动作，
  配置没读到（比如库刚升级）也只会退回只读，不会凭空放开。
- **只读时也发卡片**：用户至少要知道「我现在用的是哪个、还有哪些可能」——
  需求原话是「没有配置名单就展示当前模型 + 系统默认 + 备用模型」。
  只读时**不记序号会话**，所以此时随手发的数字不会被这条指令截走。
- **没配名单 → 兜底三项**：当前生效的、AstrBot 系统默认、等级备用模型（去重后）。
  配了名单则以名单为准（名单是管理员划的圈）。
- **群聊仅管理员可用**：群共用一个会话，谁都能切会把整个群的模型搅乱；
  管理员仍然可以（他是来调试的）。**只有管理员能豁免**这一条与上面的只读。
- **选择按「用户 + 场景」存**（``policy.feature='model_choice'``，scene = private / group）：
  群里 A 切了模型不会改 B 的模型，也不会改群的默认模型；同一个人私聊与群聊两份选择互不影响。
- **优先级：用户自己的选择 > 好友专属模型（仅私聊）> 等级模型路由**。
  用户切了不生效等于没切，所以自己选的必须最优先；管理员的「专属模型」只是**候选之一**
  （会带「专属模型」标签），而不是锁死。
- **候选名单不放任**：只能从「等级名单（或兜底三项）+ 自己的专属模型 + 当前生效的那个」里选，
  这样「让人自己挑」不会变成「谁都能挑最贵的那个」。当前生效的那个会额外列出来，
  否则卡片上就没有一行能被标成「当前使用」。
- **序号会话有 TTL 且绑定发言人**：键是 ``umo + 发言人``，所以群里别人发的数字不会误触发，
  60 秒（可配）后失效。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger

try:  # 包内正常加载
    from . import model_card
    from .gate import Subject, effect_in_scene
    from .store import MODEL_CHOICE_FEATURE
except ImportError:  # pragma: no cover - 本地平铺调试
    import model_card  # type: ignore
    from gate import Subject, effect_in_scene  # type: ignore
    from store import MODEL_CHOICE_FEATURE  # type: ignore

# 序号会话上限（防御性：长期运行不涨内存；正常同时最多几十个）
MAX_SESSIONS = 200
# TTL 边界（配置值是秒）
MIN_TTL = 10
MAX_TTL = 600

# 「当前使用」的来源文案
SOURCE_CHOICE = "你自己切换的"
SOURCE_OWN = "管理员配的专属模型"
SOURCE_LEVEL = "分组主模型"
SOURCE_LEVEL_FALLBACK = "分组备用模型（主模型不可用）"
SOURCE_DEFAULT = "系统默认模型"

# 只读 / 不可用的原因（也直接当给用户看的文案用）
REASON_LEVEL_OFF = "所在分组未开放模型切换"
REASON_GROUP_ONLY_ADMIN = "群里只有管理员能切换模型"
# 只读时的卡片底部提示
READONLY_HINT = "只读：{reason}，请联系管理员"
# 群聊里非管理员的一句话说清（不发卡片：需求是「群聊仅管理员能用」）
GROUP_REFUSE = "群里只有管理员能切换模型；你可以私聊我发 /切换模型 查看或切换。"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fmt_num(n: Any) -> str:
    """token 数字的紧凑写法（与控制台列表的 1.2K / 3.45M 口径一致）。"""
    v = _as_int(n, 0)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _logo_bytes() -> Optional[bytes]:
    """插件 logo（头像取不到时的兜底，读不到就返回 None 让卡片留空位）。"""
    try:
        p = Path(__file__).resolve().parent / "logo.png"
        return p.read_bytes() if p.is_file() else None
    except Exception:
        return None


class ModelSwitcher:
    """``/切换模型`` 的状态机 + 名单组装（一个插件实例一个）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        # key = "umo|sender_id" → {"options": [...], "expire": ts, "scene": str}
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 配置 / 会话
    # ------------------------------------------------------------------ #
    def enabled(self) -> bool:
        return bool(self._plugin._cfg("model_switch_enabled", True))

    def ttl(self) -> int:
        got = _as_int(self._plugin._cfg("model_switch_timeout_sec", 60), 60)
        return max(MIN_TTL, min(MAX_TTL, got))

    @staticmethod
    def key_of(subject: Subject) -> str:
        """会话键：**带上发言人**，否则群里别人随手发的数字会顶掉你的选择。"""
        return f"{subject.umo}|{subject.sender_id}"

    def _prune(self, now: Optional[float] = None) -> None:
        now = now if now is not None else time.time()
        stale = [k for k, v in self._pending.items() if float(v.get("expire") or 0) <= now]
        for k in stale:
            self._pending.pop(k, None)
        if len(self._pending) > MAX_SESSIONS:  # 兜底：极端情况直接清空（会话本来就是短时的）
            self._pending.clear()

    def remember(
        self,
        subject: Subject,
        options: list[dict[str, Any]],
        scene: str,
        *,
        can_switch: bool = True,
        reason: str = "",
    ) -> None:
        """记下这次列出的序号选项（用户回序号时按它换算）。

        ``can_switch=False`` 的会话不该被建立（只读时 ``handle_command`` 直接跳过这一步），
        参数留着是为了让 :meth:`apply` 有一道显式防线，而不是靠「外面记得别调」。
        """
        self._prune()
        self._pending[self.key_of(subject)] = {
            "options": options,
            "scene": scene,
            "can_switch": bool(can_switch),
            "readonly_reason": str(reason or ""),
            "expire": time.time() + self.ttl(),
        }

    def peek(self, subject: Subject) -> Optional[dict[str, Any]]:
        """取该会话待选的序号表（过期 / 不存在返回 None，过期的顺手清掉）。"""
        key = self.key_of(subject)
        item = self._pending.get(key)
        if not item:
            return None
        if float(item.get("expire") or 0) <= time.time():
            self._pending.pop(key, None)
            return None
        return item

    def clear(self, subject: Subject) -> None:
        self._pending.pop(self.key_of(subject), None)

    def sessions(self) -> int:
        self._prune()
        return len(self._pending)

    # ------------------------------------------------------------------ #
    # 名单组装
    # ------------------------------------------------------------------ #
    async def describe(self, subject: Subject) -> dict[str, Any]:
        """算出「这个人现在能用哪些模型、当前用的是哪个、能不能切」。

        Returns:
            ``{scene, level_id, level_name, level_kind, options, current_id, current_source,
            open, allowed, can_switch, readonly_reason, is_admin, group_restricted}``；
            ``options`` 每项 ``{index, provider_id, label, note, current, own, unavailable}``。

        ``open`` = 有东西可展示（只读时也是 True）；``can_switch`` = 真的能切
        （只读 / 群聊非管理员时为 False）。
        """
        plugin = self._plugin
        scene = "group" if str(subject.group_id or "") else "private"
        uid = str(subject.sender_id or "")
        is_admin = bool(getattr(subject, "is_admin", False))

        rules = plugin._rules()
        found = plugin.gate.model_level_of(subject, rules)
        level_id = int(found[1]) if found else None
        level_kind = str(found[0]) if found else ("group" if scene == "group" else "user")
        level_name = ""
        if level_id:
            try:
                lv = await plugin.store.get_level(level_id) if plugin.store else None
                level_name = str((lv or {}).get("name") or "")
            except Exception:
                level_name = ""

        allowed = [str(p or "").strip() for p in (plugin.gate.switch_options(rules, level_id) or ())]
        allowed = [p for p in allowed if p]
        level_on = plugin.gate.switch_enabled(rules, level_id)

        available = set(plugin.provider_ids())
        circuit = plugin.circuit.open_ids() if getattr(plugin, "circuit", None) else set()
        usable = available - circuit
        system_default = await self.system_default_of(subject)
        route = plugin.gate.resolve_model(subject, rules) or {}
        own = str((plugin._subject_model or {}).get(uid) or "") if scene == "private" else ""
        choice = str(effect_in_scene((plugin._model_choice or {}).get(uid), scene) or "")
        current_id, current_source = await self.current_of(
            subject, scene, usable, system_default, route
        )

        # 能不能切：群聊只放给管理员；等级开关默认关（只读），管理员同样豁免
        group_restricted = scene == "group"
        if group_restricted and not is_admin:
            can_switch, readonly_reason = False, REASON_GROUP_ONLY_ADMIN
        elif not (level_on or is_admin):
            can_switch, readonly_reason = False, REASON_LEVEL_OFF
        else:
            can_switch, readonly_reason = True, ""

        ordered: list[str] = []

        def add(pid: Any) -> None:
            text = str(pid or "").strip()
            if text and text not in ordered:
                ordered.append(text)

        if allowed:
            for pid in allowed:
                add(pid)
        else:
            # 没配名单 → 兜底展示三项：当前生效的 / 系统默认 / 等级备用。
            # 需求原话就是这么要求的：至少让人看清「现在走的是哪个、还能退到哪」。
            add(current_id)
            add(system_default)
            add(route.get("fallback_provider_id"))
        if own:
            add(own)
        # 用户自己切过的那个也要列出来（哪怕它现在不可用）——否则他「切过的东西」凭空消失，
        # 只会以为是插件把配置吃掉了
        if choice:
            add(choice)
        # 当前生效的那个也列出来：否则卡片上没有任何一行能标「当前使用」
        if current_id and current_source != SOURCE_DEFAULT:
            add(current_id)

        infos = plugin.provider_map()

        options: list[dict[str, Any]] = []
        for i, pid in enumerate(ordered, start=1):
            info = infos.get(pid) or {"label": pid}
            is_current = bool(current_id) and pid == current_id
            is_own = bool(own) and pid == own
            note = ""
            if is_current:
                note = f"当前使用 · {current_source}"
            elif is_own:
                note = "管理员为你配置的专属模型"
            options.append(
                {
                    "index": i,
                    "provider_id": pid,
                    "label": str(info.get("label") or pid),
                    "note": note,
                    "current": is_current,
                    "own": is_own,
                    "unavailable": pid not in available or pid in circuit,
                }
            )

        return {
            "scene": scene,
            "level_id": level_id,
            "level_name": level_name,
            "level_kind": level_kind,
            "options": options,
            "current_id": current_id,
            "current_source": current_source,
            "open": bool(options),
            # 名单本身（不等于 options）：用来区分「配了名单」与「走兜底三项」
            "allowed": allowed,
            "can_switch": can_switch,
            "readonly_reason": readonly_reason,
            "is_admin": is_admin,
            "group_restricted": group_restricted,
            "system_default": system_default,
            # 今日用量（信息条展示；取不到就是空字典，卡片少一行而已）
            "today": await self.today_of(subject),
        }

    async def today_of(self, subject: Subject) -> dict[str, int]:
        """今日用量：私聊 = **这个人**、群聊 = **这个群**（与总览 / 列表页同口径）。

        用户看卡片时最常问的两件事是「我现在用的是哪个模型」和「今天用掉多少」，
        所以顺手放进信息条。任何异常都返回空字典 —— 统计读不到不该影响切换。
        """
        try:
            store = self._plugin.store
            if not (store and store.ready):
                return {}
            kind = "group" if str(subject.group_id or "") else "user"
            sid = str(subject.group_id or subject.sender_id or "")
            if not sid:
                return {}
            now = datetime.now()
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            stats = await store.subject_stats(
                kind, sid, int(start.timestamp()), int(now.timestamp())
            )
            totals = (stats or {}).get("totals") or {}
            tokens = _as_int(totals.get("tok_total"), 0)
            calls = _as_int(totals.get("calls"), 0)
            if tokens <= 0 and calls <= 0:
                return {}
            return {"tokens": tokens, "calls": calls}
        except Exception as e:  # 统计失败绝不冒泡（卡片少一行，功能照常）
            logger.debug(f"[UserGateway] 读取今日用量失败（忽略）: {e}")
            return {}

    def group_label(self, data: dict[str, Any]) -> str:
        """分组的中文说法（卡片副标题用）。"""
        kind = str(data.get("level_kind") or "user")
        name = str(data.get("level_name") or "").strip()
        what = "群等级" if kind == "group" else "好友等级"
        return f"{name}（{what}）" if name else "未分组"

    async def system_default_of(self, subject: Subject) -> str:
        """AstrBot 的**系统默认**提供商 id（本会话没被插件干预时会走的那个）。"""
        try:
            prov = await self._plugin.context.get_using_provider_async(subject.umo)
            if not prov:
                return ""
            return str((getattr(prov, "provider_config", {}) or {}).get("id") or "")
        except Exception:
            return ""

    async def current_of(
        self,
        subject: Subject,
        scene: str,
        available: Optional[set[str]] = None,
        system_default: Optional[str] = None,
        route: Optional[dict[str, Any]] = None,
    ) -> tuple[str, str]:
        """这个人**当前实际会走**哪个模型 → ``(provider_id, 来源文案)``。

        必须与 ``main.route_model`` 的顺序、以及「不可用就往下回落」的语义保持一致：
        某一层配了但提供商没加载 / 正在熔断时，实际走的是更粗的一层，
        这里若还报「当前使用 = 那个不可用的」，卡片就会撒谎。

        Args:
            available: 当前可用的提供商集合；为 None 时自己算一遍。
            system_default / route: 已经算好的话就传进来，省一次查询与一次解析。
        """
        plugin = self._plugin
        uid = str(subject.sender_id or "")
        if available is None:
            available = plugin.provider_ids() - plugin.circuit.open_ids()

        choice = effect_in_scene((plugin._model_choice or {}).get(uid), scene)
        if choice and str(choice) in available:
            return str(choice), SOURCE_CHOICE
        if scene == "private":
            own = str((plugin._subject_model or {}).get(uid) or "")
            if own and own in available:
                return own, SOURCE_OWN
        if route is None:
            route = plugin.gate.resolve_model(subject, plugin._rules()) or {}
        if route:
            picked = plugin.gate.pick_provider(route, available)
            if picked:
                return (
                    str(picked["provider_id"]),
                    SOURCE_LEVEL_FALLBACK if picked.get("used_fallback") else SOURCE_LEVEL,
                )
        if system_default is None:
            system_default = await self.system_default_of(subject)
        return str(system_default or ""), SOURCE_DEFAULT

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    async def build_card(self, subject: Subject, data: dict[str, Any]) -> Optional[bytes]:
        """渲染卡片；渲染不出来返回 None（调用方退回文本列表）。"""
        plugin = self._plugin
        avatar = None
        try:
            cache = getattr(plugin, "avatars", None)
            if cache is not None and str(subject.sender_id or ""):
                avatar = cache.read_any("user", subject.sender_id)
        except Exception:
            avatar = None
        if not avatar:
            avatar = _logo_bytes()  # 头像是空位时用插件 logo 顶上，别留一块空白

        current_label = ""
        if data.get("current_id"):
            current_label = str(
                plugin.provider_info(str(data["current_id"])).get("label")
                or data["current_id"]
            )
        # 头部只放「分组」这种短信息；模型名（供应商 · 模型）单独占一行并允许折行，
        # 否则长名字必然被截断 —— 而那一行恰恰是用户最需要看全的。
        subtitle = f"分组：{self.group_label(data)}"
        info: list[dict[str, str]] = []
        if current_label:
            info.append({"label": "当前使用", "value": current_label})
        today = data.get("today") or {}
        if today:
            info.append(
                {
                    "label": "今日用量",
                    "value": f"{_fmt_num(today.get('tokens'))} tokens · "
                    f"{_as_int(today.get('calls'), 0)} 次对话",
                }
            )

        return model_card.render_model_card(
            title="模型切换",
            subtitle=subtitle,
            rows=data.get("options") or [],
            footer=self.footer_of(data),
            info=info,
            avatar=avatar,
            font_path=str(plugin._cfg("model_card_font", "") or ""),
            theme=str(plugin._cfg("model_card_theme", model_card.DEFAULT_THEME) or ""),
        )

    def footer_of(self, data: dict[str, Any]) -> str:
        """卡片底部提示：能切就说怎么切，只读就说为什么切不了。"""
        if data.get("can_switch"):
            return f"回复序号切换 · {self.ttl()} 秒内有效 · 0 = 恢复默认"
        reason = str(data.get("readonly_reason") or REASON_LEVEL_OFF)
        return READONLY_HINT.format(reason=reason)

    def text_list(self, data: dict[str, Any], ttl: int) -> str:
        """卡片渲染不出来时的纯文本兜底（功能不能因为画不出图就没了）。"""
        lines: list[str] = []
        if data.get("current_id"):
            lines.append(f"当前使用：{data['current_id']}")
        today = data.get("today") or {}
        if today:
            lines.append(
                f"今日用量：{_fmt_num(today.get('tokens'))} tokens · "
                f"{_as_int(today.get('calls'), 0)} 次对话"
            )
        lines.append("可用模型：" if not data.get("can_switch") else "可切换的模型：")
        for row in data.get("options") or []:
            tail = "（当前使用）" if row.get("current") else ("（专属模型）" if row.get("own") else "")
            mark = "（暂不可用）" if row.get("unavailable") else ""
            lines.append(f"{row.get('index')}. {row.get('label')}{tail}{mark}")
        if data.get("can_switch"):
            lines.append(f"回复序号即可切换（{ttl} 秒内有效）· 回复 0 = 恢复默认")
        else:
            reason = str(data.get("readonly_reason") or REASON_LEVEL_OFF)
            lines.append(READONLY_HINT.format(reason=reason))
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # 应用
    # ------------------------------------------------------------------ #
    async def apply(self, subject: Subject, index: int) -> dict[str, Any]:
        """按序号切换；``index=0`` 表示恢复默认（清掉自己的选择）。

        Returns:
            ``{ok, message}``：``message`` 是给用户看的一句话。
        """
        plugin = self._plugin
        item = self.peek(subject)
        if not item:
            return {"ok": False, "gone": True, "message": "选择已过期，请重新发送 /切换模型。"}
        # 只读的会话理论上不会被建立（handle_command 里不 remember），这里是二次防线：
        # 万一以后有人改成「只读也记会话」，也绝不会真的写库。
        if not item.get("can_switch"):
            reason = str(item.get("readonly_reason") or REASON_LEVEL_OFF)
            return {"ok": False, "message": f"{reason}，这次没有切换。"}
        options = list(item.get("options") or [])
        scene = str(item.get("scene") or "private")
        uid = str(subject.sender_id or "")
        if not uid:
            return {"ok": False, "message": "无法识别你的账号，切换失败。"}

        if index == 0:
            await plugin.store.set_policy(
                "user", uid, "inherit", feature=MODEL_CHOICE_FEATURE, scene=scene
            )
            await plugin.reload_rules()
            self.clear(subject)
            await plugin.store.log_audit(
                "chat", "model_switch_reset", f"{uid} scene={scene}"
            )
            return {"ok": True, "message": "已恢复为分组配置的模型，从下一条消息起生效。"}

        if index < 1 or index > len(options):
            return {
                "ok": False,
                "message": f"序号超出范围，请输入 1 - {len(options)}（或 0 恢复默认）。",
            }
        picked = options[index - 1]
        pid = str(picked.get("provider_id") or "")
        if not pid:
            return {"ok": False, "message": "该选项不可用，请重新发送 /切换模型。"}

        await plugin.store.set_policy(
            "user", uid, pid, feature=MODEL_CHOICE_FEATURE, scene=scene
        )
        await plugin.reload_rules()
        self.clear(subject)
        label = str(picked.get("label") or pid)
        await plugin.store.log_audit("chat", "model_switch", f"{uid} scene={scene} → {pid}")
        note = "" if not picked.get("unavailable") else "（该模型当前未加载，等它可用后才会生效）"
        return {
            "ok": True,
            "provider_id": pid,
            "message": f"已切换为：{label}{note}\n从下一条消息起生效。",
        }

    # ------------------------------------------------------------------ #
    # 指令入口
    # ------------------------------------------------------------------ #
    async def handle_command(self, plugin: Any, event: Any, subject: Subject, arg: str = "") -> None:
        """``/切换模型 [序号]``：不带参数发卡片（只读时也发），带参数直接切。"""
        if not self.enabled():
            return
        if not (plugin.store and plugin.store.ready):
            await plugin._send(event, "插件数据未就绪，暂时无法切换模型。")
            return

        data = await self.describe(subject)
        if data.get("group_restricted") and not data.get("is_admin"):
            # 群聊仅管理员可用：不发卡片（需求就是「群里只有管理员能用」），
            # 但要说清去哪儿用，别让用户以为是插件坏了。
            await plugin._send(event, GROUP_REFUSE)
            return
        if not data.get("open"):
            # 连展示的东西都没有（没归级、也没任何候选）：说清楚而不是发一张空卡
            await plugin._send(
                event,
                f"当前分组（{self.group_label(data)}）没有可展示的模型信息，请联系管理员。",
            )
            return

        text = str(arg or "").strip()
        if text:
            try:
                index = int(text)
            except ValueError:
                index = -1
            if index >= 0:
                if not data.get("can_switch"):
                    # 只读：明确说不能切，不给「沉默失败」的错觉
                    await plugin._send(
                        event,
                        READONLY_HINT.format(
                            reason=str(data.get("readonly_reason") or REASON_LEVEL_OFF)
                        ),
                    )
                    return
                # 带参数 = 直接切（不用先发卡片）：先记下序号表再套用
                self.remember(
                    subject,
                    data.get("options") or [],
                    str(data.get("scene") or "private"),
                    can_switch=True,
                    reason="",
                )
                res = await self.apply(subject, index)
                await plugin._send(event, res.get("message", ""))
                return
            await plugin._send(event, "参数要填序号（如 /切换模型 2），或直接发 /切换模型 看列表。")
            return

        if data.get("can_switch"):
            # 只有能切时才记序号会话：只读时随手发的数字不该被这条指令截走
            self.remember(
                subject,
                data.get("options") or [],
                str(data.get("scene") or "private"),
                can_switch=True,
                reason="",
            )
        card = await self.build_card(subject, data)
        if card is not None and await plugin._send_image(event, card):
            return
        # 渲染不出来 / 图片发不出去 → 退化成文本列表（功能不能因为一张图就没了）
        await plugin._send(event, self.text_list(data, self.ttl()))

    async def handle_index(self, plugin: Any, event: Any, subject: Subject) -> bool:
        """把一条「纯数字」消息当作序号处理；不是待选会话则返回 False（交还给正常流程）。

        Returns:
            True = 已处理（调用方应 stop 事件，避免数字又被当成聊天内容送进 LLM）。
        """
        if not self.enabled():
            return False
        if self.peek(subject) is None:
            return False
        try:
            index = int(str(event.get_message_str() or "").strip())
        except Exception:
            return False
        if index < 0:
            return False
        try:
            res = await self.apply(subject, index)
        except Exception:
            logger.exception("[UserGateway] 处理模型切换序号失败（忽略）")
            return False
        await plugin._send(event, str(res.get("message") or ""))
        return True
