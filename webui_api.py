"""控制台后端路由（AstrBot 插件 Page 的桥接 API）。

路由前缀 ``/astrbot_plugin_user_gateway``（AstrBot 约定：带插件名、不带 /page），
前端通过 ``window.AstrBotPluginPage.apiGet/apiPost`` 调用，由 Dashboard 转发。

统一返回信封：``{"code": 0, "data": ..., "message": ""}``；``code != 0`` 时前端展示 ``message``。

M0 阶段已实现：健康检查、配置读写、总览统计、好友/群缓存列表、额度增删改查、权限规则读写、明细查询。
好友/群同步（``/sync``）依赖协议端调用，在 M1 随权限内核一起接入。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from astrbot.api import logger

try:  # quart 是 AstrBot 的运行期依赖
    from quart import request
except Exception:  # pragma: no cover - 仅本地静态检查时缺失
    request = None  # type: ignore

ROUTE_PREFIX = "/astrbot_plugin_user_gateway"

# 时间范围简写 → 秒数
_RANGE_SECONDS = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #
def ok(data: Any = None) -> dict:
    """成功信封。"""
    return {"code": 0, "data": data, "message": ""}


def err(message: str, code: int = 1) -> dict:
    """失败信封。"""
    return {"code": code, "data": None, "message": message}


def _q(name: str, default: str = "") -> str:
    """读取查询参数（quart request 不可用时返回默认值）。"""
    try:
        if request is None:
            return default
        return (request.args.get(name) or default).strip()
    except Exception:
        return default


def _qi(name: str, default: int, lo: int, hi: int) -> int:
    """读取并夹紧的整型查询参数。"""
    try:
        return max(lo, min(hi, int(_q(name) or default)))
    except Exception:
        return default


async def _payload() -> dict:
    """读取 JSON 请求体。"""
    try:
        if request is None:
            return {}
        return await request.get_json(silent=True) or {}
    except Exception:
        return {}


def _load_schema() -> dict[str, Any]:
    """读取 ``_conf_schema.json``（配置项的唯一来源：默认值 / 类型 / 可选项）。"""
    try:
        path = Path(__file__).resolve().parent / "_conf_schema.json"
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[UserGateway] 读取 _conf_schema.json 失败: {e}")
        return {}


def _range_bounds(range_key: str, from_raw: str, to_raw: str) -> tuple[int, int]:
    """解析统计时间范围，返回 ``(from_ts, to_ts)``（epoch 秒）。"""
    now = int(time.time())
    if from_raw:
        try:
            return int(float(from_raw)), int(float(to_raw)) if to_raw else now
        except Exception:
            pass
    span = _RANGE_SECONDS.get(range_key or "7d", _RANGE_SECONDS["7d"])
    return now - span, now


def _as_bool(v: Any, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on", "是", "开")


# 列表页展示额度时的周期优先级：同一个对象可能同时配了日/月/累计额度，
# 列表上只展示一条，按 day > month > total 取最紧凑的那个。
_PERIOD_PREF = ("day", "month", "total")


def _quota_index(rows: list[dict]) -> dict[str, dict]:
    """把额度行按 ``scope_id`` 归并为 ``{scope_id: 最优先的那条}``。"""
    out: dict[str, dict] = {}
    for row in rows:
        sid = str(row.get("scope_id") or "")
        if not sid:
            continue
        period = str(row.get("period") or "")
        rank = _PERIOD_PREF.index(period) if period in _PERIOD_PREF else len(_PERIOD_PREF)
        cur = out.get(sid)
        if cur is None:
            out[sid] = {**row, "_rank": rank}
            continue
        if rank < cur.get("_rank", len(_PERIOD_PREF)):
            out[sid] = {**row, "_rank": rank}
    return out


def _fmt_group(row: dict, policy: dict[str, str], quota: dict[str, dict]) -> dict:
    """把群缓存行补上权限状态与额度，供前端表格直接渲染。"""
    gid = str(row.get("group_id") or "")
    q = quota.get(gid) or {}
    return {
        **row,
        "effect": policy.get(gid, "inherit"),
        "quota_limit": q.get("limit_tokens"),
        "quota_used": q.get("used_tokens"),
        "quota_mode": q.get("mode"),
    }


def _fmt_friend(row: dict, policy: dict[str, str], quota: dict[str, dict]) -> dict:
    """把好友缓存行补上权限状态与额度。"""
    uin = str(row.get("uin") or "")
    q = quota.get(uin) or {}
    return {
        **row,
        "display_name": (row.get("remark") or "").strip() or (row.get("nickname") or "").strip() or uin,
        "avatar": f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100",
        "effect": policy.get(uin, "inherit"),
        "quota_limit": q.get("limit_tokens"),
        "quota_used": q.get("used_tokens"),
        "quota_mode": q.get("mode"),
    }


# ---------------------------------------------------------------------- #
# 处理函数（第一个参数固定为插件实例）
# ---------------------------------------------------------------------- #
async def h_ping(plugin) -> dict:
    """健康检查：前端据此判断后端是否就绪，并显示版本号。"""
    return ok(
        {
            "plugin": "astrbot_plugin_user_gateway",
            "version": getattr(plugin, "plugin_version", "unknown"),
            "db_ready": bool(plugin.store and plugin.store.ready),
            "data_dir": str(getattr(plugin, "data_dir", "")),
            "db_path": str(getattr(plugin, "store", None).db_path) if plugin.store else "",
            "server_time": int(time.time()),
        },
    )


async def h_get_config(plugin) -> dict:
    """读取当前生效配置（含 schema 默认值）。"""
    schema = _load_schema()
    cfg = getattr(plugin, "config", None) or {}
    items: dict[str, Any] = {}
    for key, spec in schema.items():
        default = spec.get("default")
        try:
            val = cfg.get(key, default) if hasattr(cfg, "get") else default
        except Exception:
            val = default
        items[key] = val if val is not None else default
    return ok({"items": items, "schema": schema})


async def h_set_config(plugin) -> dict:
    """写入配置（只接受 schema 中存在的键，按 schema 类型转换并做选项校验）。"""
    body = await _payload()
    raw = body.get("items")
    if not isinstance(raw, dict):
        return err("items 必须是对象 {key: value}")

    schema = _load_schema()
    cfg = getattr(plugin, "config", None)
    if cfg is None or not hasattr(cfg, "__setitem__"):
        return err("插件配置不可写（self.config 缺失）")

    changed: list[str] = []
    for key, value in raw.items():
        spec = schema.get(key)
        if not spec:
            continue
        t = spec.get("type")
        try:
            if t == "bool":
                casted: Any = _as_bool(value, default=bool(spec.get("default")))
            elif t == "int":
                casted = int(value)
            elif t == "float":
                casted = float(value)
            else:
                casted = "" if value is None else str(value)
            options = spec.get("options")
            if options and casted not in options:
                return err(f"{key} 的取值必须是 {options} 之一（收到 {casted!r}）")
        except Exception as e:
            return err(f"{key} 类型转换失败: {e}")
        cfg[key] = casted
        changed.append(key)

    if not changed:
        return err("没有可写入的配置项（键名不在 _conf_schema.json 中）")
    try:
        if hasattr(cfg, "save_config"):
            cfg.save_config()
    except Exception as e:
        logger.warning(f"[UserGateway] save_config 失败: {e}")
        return err(f"配置已改内存但落盘失败: {e}")
    return ok({"changed": changed})


async def h_overview(plugin) -> dict:
    """总览统计：区间总量 + 趋势 + 榜单 + 模型占比 + 拒绝原因。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    from_ts, to_ts = _range_bounds(_q("range", "7d"), _q("from"), _q("to"))
    data = await plugin.store.summary(from_ts, to_ts)
    data["range_key"] = _q("range", "7d")
    return ok(data)


