"""「发出去的消息，过一会儿自动撤回」。

需求：``/切换模型`` 那张卡片是**临时**交互卡（用户扫一眼、回个序号就完事），
留在群里既占版面，又容易被后来的人当成"我该点哪个"的误导信息，所以支持 N 秒后自动撤回。

三条约束（改动时保持）：

1. **只有 aiocqhttp（QQ）能真撤回**：OneBot 有 ``delete_msg``，Telegram / Discord /
   企微这些适配器没有统一接口。所以策略是「**能撤就撤，撤不了就只发不撤**」——
   绝不会因为撤回这件事把卡片也发不出去。
2. **撤回失败不影响任何功能**：所有异常都吞在后台任务里（记 debug 日志）。
3. **宁可不撤，也不能撤错**：抓不到 message_id、或一次发送抓到多个 id（说明这段窗口里
   还有别人在发消息）时不排撤回 —— 留下卡片只是没用，撤掉别人的消息是事故。

为什么需要 :class:`_capture_sent_ids` 这么绕：
AstrBot 的适配器发出消息后**把协议端返回的 message_id 扔了**
（``aiocqhttp_message_event._dispatch_send`` → ``await bot.send_group_msg(...)`` 没有接收返回值），
而 ``delete_msg`` 必须知道 message_id。于是这里在发送**前后**临时把 bot 的发送方法包一层把
返回值捞出来，发完立刻还原（改动只存在于这一次发送的窗口内）。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, AsyncIterator, Optional

from astrbot.api import logger

# 撤回延迟的合理区间（秒）：太短用户还没看清就没了，太长（>1 小时）也没有意义
MIN_DELAY = 5
MAX_DELAY = 3600

# 要包的发送方法（OneBot 的群 / 私聊两条发送路径）
_SEND_METHODS = ("send_group_msg", "send_private_msg")
# 不同协议端 / 包装层级里 message_id 可能叫的名字
_ID_KEYS = ("message_id", "messageId", "msg_id")


def message_id_of(ret: Any) -> str:
    """从协议端的返回值里挖出 ``message_id``（挖不到返回空串）。

    aiocqhttp 的 API 方法返回的是响应体 ``data``，但不同协议端 / 不同版本可能把它包在
    ``data`` / ``result`` 里，甚至返回一个带属性的对象，所以这里都试一遍。
    """
    if ret is None or isinstance(ret, (str, int)):  # 裸标量不认（可能是 "ok" 之类的状态值）
        return ""
    if isinstance(ret, dict):
        for key in _ID_KEYS:
            got = ret.get(key)
            if got not in (None, ""):
                return str(got)
        for key in ("data", "result", "ret"):
            got = message_id_of(ret.get(key))
            if got:
                return got
        return ""
    for key in _ID_KEYS:
        got = getattr(ret, key, None)
        if got not in (None, ""):
            return str(got)
    return ""


def clamp_delay(sec: Any) -> int:
    """把配置里的秒数夹到合理区间；``<=0`` 返回 0（= 不撤回）。"""
    try:
        got = int(float(sec))
    except (TypeError, ValueError):
        return 0
    if got <= 0:
        return 0
    return max(MIN_DELAY, min(MAX_DELAY, got))


@contextlib.asynccontextmanager
async def _capture_sent_ids(bot: Any) -> AsyncIterator[list[str]]:
    """在这一次发送的窗口内，把 bot 发出的消息 id 收集到 ``ids`` 里。

    只改**实例属性**（不动类），退出时无条件还原；包不上（``__slots__`` / 只读属性等）
    就静默跳过 —— 结果是"抓不到 id、不排撤回"，而不是发不出消息。
    """
    ids: list[str] = []
    patched: dict[str, Any] = {}
    own: set[str] = set()  # 这些名字是我们新加到实例上的（还原时直接删掉，不留痕）

    for name in _SEND_METHODS:
        origin = getattr(bot, name, None)
        if not callable(origin):
            continue

        async def wrapper(*args, _origin=origin, **kwargs):
            ret = await _origin(*args, **kwargs)
            try:
                mid = message_id_of(ret)
                if mid:
                    ids.append(mid)
            except Exception:  # 解析返回值出错不该影响发送
                pass
            return ret

        try:
            had_own = name in getattr(bot, "__dict__", {})
            setattr(bot, name, wrapper)
            patched[name] = origin
            if not had_own:
                own.add(name)
        except Exception:
            continue

    try:
        yield ids
    finally:
        for name, origin in patched.items():
            try:
                if name in own:
                    delattr(bot, name)
                else:
                    setattr(bot, name, origin)
            except Exception:
                pass


class Recaller:
    """发消息 + 到点自动撤回。生命周期与插件一致。"""

    def __init__(self, plugin: Any = None) -> None:
        self._plugin = plugin
        self._tasks: set[asyncio.Task] = set()
        # 计数（控制台「运行信息」看得到；不落库）
        self.scheduled = 0
        self.recalled = 0
        self.failed = 0

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    @staticmethod
    def bot_of(event: Any) -> Optional[Any]:
        """取「能撤回消息」的协议端实例；拿不到（非 QQ / 没有 bot）返回 ``None``。"""
        try:
            if str(event.get_platform_name() or "") != "aiocqhttp":
                return None
        except Exception:
            return None
        bot = getattr(event, "bot", None)
        return bot if bot is not None else None

    async def send(
        self, event: Any, chain: Any, delay_sec: Any = 0, *, sender: Any = None
    ) -> bool:
        """发一条消息链；``delay_sec > 0`` 且平台支持时，顺排一个「到点撤回」。

        Args:
            event: 触发事件（用来取平台与协议端实例）。
            chain: ``MessageChain``。
            delay_sec: 多少秒后撤回；``0`` / 负值 / 平台不支持 = 只发不撤。
            sender: 自定义发送函数（默认 ``event.send``），便于测试与复用。

        Returns:
            True = 已发出（撤回是后台任务，不影响这个返回值）。
        """
        send = sender or event.send
        delay = clamp_delay(delay_sec)
        bot = self.bot_of(event) if delay > 0 else None
        if bot is None:
            await send(chain)
            return True

        async with _capture_sent_ids(bot) as ids:
            await send(chain)
        if len(ids) != 1:
            logger.debug(
                f"[UserGateway] 跳过自动撤回：本次发送抓到 {len(ids)} 个 message_id（期望 1）"
            )
            return True
        self._schedule(bot, ids[0], delay)
        return True

    # ------------------------------------------------------------------ #
    # 撤回
    # ------------------------------------------------------------------ #
    def _schedule(self, bot: Any, message_id: str, delay: int) -> None:
        try:
            task = asyncio.get_running_loop().create_task(
                self._recall_later(bot, message_id, delay)
            )
        except RuntimeError:  # 没有运行中的事件循环（不该发生）：只记日志
            logger.debug("[UserGateway] 无事件循环，跳过卡片自动撤回")
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self.scheduled += 1
        logger.debug(f"[UserGateway] 卡片将在 {delay} 秒后撤回（message_id={message_id}）")

    async def _recall_later(self, bot: Any, message_id: str, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            await self._delete(bot, message_id)
            self.recalled += 1
        except asyncio.CancelledError:  # 插件卸载 / 重载：安静退出
            raise
        except Exception as e:
            self.failed += 1
            logger.debug(f"[UserGateway] 自动撤回失败（忽略）: {e}")

    @staticmethod
    async def _delete(bot: Any, message_id: str) -> None:
        """调 OneBot 的 ``delete_msg``（方法不存在就退回 ``call_action``）。"""
        mid: Any = int(message_id) if str(message_id).isdigit() else message_id
        fn = getattr(bot, "delete_msg", None)
        if callable(fn):
            await fn(message_id=mid)
            return
        call = getattr(bot, "call_action", None)
        if callable(call):
            await call("delete_msg", message_id=mid)
            return
        raise RuntimeError("当前协议端没有 delete_msg")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def close(self) -> None:
        """取消所有待撤回任务（插件卸载 / 重载时调用，别让任务去碰已关闭的连接）。"""
        tasks, self._tasks = list(self._tasks), set()
        for t in tasks:
            t.cancel()
        for t in tasks:
            # CancelledError 继承自 BaseException，suppress(Exception) 挡不住它
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    def stats(self) -> dict[str, Any]:
        """概况（控制台「运行信息」的 JSON 里带上，出问题时好排查）。"""
        return {
            "pending": len(self._tasks),
            "scheduled": self.scheduled,
            "recalled": self.recalled,
            "failed": self.failed,
        }
