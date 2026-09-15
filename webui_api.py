"""控制台后端路由（AstrBot 插件 Page 的桥接 API）。

路由前缀 ``/astrbot_plugin_user_gateway``（AstrBot 约定：带插件名、不带 /page），
前端通过 ``window.AstrBotPluginPage.apiGet/apiPost`` 调用，由 Dashboard 转发。

⚠️ 返回信封必须遵循 AstrBot 的桥接约定，不能自创格式。Dashboard 侧的处理是
（见 ``dashboard/src/views/PluginPagePage.vue`` 的 ``handleBridgeRequest``）：

```js
if (response.data?.status === "error") throw new Error(response.data.message);
sendBridgeResponse(requestId, true, response.data?.data ?? response.data);
```

也就是说：
  - 成功 → ``{"status": "ok", "data": <payload>}``（Dashboard 只把 ``data`` 转发给前端）
  - 失败 → ``{"status": "error", "message": "..."}``（Dashboard 直接抛错，前端拿到 rejected promise）

M0 阶段已实现：健康检查、配置读写、总览统计、好友/群缓存列表、额度增删改查、权限规则读写、明细查询。
好友/群同步（``/sync``）依赖协议端调用，在 M1 随权限内核一起接入。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
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
    """成功信封（AstrBot 桥接约定）。"""
    return {"status": "ok", "data": data}


def err(message: str) -> dict:
    """失败信封（AstrBot 桥接约定：Dashboard 会据此抛错）。"""
    return {"status": "error", "message": message}


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

# 档位的中文名（与 gate.py 的文案保持一致；消息类型的文案在前端）
LAYER_LABELS: dict[str, str] = {
    "user": "好友专属",
    "user_level": "好友等级",
    "group": "群专属",
    "group_level": "群等级",
    "global": "全局默认",
}


def _by_scope(rows: list[dict]) -> dict[str, dict[str, dict]]:
    """把限额行归并成 ``{scope_id: {period: row}}``。"""
    out: dict[str, dict[str, dict]] = {}
    for row in rows:
        sid = str(row.get("scope_id") or "")
        if sid:
            out.setdefault(sid, {})[str(row.get("period"))] = row
    return out


def _levels_index(rows: list[dict]) -> dict[str, dict[int, dict]]:
    """``{kind: {level_id: level_row}}``，便于给列表行标等级名。"""
    out: dict[str, dict[int, dict]] = {"user": {}, "group": {}}
    for row in rows:
        try:
            out.setdefault(str(row.get("kind") or "user"), {})[int(row["id"])] = row
        except Exception:
            continue
    return out


def _effective_from_chain(chain: list[dict]) -> dict[str, Any]:
    """从 ``plugin.quota_chain()`` 的结果里挑出「生效档位」并压成一行展示数据。

    生效规则来自闸门内核（命中最具体的一层即止），这里只做展示层的取值：
    在该层内按 day > month > total 取第一个真正配了上限的周期。

    Returns:
        ``{layer, layer_label, period, limit, used, mode}``；没配额度时 ``layer`` 为空串。
    """
    for item in chain or []:
        if not item.get("effective"):
            continue
        limits = item.get("limits") or {}
        usage = item.get("usage") or {}
        for period in _PERIOD_PREF:
            row = limits.get(period)
            if not row:
                continue
            limit = int(row.get("limit_tokens") or 0)
            if limit <= 0:
                continue
            layer = str(item.get("layer") or "")
            return {
                "layer": layer,
                "layer_label": LAYER_LABELS.get(layer, str(item.get("label") or layer)),
                "scope_type": str(item.get("scope_type") or ""),
                "scope_id": str(item.get("scope_id") or ""),
                "period": period,
                "limit": limit,
                "used": int(usage.get(period) or 0),
                "mode": str(row.get("mode") or "enforce"),
                "exceeded": bool(item.get("exceeded")),
            }
    return {
        "layer": "",
        "layer_label": "",
        "scope_type": "",
        "scope_id": "",
        "period": "",
        "limit": 0,
        "used": 0,
        "mode": "",
        "exceeded": False,
    }


def _fmt_group(row: dict, ctx: dict[str, Any]) -> dict:
    """把群缓存行补上等级、权限、生效额度、今日用量与「最后回复」。"""
    gid = str(row.get("group_id") or "")
    lv_id = (ctx["subject_level"].get("group") or {}).get(gid)
    lv = (ctx["levels"].get("group") or {}).get(int(lv_id)) if lv_id else None
    q = _effective_from_chain(ctx["plugin"].quota_chain("group", gid))
    bm = ctx["last_bot"].get(gid) or {}
    return {
        **row,
        "avatar_id": gid,
        "effect": ctx["policy"].get(gid, "inherit"),
        "level_id": int(lv_id) if lv_id else None,
        "level_name": (lv or {}).get("name") or "",
        "quota": q,
        "quota_limit": q["limit"] or None,
        "quota_used": q["used"] if q["limit"] else None,
        "quota_mode": q["mode"] or None,
        "quota_layer": q["layer"],
        "today_tokens": int(ctx["today"].get(gid, 0)),
        "last_bot_ts": int(bm.get("ts") or 0),
        "last_bot_kind": str(bm.get("kind") or ""),
        "last_bot_command": str(bm.get("command") or ""),
        "last_bot_preview": str(bm.get("preview") or ""),
    }


def _fmt_friend(row: dict, ctx: dict[str, Any]) -> dict:
    """把好友缓存行补上等级、权限、生效额度、今日用量与「最后回复」。"""
    uin = str(row.get("uin") or "")
    lv_id = (ctx["subject_level"].get("user") or {}).get(uin)
    lv = (ctx["levels"].get("user") or {}).get(int(lv_id)) if lv_id else None
    q = _effective_from_chain(ctx["plugin"].quota_chain("user", uin))
    bm = ctx["last_bot"].get(uin) or {}
    return {
        **row,
        "display_name": (row.get("remark") or "").strip() or (row.get("nickname") or "").strip() or uin,
        "avatar": f"https://q1.qlogo.cn/g?b=qq&nk={uin}&s=100",
        "avatar_id": uin,
        "effect": ctx["policy"].get(uin, "inherit"),
        "level_id": int(lv_id) if lv_id else None,
        "level_name": (lv or {}).get("name") or "",
        "quota": q,
        "quota_limit": q["limit"] or None,
        "quota_used": q["used"] if q["limit"] else None,
        "quota_mode": q["mode"] or None,
        "quota_layer": q["layer"],
        "today_tokens": int(ctx["today"].get(uin, 0)),
        "last_bot_ts": int(bm.get("ts") or 0),
        "last_bot_kind": str(bm.get("kind") or ""),
        "last_bot_command": str(bm.get("command") or ""),
        "last_bot_preview": str(bm.get("preview") or ""),
    }


def _today_bounds() -> tuple[int, int]:
    """今日（本地 0 点 → 现在）的时间戳区间。"""
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


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
            # 同步状态（控制台顶部据此提示「尚未同步 / 同步失败」）
            "sync": {
                "last_at": int(getattr(plugin.scheduler, "last_at", 0) or 0),
                "running": bool(getattr(plugin.scheduler, "running", False)),
                "ok": bool((getattr(plugin.scheduler, "last_result", {}) or {}).get("ok")),
                "error": str((getattr(plugin.scheduler, "last_result", {}) or {}).get("error") or ""),
            },
            # 内存里已加载的规则数量（便于确认改动是否真的生效）
            "rules": {
                "effect_users": len(getattr(plugin, "_effect_user", {}) or {}),
                "effect_groups": len(getattr(plugin, "_effect_group", {}) or {}),
                "levels": len(getattr(plugin, "_level_effect", {}) or {}),
                "leveled_users": len(getattr(plugin, "_subject_level_user", {}) or {}),
                "leveled_groups": len(getattr(plugin, "_subject_level_group", {}) or {}),
                "limits": sum(len(v) for v in (getattr(plugin, "_limits", {}) or {}).values()),
                "usage_users": len(getattr(plugin, "_usage_user", {}) or {}),
                "usage_groups": len(getattr(plugin, "_usage_group", {}) or {}),
            },
            # 头像缓存概况（抓了多少、占多少、最旧的是什么时候）
            "avatars": (
                plugin.avatars.stats() if getattr(plugin, "avatars", None) is not None else {}
            ),
            # 模型路由概况（等级模型是否在生效、哪些提供商被熔断了）
            "model_route": {
                "enabled": bool(plugin._cfg("model_route_enabled", True)),
                "levels": len(getattr(plugin, "_level_route", {}) or {}),
                "routed_sessions": len(getattr(plugin, "_last_route", {}) or {}),
                "circuit": (plugin.circuit.snapshot() if getattr(plugin, "circuit", None) else {}),
            },
        },
    )


async def _list_ctx(plugin, kind: str) -> dict[str, Any]:
    """装配列表页要用的映射。

    等级 / 限额 / 用量 / 归级 / 权限这几张都直接取插件**内存里已有的快照**
    （闸门热路径的同一份数据，reload_rules 时整体重建），避免每翻一页都做十来次查询，
    也保证「界面上看到的」与「闸门实际用的」完全一致。只有等级名与最后回复需要查库。
    """
    store = plugin.store
    from_ts, to_ts = _today_bounds()
    return {
        "plugin": plugin,
        "levels": _levels_index(await store.list_levels()),
        "limits": getattr(plugin, "_limits", {}) or {},
        "usage": {
            "user": getattr(plugin, "_usage_user", {}) or {},
            "group": getattr(plugin, "_usage_group", {}) or {},
        },
        "subject_level": {
            "user": getattr(plugin, "_subject_level_user", {}) or {},
            "group": getattr(plugin, "_subject_level_group", {}) or {},
        },
        "policy": (
            getattr(plugin, "_effect_user", {}) or {}
            if kind == "user"
            else getattr(plugin, "_effect_group", {}) or {}
        ),
        "last_bot": await store.bot_message_map(kind),
        "today": await store.usage_sums("sender_id" if kind == "user" else "group_id", from_ts, to_ts),
    }


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
    """总览统计：区间总量 + 趋势 + 榜单 + 模型占比 + 拒绝原因 + bot 回复类型分布。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    from_ts, to_ts = _range_bounds(_q("range", "7d"), _q("from"), _q("to"))
    data = await plugin.store.summary(from_ts, to_ts)
    data["range_key"] = _q("range", "7d")
    try:
        data["bot"] = await plugin.store.bot_message_summary(from_ts, to_ts)
    except Exception as e:
        logger.warning(f"[UserGateway] 汇总最后消息失败（忽略）: {e}")
        data["bot"] = {"kinds": [], "sessions": 0, "latest_ts": 0}
    return ok(data)