async def h_friends(plugin) -> dict:
    """好友列表（含权限状态与额度）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    res = await plugin.store.list_friends(
        platform_id=_q("platform") or None,
        keyword=_q("q"),
        limit=_qi("size", 50, 1, 500),
        offset=_qi("page", 1, 1, 10**6) - 1,
    )
    policy = await plugin.store.effect_map("user")
    quotas = _quota_index(await plugin.store.list_quotas("user"))
    res["rows"] = [_fmt_friend(r, policy, quotas) for r in res["rows"]]
    res["page"] = _qi("page", 1, 1, 10**6)
    res["size"] = _qi("size", 50, 1, 500)
    return ok(res)


async def h_groups(plugin) -> dict:
    """群列表（含权限状态与额度）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    res = await plugin.store.list_groups(
        platform_id=_q("platform") or None,
        keyword=_q("q"),
        limit=_qi("size", 50, 1, 500),
        offset=_qi("page", 1, 1, 10**6) - 1,
    )
    policy = await plugin.store.effect_map("group")
    quotas = _quota_index(await plugin.store.list_quotas("group"))
    res["rows"] = [_fmt_group(r, policy, quotas) for r in res["rows"]]
    res["page"] = _qi("page", 1, 1, 10**6)
    res["size"] = _qi("size", 50, 1, 500)
    return ok(res)


