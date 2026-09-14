# 更新日志

本文件记录插件各版本的改动。版本号与 `metadata.yaml` 保持一致。

## v0.3.0（M2：自定义等级与三层限额、头像后端缓存、bot 最后回复、权限三态控件）

### 1. 自定义等级 + 三层限额（存储结构 v2）

- 新增「等级」：好友与群**各一套**，可自定义名称 / 排序 / 说明；等级可带
  **默认 LLM 权限**（继承 / 放行 / 禁止）与**额度模板**（日 / 月 / 累计 + 拦截 / 观察）。
- 好友 / 群可逐个或批量归级（列表页直接下拉选等级）。
- 限额变成三层结构：**全局默认 → 等级模板 → 对象专属**，解析规则：
  `好友专属 → 好友等级 → 群专属 → 群等级 → 全局`，**命中最具体的一层即止**。
  这样「VIP 等级 50 万」不会被「全局 5 万」反手拦掉，等级才能真正当档位用
  （旧版是「用户额度与群额度都参与判定」，语义已按新模型收紧，详见 README）。
- **额度与用量分离**（schema v1 → v2，自动迁移）：`llm_quota` 只存上限，
  已用量移到独立的 `usage_counter`；换额度模板不再清零用量。
  用量现在**无条件记账**（即使对象没配额度）——等级 / 全局额度是模板，
  判定必须用对象自己的用量。
- `limit_tokens=0` 的语义定为**「明确不限」并占住档位**（更粗的层不再参与）；
  要恢复继承需显式删除该周期（`limit_tokens=null` / `delete=true`）。
  否则「VIP 等级 = 不限」永远会被全局额度先拦掉。

### 2. bot 最后回复（时间 + 类型）

- 私聊 / 群聊列表新增「最后回复」列：bot 在该会话**最后一条消息的时间**与类型
  （**LLM 回复 / 指令回复 / 普通消息**），悬浮可看预览与指令名；可按「最近回复」排序。
- 类型判定不猜文本：LLM 由本插件的 `on_llm_request` 钩子给事件打标；
  指令取 waking 阶段写进 `activated_handlers` 的指令过滤器；其余归为普通消息。
- 记录点是对 `AstrMessageEvent.send` 的补丁（幂等安装、terminate 还原），
  而不是官方的 `after_message_sent` 钩子——后者在流式回复、空链、插件直发等
  多条路径上不会触发，覆盖不全。新配置项 `track_bot_messages` 可关。

### 3. 头像：后端抓取 + 缓存 + 更新

- 新增 `avatar.py`：好友头像与**群头像**都由后端从腾讯 CDN 抓取并缓存到
  `data/plugin_data/astrbot_plugin_user_gateway/avatars/`，按 `avatar_cache_days`
  （默认 30 天）过期重取；抓取失败自动用过期缓存兜底（离线可用）。
- 前端批量按当前页的 id 拉取（data URI，带在途去重），三级兜底：
  后端缓存 → 腾讯 CDN → 首字母色块。
- 列表页新增「更新头像」按钮（强制重取当前页）；配置项 `avatar_source`
  （`backend` 默认 / `cdn` 旧行为 / `off`）、`avatar_cache_days`、`avatar_timeout_sec`。

### 4. 权限操作改为三态分段控件

- 「放行 / 禁止」两个并排按钮容易让人以为是两个可同时打开的开关。
  实际语义是**同一个开关的三个互斥状态**（继承 / 放行 / 禁止），
  现改为一个分段控件并给每个状态配了说明 tooltip；批量操作同步改为
  「批量权限」下拉（放行 / 禁止 / 恢复继承）。

### 5. 其它

- 控制台接口新增：`/levels`、`/levels/delete`、`/subject-level`、`/avatars`、
  `/avatars/refresh`；`/friends` `/groups` 支持服务端权限过滤与「最近回复 / 等级」排序；
  `/subject` 返回等级、额度档位链与最后回复；总览新增 bot 回复类型分布。