async def h_friends(plugin) -> dict:
    """好友列表（含头像、等级、权限、生效额度、今日用量、最后回复）。

    排序：``active``（默认，按同步时间）/ ``usage``（今日 token）/ ``name`` / ``qq`` /
    ``level``（按等级名）/ ``last``（最近有 bot 回复的排前面）。
    除 ``active`` 外都需要**先取全量再排序分页**，好友量级通常只有数百，
    整表取回可接受（上限 5000 条防爆）。``effect`` 可再按权限过滤（同样是全量语义）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    sort = _q("sort", "active") or "active"
    page = _qi("page", 1, 1, 10**6)
    size = _qi("size", 50, 1, 500)
    platform = _q("platform") or None
    kw = _q("q")
    effect_filter = _q("effect")

    full = sort in ("usage", "name", "qq", "level", "last") or effect_filter in ("allow", "deny", "inherit")
    if full:
        res = await plugin.store.list_friends(platform_id=platform, keyword=kw, limit=5000, offset=0)
    else:
        res = await plugin.store.list_friends(
            platform_id=platform, keyword=kw, limit=size, offset=(page - 1) * size
        )

    ctx = await _list_ctx(plugin, "user")
    rows = [_fmt_friend(r, ctx) for r in res["rows"]]
    total = res["total"]

    if effect_filter in ("allow", "deny", "inherit"):
        rows = [r for r in rows if str(r.get("effect") or "inherit") == effect_filter]

    if sort == "usage":
        rows.sort(key=lambda r: int(r.get("today_tokens") or 0), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: str(r.get("display_name") or ""))
    elif sort == "qq":
        rows.sort(key=lambda r: str(r.get("uin") or ""))
    elif sort == "level":
        rows.sort(key=lambda r: (str(r.get("level_name") or "~"), str(r.get("display_name") or "")))
    elif sort == "last":
        rows.sort(key=lambda r: int(r.get("last_bot_ts") or 0), reverse=True)

    if full:
        total = len(rows)
        rows = rows[(page - 1) * size : page * size]

    return ok({"total": total, "rows": rows, "page": page, "size": size, "sort": sort})


async def h_groups(plugin) -> dict:
    """群列表（含群头像、等级、权限、生效额度、今日用量、最后回复）。排序语义同好友列表。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    sort = _q("sort", "active") or "active"
    page = _qi("page", 1, 1, 10**6)
    size = _qi("size", 50, 1, 500)
    platform = _q("platform") or None
    kw = _q("q")
    effect_filter = _q("effect")

    full = (
        sort in ("usage", "name", "group", "size", "level", "last")
        or effect_filter in ("allow", "deny", "inherit")
    )
    if full:
        res = await plugin.store.list_groups(platform_id=platform, keyword=kw, limit=5000, offset=0)
    else:
        res = await plugin.store.list_groups(
            platform_id=platform, keyword=kw, limit=size, offset=(page - 1) * size
        )

    ctx = await _list_ctx(plugin, "group")
    rows = [_fmt_group(r, ctx) for r in res["rows"]]
    total = res["total"]

    if effect_filter in ("allow", "deny", "inherit"):
        rows = [r for r in rows if str(r.get("effect") or "inherit") == effect_filter]

    if sort == "usage":
        rows.sort(key=lambda r: int(r.get("today_tokens") or 0), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: str(r.get("name") or ""))
    elif sort == "group":
        rows.sort(key=lambda r: str(r.get("group_id") or ""))
    elif sort == "size":
        rows.sort(key=lambda r: int(r.get("member_count") or 0), reverse=True)
    elif sort == "level":
        rows.sort(key=lambda r: (str(r.get("level_name") or "~"), str(r.get("name") or "")))
    elif sort == "last":
        rows.sort(key=lambda r: int(r.get("last_bot_ts") or 0), reverse=True)

    if full:
        total = len(rows)
        rows = rows[(page - 1) * size : page * size]

    return ok({"total": total, "rows": rows, "page": page, "size": size, "sort": sort})


