# 更新日志

本文件记录各版本的改动。版本号与 `metadata.yaml` 保持一致。

## v0.7.1（列表体验：等级筛选 / 默认按最后回复倒序 / 等级数量）

三处列表体验调整，私聊与群聊两页一致：

- **默认排序改为「最近回复」倒序**：进页面先看「谁最近还在用」，从没回复过的对象沉底。
  排序下拉里「最近回复」提到第一个（后端 `sort` 默认值也一并改成 `last`）。
- **新增「等级」筛选**：可选「全部等级 / 未分组 / 某个等级」，
  与已有的「LLM 权限 / 指令权限」筛选叠加生效。
  后端 `/friends`、`/groups` 新增 `level_id` 参数（空 = 全部，`0` = 未分组，`>0` = 等级 id）。
- **等级数量只在「下拉」里显示，且实时刷新**：
  - 等级管理页表格去掉「成员数」列；
  - 私聊 / 群聊页的「等级筛选」下拉与「等级」列下拉继续带数量 `名称（N）`；
  - 每次刷新列表时顺带重新拉一次等级，所以在等级管理页改完归级回到列表就是新数量
    （之前只在进页面时取一次，数量会停在旧值）。

## v0.7.0（M6：统计页 —— 明细 / 指令维度 / 审计 / 导出）

### 背景

后端的 `/usage`（明细）与 `/audit`（审计）从 M0 就在，但**前端一直没有页面用它们**，
PRD §4.4 要求的「明细 + CSV 导出」以及 M3 追加的「最常触发 / 被拒指令 Top10、时段热力图」
都还空着。本次补齐，新增「统计」页。

### 新增

- **统计页（`#/stats`）**
  - 用量明细：区间（今日 / 7 日 / 30 日）+ 类型（LLM / 指令）+ 状态（正常 / 被拒）+
    对象 ID + 发起人筛选，分页，token 分项与「估算」标记；
  - **CSV 导出**：后端生成（带 UTF-8 BOM，Excel 不乱码），前端 Blob 落地；
    沙箱可能拦下载，所以同时弹出内容供复制；
  - **最常触发的指令 / 最常被拦的指令 Top10**；
  - **LLM 活跃时段热力图**（24 小时 × 星期，按本地时间分桶）；
  - **操作审计**（谁在什么时候改了什么规则，payload 可悬停查看）。
- **指令触发流水**：新增配置 `track_command_usage`（默认开），把成功执行的指令也记一条
  `kind='command'` 的流水，用于「最常触发的指令」。它与 LLM 口径完全分开
  （`calls` / token 只统计 `kind='llm'`，已加自检固定这一点）；关掉后统计页只会看到被拦记录。
- 后端：`store.top_commands()` / `store.hour_heatmap()` / `store.export_usage_csv()`；
  新接口 `/stats/commands`、`/usage/export`。
- 私聊 / 群聊页支持按**指令权限**过滤；两页顶部新增「权限怎么算」说明卡
  （讲清「继承 / 放行 / 禁止」是**互斥三态**，以及 LLM 与指令是两套独立规则）。

### 修正

- 「指令」页的说明还写着「指令权限不参与等级档位」——M5 之后已不成立，改正为
  「等级只管整体能不能用指令，且压得住单条指令的放行」。
- 群聊页挂着「群成员级管控将在 M3 上线」的过时标签（该功能**实际未实现**），移除；
  README 里把它从「M3 已完成」改为「待办」。

### 自检

- `tests/test_store.py` 新增 [5.1]：指令 Top 榜（含排序）、热力图分桶、
  CSV（BOM / 表头 / 行数 / 按类型过滤）、以及「指令流水不污染 LLM 口径」。

## v0.6.0（M5：指令权限补齐「对象 / 等级」维度）

v0.5.0 的指令权限只能**按指令名**配（一级一条规则），少了 LLM 权限那样的「对象维度」：
没法一次性说「这个等级 / 这个群不能用任何指令」。本次补齐，两个维度并存。

### 能力

- **私聊 / 群聊页新增「指令权限」列**：三态（继承 / 放行 / 禁止），
  与「LLM 权限」并排、但完全独立；批量操作下拉也分成「LLM 权限 / 指令权限」两组。
- **等级新增「默认指令权限」（三态）**：设为**禁止** → 该等级下的好友 / 群
  **不能用任何指令**；等级列表的「默认权限」列同时展示 LLM 与指令两套结论。
- 详情抽屉的权限卡拆成两行（LLM / 指令），并显示指令权限的**生效来源**
  （好友专属 / 好友等级 / … / 系统默认）。
