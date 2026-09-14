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

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register

try:  # 包内相对导入（AstrBot 正常加载路径）
    from .store import Store
    from .webui_api import register_apis
except ImportError:  # pragma: no cover - 兼容非包环境（本地调试直接运行本模块）
    from store import Store  # type: ignore
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

        logger.info(f"[UserGateway] 初始化完成（v{self.plugin_version}）")

    async def terminate(self) -> None:
        """关闭数据库连接。"""
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
        now = now or datetime.now()
        try:
            if period == "day":
                nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                return int(nxt.timestamp())
            if period == "month":
                year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
                return int(datetime(year, month, 1, 0, 0, 0).timestamp())
        except Exception:
            return None
        return None