- 详情抽屉新增「额度档位链」：逐层显示 好友专属 / 等级 / 群 / 全局 的配置与用量，
  并标出「生效」「已超限」的那一层 —— 界面上能直接回答「这个人到底按哪条额度在算」。
- 自检脚本扩充：`tests/test_gate.py`（档位链与三层额度解析）、
  `tests/test_store.py`（等级 / 归级 / 用量计数 / 最后消息 / **v1→v2 迁移**）。

### 升级注意

- 数据库自动从 v1 迁移到 v2（首次加载时执行，已用量会平移进用量计数表）。
- 若之前用 `load_avatars=false`，现在请把 `avatar_source` 设为 `off`（两者都会关闭头像）。

## v0.2.1（修复：v0.2.0 的安装包缺文件，导致插件加载即报 No module named 'quota'）

现象：装上 v0.2.0 后插件报 `No module named 'quota'`，无法加载。

根因（**发布包缺文件**，不是代码问题）：`build_zip.ps1` 用的是**显式包含清单** `$includeList`，
v0.2.0 新增了三个顶层模块（`gate.py` / `quota.py` / `sync.py`）却忘了加进清单，
于是打出的 zip 里只有 `main.py` / `store.py` / `webui_api.py`。
`main.py` 的相对导入 `from . import quota` 因此失败，而它的 `except ImportError` 兜底
（为「本地直接跑 main.py」准备的平铺导入）又必然再失败一次，
最终把真因掩盖成了看起来像代码问题的 `No module named 'quota'`。

修复：

- `build_zip.ps1`：补进 `gate.py` / `quota.py` / `sync.py`；并新增**打包前自检** ——
  仓库里任何顶层 `.py` 没进 `$includeList` 就直接报错退出，杜绝同类漏包
  （这类问题静默于构建期、暴露于安装期、且错误信息误导，必须让它早期失败）。
- `main.py`：导入段不再吞掉真因。插件目录先加进 `sys.path`（本地调试用），
  相对导入失败时若平铺导入也失败，则抛出同时包含两个原始错误的异常，
  并明确提示「若是 No module named 'gate'/'quota'/'sync' 则说明安装包少了文件」。

## v0.2.0（M1：LLM 权限内核、好友群同步、真实数据）

需求与设计见工作区 `docs/astrbot_plugin_user_gateway_PRD.md`。

### 新增：LLM 闸门（本版核心）

- `gate.py`：**纯逻辑判定内核**（零 IO、不依赖 AstrBot，便于单测）
  - 三级作用域：用户级 > 群级 > 全局默认，效果 `allow` / `deny` / `inherit`
  - 管理员豁免（`admins_id`，可关）
  - 额度判定：按「对象 × 周期」逐条比对，多周期全部生效、命中任一即超限
    （优先报告最紧凑的周期）；`mode=observe` 只记录不拦截
  - `Cooldown`：同一对象 N 秒内只提示一次，避免连续被拒时刷屏
- `main.py` 接入 `@filter.on_llm_request()`：拒绝时**直发提示** + `event.stop_event()`
  掐断本次 LLM 调用（依据 agent 阶段 `if await call_event_hook(...): return`）。
  选它而不是消息级硬拦，因为**只影响 LLM** —— 指令与其它插件的 handler 完全不受影响。
- 判定全部走内存缓存（策略写入后 `reload_rules()` 重建），热路径零 SQL；
  任何异常一律**放行**（fail-open），绝不吞消息。
- `guard_priority` 配置真的生效：`initialize()` 里改写已注册钩子的
  `extras_configs["priority"]` 并原地重排注册表（用于「被别的插件抢先 stop」时抢回位置）。

### 新增：token 记账与额度扣减

- `@filter.on_llm_response()` 记录每次 agent 结束的用量（输入 / 缓存 / 输出、模型、供应商、
  延迟），并按内存额度缓存累加已用量（一条 SQL 覆盖该对象所有周期）。