async def h_sync(plugin) -> dict:
    """手动同步好友与群列表（与定时同步共用同一条路径，内部有互斥锁）。"""
    try:
        res = await plugin.scheduler.sync_once()
    except Exception as e:
        logger.exception("[UserGateway] 手动同步异常")
        return err(f"同步失败: {e}")
    if not res.get("ok"):
        return err(str(res.get("error") or "同步失败"))
    return ok(res)


async def h_subject(plugin) -> dict:
    """单个对象（好友 / 群）的详情：基础信息 + 权限 + 额度 + 区间用量与曲线。

    ``type=user`` 按 sender_id 聚合（含该用户在任意群里的用量）；
    ``type=group`` 按 group_id 聚合。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    subject_type = _q("type", "user") or "user"
    subject_id = _q("id")
    if subject_type not in ("user", "group"):
        return err("type 必须是 user 或 group")
    if not subject_id:
        return err("缺少 id")

    days = _qi("days", 7, 1, 90)
    now = int(time.time())
    from_ts, to_ts = now - days * 86400, now

    info: dict | None = None
    try:
        info = (
            await plugin.store.get_group(subject_id)
            if subject_type == "group"
            else await plugin.store.get_friend(subject_id)
        )
    except Exception as e:
        logger.warning(f"[UserGateway] 读取对象缓存失败（忽略）: {e}")

    effect = await plugin.store.get_effect(subject_type, subject_id)
    quotas = await plugin.store.list_quotas_of(subject_type, subject_id)
    stats = await plugin.store.subject_stats(subject_type, subject_id, from_ts, to_ts)

    from_ts_today, to_ts_today = _today_bounds()
    today = await plugin.store.subject_stats(subject_type, subject_id, from_ts_today, to_ts_today)

    recent = await plugin.store.query_usage(
        from_ts=from_ts,
        to_ts=to_ts,
        sender_id=subject_id if subject_type == "user" else None,
        limit=50,
        offset=0,
    )

    # 等级 / 额度档位链 / 用量计数 / 最后回复
    level_id = await plugin.store.get_subject_level(subject_type, subject_id)
    level = await plugin.store.get_level(level_id) if level_id else None
    level_quotas = await plugin.store.list_quotas_of("level", str(level_id)) if level_id else []
    chain = plugin.quota_chain(subject_type, subject_id)
    bot = await plugin.store.get_bot_message(subject_type, subject_id)
    usage = await plugin.store.get_usage(subject_type, subject_id)
    model_route = plugin.model_route_of(subject_type, subject_id)

    return ok(
        {
            "type": subject_type,
            "id": subject_id,
            "info": info,
            "effect": effect or "inherit",
            "quotas": quotas,
            "level_id": level_id,
            "level": level,
            "level_quotas": level_quotas,
            "model_route": model_route,
            "quota_chain": chain,
            "quota": _effective_from_chain(chain),
            "usage": usage,
            "bot": bot,
            "days": days,
            "stats": stats,
            "today": today.get("totals", {}),
            "recent": recent.get("rows", []),
        },
    )


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
    """读取限额配置。

    返回的每行会补上 ``used_tokens``：
    - ``scope_type=user|group``（对象专属）→ 该对象自己的用量计数；
    - ``scope_type=level|global``（模板）→ ``used_tokens=None``，
      因为模板是「每个对象各自的上限」，没有单一的合计值（合计口径看总览页）。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    rows = await plugin.store.list_quotas(_q("scope_type") or None)
    usage = {
        "user": getattr(plugin, "_usage_user", {}) or {},
        "group": getattr(plugin, "_usage_group", {}) or {},
    }
    out = []
    for row in rows:
        st = str(row.get("scope_type") or "")
        sid = str(row.get("scope_id") or "")
        period = str(row.get("period") or "")
        used = None
        if st in ("user", "group"):
            used = int(((usage.get(st) or {}).get(sid) or {}).get(period, {}).get("used_tokens") or 0)
        out.append({**row, "used_tokens": used})
    return ok({"items": out})


