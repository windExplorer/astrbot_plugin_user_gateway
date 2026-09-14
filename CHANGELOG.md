# 更新日志

本文件记录插件各版本的改动。版本号与 `metadata.yaml` 保持一致。

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