- provider 不返回 usage 时按字符数粗估并标 `estimated=1`（控制台区分展示，不与真实用量混淆）。
- 额度达 `warn_ratio` 时给管理员发**一次**预警（只在跨过阈值那一刻报）。
- 额度命中时给用户提示 + （可选）通知管理员；被拒事件写流水（`status=denied` + 原因）。
- 后台维护任务每分钟扫描到期的日/月额度并清零、重建内存缓存。
- **已知粒度偏差（计划 M2 处理）**：`on_llm_response` 只在「最终无工具调用的那一轮」触发
  （`tool_loop_agent_runner.py:912`），多轮工具循环的中间轮 token 不计入 —— 对限额是系统性低估。
  M2 会改为按轮累计，或增量读取 AstrBot 自带的 `provider_stats` 表。

### 新增：好友 / 群同步

- `sync.py`：枚举所有 aiocqhttp 平台实例 → `get_client().call_action("get_friend_list" /
  "get_group_list")` → **覆盖式**写入缓存；逐平台收集成败原因，绝不向上抛
  （一个平台失败不影响另一个，也不影响插件其它功能）。
- 后台按 `sync_interval_min` 定时同步（启动后先等 30 秒，避开协议端尚未连上的窗口）；
  控制台「立即同步」走同一条路径（内部互斥锁，避免手动与定时同时打协议端）。
- 顶部状态：`同步于 <时间>` / `同步失败`（悬停看具体原因）。

### 控制台

- 私聊 / 群聊页：新增「今日用量」列、**排序**（最近活跃 / 今日用量 / 昵称 / QQ 号，群另有人数）、
  「同步列表」按钮；**点击对象名打开详情抽屉**。
- 新增 `SubjectDrawer` 组件：对象信息 + 权限一键切换 + 各周期额度进度与快捷设置（含清零）+
  近 7 日用量柱线图 + 近期流水表。
- 新增接口 `POST /sync`、`GET /subject`；`/friends`、`/groups` 支持 `sort` 参数；
  `/ping` 增加同步状态与「内存中已加载的规则数量」（便于确认改动是否真的生效）。

### 测试

- 新增 `tests/test_gate.py`：判定内核 44 项断言（三级作用域优先级、管理员豁免、各开关、
  额度多周期与观察模式、提示冷却、周期重置时间、token 口径换算与估算）。
  用法：`uv run --no-project python tests/test_gate.py`
- `tests/test_store.py` 扩展到覆盖新 DAO：`usage_sums`（按维度聚合 + 非白名单列拒绝）、
  `subject_stats`、`get_friend` / `get_group`、`list_quotas_of`、
  `add_used` 省略周期时全周期累加、`reset_used` 带 `reset_at`。

## v0.1.1（修复：控制台页面在 AstrBot 里直接崩白屏 —— sandbox iframe 与桥接信封）

现象：装好后打开「萌萌权限控制台」页面，控制台报
`SecurityError: Failed to read the 'localStorage' property from 'Window': The document is sandboxed and lacks the 'allow-same-origin' flag`，页面白屏。

根因（两个都属于「AstrBot 插件 Page 的运行环境约束」，之前没有查证就按普通网页写了）：

1. **插件页跑在没有 `allow-same-origin` 的 sandbox iframe 里**。
   AstrBot 的 iframe 属性是 `sandbox="allow-scripts allow-forms allow-downloads"`
   （`dashboard/src/views/PluginPagePage.vue`）。缺少 `allow-same-origin` 意味着
   `localStorage` / `sessionStorage` / `document.cookie` 一访问就抛 `SecurityError`。
   而本插件在 `App.vue` 的 setup 顶层直接读了 `localStorage`（读主题），
   异常发生在 `app.mount()` 期间 → 整个页面直接崩掉。
