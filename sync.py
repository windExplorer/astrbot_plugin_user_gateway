"""好友 / 群列表同步（aiocqhttp · OneBot v11）。

数据来源是协议端的两条标准动作：

- ``get_friend_list`` → ``[{user_id, nickname, remark}]``
- ``get_group_list``  → ``[{group_id, group_name, member_count, max_member_count, owner}]``

调用路径：``context.platform_manager.platform_insts`` 里筛出 ``aiocqhttp`` 实例 →
``platform.get_client()`` 拿到 ``CQHttp`` → ``call_action(...)``
（依据：``aiocqhttp_platform_adapter.py:512`` 的 ``get_client() -> CQHttp``）。

设计要点：
- **一次同步失败不能影响插件其它功能**：所有异常都被收集进返回值，绝不向上抛。
- **同步是「覆盖式」写入缓存**，协议端删掉的好友在本地也应当消失（不删历史用量）。
- 支持同时连接多个机器人（每个 aiocqhttp 实例一个 ``platform_id``），逐个同步。
- 后台按 ``sync_interval_min`` 定时跑，页面上的「立即同步」按钮走同一条路径。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from astrbot.api import logger


def _iter_aiocqhttp_platforms(context: Any) -> list[Any]:
    """列出所有 aiocqhttp 平台实例（用户可能同时运行多个机器人）。"""
    out: list[Any] = []
    try:
        insts = list(getattr(context.platform_manager, "platform_insts", []) or [])
    except Exception as e:
        logger.warning(f"[UserGateway] 枚举平台实例失败: {e}")
        return out
    for p in insts:
        try:
            if p.meta().name == "aiocqhttp":
                out.append(p)
        except Exception:
            continue
    return out


async def _call(bot: Any, action: str, timeout: float, **params: Any) -> Any:
    """调用一次 OneBot 动作（带超时；``params`` 透传给 call_action）。"""
    return await asyncio.wait_for(bot.call_action(action, **params), timeout=timeout)


async def sync_group_members(
    context: Any,
    store: Any,
    group_id: str,
    platform_id: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """同步**某个群**的成员列表到缓存（``get_group_member_list``）。

    为什么不跟着定时同步一起跑：成员列表比群列表大得多（几百人 × 几百群），
    默认只在用户点「同步成员」时拉取；需要定时同步可在配置里打开 ``sync_group_members``。

    Returns:
        ``{ok, group_id, count, platform_id, error}`` —— 失败也只记录不抛。
    """
    gid = str(group_id or "").strip()
    result: dict[str, Any] = {"ok": False, "group_id": gid, "count": 0, "platform_id": "", "error": ""}
    if not gid:
        result["error"] = "缺少群号"
        return result
    if not (store and getattr(store, "ready", False)):
        result["error"] = "数据库未就绪"
        return result
    try:
        gid_int = int(gid)
    except (TypeError, ValueError):
        result["error"] = f"群号必须是数字：{gid}（目前只支持 QQ 群）"
        return result

    platforms = _iter_aiocqhttp_platforms(context)
    if platform_id:
        same = [p for p in platforms if str(getattr(p.meta(), "id", "") or "") == platform_id]
        platforms = same or platforms
    if not platforms:
        result["error"] = "未找到 aiocqhttp 平台实例（请在 AstrBot 里配置并启用一个 OneBot 适配器）"
        return result

    errors: list[str] = []
    for p in platforms:
        pid = ""
        try:
            pid = str(p.meta().id or "")
        except Exception:
            pid = ""
        try:
            bot = p.get_client()
            members = await _call(bot, "get_group_member_list", timeout, group_id=gid_int)
            if not isinstance(members, list):
                raise TypeError(f"get_group_member_list 返回了非列表: {type(members).__name__}")
            n = await store.upsert_group_members(pid, gid, members)
            result.update(ok=True, count=n, platform_id=pid)
            logger.info(f"[UserGateway] 群 {gid} 成员同步完成：{n} 人")
            return result
        except Exception as e:
            errors.append(f"{pid or '?'}: {e}")
            logger.warning(f"[UserGateway] 同步群 {gid} 成员失败（{pid or '?'}）: {e}")
    result["error"] = "；".join(errors) or "同步失败"
    return result


async def sync_all(context: Any, store: Any, timeout: float = 15.0) -> dict[str, Any]:
    """同步所有 aiocqhttp 实例的好友与群列表到缓存。

    Returns:
        ``{ok, total_friends, total_groups, platforms: [{platform_id, ok, friends, groups, error}]}``
        —— 逐个平台的成败都在结果里，调用方据此提示用户。
    """
    result: dict[str, Any] = {"ok": False, "total_friends": 0, "total_groups": 0, "platforms": []}
    platforms = _iter_aiocqhttp_platforms(context)
    if not platforms:
        result["error"] = "未找到 aiocqhttp 平台实例（请在 AstrBot 里配置并启用一个 OneBot 适配器）"
        return result

    for p in platforms:
        pid = ""
        try:
            pid = str(p.meta().id or "")
        except Exception:
            pid = ""
        entry: dict[str, Any] = {"platform_id": pid, "ok": False, "friends": 0, "groups": 0, "error": ""}

        if not store or not getattr(store, "ready", False):
            entry["error"] = "数据库未就绪"
            result["platforms"].append(entry)
            continue

        try:
            bot = p.get_client()
        except Exception as e:
            entry["error"] = f"获取协议端客户端失败: {e}"
            result["platforms"].append(entry)
            continue

        # 好友
        try:
            friends = await _call(bot, "get_friend_list", timeout)
            if not isinstance(friends, list):
                raise TypeError(f"get_friend_list 返回了非列表: {type(friends).__name__}")
            entry["friends"] = await store.upsert_friends(pid, friends)
        except Exception as e:
            entry["error"] = f"拉取好友列表失败: {e}"
            logger.warning(f"[UserGateway] 同步 {pid} 好友列表失败: {e}")

        # 群（不因好友失败而跳过：两者互相独立）
        try:
            groups = await _call(bot, "get_group_list", timeout)
            if not isinstance(groups, list):
                raise TypeError(f"get_group_list 返回了非列表: {type(groups).__name__}")
            entry["groups"] = await store.upsert_groups(pid, groups)
        except Exception as e:
            entry["error"] = (entry["error"] + "；" if entry["error"] else "") + f"拉取群列表失败: {e}"
            logger.warning(f"[UserGateway] 同步 {pid} 群列表失败: {e}")

        entry["ok"] = entry["friends"] > 0 or entry["groups"] > 0
        result["total_friends"] += entry["friends"]
        result["total_groups"] += entry["groups"]
        result["platforms"].append(entry)

    result["ok"] = any(p.get("ok") for p in result["platforms"])
    if not result["ok"]:
        result.setdefault("error", "所有平台同步均失败：" + "；".join(
            f"{p.get('platform_id') or '?'}: {p.get('error') or '未知原因'}" for p in result["platforms"]
        ))
    return result


class SyncScheduler:
    """按 ``sync_interval_min`` 周期性同步的轻量调度器。

    只用 ``asyncio`` 起一个任务，不引入任何调度依赖；插件 ``terminate()`` 时取消。
    """

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._task: Optional[asyncio.Task] = None
        self.running = False
        self.last_at: int = 0
        self.last_result: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def sync_once(self) -> dict[str, Any]:
        """执行一次同步（带互斥，避免手动与定时同时打协议端）。"""
        async with self._lock:
            timeout = float(self.plugin._cfg("sync_timeout_sec", 15) or 15)
            res = await sync_all(self.plugin.context, self.plugin.store, timeout)
            try:
                members = await self._sync_members_if_enabled(timeout)
                if members:
                    res["total_members"] = members
            except Exception as e:
                logger.warning(f"[UserGateway] 同步群成员失败（忽略）: {e}")
            self.last_at = int(time.time())
            self.last_result = res
            try:
                if self.plugin.store and self.plugin.store.ready:
                    await self.plugin.store.set_setting("sync_last_at", str(self.last_at))
            except Exception:
                pass
            if res.get("ok"):
                logger.info(
                    f"[UserGateway] 同步完成：好友 {res['total_friends']} 个 / 群 {res['total_groups']} 个",
                )
            else:
                logger.warning(f"[UserGateway] 同步失败：{res.get('error')}")
            return res

    async def _sync_members_if_enabled(self, timeout: float) -> int:
        """配置打开时，把所有已缓存群的成员也同步一遍。

        **默认关闭**（``sync_group_members=false``）：成员数是「群数 × 人数」量级，
        全量拉取很吃协议端，通常只在需要做群成员级管控时手动同步个别群。
        打开后返回共同步到的成员条数。
        """
        if not bool(self.plugin._cfg("sync_group_members", False)):
            return 0
        store = self.plugin.store
        if not (store and getattr(store, "ready", False)):
            return 0
        res = await store.list_groups(limit=2000, offset=0)
        total = 0
        for row in res.get("rows", []) or []:
            got = await sync_group_members(
                self.plugin.context,
                store,
                str(row.get("group_id") or ""),
                str(row.get("platform_id") or ""),
                timeout,
            )
            if got.get("ok"):
                total += int(got.get("count") or 0)
        if total:
            logger.info(f"[UserGateway] 群成员同步完成：共同步 {total} 条")
        return total

    async def start(self) -> None:
        """启动后台轮询（``sync_interval_min<=0`` 时不启动）。"""
        interval = int(self.plugin._cfg("sync_interval_min", 30) or 0)
        if interval <= 0:
            logger.info("[UserGateway] 自动同步已关闭（sync_interval_min=0）")
            return
        if self._task is not None:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop(interval))
        logger.info(f"[UserGateway] 已启动好友/群自动同步，每 {interval} 分钟一次")

    async def stop(self) -> None:
        """停止后台轮询。"""
        self.running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _loop(self, interval_min: int) -> None:
        # 启动后先等一小会儿再拉，避开 AstrBot 启动阶段协议端还没连上的窗口
        await asyncio.sleep(min(30, interval_min * 60))
        while self.running:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[UserGateway] 定时同步异常（忽略）: {e}")
            try:
                await asyncio.sleep(max(60, interval_min * 60))
            except asyncio.CancelledError:
                raise