- 「指令」页顶部说明补上了「对象维度在哪配」，避免只看到单条指令规则。

### 判定：两段式（`gate.check_command`）

```
第一段 · 对象级总权限：好友专属 → 好友等级 → 群专属 → 群等级 → 全局
        —— 「这个人 / 这个群能不能用指令」；命中 deny 直接拦
第二段 · 该条指令的规则：好友专属 → 群专属 → 这条指令的全局开关
        —— 「这条指令能不能用」；都没有则用配置「指令默认策略」兜底
```

- 对象级 `deny` 是**硬拦截**，但更具体的层可以覆盖：把整个等级设为禁止，
  再给某个人设「放行」，就能实现「整档禁止 + 白名单」；
- 单条指令的规则**不带等级层**（等级只管整体开关，不管某一条指令），
  所以「等级禁止」压得住单条指令的「放行」，反之不行（要在对象级开白名单）；
- 兜底仍是与 LLM 完全独立的 `default_command_effect`。

### 存储与实现

- 复用 `policy` 表：对象级指令权限的 feature 键是 **`command`**（单条指令仍是
  `command:<指令名>`，前缀不重叠、互不干扰），**无需新建表**；
- 结构 v4 → v5：`quota_level` 新增 `command_effect` 列（`ALTER TABLE`，可重入）；
- `gate.py` 抽出通用档位链解析 `_first_effect()`，LLM 权限与指令总权限共用同一条链
  （行为不变，原有用例全绿）；
- `/policy` 放宽 feature 校验（`llm` / `command` / `command:<指令名>`）；
  `/subject` 返回 `command_master`（显式值 + 生效层）；`/levels` 返回 `effect_command`。
- 自检新增 [14]：对象级指令权限（群禁止 → 整群拦截、好友放行 → 白名单、
  等级禁止 → 整档拦截、对象级 deny 优先于单条指令、对象级放行仍受单条指令约束）；
  store 侧覆盖 `command_effect` 读写与 v5 迁移。

## v0.5.0（M4：指令权限 —— 按指令名配置禁用 / 放行）

之前只有 LLM 权限，指令权限一直没落地（PRD 里排到 M3）。本次补齐。

### 能力

- 控制台新增 **「指令」页**：自动列出 AstrBot 里**所有已注册的指令**
  （带说明、别名、来源插件、指令组标记），每条指令一份独立规则。
- 每条指令有：

  - **全局策略**（三态：继承 / 放行 / 禁止）——一键「全局禁用这条指令」；
  - **例外规则**：针对某好友 / 某群单独放行或禁止（用于「全局禁止 + 个别白名单」这类场景）。

- 解析顺序：**好友专属 → 群专属 → 该指令的全局策略 → 配置里的「指令默认策略」**，
  **命中最具体的一层即止**。
- 「指令默认策略」可设为 `deny` 变成**白名单模式**（未显式放行的对象用不了任何指令）；
  它与 LLM 的「全局默认策略」**相互独立**，把 LLM 设成白名单不会顺带关掉所有指令。
- 被拦截时给用户发提示（文案可配，支持 `{command}` 占位），管理员豁免沿用配置。

### 拦截机制（AstrBot 4.27）

- 本插件注册一个 **「所有消息 + 高优先级」** 的处理器
  （`@filter.event_message_type(ALL, priority=command_guard_priority)`）。
  waking 阶段已把本次命中的 handler 写进 `activated_handlers`
  （`waking_check/stage.py:253`），而 process 阶段**按优先级降序**依次执行这些 handler、
  并在每个 handler 前检查 `event.is_stopped()`（`star_request.py:36-38`）——
  所以在这个处理器里 `stop_event()` 就能让后面的指令 handler 不执行（也不会进 LLM）。
- 指令名取 `get_complete_command_names()[0]`（完整主名，子指令形如 `父 子`），
  **别名与主名共用一条规则**（不单独建键）。
- 指令权限**不参与等级档位**：等级是「模型 + 额度 + LLM 权限」的概念，
  让「等级 = 禁止」顺带禁掉指令会让两件事莫名牵连。
- 存储复用 `policy` 表：`feature = 'command:<指令名>'`（LLM 仍是 `'llm'`），
  `scope_type` 扩展出 `global`（`scope_id='*'`），**无需改表结构**。
- 拦截流水记 `kind='command'`，不污染 LLM 的调用统计口径。
- 指令被卸载/改名后残留的规则可在页面上「清理失效规则」。

### 其它

