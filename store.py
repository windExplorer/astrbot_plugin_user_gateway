"""SQLite 存储层：权限规则、等级、token 额度、用量、好友/群缓存、最后消息。

设计要点（详见 docs/astrbot_plugin_user_gateway_PRD.md §7）：

- 单文件库 ``<插件数据目录>/user_gateway.db``，WAL 模式 + ``synchronous=NORMAL``，
  满足「统计写入频繁 + 控制台随时查询」的并发读写。
- 时间统一用 **epoch 秒**（INTEGER），聚合按 SQLite 的 ``localtime`` 分桶，
  避免 Python 侧时区换算带来的边界问题。
- ``usage_log`` 是统计与明细的唯一事实来源：被拒绝的请求也写一条（``status='denied'``），
  只有真正成功/失败的 LLM 调用写 token 数。
- **额度与用量分离**（v2）：``llm_quota`` 只存「限额规则」（可能是某个对象专属，也可能是
  全局/某等级这类模板），``usage_counter`` 存「每个对象每个周期的已用量」。
  两者分离才能在引入等级后做到「额度模板换一个，既有用量不清零」。
- 额度解析优先级（最具体的一层生效）：
  ``user 专属 → 用户等级 → group 专属 → 群等级 → global 全局``。
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


SCHEMA_VERSION = 5

# 额度周期：越靠前越"紧凑"，超限时优先报告它
PERIODS: tuple[str, ...] = ("day", "month", "total")

# policy 表的 feature 维度：
#   llm              → LLM 对话权限
#   command          → **对象级指令总权限**（M5：这个好友/群/等级能不能用指令）
#   command:<指令名>  → 某条指令的权限（指令名取 AstrBot 注册表里的完整主名，
#                      形如 "帮助" / "群管 禁言"，别名不单独建键）
# 注意 `command` 与 `command:%` 前缀不重叠，所以两类规则互不干扰。
LLM_FEATURE = "llm"
COMMAND_MASTER_FEATURE = "command"
COMMAND_FEATURE_PREFIX = "command:"


def command_feature(command: str) -> str:
    """指令权限在 ``policy.feature`` 里的键。"""
    return f"{COMMAND_FEATURE_PREFIX}{str(command or '').strip()}"


def command_of_feature(feature: str) -> str:
    """从 feature 键反解指令名（不是指令 feature 时返回空串）。"""
    text = str(feature or "")
    return text[len(COMMAND_FEATURE_PREFIX) :] if text.startswith(COMMAND_FEATURE_PREFIX) else ""

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

-- token 限额规则（v2 起只存「上限」，已用量在 usage_counter）
-- scope_type: user | group（某对象专属） / level（某等级模板） / global（全局模板，scope_id 固定 '*'）
CREATE TABLE IF NOT EXISTS llm_quota (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type   TEXT    NOT NULL,
    scope_id     TEXT    NOT NULL,
    period       TEXT    NOT NULL,
    limit_tokens INTEGER NOT NULL,
    mode         TEXT    NOT NULL DEFAULT 'enforce',
    reset_at     INTEGER,
    updated_at   INTEGER NOT NULL,
    UNIQUE (scope_type, scope_id, period)
);
CREATE INDEX IF NOT EXISTS idx_quota_scope ON llm_quota(scope_type, scope_id);

-- 自定义等级：私聊与群聊各一套（kind 区分），等级本身可带默认权限与模型路由
-- effect         → 默认 **LLM** 权限（inherit | allow | deny）
-- command_effect → 默认 **指令** 权限（M5；inherit | allow | deny，deny 即该等级不能用任何指令）
-- provider_id / fallback_provider_id 存 AstrBot 的**提供商 id**（在 AstrBot「模型提供商」里配置的那个）。
-- 模型选择以「提供商」为单位：AstrBot 里一个提供商就对应一个模型，
-- 再单独存一个模型名只会让配置变含混（v0.4.0 的教训），故不设该列。
CREATE TABLE IF NOT EXISTS quota_level (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                 TEXT    NOT NULL,
    name                 TEXT    NOT NULL,
    description          TEXT    NOT NULL DEFAULT '',
    effect               TEXT    NOT NULL DEFAULT 'inherit',
    command_effect       TEXT    NOT NULL DEFAULT 'inherit',
    sort_order           INTEGER NOT NULL DEFAULT 0,
    provider_id          TEXT    NOT NULL DEFAULT '',
    fallback_provider_id TEXT    NOT NULL DEFAULT '',
    updated_at           INTEGER NOT NULL,
    UNIQUE (kind, name)
);

-- 对象归级：一个好友/群最多属于一个等级
CREATE TABLE IF NOT EXISTS subject_level (
    scope_type TEXT    NOT NULL,
    scope_id   TEXT    NOT NULL,
    level_id   INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (scope_type, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_subject_level ON subject_level(level_id);

-- 用量计数：每个对象每个周期的已用量（与「是否配了额度」无关，始终累加）
CREATE TABLE IF NOT EXISTS usage_counter (
    scope_type  TEXT    NOT NULL,
    scope_id    TEXT    NOT NULL,
    period      TEXT    NOT NULL,
    used_tokens INTEGER NOT NULL DEFAULT 0,
    reset_at    INTEGER,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (scope_type, scope_id, period)
);

-- bot 最后一条消息（每个会话一行，覆盖式更新）
CREATE TABLE IF NOT EXISTS bot_message (
    scope_type  TEXT    NOT NULL,
    scope_id    TEXT    NOT NULL,
    platform_id TEXT    NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL,
    kind        TEXT    NOT NULL DEFAULT 'normal',
    command     TEXT    NOT NULL DEFAULT '',
    preview     TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (scope_type, scope_id)
);
CREATE INDEX IF NOT EXISTS idx_bot_msg_ts ON bot_message(ts DESC);

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
        """建立连接、开启 WAL、建表、跑迁移并记录 schema 版本。可重复调用。"""
        if self._db is not None:
            return
        db = await aiosqlite.connect(self.db_path)
        db.row_factory = aiosqlite.Row
        # WAL：读写并发不互相阻塞（统计写入频繁，控制台同时查询）
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        self._db = db

        old_version = await self._schema_version(db)
        await db.executescript(SCHEMA_SQL)
        if old_version < SCHEMA_VERSION:
            await self._migrate(db, old_version)
        await db.commit()
        await self.set_setting("schema_version", str(SCHEMA_VERSION))

    @staticmethod
    async def _schema_version(db: aiosqlite.Connection) -> int:
        """读库里的 schema 版本；`settings` 表还不存在时视为 0（全新库）。"""
        try:
            async with db.execute("SELECT value FROM settings WHERE key = 'schema_version'") as cur:
                row = await cur.fetchone()
            return int(row["value"]) if row and row["value"] else 0
        except Exception:
            return 0

    @staticmethod
    async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
        """列出表的所有列名（表不存在时返回空集）。"""
        try:
            async with db.execute(f"PRAGMA table_info({table})") as cur:
                return {str(r["name"]) for r in await cur.fetchall()}
        except Exception:
            return set()

    @classmethod
    async def _migrate(cls, db: aiosqlite.Connection, old_version: int) -> None:
        """按版本号做结构迁移。每一步都必须可重入（靠列/表存在性判断，而不是靠版本号）。"""
        if old_version < 2:
            await cls._migrate_v1_to_v2(db)
        if old_version < 3:
            await cls._migrate_v2_to_v3(db)
        if old_version < 4:
            await cls._migrate_v3_to_v4(db)
        if old_version < 5:
            await cls._migrate_v4_to_v5(db)

    @staticmethod
    async def _migrate_v4_to_v5(db: aiosqlite.Connection) -> None:
        """v4 → v5：等级新增「默认指令权限」列（``command_effect``）。"""
        if "command_effect" in await Store._table_columns(db, "quota_level"):
            return
        await db.execute(
            "ALTER TABLE quota_level ADD COLUMN command_effect TEXT NOT NULL DEFAULT 'inherit'"
        )

    @staticmethod
    async def _migrate_v3_to_v4(db: aiosqlite.Connection) -> None:
        """v3 → v4：删掉等级里多余的 ``model`` 列。

        模型选择以「提供商」为单位（AstrBot 里一个提供商绑定一个模型），
        再单独存一个模型名会让配置含混，故移除。重建表以兼容旧版 SQLite。
        """
        if "model" not in await Store._table_columns(db, "quota_level"):
            return
        await db.executescript(
            """
            ALTER TABLE quota_level RENAME TO quota_level_v3;
            CREATE TABLE quota_level (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                kind                 TEXT    NOT NULL,
                name                 TEXT    NOT NULL,
                description          TEXT    NOT NULL DEFAULT '',
                effect               TEXT    NOT NULL DEFAULT 'inherit',
                sort_order           INTEGER NOT NULL DEFAULT 0,
                provider_id          TEXT    NOT NULL DEFAULT '',
                fallback_provider_id TEXT    NOT NULL DEFAULT '',
                updated_at           INTEGER NOT NULL,
                UNIQUE (kind, name)
            );
            INSERT INTO quota_level(id, kind, name, description, effect, sort_order,
                                    provider_id, fallback_provider_id, updated_at)
                SELECT id, kind, name, description, effect, sort_order,
                       provider_id, fallback_provider_id, updated_at
                FROM quota_level_v3;
            DROP TABLE quota_level_v3;
            """,
        )

    @staticmethod
    async def _migrate_v2_to_v3(db: aiosqlite.Connection) -> None:
        """v2 → v3：等级新增「模型路由」三列（主提供商 / 模型名 / 备用提供商）。

        SQLite 的 ``ALTER TABLE ADD COLUMN`` 对「常量默认值」的列是可用的，
        且这里按**列是否存在**判断，重复执行安全。
        """
        cols = await Store._table_columns(db, "quota_level")
        if not cols:
            return  # 全新库：SCHEMA_SQL 已经带上这三列
        for name in ("provider_id", "model", "fallback_provider_id"):
            if name in cols:
                continue
            await db.execute(
                f"ALTER TABLE quota_level ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    async def _migrate_v1_to_v2(db: aiosqlite.Connection) -> None:
        """v1 → v2：额度与用量分离。

        v1 的 ``llm_quota.used_tokens`` 同时承担「限额」和「已用量」，引入等级模板后
        必须拆开（换额度模板不该清空用量），故把已用量搬进 ``usage_counter`` 并重建
        ``llm_quota`` 去掉该列。
        """
        if "used_tokens" not in await Store._table_columns(db, "llm_quota"):
            return
        # 1) 已用量迁入 usage_counter（v1 只有 user/group 两种 scope）
        await db.execute(
            """
            INSERT INTO usage_counter(scope_type, scope_id, period, used_tokens, reset_at, updated_at)
            SELECT scope_type, scope_id, period, used_tokens, reset_at, updated_at
            FROM llm_quota WHERE used_tokens > 0
            ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET
                used_tokens = excluded.used_tokens,
                reset_at = excluded.reset_at
            """,
        )
        # 2) 重建 llm_quota（去掉 used_tokens 列）。
        #    注意先 DROP INDEX：SQLite 重命名表时索引会跟着走，不先删掉的话
        #    新表建索引会因为「同名索引已存在」而被 IF NOT EXISTS 跳过，导致新表没有索引。
        await db.executescript(
            """
            DROP INDEX IF EXISTS idx_quota_scope;
            ALTER TABLE llm_quota RENAME TO llm_quota_v1;
            CREATE TABLE llm_quota (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_type   TEXT    NOT NULL,
                scope_id     TEXT    NOT NULL,
                period       TEXT    NOT NULL,
                limit_tokens INTEGER NOT NULL,
                mode         TEXT    NOT NULL DEFAULT 'enforce',
                reset_at     INTEGER,
                updated_at   INTEGER NOT NULL,
                UNIQUE (scope_type, scope_id, period)
            );
            INSERT INTO llm_quota(id, scope_type, scope_id, period, limit_tokens, mode, reset_at, updated_at)
                SELECT id, scope_type, scope_id, period, limit_tokens, mode, reset_at, updated_at
                FROM llm_quota_v1;
            DROP TABLE llm_quota_v1;
            CREATE INDEX IF NOT EXISTS idx_quota_scope ON llm_quota(scope_type, scope_id);
            """,
        )

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
        feature: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """列出规则；``scope_type`` / ``feature`` 都可选过滤（都不传 = 全部规则）。"""
        sql = "SELECT scope_type, scope_id, feature, effect, note, updated_at FROM policy WHERE 1 = 1"
        args: list[Any] = []
        if feature:
            sql += " AND feature = ?"
            args.append(feature)
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

    async def command_policies(self) -> dict[str, dict[str, dict[str, str]]]:
        """所有指令规则，形如 ``{指令名: {scope_type: {scope_id: effect}}}``（闸门热路径用）。"""
        async with self._conn().execute(
            "SELECT feature, scope_type, scope_id, effect FROM policy WHERE feature LIKE ?",
            (f"{COMMAND_FEATURE_PREFIX}%",),
        ) as cur:
            out: dict[str, dict[str, dict[str, str]]] = {}
            for r in await cur.fetchall():
                cmd = command_of_feature(str(r["feature"]))
                if cmd:
                    out.setdefault(cmd, {}).setdefault(str(r["scope_type"]), {})[
                        str(r["scope_id"])
                    ] = str(r["effect"])
            return out

    async def delete_stale_command_policies(self, alive: list[str]) -> int:
        """清理已失效的指令规则（指令被卸载/改名后残留的行）。返回删除行数。

        Args:
            alive: 当前注册表里仍然存在的指令名列表。
        """
        features = [command_feature(c) for c in alive if str(c or "").strip()]
        db = self._conn()
        if features:
            marks = ", ".join("?" for _ in features)
            cur = await db.execute(
                f"DELETE FROM policy WHERE feature LIKE ? AND feature NOT IN ({marks})",
                [f"{COMMAND_FEATURE_PREFIX}%", *features],
            )
        else:
            cur = await db.execute(
                "DELETE FROM policy WHERE feature LIKE ?", (f"{COMMAND_FEATURE_PREFIX}%",)
            )
        await db.commit()
        return cur.rowcount or 0

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
    ) -> None:
        """新建或更新一条限额规则。

        v2 起这里**只写上限**，已用量在 ``usage_counter``；因此改额度不会清零已用量
        （要清零请用 :meth:`reset_used`）。``scope_type`` 支持
        ``user`` / ``group``（对象专属）与 ``level`` / ``global``（模板）。
        """
        db = self._conn()
        await db.execute(
            "INSERT INTO llm_quota(scope_type, scope_id, period, limit_tokens, mode, reset_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET "
            "limit_tokens = excluded.limit_tokens, mode = excluded.mode, "
            "reset_at = excluded.reset_at, updated_at = excluded.updated_at",
            (scope_type, str(scope_id), period, int(limit_tokens), mode, reset_at, now_ts()),
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

    async def add_used(
        self,
        scope_type: str,
        scope_id: str,
        tokens: int,
        reset_at_map: Optional[dict[str, Optional[int]]] = None,
    ) -> int:
        """把一次消费累加进 ``usage_counter``（日 / 月 / 累计三个周期各一条）。

        与 v1 的关键区别：**即使该对象没有任何额度配置也会累加**。因为等级额度与全局额度
        都是「模板」，判断是否超限时必须用**该对象自己的**用量，所以用量必须无条件记账。

        Args:
            reset_at_map: ``{period: 重置时间戳}``，仅在**首次创建**该周期计数行时写入；
                已存在的行不动 ``reset_at``（否则每次调用都会把重置时间往后推，永远不重置）。

        Returns:
            实际累加的 token 数（``tokens<=0`` 时为 0）。
        """
        if tokens <= 0:
            return 0
        reset_at_map = reset_at_map or {}
        now = now_ts()
        rows = [
            (scope_type, str(scope_id), period, int(tokens), reset_at_map.get(period), now)
            for period in PERIODS
        ]
        db = self._conn()
        await db.executemany(
            "INSERT INTO usage_counter(scope_type, scope_id, period, used_tokens, reset_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET "
            "used_tokens = used_tokens + excluded.used_tokens, updated_at = excluded.updated_at",
            rows,
        )
        await db.commit()
        return int(tokens)

    async def reset_used(
        self,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        period: Optional[str] = None,
        reset_at: Optional[int] = None,
    ) -> int:
        """清零已用量（可按对象/周期过滤），返回受影响行数。作用于 ``usage_counter``。"""
        sql = "UPDATE usage_counter SET used_tokens = 0, updated_at = ?"
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
    # 等级（kind=user 的私聊等级 / kind=group 的群聊等级）
    # ------------------------------------------------------------------ #
    async def list_levels(self, kind: Optional[str] = None) -> list[dict[str, Any]]:
        """列出等级；``kind`` 为空时返回私聊+群聊全部（按 kind、排序号）。"""
        sql = "SELECT * FROM quota_level"
        args: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            args.append(kind)
        sql += " ORDER BY kind, sort_order, id"
        async with self._conn().execute(sql, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_level(self, level_id: int) -> Optional[dict[str, Any]]:
        async with self._conn().execute("SELECT * FROM quota_level WHERE id = ?", (int(level_id),)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_level(
        self,
        kind: str,
        name: str,
        description: str = "",
        effect: str = "inherit",
        sort_order: int = 0,
        level_id: Optional[int] = None,
        provider_id: str = "",
        fallback_provider_id: str = "",
        command_effect: str = "inherit",
    ) -> int:
        """新建 / 更新等级，返回等级 id（``level_id`` 为空则按 ``(kind, name)`` 新建）。

        - ``effect``：等级默认 **LLM** 权限；``command_effect``：等级默认 **指令** 权限；
        - ``provider_id`` / ``fallback_provider_id`` 是模型路由：属于该等级的对象
          走指定提供商，主提供商不可用（未加载 / 熔断）时用备用那个。
        """
        db = self._conn()
        if level_id:
            await db.execute(
                "UPDATE quota_level SET kind = ?, name = ?, description = ?, effect = ?, "
                "command_effect = ?, sort_order = ?, provider_id = ?, fallback_provider_id = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    kind,
                    name,
                    description,
                    effect,
                    command_effect,
                    int(sort_order),
                    str(provider_id or ""),
                    str(fallback_provider_id or ""),
                    now_ts(),
                    int(level_id),
                ),
            )
            await db.commit()
            return int(level_id)
        await db.execute(
            "INSERT INTO quota_level(kind, name, description, effect, command_effect, sort_order, "
            "provider_id, fallback_provider_id, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(kind, name) DO UPDATE SET "
            "description = excluded.description, effect = excluded.effect, "
            "command_effect = excluded.command_effect, "
            "sort_order = excluded.sort_order, provider_id = excluded.provider_id, "
            "fallback_provider_id = excluded.fallback_provider_id, "
            "updated_at = excluded.updated_at",
            (
                kind,
                name,
                description,
                effect,
                command_effect,
                int(sort_order),
                str(provider_id or ""),
                str(fallback_provider_id or ""),
                now_ts(),
            ),
        )
        await db.commit()
        async with db.execute(
            "SELECT id FROM quota_level WHERE kind = ? AND name = ?", (kind, name)
        ) as cur:
            row = await cur.fetchone()
        return int(row["id"]) if row else 0

    async def delete_level(self, level_id: int) -> None:
        """删除等级：连带删掉该等级的额度模板与所有归级记录（对象回到「无等级」）。"""
        db = self._conn()
        await db.execute("DELETE FROM quota_level WHERE id = ?", (int(level_id),))
        await db.execute(
            "DELETE FROM llm_quota WHERE scope_type = 'level' AND scope_id = ?", (str(level_id),)
        )
        await db.execute("DELETE FROM subject_level WHERE level_id = ?", (int(level_id),))
        await db.commit()

    async def level_counts(self) -> dict[int, int]:
        """每个等级下的对象数量 ``{level_id: count}``（控制台列表展示用）。"""
        async with self._conn().execute(
            "SELECT level_id, COUNT(*) AS c FROM subject_level GROUP BY level_id"
        ) as cur:
            return {int(r["level_id"]): int(r["c"]) for r in await cur.fetchall()}

    # ------------------------------------------------------------------ #
    # 对象归级
    # ------------------------------------------------------------------ #
    async def subject_level_map(self, scope_type: str) -> dict[str, int]:
        """取某作用域下 ``{scope_id: level_id}`` 映射（闸门热路径全内存判定用）。"""
        async with self._conn().execute(
            "SELECT scope_id, level_id FROM subject_level WHERE scope_type = ?", (scope_type,)
        ) as cur:
            return {str(r["scope_id"]): int(r["level_id"]) for r in await cur.fetchall()}

    async def get_subject_level(self, scope_type: str, scope_id: str) -> Optional[int]:
        async with self._conn().execute(
            "SELECT level_id FROM subject_level WHERE scope_type = ? AND scope_id = ?",
            (scope_type, str(scope_id)),
        ) as cur:
            row = await cur.fetchone()
        return int(row["level_id"]) if row else None

    async def set_subject_level(self, scope_type: str, scope_id: str, level_id: Optional[int]) -> None:
        """给对象设定等级；``level_id`` 为空表示取消等级（删除记录）。"""
        db = self._conn()
        if not level_id:
            await db.execute(
                "DELETE FROM subject_level WHERE scope_type = ? AND scope_id = ?",
                (scope_type, str(scope_id)),
            )
        else:
            await db.execute(
                "INSERT INTO subject_level(scope_type, scope_id, level_id, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
                "level_id = excluded.level_id, updated_at = excluded.updated_at",
                (scope_type, str(scope_id), int(level_id), now_ts()),
            )
        await db.commit()

    # ------------------------------------------------------------------ #
    # 用量计数（与限额规则分离）
    # ------------------------------------------------------------------ #
    async def usage_map(self, scope_type: str) -> dict[str, dict[str, dict[str, Any]]]:
        """取某作用域的用量 ``{scope_id: {period: row}}``（闸门热路径用）。"""
        async with self._conn().execute(
            "SELECT * FROM usage_counter WHERE scope_type = ?", (scope_type,)
        ) as cur:
            out: dict[str, dict[str, dict[str, Any]]] = {}
            for r in await cur.fetchall():
                row = dict(r)
                out.setdefault(str(row["scope_id"]), {})[str(row["period"])] = row
            return out

    async def get_usage(self, scope_type: str, scope_id: str) -> dict[str, dict[str, Any]]:
        """取单个对象的用量 ``{period: row}``。"""
        async with self._conn().execute(
            "SELECT * FROM usage_counter WHERE scope_type = ? AND scope_id = ?",
            (scope_type, str(scope_id)),
        ) as cur:
            return {str(r["period"]): dict(r) for r in await cur.fetchall()}

    async def counters_due(self, now: Optional[int] = None) -> list[dict[str, Any]]:
        """列出 ``reset_at`` 已到期的用量计数行（维护任务据此清零）。"""
        ts = int(now if now is not None else now_ts())
        async with self._conn().execute(
            "SELECT * FROM usage_counter WHERE reset_at IS NOT NULL AND reset_at <= ?", (ts,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ------------------------------------------------------------------ #
    # bot 最后一条消息
    # ------------------------------------------------------------------ #
    async def set_bot_message(
        self,
        scope_type: str,
        scope_id: str,
        kind: str,
        *,
        ts: Optional[int] = None,
        command: str = "",
        preview: str = "",
        platform_id: str = "",
    ) -> None:
        """记录 bot 在某会话里的最后一条消息（覆盖式，每个会话只留最新一条）。"""
        if not scope_id:
            return
        db = self._conn()
        await db.execute(
            "INSERT INTO bot_message(scope_type, scope_id, platform_id, ts, kind, command, preview) "
            "VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(scope_type, scope_id) DO UPDATE SET "
            "platform_id = excluded.platform_id, ts = excluded.ts, kind = excluded.kind, "
            "command = excluded.command, preview = excluded.preview",
            (
                scope_type,
                str(scope_id),
                platform_id,
                int(ts if ts is not None else now_ts()),
                kind,
                command[:64],
                preview[:120],
            ),
        )
        await db.commit()

    async def bot_message_summary(self, from_ts: int, to_ts: int) -> dict[str, Any]:
        """最后消息的类型分布 + 总会话数（总览页展示「bot 最近都在回什么」）。"""
        db = self._conn()
        async with db.execute(
            "SELECT kind, COUNT(*) AS cnt FROM bot_message WHERE ts >= ? AND ts <= ? GROUP BY kind",
            (int(from_ts), int(to_ts)),
        ) as cur:
            kinds = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(ts), 0) AS latest FROM bot_message"
        ) as cur:
            row = await cur.fetchone()
        return {
            "kinds": kinds,
            "sessions": int(row["c"]) if row else 0,
            "latest_ts": int(row["latest"]) if row else 0,
        }

    async def bot_message_map(self, scope_type: str) -> dict[str, dict[str, Any]]:
        """取某作用域下所有会话的最后消息 ``{scope_id: row}``。"""
        async with self._conn().execute(
            "SELECT * FROM bot_message WHERE scope_type = ?", (scope_type,)
        ) as cur:
            return {str(r["scope_id"]): dict(r) for r in await cur.fetchall()}

    async def get_bot_message(self, scope_type: str, scope_id: str) -> Optional[dict[str, Any]]:
        async with self._conn().execute(
            "SELECT * FROM bot_message WHERE scope_type = ? AND scope_id = ?",
            (scope_type, str(scope_id)),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

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

    async def usage_sums(self, column: str, from_ts: int, to_ts: int) -> dict[str, int]:
        """按某个维度列聚合区间内的 token 总量，返回 ``{值: tokens}``。

        ``column`` 只允许白名单内的固定列名（防注入）：``sender_id`` / ``group_id`` / ``scope_id``。
        用于列表页「今日用量」排序与展示。
        """
        if column not in ("sender_id", "group_id", "scope_id"):
            raise ValueError(f"不支持的聚合列: {column}")
        async with self._conn().execute(
            f"""SELECT {column} AS k,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens
                FROM usage_log
                WHERE ts >= ? AND ts <= ? AND {column} <> ''
                GROUP BY {column}""",
            (int(from_ts), int(to_ts)),
        ) as cur:
            return {str(r["k"]): int(r["tokens"] or 0) for r in await cur.fetchall()}

    async def subject_stats(self, subject_type: str, subject_id: str, from_ts: int, to_ts: int) -> dict[str, Any]:
        """单个对象（人 / 群）在区间内的用量总览与按天曲线。

        - ``user``：按 ``sender_id`` 聚合（这样该用户在**任意群**里的用量都能算进来）
        - ``group``：按 ``group_id`` 聚合
        """
        col = "group_id" if subject_type == "group" else "sender_id"
        where = f"WHERE {col} = ? AND ts >= ? AND ts <= ?"
        args = [str(subject_id), int(from_ts), int(to_ts)]
        db = self._conn()

        async with db.execute(
            f"""SELECT COUNT(*) AS events,
                       SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) AS denied,
                       COALESCE(SUM(tok_in_other), 0)  AS tok_in_other,
                       COALESCE(SUM(tok_in_cached), 0) AS tok_in_cached,
                       COALESCE(SUM(tok_out), 0)       AS tok_out,
                       SUM(estimated)                  AS estimated,
                       COALESCE(AVG(CASE WHEN latency_ms > 0 THEN latency_ms END), 0) AS avg_latency
                FROM usage_log {where}""",
            args,
        ) as cur:
            totals = dict(await cur.fetchone() or {})
        totals["tok_total"] = (
            int(totals.get("tok_in_other") or 0)
            + int(totals.get("tok_in_cached") or 0)
            + int(totals.get("tok_out") or 0)
        )

        async with db.execute(
            f"""SELECT strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS day,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens,
                       SUM(CASE WHEN kind = 'llm' THEN 1 ELSE 0 END) AS calls,
                       SUM(CASE WHEN status = 'denied' THEN 1 ELSE 0 END) AS denied
                FROM usage_log {where}
                GROUP BY day ORDER BY day""",
            args,
        ) as cur:
            series = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            f"""SELECT COALESCE(NULLIF(model, ''), '(未知)') AS model,
                       COALESCE(SUM(tok_in_other + tok_in_cached + tok_out), 0) AS tokens
                FROM usage_log {where} AND kind = 'llm'
                GROUP BY model ORDER BY tokens DESC LIMIT 10""",
            args,
        ) as cur:
            by_model = [dict(r) for r in await cur.fetchall()]

        return {"totals": totals, "series": series, "by_model": by_model}

    async def get_friend(self, uin: str, platform_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """取单个好友缓存行。"""
        sql = "SELECT * FROM friend_cache WHERE uin = ?"
        args: list[Any] = [str(uin)]
        if platform_id:
            sql += " AND platform_id = ?"
            args.append(platform_id)
        async with self._conn().execute(sql + " LIMIT 1", args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_group(self, group_id: str, platform_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        """取单个群缓存行。"""
        sql = "SELECT * FROM group_cache WHERE group_id = ?"
        args: list[Any] = [str(group_id)]
        if platform_id:
            sql += " AND platform_id = ?"
            args.append(platform_id)
        async with self._conn().execute(sql + " LIMIT 1", args) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_quotas_of(self, scope_type: str, scope_id: str) -> list[dict[str, Any]]:
        """取某个对象的所有周期额度。"""
        async with self._conn().execute(
            "SELECT * FROM llm_quota WHERE scope_type = ? AND scope_id = ? ORDER BY period",
            (scope_type, str(scope_id)),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

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
            f"SELECT * FROM group_cache{where} ORDER BY updated_at DESC, group_id LIMIT ? OFFSET ?",
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