2. **桥接返回信封用错了格式**。Dashboard 的处理是
   `if (response.data?.status === "error") throw ...; sendBridgeResponse(..., response.data?.data ?? response.data)`
   —— 它会把 `data` 再脱一层、并把 `status === "error"` 当成失败抛错。
   后端原先自创的 `{code, data, message}` 会被脱层后只剩内层 data（成功尚可），
   失败时语义错乱。

修复：

- 新增 `webui-src/src/bridge.ts`：统一封装官方 bridge（`getBridge/getPageBridge/getContext/onContext`）
  与 **sandbox 安全存储** `storageGet/storageSet`（内部 try/catch，localStorage 不可用时退回内存）。
- `App.vue` 不再碰 `localStorage`；主题初值改为按「用户手选 → `<html data-theme>` → bridge `context.isDark`」
  三级回退，并订阅 `onContext` 跟随面板主题切换、同步页面标题。
- 后端信封改为 AstrBot 约定：成功 `{"status":"ok","data":...}`、失败 `{"status":"error","message":...}`；
  前端 `unwrap()` 改为防御式解包（两种信封都能吃）。
- 顺带修正：`apiGet/apiPost` 之前对**任何**错误都会换 4 种端点写法重试，
  业务错误（如参数校验失败）会被重复打到后端（POST 会重复写库）。
  现在只有「路由不存在 / 404」才换下一种，其余错误立即抛出。

验证：`build_webui.ps1` 重新构建通过（产物 1.37MB / gzip 425KB）；后端信封改动后 `tests/test_store.py` 仍全绿。

## v0.1.0（M0 骨架：元数据、存储层、控制台接口与前端工程）

需求与设计见工作区文档 `docs/astrbot_plugin_user_gateway_PRD.md`。本次是首个可安装版本，
只做基础设施，**尚未接入 LLM 拦截**（那是 M1）。

- 清掉 AstrBot `helloworld` 模板：`metadata.yaml` 落为 `astrbot_plugin_user_gateway` /
  展示名「萌萌权限控制台」/ 作者 windExplorer / `astrbot_version: ">=4.22.0"` /
  `pages: permission-console`；新增 `_conf_schema.json`（17 项配置，含逐项 hint）。
- `store.py`：SQLite（WAL）持久层，建 7 张表 —— `settings` / `friend_cache` / `group_cache` /
  `policy`（预留 `feature` 供 M3 指令权限）/ `llm_quota`（day|month|total）/ `usage_log`
  （统计与明细的唯一事实来源，含 `estimated` 标记）/ `audit_log`；含索引、超期清理、
  好友与群缓存的覆盖式 upsert 与模糊搜索。
- `main.py`：插件生命周期（数据目录 → 建库 → 加载规则到内存 → 清理超期明细 → 注册路由），
  `reload_rules()` 把权限/额度加载进内存缓存（M1 闸门热路径零 IO），
  `quota_reset_at()` 计算日/月重置时间戳；全程 fail-open（任何异常都不影响消息通行）。
- `webui_api.py`：13 条控制台路由（健康检查 / 配置读写 / 总览统计 / 好友群列表 / 权限规则 /
  额度增删改查与清零 / 用量明细 / 审计），统一 `{code,data,message}` 信封；
  配置默认值与类型直接从 `_conf_schema.json` 读取，避免两处漂移。
- 前端 `webui-src/`：Vue3 + TS + Vite + Naive UI + ECharts，hash 路由，单文件产物；
  5 个页面（总览 / 私聊 / 群聊 / 限额 / 配置）+ 深浅主题 + 头像组件（CDN 失败回退首字母色块）；
  ECharts 按需引入以控制产物体积。
- 构建与打包：`build_webui.ps1`（从 `metadata.yaml` 注入版本号后 vite build）、
  `build_zip.ps1`（显式包含清单 + 撞版检测 + 顶层结构自校验，zip 内套插件目录）。
- `tests/test_store.py`：存储层自检脚本（44 项断言），
  用法 `uv run --no-project --with aiosqlite python tests/test_store.py`。