async def h_get_policy(plugin) -> dict:
    """读取权限规则（可按作用域过滤）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    rows = await plugin.store.list_policies(scope_type=_q("scope_type") or None)
    return ok({"items": rows})


async def h_set_policy(plugin) -> dict:
    """写入权限规则。body: {scope_type, scope_id, effect, feature?} 或 {items: [...]}。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    items = body.get("items")
    if not isinstance(items, list):
        items = [body]

    applied = []
    for it in items:
        if not isinstance(it, dict):
            continue
        scope_type = str(it.get("scope_type") or "").strip()
        scope_id = str(it.get("scope_id") or "").strip()
        effect = str(it.get("effect") or "").strip()
        feature = str(it.get("feature") or "llm").strip() or "llm"
        if scope_type not in ("user", "group"):
            return err("scope_type 必须是 user 或 group")
        if not scope_id:
            return err("scope_id 不能为空")
        if effect not in ("allow", "deny", "inherit"):
            return err("effect 必须是 allow / deny / inherit")
        await plugin.store.set_policy(scope_type, scope_id, effect, feature=feature)
        applied.append({"scope_type": scope_type, "scope_id": scope_id, "effect": effect, "feature": feature})

    if not applied:
        return err("没有可应用的规则")
    await plugin.store.log_audit("console", "set_policy", json.dumps(applied, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"applied": applied})


async def h_get_quota(plugin) -> dict:
    """读取额度配置。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    rows = await plugin.store.list_quotas(_q("scope_type") or None)
    return ok({"items": rows})


async def h_set_quota(plugin) -> dict:
    """写入额度。body: {scope_type, scope_id, period, limit_tokens, mode?, reset_used?}。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    items = body.get("items")
    if not isinstance(items, list):
        items = [body]

    applied = []
    for it in items:
        if not isinstance(it, dict):
            continue
        scope_type = str(it.get("scope_type") or "").strip()
        scope_id = str(it.get("scope_id") or "").strip()
        period = str(it.get("period") or "day").strip()
        mode = str(it.get("mode") or "enforce").strip()
        if scope_type not in ("user", "group"):
            return err("scope_type 必须是 user 或 group")
        if not scope_id:
            return err("scope_id 不能为空")
        if period not in ("day", "month", "total"):
            return err("period 必须是 day / month / total")
        if mode not in ("enforce", "observe"):
            return err("mode 必须是 enforce / observe")
        try:
            limit_tokens = int(it.get("limit_tokens"))
        except Exception:
            return err("limit_tokens 必须是整数")
        if limit_tokens < 0:
            return err("limit_tokens 不能为负")

        if limit_tokens == 0:
            await plugin.store.delete_quota(scope_type, scope_id, period)
            applied.append({"scope_type": scope_type, "scope_id": scope_id, "period": period, "deleted": True})
            continue

        reset_at = plugin.quota_reset_at(period)
        await plugin.store.upsert_quota(
            scope_type,
            scope_id,
            period,
            limit_tokens,
            mode=mode,
            reset_at=reset_at,
            keep_used=not _as_bool(it.get("reset_used"), default=False),
        )
        applied.append(
            {"scope_type": scope_type, "scope_id": scope_id, "period": period, "limit_tokens": limit_tokens, "mode": mode},
        )

    if not applied:
        return err("没有可应用的额度")
    await plugin.store.log_audit("console", "set_quota", json.dumps(applied, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"applied": applied})


