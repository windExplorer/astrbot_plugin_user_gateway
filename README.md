# 萌萌权限控制台（astrbot_plugin_user_gateway）

AstrBot 插件：**按好友 / 按群细粒度管控 LLM 使用权限与 token 额度**，并提供用量统计与可视化控制台。

- 插件展示名：**萌萌权限控制台**
- 需求与设计文档：工作区 `docs/astrbot_plugin_user_gateway_PRD.md`
- 支持平台：`aiocqhttp`（好友 / 群列表同步依赖 OneBot 接口）；权限与额度内核不依赖具体平台
- 要求 AstrBot：`>=4.22.0`

## 功能

| 模块 | 说明 | 状态 |
| --- | --- | --- |
| LLM 权限 | 用户级 > 群级 > 全局默认三级作用域，`放行 / 禁止 / 继承` | M1 |
| token 限额 | 按日 / 按月 / 累计配置额度，超限拒绝；支持「观察模式」只记账不拦 | M1 / M2 |
| 用量统计 | 调用次数、token（输入 / 缓存 / 输出）、被拒次数、活跃对象、平均延迟、趋势与榜单、明细导出 | M2 |
| 好友 / 群列表 | 头像、QQ 号、昵称（备注优先），支持搜索 / 排序 / 批量操作 | M1 |
| 指令权限 | 按指令名黑白名单 | M3 |
| 群内成员级管控 | 群页展开成员并单独设权限 / 额度 | M3 |

> 当前版本 **v0.1.0（M0 骨架）**：安装后控制台可打开，配置读写与统计查询链路已通，
> 但**尚未接入 LLM 拦截**（拦截内核在 M1）。数据基座（存储层）已完整实现并有自检脚本。

## 安装

1. 运行 `build_webui.ps1` 构建前端，再运行 `build_zip.ps1` 打包；
2. 在 AstrBot 后台「插件市场 → 本地插件 → 从文件安装」上传 `dist/astrbot_plugin_user_gateway_v<版本>.zip`；
3. `requirements.txt` 中的 `aiosqlite` 会随插件自动安装。

安装后数据落在 `data/plugin_data/astrbot_plugin_user_gateway/user_gateway.db`。

## 控制台页面

| 页面 | 路由 | 内容 |
| --- | --- | --- |
| 总览 | `#/overview` | 指标卡 + 用量趋势 / 模型占比 / 拒绝原因分布 + 用量 Top 对象 |
| 私聊 | `#/friends` | QQ 好友列表（头像 / QQ / 昵称 / 备注 / 权限 / 额度）+ 逐个与批量权限管控 |
| 群聊 | `#/groups` | 群列表（群名 / 群号 / 人数 / 权限 / 额度）+ 逐个与批量权限管控 |
| 限额 | `#/quota` | 额度配置（对象 / 周期 / 上限 / 模式）+ 清零 / 删除 |
| 配置 | `#/config` | 分区表单（总开关 / 策略 / 拒绝行为 / 额度 / 同步 / 统计 / 高级）+ 运行信息 |

## 配置项

全部配置在 `_conf_schema.json` 中定义（AstrBot 插件配置页可渲染，控制台「配置」页亦可编辑），
每个键都带 `hint` 说明。重点项：

- `default_effect`：全局默认策略（`allow` 先放行再逐个限制 / `deny` 白名单模式）
- `admin_exempt`：管理员豁免（默认开，避免把自己锁在外面）
- `quota_mode`：`observe` 只记账不拦截（**默认，建议先跑一周**）/ `enforce` 超限拦截
- `count_cached_tokens`：缓存 token 是否计入额度
- `retention_days`：用量明细保留天数（默认 180，`0` = 永久）

## 设计红线（改动时务必遵守）

1. **fail-open**：本插件任何异常（判定出错 / 数据库打不开 / 配置损坏）都必须放行消息，
   绝不能把用户消息吞掉。
2. **判定走内存**：闸门热路径只读内存缓存，写操作后统一 `reload_rules()` 重建。
3. **不注册聊天管理指令**：管控入口只有控制台，避免「权限插件的指令自己也需要权限」的循环问题。

## 开发

```powershell
# 前端：首次安装依赖 + 构建（会从 metadata.yaml 注入版本号）
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_webui.ps1

# 存储层自检（不需要 AstrBot 环境）
uv run --no-project --with aiosqlite python tests/test_store.py

# 打包（要求 pages/permission-console/ 已构建；同名版本会拒绝覆盖，需先升版本）
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_zip.ps1
```

### 目录结构

```
astrbot_plugin_user_gateway/
├── main.py              # 插件入口：生命周期、规则内存缓存、控制台路由注册
├── store.py             # SQLite 持久层（权限规则 / 额度 / 用量日志 / 好友群缓存）
├── webui_api.py         # 控制台后端路由（{code,data,message} 信封）
├── _conf_schema.json    # 配置项定义（唯一来源）
├── metadata.yaml        # 插件元数据（版本号唯一来源）
├── pages/permission-console/   # 前端构建产物（进 git，打包依赖）
├── webui-src/           # 前端源码（Vue3 + TS + Naive UI + ECharts）
├── tests/test_store.py  # 存储层自检脚本
├── build_webui.ps1      # 构建前端（注入版本）
└── build_zip.ps1        # 打包（显式清单 + 撞版检测）
```

### 新增顶层模块时

必须同步做两件事，否则发布包会静默缺文件：

1. 加入 `build_zip.ps1` 的 `$includeList`；
2. 若该模块需要在插件热更新时生效，确认它被重新导入（见主 `main.py` 的导入区）。
