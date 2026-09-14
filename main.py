"""萌萌权限控制台：按好友 / 按群管控 LLM 权限与 token 额度，并提供用量统计。

实现依据见工作区文档 ``docs/astrbot_plugin_user_gateway_PRD.md``。

当前进度（M0 骨架）：
- 插件生命周期：数据目录、SQLite 建库、配置读取、控制台路由注册
- 控制台后端接口：健康检查 / 配置读写 / 总览统计 / 好友群列表 / 额度 / 明细
- **LLM 闸门与好友群同步在 M1 接入**（本文件已预留 ``reload_rules()`` 与内存规则缓存）

设计红线（改动时务必保持）：
1. **fail-open**：本插件任何异常都必须放行消息，绝不能把用户消息吞掉。
2. 判定走内存缓存（``_effect_*`` / ``_quota_*``），只有写操作后 ``reload_rules()`` 重建，
   保证闸门热路径不做磁盘 / SQL 查询。
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter as astr_filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, StarTools, register

try:  # 钩子类型仅用于注解；缺失不影响运行
    from astrbot.api.provider import LLMResponse, ProviderRequest
except ImportError:  # pragma: no cover
    LLMResponse = Any  # type: ignore
    ProviderRequest = Any  # type: ignore

try:  # 包内相对导入（AstrBot 正常加载路径）
    from . import quota as quota_mod
    from .gate import Cooldown, Gate, Subject
    from .store import Store
    from .sync import SyncScheduler
    from .webui_api import register_apis
except ImportError:  # pragma: no cover - 兼容非包环境（本地调试直接运行本模块）
    import quota as quota_mod  # type: ignore
    from gate import Cooldown, Gate, Subject  # type: ignore
    from store import Store  # type: ignore
    from sync import SyncScheduler  # type: ignore
    from webui_api import register_apis  # type: ignore

PLUGIN_NAME = "astrbot_plugin_user_gateway"


def _read_plugin_version() -> str:
    """从 metadata.yaml 读取版本号（单一来源），失败时降级为 0.0.0。"""
    try:
        text = (Path(__file__).resolve().parent / "metadata.yaml").read_text(encoding="utf-8")
        for line in text.splitlines():
            m = re.match(r"\s*version\s*:\s*v?([0-9][^\s#]*)", line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "0.0.0"


@register(
    PLUGIN_NAME,
    "windExplorer",
    "按好友 / 按群细粒度管控 LLM 使用权限与 token 额度，并提供用量统计控制台。",
    _read_plugin_version(),
)
class UserGatewayPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        # AstrBot 注入的插件配置（AstrBotConfig，dict 子类，带 save_config()）
        self.config = config or {}
        self.plugin_version = _read_plugin_version()

        # 数据目录与数据库（initialize() 中创建）
        self.data_dir: Path = Path(".")
        self.store: Optional[Store] = None

        # 闸门热路径用的内存规则缓存（reload_rules() 重建）
        self._effect_user: dict[str, str] = {}
        self._effect_group: dict[str, str] = {}
        self._quota_user: dict[str, dict[str, dict[str, Any]]] = {}
        self._quota_group: dict[str, dict[str, dict[str, Any]]] = {}

        # M1：判定内核 / 提示冷却 / 后台任务
        self.gate = Gate(self._cfg)
        self._notify_cooldown = Cooldown(int(self._cfg("notice_cooldown_sec", 60) or 0))
        self.scheduler = SyncScheduler(self)
        # 会话 → 本次 LLM 请求的起始信息（用于估算 token 与统计延迟）
        self._inflight: dict[str, dict[str, Any]] = {}
        self._maintenance_task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def initialize(self) -> None:
        """创建数据目录、建库、注册控制台路由。"""
        try:
            self.data_dir = self._resolve_data_dir()
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.error(f"[UserGateway] 数据目录创建失败: {e}")
            return

        try:
            self.store = Store(str(self.data_dir / "user_gateway.db"))
            await self.store.open()
            logger.info(f"[UserGateway] 数据库就绪: {self.store.db_path}")
        except Exception as e:
            # 数据库打不开时插件仍要保持可用（fail-open：不拦截任何消息）
            logger.error(f"[UserGateway] 数据库初始化失败，插件将不拦截任何消息: {e}")
            self.store = None
            return

        await self.reload_rules()
        await self._purge_old_usage()

        try:
            register_apis(self)
        except Exception as e:
            logger.error(f"[UserGateway] 控制台路由注册失败: {e}")

        # 把配置里的闸门优先级应用到已注册的钩子上
        self._apply_guard_priority()

        # 后台任务：好友/群定时同步 + 额度过期维护
        self._running = True
        try:
            await self.scheduler.start()
        except Exception as e:
            logger.warning(f"[UserGateway] 启动自动同步失败（忽略）: {e}")
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

        logger.info(
            f"[UserGateway] 初始化完成（v{self.plugin_version}）｜"
            f"LLM 管控={'开' if self._cfg('llm_guard_enabled', True) else '关'} "
            f"额度={'开' if self._cfg('quota_enabled', True) else '关'}"
            f"（{self._cfg('quota_mode', 'observe')}）"
        )

    async def terminate(self) -> None:
        """停止后台任务并关闭数据库连接。"""
        self._running = False
        for task in (self._maintenance_task, getattr(self.scheduler, "_task", None)):
            if task is not None:
                task.cancel()
        self._maintenance_task = None
        try:
            await self.scheduler.stop()
        except Exception:
            pass
        try:
            if self.store:
                await self.store.close()
                logger.info("[UserGateway] 数据库已关闭")
        except Exception as e:
            logger.warning(f"[UserGateway] 关闭数据库异常（忽略）: {e}")

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _resolve_data_dir(self) -> Path:
        """取插件专属数据目录（``data/plugin_data/<插件名>``），失败时退回插件目录。"""
        try:
            d = StarTools.get_data_dir(PLUGIN_NAME)
            if d:
                return Path(d)
        except Exception as e:
            logger.warning(f"[UserGateway] get_data_dir 失败，回退插件目录: {e}")
        return Path(__file__).resolve().parent

    def _cfg(self, key: str, default: Any = None) -> Any:
        """安全读取配置项（配置对象缺失 / 类型异常时返回默认值）。"""
        try:
            if hasattr(self.config, "get"):
                v = self.config.get(key)
                return default if v is None else v
        except Exception:
            pass
        return default

    async def _purge_old_usage(self) -> None:
        """按配置清理超期用量明细。"""
        try:
            days = int(self._cfg("retention_days", 180) or 0)
            n = await self.store.purge_old_usage(days)
            if n:
                logger.info(f"[UserGateway] 已清理 {n} 条超期用量明细（保留 {days} 天）")
        except Exception as e:
            logger.warning(f"[UserGateway] 清理超期明细失败（忽略）: {e}")

    async def reload_rules(self) -> None:
        """把权限规则与额度从 SQLite 重新加载进内存缓存（写操作后调用）。

        闸门（M1 的 on_llm_request 钩子）只读这些字典，保证热路径无 IO。
        """
        if not (self.store and self.store.ready):
            return
        try:
            self._effect_user = await self.store.effect_map("user")
            self._effect_group = await self.store.effect_map("group")

            def _group_by_scope(rows: list[dict]) -> dict[str, dict[str, dict[str, Any]]]:
                out: dict[str, dict[str, dict[str, Any]]] = {}
                for row in rows:
                    out.setdefault(str(row["scope_id"]), {})[str(row["period"])] = row
                return out

            self._quota_user = _group_by_scope(await self.store.list_quotas("user"))
            self._quota_group = _group_by_scope(await self.store.list_quotas("group"))

            if self._cfg("debug_log", False):
                logger.info(
                    f"[UserGateway] 规则已加载: 用户权限 {len(self._effect_user)} 条 / "
                    f"群权限 {len(self._effect_group)} 条 / "
                    f"用户额度 {len(self._quota_user)} 条 / 群额度 {len(self._quota_group)} 条",
                )
        except Exception as e:
            # 加载失败按"没有规则"处理（全部继承默认策略），不影响消息通行
            logger.error(f"[UserGateway] 规则加载失败（按无规则处理）: {e}")

    @staticmethod
    def quota_reset_at(period: str, now: Optional[datetime] = None) -> Optional[int]:
        """计算额度的下次重置时间戳（epoch 秒）；``total`` 周期不重置，返回 None。"""
        return quota_mod.next_reset_at(period, now)

    def _apply_guard_priority(self) -> None:
        """把 ``guard_priority`` 配置应用到已注册的 on_llm_request 钩子上。

        装饰器在类定义时就把优先级固定了，这里改 ``extras_configs`` 并对注册表**原地重排**
        （只重排现有条目、不新增，幂等）。作用：当别的插件抢先 stop 了事件导致本插件闸门
        不执行时，可以把这里调大写来抢在前面。
        """
        try:
            from astrbot.core.star.star_handler import EventType, star_handlers_registry

            want = int(self._cfg("guard_priority", 100) or 100)
            handlers = getattr(star_handlers_registry, "_handlers", None)
            if not handlers:
                return
            changed = False
            for md in handlers:
                if (
                    getattr(md, "event_type", None) is EventType.OnLLMRequestEvent
                    and md.handler_module_path == __name__
                    and md.extras_configs.get("priority") != want
                ):
                    md.extras_configs["priority"] = want
                    changed = True
            if changed:
                handlers.sort(key=lambda h: -h.extras_configs.get("priority", 0))
                logger.info(f"[UserGateway] 闸门优先级已应用: priority={want}")
        except Exception as e:
            logger.warning(f"[UserGateway] 应用闸门优先级失败（忽略）: {e}")

    # ------------------------------------------------------------------ #
    # M1 核心：LLM 请求闸门
    # ------------------------------------------------------------------ #
    @astr_filter.on_llm_request()
    async def guard_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """LLM 请求前的权限与额度闸门（PRD §8.1）。

        放行时不做任何事；拒绝时**直发提示**并 ``event.stop_event()`` 掐断本次 LLM 调用。

        AstrBot 侧依据：agent 阶段 `if await call_event_hook(event, OnLLMRequestEvent, req): return`，
        事件被 stop 后不会再向 provider 发请求，也不会影响指令与其它插件的 handler。
        """
        try:
            if not (self.store and self.store.ready):
                return  # fail-open：数据库不可用时不拦任何请求
            subject = self._subject_of(event)
            verdict = self.gate.evaluate(
                subject,
                self._effect_user,
                self._effect_group,
                self._quota_user,
                self._quota_group,
            )
            self._mark_request(event, req)

            if verdict.allow:
                if verdict.observed:
                    # 观察模式：本该被拦但放行，记一条流水便于灰度评估
                    await self._log_denied(subject, verdict, blocked=False)
                elif self._cfg("debug_log", False):
                    logger.info(f"[UserGateway] 放行 {subject.umo} → {verdict.detail}")
                return

            await self._log_denied(subject, verdict, blocked=True)
            await self._notify_denied(event, subject, verdict)
            event.stop_event()
            logger.info(f"[UserGateway] 已拦截 LLM 请求 {subject.umo} → {verdict.detail}")
        except Exception:
            # 红线：权限插件自身异常必须放行，绝不能把用户消息吞掉
            logger.exception("[UserGateway] 闸门异常，已放行（fail-open）")

    @astr_filter.on_llm_response()
    async def record_llm_usage(self, event: AstrMessageEvent, response: LLMResponse) -> None:
        """记录一次 LLM 用量（AstrBot 每轮 agent 结束时触发一次）。

        ⚠️ 已知粒度：该钩子只在「最终无工具调用」的那一轮触发
        （`tool_loop_agent_runner.py:912`），因此**多轮工具循环的中间轮 token 不会计入**。
        对限额而言这是系统性低估，M2 会改为按轮累计或增量读 `provider_stats`。
        """
        try:
            if not (self.store and self.store.ready):
                return
            if not self._cfg("stats_enabled", True):
                return
            await self._record_usage(event, response)
        except Exception:
            logger.exception("[UserGateway] 用量记录异常（忽略，不影响对话）")

    # ------------------------------------------------------------------ #
    # 闸门辅助
    # ------------------------------------------------------------------ #
    @staticmethod
    def _subject_of(event: AstrMessageEvent) -> Subject:
        """从事件里解析出判定对象（任何取值失败都退化为空，不影响放行判断）。"""
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        try:
            group_id = str(event.get_group_id() or "")
        except Exception:
            group_id = ""
        try:
            platform_id = str(event.get_platform_id() or "")
        except Exception:
            platform_id = ""
        return Subject(
            sender_id=sender_id,
            group_id=group_id,
            is_admin=is_admin,
            umo=str(getattr(event, "unified_msg_origin", "") or ""),
            platform_id=platform_id,
        )

    def _mark_request(self, event: AstrMessageEvent, req: Any) -> None:
        """记下本次请求的起始时间与提示词长度（供用量估算与延迟统计）。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            if not umo:
                return
            if len(self._inflight) > 2000:  # 防御性清理，避免长期运行内存增长
                self._inflight.clear()
            self._inflight[umo] = {
                "ts": time.time(),
                "prompt_len": len(str(getattr(req, "prompt", "") or "")),
            }
        except Exception:
            pass

    async def _record_usage(self, event: AstrMessageEvent, response: Any) -> None:
        """把一次 LLM 响应写入统计，并累加相关对象的额度已用量。"""
        subject = self._subject_of(event)
        info = self._inflight.pop(subject.umo, None) or {}

        count_cached = bool(self._cfg("count_cached_tokens", True))
        tok_in_other, tok_in_cached, tok_out, total = quota_mod.tokens_from_usage(
            getattr(response, "usage", None),
            count_cached=count_cached,
        )
        estimated = 0
        if total <= 0:
            # provider 没返回 usage：按字符数粗估，并打标记（控制台会区分展示）
            prompt_len = int(info.get("prompt_len", 0) or 0)
            est_out = quota_mod.estimate_tokens(getattr(response, "completion_text", "") or "")
            est_in = max(1, prompt_len // quota_mod.CHARS_PER_TOKEN) if prompt_len else 0
            if est_in + est_out > 0:
                tok_in_other, tok_in_cached, tok_out = est_in, 0, est_out
                total, estimated = est_in + est_out, 1

        latency_ms = 0
        ts = info.get("ts")
        if ts:
            latency_ms = max(0, int((time.time() - float(ts)) * 1000))

        provider_id = model = ""
        try:
            prov = await self.context.get_using_provider_async(subject.umo)
            if prov is not None:
                provider_id = str((getattr(prov, "provider_config", {}) or {}).get("id", "") or "")
                model = str(prov.get_model() or "")
        except Exception:
            pass

        # 归属对象：群消息记在群上、私聊记在人上（sender_id / group_id 另行保留，可细查）
        scope_type = "group" if subject.group_id else "user"
        scope_id = subject.group_id or subject.sender_id

        await self.store.log_usage(
            platform_id=subject.platform_id,
            umo=subject.umo,
            scope_type=scope_type,
            scope_id=scope_id,
            sender_id=subject.sender_id,
            group_id=subject.group_id,
            kind="llm",
            provider_id=provider_id,
            model=model,
            tok_in_other=tok_in_other,
            tok_in_cached=tok_in_cached,
            tok_out=tok_out,
            estimated=estimated,
            status="ok",
            latency_ms=latency_ms,
        )

        if total <= 0 or not self._cfg("quota_enabled", True):
            return

        # 累加额度（未配置额度的对象是 no-op），并同步内存缓存让下一次判定立刻看到新用量
        warns: list[dict] = []
        for st, sid in (("user", subject.sender_id), ("group", subject.group_id)):
            if not sid:
                continue
            try:
                await self.store.add_used(st, sid, total)
            except Exception as e:
                logger.warning(f"[UserGateway] 累加额度失败（忽略）: {e}")
            warns.extend(self._bump_memory_quota(st, sid, total))

        if warns and self._cfg("notify_admin", False):
            await self._notify_admins(
                "额度预警：" + "；".join(
                    f"{w['scope_type']}:{w['scope_id']} {w['period']} 已用 "
                    f"{w['used']}/{w['limit']}（{w['percent']}%）"
                    for w in warns
                ),
                subject.platform_id,
            )

    def _bump_memory_quota(self, scope_type: str, scope_id: str, tokens: int) -> list[dict]:
        """把刚消费的 token 同步进内存额度缓存，返回本次新触发的预警列表。"""
        table = self._quota_user if scope_type == "user" else self._quota_group
        rows = table.get(scope_id)
        if not rows:
            return []
        ratio = float(self._cfg("warn_ratio", 0.8) or 0)
        warns: list[dict] = []
        for period, row in rows.items():
            before = int(row.get("used_tokens") or 0)
            after = before + tokens
            row["used_tokens"] = after
            limit = int(row.get("limit_tokens") or 0)
            if limit <= 0:
                continue
            # 只在「跨过阈值的那一刻」报一次，避免每次调用都刷屏
            if quota_mod.should_warn(after, limit, ratio) and not quota_mod.should_warn(before, limit, ratio):
                warns.append(
                    {
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "period": period,
                        "used": after,
                        "limit": limit,
                        "percent": round(after * 100 / limit, 1),
                    },
                )
        return warns

    async def _log_denied(self, subject: Subject, verdict: Any, blocked: bool) -> None:
        """写一条拦截/观察流水。``blocked=False`` 表示观察模式（放行但记录）。"""
        try:
            await self.store.log_usage(
                platform_id=subject.platform_id,
                umo=subject.umo,
                scope_type=verdict.scope_type or ("group" if subject.group_id else "user"),
                scope_id=verdict.scope_id or (subject.group_id or subject.sender_id),
                sender_id=subject.sender_id,
                group_id=subject.group_id,
                kind="llm",
                status="denied" if blocked else "ok",
                deny_reason=verdict.reason if blocked else f"observe:{verdict.reason}",
            )
        except Exception as e:
            logger.warning(f"[UserGateway] 写拦截流水失败（忽略）: {e}")

    async def _notify_denied(self, event: AstrMessageEvent, subject: Subject, verdict: Any) -> None:
        """给被拒用户发提示（带冷却），必要时同时通知管理员。"""
        key = f"{verdict.scope_type}:{verdict.scope_id}"
        if not self._notify_cooldown.allow(key):
            return

        if not self._cfg("silent", False):
            text = str(self._cfg("deny_notice", "") or "").strip()
            if text:
                await self._send(event, text)

        if self._cfg("notify_admin", False):
            who = subject.sender_id or subject.umo
            where = f"群 {subject.group_id}" if subject.group_id else "私聊"
            await self._notify_admins(
                f"已拦截一次 LLM 请求：{who}（{where}）｜原因 {verdict.reason}｜{verdict.detail}",
                subject.platform_id,
            )

    @staticmethod
    async def _send(event: AstrMessageEvent, text: str) -> None:
        """直发一条文本。

        必须是**直发**而不是 yield：闸门随后会 stop_event，yield 出去的结果不会被发送
        （pipeline 在 stop 后中断）。
        """
        try:
            await event.send(MessageChain([Plain(str(text))]))
        except Exception as e:
            logger.warning(f"[UserGateway] 发送提示失败（忽略）: {e}")

    async def _notify_admins(self, text: str, platform_id: str) -> None:
        """给全局配置里的管理员发私聊通知（best-effort，失败只记日志）。

        UMO 拼接依据 aiocqhttp 适配器：私聊 session_id 就是对方 QQ，
        故 ``platform_id:FriendMessage:{admin_qq}``。
        """
        try:
            cfg = self.context.get_config()
            admins = list((cfg or {}).get("admins_id", []) or [])
        except Exception as e:
            logger.warning(f"[UserGateway] 读取管理员列表失败（忽略通知）: {e}")
            return
        if not admins:
            return
        for admin in admins:
            aid = str(admin or "").strip()
            if not aid:
                continue
            umo = f"{platform_id}:FriendMessage:{aid}" if platform_id else ""
            if not umo:
                continue
            try:
                await self.context.send_message(umo, MessageChain([Plain(str(text))]))
            except Exception as e:
                logger.warning(f"[UserGateway] 通知管理员 {aid} 失败（忽略）: {e}")

    # ------------------------------------------------------------------ #
    # 后台维护：额度重置 + 冷却清理
    # ------------------------------------------------------------------ #
    async def _maintenance_loop(self) -> None:
        """每分钟一次：把到期的额度清零并重建内存缓存；顺带清理冷却表。"""
        while self._running:
            try:
                await asyncio.sleep(60)
                await self._reset_stale_quotas()
                self._notify_cooldown.prune()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[UserGateway] 维护任务异常（忽略）: {e}")

    async def _reset_stale_quotas(self) -> None:
        """按 ``reset_at`` 清零过期额度（日/月周期），并重建内存缓存。"""
        if not (self.store and self.store.ready):
            return
        rows = await self.store.list_quotas()
        now = int(time.time())
        reset_count = 0
        for row in rows:
            if not quota_mod.is_stale(row.get("reset_at"), now):
                continue
            period = str(row.get("period") or "")
            try:
                await self.store.reset_used(
                    scope_type=str(row.get("scope_type") or ""),
                    scope_id=str(row.get("scope_id") or ""),
                    period=period,
                    reset_at=quota_mod.next_reset_at(period),
                )
                reset_count += 1
            except Exception as e:
                logger.warning(f"[UserGateway] 重置额度失败（忽略）: {e}")
        if reset_count:
            logger.info(f"[UserGateway] 已重置 {reset_count} 条到期额度")
            await self.reload_rules()