async def h_reset_quota(plugin) -> dict:
    """清零已用量。body: {scope_type?, scope_id?, period?}（全空 = 全部清零）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    n = await plugin.store.reset_used(
        scope_type=str(body.get("scope_type") or "").strip() or None,
        scope_id=str(body.get("scope_id") or "").strip() or None,
        period=str(body.get("period") or "").strip() or None,
    )
    await plugin.store.log_audit("console", "reset_quota", json.dumps(body, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"reset": n})


async def h_usage(plugin) -> dict:
    """用量/事件明细（分页）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    from_ts, to_ts = _range_bounds(_q("range", "7d"), _q("from"), _q("to"))
    res = await plugin.store.query_usage(
        from_ts=from_ts,
        to_ts=to_ts,
        scope_type=_q("scope_type") or None,
        scope_id=_q("scope_id") or None,
        sender_id=_q("sender_id") or None,
        status=_q("status") or None,
        kind=_q("kind") or None,
        limit=_qi("size", 50, 1, 500),
        offset=_qi("page", 1, 1, 10**6) - 1,
    )
    res["page"] = _qi("page", 1, 1, 10**6)
    res["size"] = _qi("size", 50, 1, 500)
    return ok(res)


async def h_audit(plugin) -> dict:
    """管理员操作审计。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    res = await plugin.store.list_audit(
        limit=_qi("size", 50, 1, 200),
        offset=_qi("page", 1, 1, 10**6) - 1,
    )
    return ok(res)


# ---------------------------------------------------------------------- #
# 注册
# ---------------------------------------------------------------------- #
def _bind(plugin, fn: Callable[[Any], Awaitable[dict]]) -> Callable[[], Awaitable[dict]]:
    """把 ``fn(plugin)`` 绑定成 AstrBot 可直接调用的无参协程（保留函数名便于日志识别）。"""

    async def handler() -> dict:
        try:
            return await fn(plugin)
        except Exception as e:  # 任何异常都转成信封，避免前端只看到 500
            logger.exception(f"[UserGateway] {fn.__name__} 处理异常")
            return err(f"{fn.__name__} 失败: {e}")

    handler.__name__ = f"usergw_{fn.__name__}"
    return handler


def register_apis(plugin) -> None:
    """把控制台路由注册到 AstrBot（在插件 initialize() 中调用）。"""
    routes: list[tuple[str, Callable[[Any], Awaitable[dict]], list[str]]] = [
        ("/ping", h_ping, ["GET"]),
        ("/config", h_get_config, ["GET"]),
        ("/config", h_set_config, ["POST"]),
        ("/overview", h_overview, ["GET"]),
        ("/friends", h_friends, ["GET"]),
        ("/groups", h_groups, ["GET"]),
        ("/policy", h_get_policy, ["GET"]),
        ("/policy", h_set_policy, ["POST"]),
        ("/quota", h_get_quota, ["GET"]),
        ("/quota", h_set_quota, ["POST"]),
        ("/quota/reset", h_reset_quota, ["POST"]),
        ("/usage", h_usage, ["GET"]),
        ("/audit", h_audit, ["GET"]),
    ]
    for path, fn, methods in routes:
        plugin.context.register_web_api(
            f"{ROUTE_PREFIX}{path}",
            _bind(plugin, fn),
            methods,
            f"UserGateway {path}",
        )
    logger.info(f"[UserGateway] 已注册 {len(routes)} 条控制台路由（前缀 {ROUTE_PREFIX}）")