async def h_set_quota(plugin) -> dict:
    """写入限额。body: ``{scope_type, scope_id, period, limit_tokens, mode?}`` 或 ``{items: [...]}``。

    ``scope_type`` 支持 ``user`` / ``group``（对象专属）、``level``（等级模板，
    ``scope_id`` 传等级 id）、``global``（全局模板，``scope_id`` 固定 ``*``）。

    ⚠️ ``limit_tokens`` 的语义（见 ``gate.check_quota`` 的说明）：

    - ``0`` = **明确不限**，该档位仍然「占位」，更粗的档位不再生效
      （所以「VIP 等级 = 不限」不会被全局额度反手拦掉）；
    - ``null`` 或 ``delete=true`` = **删掉该周期**，该档位不再占位。
    """
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
        if scope_type not in ("user", "group", "level", "global"):
            return err("scope_type 必须是 user / group / level / global")
        if scope_type == "global":
            scope_id = "*"
        if not scope_id:
            return err("scope_id 不能为空")
        if period not in ("day", "month", "total"):
            return err("period 必须是 day / month / total")
        if mode not in ("enforce", "observe"):
            return err("mode 必须是 enforce / observe")

        raw_limit = it.get("limit_tokens")
        if _as_bool(it.get("delete"), default=False) or raw_limit is None:
            await plugin.store.delete_quota(scope_type, scope_id, period)
            applied.append({"scope_type": scope_type, "scope_id": scope_id, "period": period, "deleted": True})
            continue
        try:
            limit_tokens = int(raw_limit)
        except Exception:
            return err("limit_tokens 必须是整数（0 表示不限；null / delete=true 表示删除）")
        if limit_tokens < 0:
            return err("limit_tokens 不能为负")

        await plugin.store.upsert_quota(
            scope_type,
            scope_id,
            period,
            limit_tokens,
            mode=mode,
            reset_at=plugin.quota_reset_at(period),
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
    """清零已用量（作用于 usage_counter）。

    body: ``{scope_type?, scope_id?, period?}`` —— 全空表示**把所有对象的用量清零**。
    注意：只会动「用量计数」，不会删掉任何额度配置。
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    scope_type = str(body.get("scope_type") or "").strip()
    if scope_type and scope_type not in ("user", "group"):
        return err("scope_type 必须是 user / group（留空则全部清零）")
    n = await plugin.store.reset_used(
        scope_type=scope_type or None,
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
# 等级 / 归级
# ---------------------------------------------------------------------- #
async def h_levels(plugin) -> dict:
    """等级列表（含每个等级的额度模板与成员数）。``?kind=user|group`` 可只取一类。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    levels = await plugin.store.list_levels(_q("kind") or None)
    counts = await plugin.store.level_counts()
    limits = _by_scope(await plugin.store.list_quotas("level"))
    items = []
    for lv in levels:
        lid = int(lv["id"])
        items.append(
            {
                "id": lid,
                "kind": str(lv.get("kind") or "user"),
                "name": str(lv.get("name") or ""),
                "description": str(lv.get("description") or ""),
                "effect": str(lv.get("effect") or "inherit"),
                "sort_order": int(lv.get("sort_order") or 0),
                "provider_id": str(lv.get("provider_id") or ""),
                "fallback_provider_id": str(lv.get("fallback_provider_id") or ""),
                "members": int(counts.get(lid, 0)),
                "quotas": limits.get(str(lid), {}),
            },
        )
    return ok({"items": items, "counts": counts})


async def h_providers(plugin) -> dict:
    """列出可用的对话模型提供商（等级里选「走哪个模型」用）。

    **模型选择以「提供商」为单位**（参考 model_panel 的做法）：AstrBot 里一个提供商就绑定
    一个模型，所以下拉里一项 = 一个提供商，显示成「名称 · 模型」。
    只列**已加载**的：AstrBot 遇到不存在的提供商 id 会直接放弃本次 LLM 请求，
    所以必须让前端只能从这份名单里选。附带返回当前熔断中的提供商与 AstrBot 的默认提供商。
    """
    default_id = ""
    try:
        prov = await plugin.context.get_using_provider_async()
        default_id = str((getattr(prov, "provider_config", {}) or {}).get("id") or "")
    except Exception:
        default_id = ""

    items: list[dict] = []
    try:
        for p in plugin.context.get_all_providers() or []:
            cfg = getattr(p, "provider_config", {}) or {}
            pid = str(cfg.get("id") or "")
            if not pid:
                continue
            # 供应商名取自提供商源/名称，**不能**退化成 type（openai_chat_completion 这类）
            name = str(
                cfg.get("provider_source_id") or cfg.get("name") or cfg.get("provider") or pid
            )
            try:
                model = str(p.get_model() or "")
            except Exception:
                model = ""
            if not model:
                model = str(cfg.get("model") or cfg.get("default_model") or "")
            if name and model and name != model:
                label = f"{name} · {model}"
            else:
                label = name or model or pid
            items.append(
                {
                    "id": pid,
                    "name": name,
                    "model": model,
                    "label": label,
                    "type": str(cfg.get("type") or cfg.get("provider_type") or ""),
                    "modalities": list(cfg.get("modalities") or []),
                    "is_default": bool(default_id and pid == default_id),
                },
            )
    except Exception as e:
        logger.warning(f"[UserGateway] 读取提供商列表失败: {e}")

    items.sort(key=lambda it: (not it["is_default"], it["label"]))
    circuit = getattr(plugin, "circuit", None)
    return ok(
        {
            "items": items,
            "default_id": default_id,
            "circuit_open": sorted(circuit.open_ids()) if circuit else [],
            "route_enabled": bool(plugin._cfg("model_route_enabled", True)),
        },
    )


async def h_set_level(plugin) -> dict:
    """新建 / 更新等级，并可同时写入该等级的额度模板。

    body: ``{id?, kind, name, description?, effect?, sort_order?,
    quotas?: [{period, limit_tokens, mode?}]}``
    （``limit_tokens=0`` 表示删掉该周期的额度）
    """
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    kind = str(body.get("kind") or "user").strip()
    name = str(body.get("name") or "").strip()
    effect = str(body.get("effect") or "inherit").strip()
    if kind not in ("user", "group"):
        return err("kind 必须是 user 或 group")
    if not name:
        return err("等级名称不能为空")
    if effect not in ("inherit", "allow", "deny"):
        return err("effect 必须是 inherit / allow / deny（等级默认 LLM 权限）")
    try:
        sort_order = int(body.get("sort_order") or 0)
    except Exception:
        sort_order = 0
    try:
        level_id = int(body["id"]) if body.get("id") else None
    except Exception:
        level_id = None
    if level_id is not None:
        old = await plugin.store.get_level(level_id)
        if not old:
            return err(f"等级 {level_id} 不存在")

    new_id = await plugin.store.upsert_level(
        kind,
        name,
        description=str(body.get("description") or "").strip(),
        effect=effect,
        sort_order=sort_order,
        level_id=level_id,
        provider_id=str(body.get("provider_id") or "").strip(),
        fallback_provider_id=str(body.get("fallback_provider_id") or "").strip(),
    )
    if not new_id:
        return err("等级写入失败")

    quotas = body.get("quotas")
    changed_quota = False
    if isinstance(quotas, list):
        for q in quotas:
            if not isinstance(q, dict):
                continue
            period = str(q.get("period") or "").strip()
            if period not in ("day", "month", "total"):
                continue
            mode = str(q.get("mode") or "enforce").strip()
            if mode not in ("enforce", "observe"):
                mode = "enforce"
            if _as_bool(q.get("delete"), default=False) or q.get("limit_tokens") is None:
                await plugin.store.delete_quota("level", str(new_id), period)
            else:
                try:
                    limit = max(0, int(q.get("limit_tokens")))
                except Exception:
                    continue
                # limit=0 = 明确「不限」：保留该行占住档位（见 h_set_quota 说明）
                await plugin.store.upsert_quota(
                    "level",
                    str(new_id),
                    period,
                    limit,
                    mode=mode,
                    reset_at=plugin.quota_reset_at(period),
                )
            changed_quota = True

    await plugin.store.log_audit(
        "console",
        "set_level",
        json.dumps({"id": new_id, "kind": kind, "name": name, "quotas": changed_quota}, ensure_ascii=False),
    )
    await plugin.reload_rules()
    return ok({"id": new_id})


async def h_delete_level(plugin) -> dict:
    """删除等级（连带清掉它的额度模板与所有归级记录，相关对象回到「无等级」）。"""
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    try:
        level_id = int(body.get("id"))
    except Exception:
        return err("缺少 id")
    lv = await plugin.store.get_level(level_id)
    if not lv:
        return err(f"等级 {level_id} 不存在")
    await plugin.store.delete_level(level_id)
    await plugin.store.log_audit("console", "delete_level", json.dumps({"id": level_id}, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"deleted": level_id})


async def h_set_subject_level(plugin) -> dict:
    """给好友 / 群设置等级。

    body: ``{scope_type, scope_id, level_id}`` 或 ``{items: [...]}``；
    ``level_id`` 为 0 / null 表示取消等级。
    """
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
        if scope_type not in ("user", "group"):
            return err("scope_type 必须是 user 或 group")
        if not scope_id:
            return err("scope_id 不能为空")
        raw = it.get("level_id")
        try:
            level_id = int(raw) if raw else None
        except Exception:
            level_id = None
        if level_id:
            lv = await plugin.store.get_level(level_id)
            if not lv:
                return err(f"等级 {level_id} 不存在")
            lv_kind = str(lv.get("kind") or "user")
            if lv_kind != scope_type:
                return err(
                    f"「{lv.get('name')}」是{'好友' if lv_kind == 'user' else '群聊'}等级，"
                    f"不能用在{'好友' if scope_type == 'user' else '群聊'}上"
                )
        await plugin.store.set_subject_level(scope_type, scope_id, level_id)
        applied.append({"scope_type": scope_type, "scope_id": scope_id, "level_id": level_id})

    if not applied:
        return err("没有可应用的变化")
    await plugin.store.log_audit("console", "set_subject_level", json.dumps(applied, ensure_ascii=False))
    await plugin.reload_rules()
    return ok({"applied": applied})


# ---------------------------------------------------------------------- #
# 头像
# ---------------------------------------------------------------------- #
async def h_avatars(plugin) -> dict:
    """批量取头像，返回 ``{id: data URI}``（前端直接塞 ``<img src>``）。

    参数：``?type=user|group&ids=1,2,3&force=0&days=30``。
    只对可见行的 id 按需抓取，抓不到的 id 不会出现在结果里（前端退化为首字母色块）。
    """
    cache = getattr(plugin, "avatars", None)
    if cache is None:
        return ok({"items": {}, "stats": {}, "error": "头像缓存未初始化"})
    # 配置页关掉头像时直接返回空（前端退化为首字母色块），逻辑收在服务端、前端不用关心配置
    if (
        str(plugin._cfg("avatar_source", "backend") or "backend").strip().lower() == "off"
        or not _as_bool(plugin._cfg("load_avatars", True), default=True)
    ):
        return ok({"items": {}, "stats": cache.stats(), "requested": 0, "got": 0, "disabled": True})
    kind = _q("type", "user") or "user"
    if kind not in ("user", "group"):
        return err("type 必须是 user 或 group")
    raw = _q("ids")
    ids = [s.strip() for s in raw.split(",") if s.strip()][:400]
    if not ids:
        return ok({"items": {}, "stats": cache.stats(), "requested": 0, "got": 0})
    force = _q("force").lower() in ("1", "true", "yes", "on")
    default_days = int(plugin._cfg("avatar_cache_days", 30) or 0)
    days = _qi("days", default_days, 0, 3650)
    try:
        items = await cache.ensure_map(kind, ids, force=force, max_age_days=days)
    except Exception as e:
        logger.warning(f"[UserGateway] 取头像失败（忽略）: {e}")
        items = {}
    return ok({"items": items, "stats": cache.stats(), "requested": len(ids), "got": len(items)})


async def h_avatars_refresh(plugin) -> dict:
    """更新头像缓存。

    body: ``{type?, ids?: [...]}``；``ids`` 为空表示「把该类型**所有已缓存**的头像重取一遍」
    （用于好友换了头像之后手动刷新）。
    """
    cache = getattr(plugin, "avatars", None)
    if cache is None:
        return err("头像缓存未初始化")
    if not (plugin.store and plugin.store.ready):
        return err("数据库未就绪")
    body = await _payload()
    kind = str(body.get("type") or "user").strip()
    if kind not in ("user", "group"):
        return err("type 必须是 user 或 group")
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        prefix = "group_" if kind == "group" else "user_"
        try:
            ids = [f.stem[len(prefix):] for f in cache.root.glob(f"{prefix}*.img")]
        except Exception:
            ids = []
    if not ids:
        return ok({"refreshed": 0, "failed": 0, "stats": cache.stats()})
    got = await cache.ensure(kind, ids, force=True)
    await plugin.store.log_audit(
        "console", "refresh_avatars", json.dumps({"type": kind, "n": len(ids)}, ensure_ascii=False)
    )
    return ok({"refreshed": len(got), "failed": len(ids) - len(got), "stats": cache.stats()})


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
        ("/sync", h_sync, ["POST"]),
        ("/subject", h_subject, ["GET"]),
        ("/policy", h_get_policy, ["GET"]),
        ("/policy", h_set_policy, ["POST"]),
        ("/quota", h_get_quota, ["GET"]),
        ("/quota", h_set_quota, ["POST"]),
        ("/quota/reset", h_reset_quota, ["POST"]),
        ("/levels", h_levels, ["GET"]),
        ("/levels", h_set_level, ["POST"]),
        ("/levels/delete", h_delete_level, ["POST"]),
        ("/subject-level", h_set_subject_level, ["POST"]),
        ("/providers", h_providers, ["GET"]),
        ("/avatars", h_avatars, ["GET"]),
        ("/avatars/refresh", h_avatars_refresh, ["POST"]),
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
