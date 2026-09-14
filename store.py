"""SQLite 存储层：权限规则、token 额度、用量日志、好友/群缓存。

设计要点（详见 docs/astrbot_plugin_user_gateway_PRD.md §7）：

- 单文件库 ``<插件数据目录>/user_gateway.db``，WAL 模式 + ``synchronous=NORMAL``，
  满足「统计写入频繁 + 控制台随时查询」的并发读写。
- 时间统一用 **epoch 秒**（INTEGER），聚合按 SQLite 的 ``localtime`` 分桶，
  避免 Python 侧时区换算带来的边界问题。
- ``usage_log`` 是统计与明细的唯一事实来源：被拒绝的请求也写一条（``status='denied'``），
  只有真正成功/失败的 LLM 调用写 token 数。
- 所有写操作都是幂等 upsert，冷启动与并发同步都安全。
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

try:
    import aiosqlite
except ImportError as e:  # pragma: no cover - AstrBot 启动时按 requirements 安装
    raise ImportError(
        "astrbot_plugin_user_gateway 需要 aiosqlite，请先安装：pip install aiosqlite",
    ) from e


SCHEMA_VERSION = 1

SCHEMA_SQL = """
-- 插件内部 KV（schema_version、上次同步时间等）
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  INTEGER NOT NULL
);

-- 好友缓存（来自协议端 get_friend_list）
CREATE TABLE IF NOT EXISTS friend_cache (
    platform_id TEXT    NOT NULL,
    uin         TEXT    NOT NULL,
    nickname    TEXT    NOT NULL DEFAULT '',
    remark      TEXT    NOT NULL DEFAULT '',
    is_friend   INTEGER NOT NULL DEFAULT 1,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (platform_id, uin)
);

-- 群缓存（来自协议端 get_group_list）
CREATE TABLE IF NOT EXISTS group_cache (
    platform_id      TEXT    NOT NULL,
    group_id         TEXT    NOT NULL,
    name             TEXT    NOT NULL DEFAULT '',
    member_count     INTEGER NOT NULL DEFAULT 0,
    max_member_count INTEGER NOT NULL DEFAULT 0,
    owner            TEXT    NOT NULL DEFAULT '',
    updated_at       INTEGER NOT NULL,
    PRIMARY KEY (platform_id, group_id)
);

-- 权限规则：feature 预留 'command'（M3 指令权限）
CREATE TABLE IF NOT EXISTS policy (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type  TEXT    NOT NULL,
    scope_id    TEXT    NOT NULL,
    feature     TEXT    NOT NULL DEFAULT 'llm',
    effect      TEXT    NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL,
    UNIQUE (scope_type, scope_id, feature)
);
CREATE INDEX IF NOT EXISTS idx_policy_scope ON policy(scope_type, scope_id);

-- token 额度：period = day | month | total
CREATE TABLE IF NOT EXISTS llm_quota (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type   TEXT    NOT NULL,
    scope_id     TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    limit_tokens INTEGER NOT NULL,
    used_tokens  INTEGER NOT NULL DEFAULT 0,
    mode         TEXT    NOT NULL DEFAULT 'enforce',
    reset_at     INTEGER,
    updated_at   INTEGER NOT NULL,
    UNIQUE (scope_type, scope_id, period)
);
CREATE INDEX IF NOT EXISTS idx_quota_scope ON llm_quota(scope_type, scope_id);