- 配置页新增「指令权限」分区；`/commands`、`/commands/prune` 接口；
  `/policy` 支持 `feature` 参数与 `scope_type=global`（LLM 的全局默认仍走配置项，不允许写库）。
- 自检扩充：[13] 指令权限判定链（好友/群/全局/默认、白名单模式、与 LLM 默认互不影响）、
  store 侧的指令 feature 读写与失效规则清理。

## v0.4.1（修正模型选择：两个可搜索下拉，去掉多余的「模型名」）

v0.4.0 的等级模型选择把「提供商」和「模型名」拆成两个输入框，配置含混、也不符合
AstrBot 的模型口径。本次按 model_panel 的做法重做：

- **模型以「提供商」为单位**：AstrBot 里一个模型提供商就绑定一个模型，
  所以下拉里**一项 = 一个模型**，显示为「供应商 · 模型」
  （供应商名取 `provider_source_id` / `name`，不用 `openai_chat_completion` 这类 type）。
- **只留两个下拉**：主模型、备用模型；都支持**输入筛选**（filterable）、可清空。
  当前默认的模型会标注「当前默认」，处于熔断的会标注「熔断中」。
- 等级编辑器里不再有单独的「模型名」输入框；`selected_model` 也不再下发
  （换提供商即换模型，AstrBot 会用该提供商自己的模型）。
- 存储结构 v3 → v4：去掉 `quota_level.model` 列（重建表迁移，老数据保留）。
- `/providers` 增加 `name` / `label` / `is_default` 字段；等级列表的「模型」列直接用
  「供应商 · 模型」展示主/备模型。自检同步更新（gate 的路由/降级用例、store 的 v2→v4 迁移）。

## v0.4.0（M3：等级模型路由 —— 主模型 / 备用模型）

### 能力

- **等级可指定模型**：主模型（提供商）+ 可选模型名 + 备用模型（提供商）。
  属于该等级的对象，每次 LLM 请求前都会被下发到指定提供商。
- **路由语义**：**私聊按「好友等级」、群聊按「群等级」**。
  有意与权限/额度的档位链不同 —— 群聊的上下文与计费都属于「群」（同一个 umo），
  若按发言人切模型会让同一个群的上下文串味；所以群聊里**不会**回落到发言人的好友等级。
- **主模型不可用 → 自动走备用**：两种情况都会落过去
  ① 主提供商当前未加载（被 disabled / 删了）；
  ② 主提供商连续失败达到阈值触发**熔断**（默认连续 2 次，熔断 300s，到点自动恢复）。
  插件只会统计**自己下发过**的提供商，不碰 AstrBot 的默认模型。
- 提供商列表由后端 `/providers` 提供（只列**已加载**的），前端只能从名单里选。

### 实现依据（AstrBot 4.27，全部走官方通道，没有改框架内部）

- 下发布点是 `@filter.on_waiting_llm_request()`（`internal.py:214`）。它早于
  `_select_provider()`（`:230`，读 `event.get_extra("selected_provider")`）与 `req` 构造
  （`:238`，读 `selected_model` → `req.model`），所以来得及生效；
  而 `on_llm_request`（`:333`）在提供商已选定之后才触发，那里只能再兜一次 `req.model`。
- **绝不设未知的提供商 id**：`astr_main_agent.py:242` 遇到不存在的 id 会直接放弃本次请求并报错。
  插件先做「已加载 + 未熔断」校验，都不满足就**不干预**（走 AstrBot 默认模型）。
- 模型名只作用于主提供商；落到备用时用备用自己的默认模型
  （与 AstrBot 自己切提供商时的处理一致，`astr_main_agent.py:1684`）。
- AstrBot 内置的 `fallback_provider_ids` 是**全局**配置，且在 `MainAgentBuildConfig`
  构造时就固定（`internal.py:143`），插件无法按会话注入 —— 所以等级级的主/备切换由
  本插件的「选择阶段 + 熔断」实现。把等级用到的备用提供商也加进 AstrBot 全局
  「备用提供商」列表，还能额外获得框架级的运行时兜底。

### 其它

- 控制台：新增 `/providers` 接口；「限额 → 等级管理」里可直接选主/备模型与模型名；
  等级列表新增「模型」列；详情抽屉显示「生效模型」；配置页新增「模型路由」分区
  （总开关 / 熔断阈值 / 熔断时长）。
- 存储结构 v2 → v3：`quota_level` 新增 `provider_id` / `model` / `fallback_provider_id`
  三列（自动 `ALTER TABLE` 迁移，可重入）。

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