-- 用量 / 事件日志
CREATE TABLE IF NOT EXISTS usage_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    platform_id   TEXT    NOT NULL DEFAULT '',
    umo           TEXT    NOT NULL DEFAULT '',
    scope_type    TEXT    NOT NULL DEFAULT '',
    scope_id      TEXT    NOT NULL DEFAULT '',
    sender_id     TEXT    NOT NULL DEFAULT '',
    group_id      TEXT    NOT NULL DEFAULT '',
    kind          TEXT    NOT NULL DEFAULT 'llm',
    command_name  TEXT    NOT NULL DEFAULT '',
    provider_id   TEXT    NOT NULL DEFAULT '',
    model         TEXT    NOT NULL DEFAULT '',
    tok_in_other  INTEGER NOT NULL DEFAULT 0,
    tok_in_cached INTEGER NOT NULL DEFAULT 0,
    tok_out       INTEGER NOT NULL DEFAULT 0,
    estimated     INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'ok',
    deny_reason   TEXT    NOT NULL DEFAULT '',
    latency_ms    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_ts          ON usage_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_scope       ON usage_log(scope_type, scope_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_kind        ON usage_log(kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_usage_sender      ON usage_log(sender_id, ts DESC);

-- 管理员操作审计
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    actor   TEXT    NOT NULL DEFAULT '',
    action  TEXT    NOT NULL DEFAULT '',
    payload TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts DESC);
"""

# usage_log 中可被前端筛选的字段白名单（防 SQL 注入：只允许固定列名进 SQL）
_USAGE_COLUMNS = (
    "ts", "platform_id", "umo", "scope_type", "scope_id", "sender_id", "group_id",
    "kind", "command_name", "provider_id", "model",
    "tok_in_other", "tok_in_cached", "tok_out", "estimated",
    "status", "deny_reason", "latency_ms",
)


def now_ts() -> int:
    """当前时间戳（秒）。"""
    return int(time.time())


class Store:
    """插件持久层。生命周期与插件实例一致：``open()`` / ``close()``。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def open(self) -> None:
        """建立连接、开启 WAL、建表并记录 schema 版本。可重复调用。"""
        if self._db is not None:
            return
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        # WAL：读写并发不互相阻塞（统计写入频繁，控制台同时查询）
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        self._db = db
        await db.executescript(SCHEMA_SQL)
        await db.commit()
        await self.set_setting("schema_version", str(SCHEMA_VERSION))

    async def close(self) -> None:
        """关闭连接（幂等）。"""
        db, self._db = self._db, None
        if db is not None:
            try:
                await db.close()
            except Exception:
                pass

    @property
    def ready(self) -> bool:
        return self._db is not None

    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store 尚未 open()")
        return self._db

    # ------------------------------------------------------------------ #
    # settings（内部 KV）
    # ------------------------------------------------------------------ #
    async def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        async with self._conn().execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, str(value), now_ts()),
        )
        await db.commit()

    # ------------------------------------------------------------------ #
    # 权限规则
    # ------------------------------------------------------------------ #
    async def get_effect(
        self,
        scope_type: str,
        scope_id: str,
        feature: str = "llm",
    ) -> Optional[str]:
        """取某对象的权限效果；无记录返回 None（表示"继承上级"）。"""
        async with self._conn().execute(
            "SELECT effect FROM policy WHERE scope_type = ? AND scope_id = ? AND feature = ?",
            (scope_type, str(scope_id), feature),
        ) as cur:
            row = await cur.fetchone()
        return row["effect"] if row else None

    async def set_policy(
        self,
        scope_type: str,
        scope_id: str,
        effect: str,
        feature: str = "llm",
        note: str = "",
    ) -> None:
        """写入/更新一条权限规则。``effect='inherit'`` 与删除等价。"""
        db = self._conn()
        if effect == "inherit":
            await db.execute(
                "DELETE FROM policy WHERE scope_type = ? AND scope_id = ? AND feature = ?",
                (scope_type, str(scope_id), feature),
            )
        else:
            await db.execute(
                "INSERT INTO policy(scope_type, scope_id, feature, effect, note, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(scope_type, scope_id, feature) DO UPDATE SET "
                "effect = excluded.effect, note = excluded.note, updated_at = excluded.updated_at",
                (scope_type, str(scope_id), feature, effect, note, now_ts()),
            )
        await db.commit()

    async def list_policies(
        self,
        scope_type: Optional[str] = None,
        feature: str = "llm",
    ) -> list[dict[str, Any]]:
        """列出规则；可按作用域类型过滤。返回 ``{scope_id: effect}`` 形式之外的全量行。"""
        sql = "SELECT scope_type, scope_id, feature, effect, note, updated_at FROM policy WHERE feature = ?"
        args: list[Any] = [feature]
        if scope_type:
            sql += " AND scope_type = ?"
            args.append(scope_type)
        async with self._conn().execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def effect_map(
        self,
        scope_type: str,
        feature: str = "llm",
    ) -> dict[str, str]:
        """取某作用域下 ``{scope_id: effect}`` 映射，供闸门热路径全内存判定。"""
        rows = await self.list_policies(scope_type=scope_type, feature=feature)
        return {str(r["scope_id"]): str(r["effect"]) for r in rows}

    # ------------------------------------------------------------------ #
    # 额度
    # ------------------------------------------------------------------ #
    async def get_quota(self, scope_type: str, scope_id: str, period: str) -> Optional[dict[str, Any]]:
        async with self._conn().execute(
            "SELECT * FROM llm_quota WHERE scope_type = ? AND scope_id = ? AND period = ?",
            (scope_type, str(scope_id), period),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_quotas(self, scope_type: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM llm_quota"
        args: list[Any] = []
        if scope_type:
            sql += " WHERE scope_type = ?"
            args.append(scope_type)
        sql += " ORDER BY updated_at DESC"
        async with self._conn().execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def upsert_quota(
        self,
        scope_type: str,
        scope_id: str,
        period: str,
        limit_tokens: int,
        mode: str = "enforce",
        reset_at: Optional[int] = None,
        keep_used: bool = True,
    ) -> None:
        """新建或更新额度。``keep_used=False`` 时顺带把已用量清零（改额度时用）。"""
        db = self._conn()
        used = 0
        if keep_used:
            old = await self.get_quota(scope_type, scope_id, period)
            used = int(old["used_tokens"]) if old else 0
        await db.execute(
            "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, used_tokens, mode, reset_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET "
            "limit_tokens = excluded.limit_tokens, used_tokens = excluded.used_tokens, "
            "mode = excluded.mode, reset_at = excluded.reset_at, updated_at = excluded.updated_at",
            (scope_type, str(scope_id), period, int(limit_tokens), used, mode, reset_at, now_ts()),
        )
        await db.commit()

    async def delete_quota(self, scope_type: str, scope_id: str, period: Optional[str] = None) -> None:
        db = self._conn()
        if period:
            await db.execute(
                "DELETE FROM llm_quota WHERE scope_type = ? AND scope_id = ? AND period = ?",
                (scope_type, str(scope_id), period),
            )
        else:
            await db.execute(
                "DELETE FROM llm_quota WHERE scope_type = ? AND scope_id = ?",
                (scope_type, str(scope_id)),
            )
        await db.commit()

    async def add_used(self, scope_type: str, scope_id: str, period: str, tokens: int) -> None:
        """累加已用量（只更新已存在的额度行，未配置额度则忽略）。"""
        if tokens <= 0:
            return
        db = self._conn()
        await db.execute(
            "UPDATE llm_quota SET used_tokens = used_tokens + ?, updated_at = ? "
            "WHERE scope_type = ? AND scope_id = ? AND period = ?",
            (int(tokens), now_ts(), scope_type, str(scope_id), period),
        )
        await db.commit()

    async def reset_used(
        self,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        period: Optional[str] = None,
        reset_at: Optional[int] = None,
    ) -> int:
        """清零已用量（可按对象/周期过滤），返回受影响行数。"""
        sql = "UPDATE llm_quota SET used_tokens = 0, updated_at = ?"
        args: list[Any] = [now_ts()]
        if reset_at is not None:
            sql += ", reset_at = ?"
            args.append(reset_at)
        where, wargs = self._scope_where(scope_type, scope_id, period)
        if where:
            sql += " WHERE " + where
            args.extend(wargs)
        db = self._conn()
        cur = await db.execute(sql, args)
        await db.commit()
        return cur.rowcount or 0

    @staticmethod
    def _scope_where(
        scope_type: Optional[str],
        scope_id: Optional[str],
        period: Optional[str],
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        if scope_type:
            parts.append("scope_type = ?")
            args.append(scope_type)
        if scope_id:
            parts.append("scope_id = ?")
            args.append(str(scope_id))
        if period:
            parts.append("period = ?")
            args.append(period)
        return " AND ".join(parts), args

    # ------------------------------------------------------------------ #
    # 用量日志
    # ------------------------------------------------------------------ #
    async def log_usage(self, **fields: Any) -> None:
        """写一条用量/事件日志。未知字段忽略，缺失字段用表默认值。"""
        data = {k: v for k, v in fields.items() if k in _USAGE_COLUMNS}
        data.setdefault("ts", now_ts())
        cols = ", ".join(data.keys())
        marks = ", ".join("?" for _ in data)
        db = self._conn()
        await db.execute(f"INSERT INTO usage_log({cols}) VALUES({marks})", tuple(data.values()))
        await db.commit()

    async def query_usage(
        self,
        *,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        sender_id: Optional[str] = None,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """分页查询明细，返回 ``{total, rows}``。"""
        where, args = self._usage_where(from_ts, to_ts, scope_type, scope_id, sender_id, status, kind)
        db = self._conn()
        async with db.execute(f"SELECT COUNT(*) AS c FROM usage_log{where}", args) as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            f"SELECT * FROM usage_log{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}

    @staticmethod
    def _usage_where(
        from_ts: Optional[int],
        to_ts: Optional[int],
        scope_type: Optional[str],
        scope_id: Optional[str],
        sender_id: Optional[str],
        status: Optional[str],
        kind: Optional[str],
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        if from_ts is not None:
            parts.append("ts >= ?")
            args.append(int(from_ts))
        if to_ts is not None:
            parts.append("ts <= ?")
            args.append(int(to_ts))
        for col, val in (
            ("scope_type", scope_type),
            ("scope_id", scope_id),
            ("sender_id", sender_id),
            ("status", status),
            ("kind", kind),
        ):
            if val:
                parts.append(f"{col} = ?")
                args.append(str(val))
        return (" WHERE " + " AND ".join(parts)) if parts else "", args

    async def summary(self, from_ts: int, to_ts: int) -> dict[str, Any]:
        """区间总览：总量、分项、趋势、榜单、拒绝原因。供控制台总览页使用。"""
        db = self._conn()
        where = "WHERE ts >= ? AND ts <= ?"
        args = [int(from_ts), int(to_ts)]

        async with db.execute(
            f"""SELECT
                    COUNT(*)                                              AS events,
                    SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END)         AS calls,
                    SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END)    AS denied,
                    SUM(CASE WHEN status = 'error'  THEN 1 ELSE 0 END)    AS errors,
                    COALESCE(SUM(tok_in_other), 0)                        AS tok_in_other,
                    COALESCE(SUM(tok_in_cached), 0)                       AS tok_in_cached,
                    COALESCE(SUM(tok_out), 0)                             AS tok_out,
                    SUM(estimated)                                        AS estimated,
                    COUNT(DISTINCT CASE WHEN sender_id <> '' THEN sender_id END) AS users,
                    COUNT(DISTINCT CASE WHEN group_id  <> '' THEN group_id  END) AS groups,
                    COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) AS avg_latency
                FROM usage_log {where}""",
            args,
        ) as cur:
            totals = dict(await cur.fetchone() or {})

        # 按本地日分桶的趋势
        async with db.execute(
            f"""SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS day,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens,
                       SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) AS denied
                FROM usage_log {where}
                GROUP BY day ORDER BY day""",
            args,
        ) as cur:
            trend = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT scope_type, scope_id,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens,
                       COUNT(*) AS events
                FROM usage_log {where} AND scope_id <> ''
                GROUP BY scope_type, scope_id ORDER BY tokens DESC LIMIT 20""",
            args,
        ) as cur:
            top_scopes = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT COALESCE(NULLIF(model, ''), '(未知)') AS model,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens
                FROM usage_log {where} AND kind = 'llm'
                GROUP BY model ORDER BY tokens DESC LIMIT 12""",
            args,
        ) as cur:
            by_model = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT COALESCE(NULLIF(deny_reason, ''), '(其它)') AS reason, COUNT(*) AS cnt
                FROM usage_log {where} AND status = 'denied'
                GROUP BY reason ORDER BY cnt DESC""",
            args,
        ) as cur:
            deny_reasons = [dict(r) for r in await cur.fetchall()]

        totals["tok_total"] = (
            int(totals.get("tok_in_other") or 0)
            + int(totals.get("tok_in_cached") or 0)
            + int(totals.get("tok_out") or 0)
        )
        return {
            "range": {"from": int(from_ts), "to": int(to_ts)},
            "totals": totals,
            "trend": trend,
            "top_scopes": top_scopes,
            "by_model": by_model,
            "deny_reasons": deny_reasons,
        }

    async def purge_old_usage(self, retention_days: int) -> int:
        """清理超期明细；``retention_days<=0`` 表示永久保留。返回删除行数。"""
        if retention_days <= 0:
            return 0
        cutoff = now_ts() - retention_days * 86400
        db = self._conn()
        cur = await db.execute("DELETE FROM usage_log WHERE ts < ?", (cutoff,))
        await db.commit()
        return cur.rowcount or 0

    # ------------------------------------------------------------------ #
    # 好友 / 群缓存
    # ------------------------------------------------------------------ #
    async def upsert_friends(self, platform_id: str, items: Iterable[dict[str, Any]]) -> int:
        """整体覆盖式写入好友缓存（同一平台）。返回写入条数。"""
        rows = [
            (
                platform_id,
                str(it.get("user_id") or it.get("uin") or "").strip(),
                str(it.get("nickname") or "").strip(),
                str(it.get("remark") or "").strip(),
                1,
                now_ts(),
            )
            for it in items
            if str(it.get("user_id") or it.get("uin") or "").strip()
        ]
        if not rows:
            return 0
        db = self._conn()
        await db.executemany(
            "INSERT INTO friend_cache(platform_id, uin, nickname, remark, is_friend, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(platform_id, uin) DO UPDATE SET "
            "nickname = excluded.nickname, remark = excluded.remark, "
            "is_friend = excluded.is_friend, updated_at = excluded.updated_at",
            rows,
        )
        await db.commit()
        return len(rows)

    async def list_friends(
        self,
        platform_id: Optional[str] = None,
        keyword: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """好友列表（昵称/备注模糊搜索），返回 ``{total, rows}``。"""
        where, args = self._cache_where(platform_id, keyword, ("uin", "nickname", "remark"))
        db = self._conn()
        async with db.execute(f"SELECT COUNT(*) AS c FROM friend_cache{where}", args) as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            f"SELECT * FROM friend_cache{where} ORDER BY updated_at DESC, uin LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}

    async def upsert_groups(self, platform_id: str, items: Iterable[dict[str, Any]]) -> int:
        """整体覆盖式写入群缓存（同一平台）。返回写入条数。"""
        rows = [
            (
                platform_id,
                str(it.get("group_id") or "").strip(),
                str(it.get("group_name") or it.get("name") or "").strip(),
                int(it.get("member_count") or 0),
                int(it.get("max_member_count") or 0),
                str(it.get("owner") or "").strip(),
                now_ts(),
            )
            for it in items
            if str(it.get("group_id") or "").strip()
        ]
        if not rows:
            return 0
        db = self._conn()
        await db.executemany(
            "INSERT INTO group_cache(platform_id, group_id, name, member_count, max_member_count, owner, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(platform_id, group_id) DO UPDATE SET "
            "name = excluded.name, member_count = excluded.member_count, "
            "max_member_count = excluded.max_member_count, owner = excluded.owner, "
            "updated_at = excluded.updated_at",
            rows,
        )
        await db.commit()
        return len(rows)

    async def list_groups(
        self,
        platform_id: Optional[str] = None,
        keyword: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """群列表（群名/群号模糊搜索），返回 ``{total, rows}``。"""
        where, args = self._cache_where(platform_id, keyword, ("group_id", "name"))
        db = self._conn()
        async with db.execute(f"SELECT COUNT(*) AS c FROM group_cache{where}", args) as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            f"SELECT * FROM group_cache{where} ORDER BY member_count DESC, group_id LIMIT ? OFFSET ?",
            [*args, int(limit), int(offset)],
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}

    @staticmethod
    def _cache_where(
        platform_id: Optional[str],
        keyword: str,
        search_cols: tuple[str, ...],
    ) -> tuple[str, list[Any]]:
        parts: list[str] = []
        args: list[Any] = []
        if platform_id:
            parts.append("platform_id = ?")
            args.append(platform_id)
        kw = (keyword or "").strip()
        if kw:
            like = f"%{kw}%"
            parts.append("(" + " OR ".join(f"{c} LIKE ?" for c in search_cols) + ")")
            args.extend([like] * len(search_cols))
        return (" WHERE " + " AND ".join(parts)) if parts else "", args

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    async def log_audit(self, actor: str, action: str, payload: str = "") -> None:
        db = self._conn()
        await db.execute(
            "INSERT INTO audit_log(ts, actor, action, payload) VALUES(?, ?, ?, ?)",
            (now_ts(), actor, action, payload),
        )
        await db.commit()

    async def list_audit(self, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        db = self._conn()
        async with db.execute("SELECT COUNT(*) AS c FROM audit_log") as cur:
            row = await cur.fetchone()
            total = int(row["c"]) if row else 0
        async with db.execute(
            "SELECT * FROM audit_log ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (int(limit), int(offset)),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return {"total": total, "rows": rows}
