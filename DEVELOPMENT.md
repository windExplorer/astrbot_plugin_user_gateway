# 开发者文档（DEVELOPMENT）

> 用户向的安装与使用说明在 [README.md](README.md)；本文件只讲开发相关内容。
> 需求与设计文档见工作区 `docs/astrbot_plugin_user_gateway_PRD.md`，
> 各版本改动见 [CHANGELOG.md](CHANGELOG.md)。

## 开发环境常用命令

```powershell
# 前端：首次安装依赖 + 构建（会从 metadata.yaml 注入版本号）
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_webui.ps1

# 存储层自检（不需要 AstrBot 环境）
uv run --no-project --with aiosqlite python tests/test_store.py

# 判定内核自检（同上）
uv run --no-project python tests/test_gate.py

# 打包（要求 pages/permission-console/ 已构建；同名版本会拒绝覆盖，需先升版本）
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_zip.ps1
```

## 设计红线（改动时务必遵守）

1. **fail-open**：本插件任何异常（判定出错 / 数据库打不开 / 配置损坏）都必须放行消息，
   绝不能把用户消息吞掉。
2. **判定走内存**：闸门热路径只读内存缓存，写操作后统一 `reload_rules()` 重建。
3. **不注册聊天管理指令**：管控入口只有控制台，避免「权限插件的指令自己也需要权限」的循环问题。

## 目录结构

```
astrbot_plugin_user_gateway/
├── main.py              # 插件入口：生命周期、规则内存缓存、发送钩子（最后回复）、控制台路由注册
├── gate.py              # 判定内核（纯逻辑零 IO）：档位链 / 权限 / 额度 / 冷却
├── quota.py             # 额度纯计算：周期重置、token 口径换算与估算
├── store.py             # SQLite 持久层（规则 / 等级 / 限额 / 用量 / 缓存 / 最后消息）
├── sync.py              # 好友 / 群列表同步（OneBot）+ 定时调度
├── avatar.py            # QQ / 群头像抓取与磁盘缓存
├── webui_api.py         # 控制台后端路由（AstrBot 桥接信封）
├── _conf_schema.json    # 配置项定义（唯一来源）
├── metadata.yaml        # 插件元数据（版本号唯一来源）
├── pages/permission-console/   # 前端构建产物（进 git，打包依赖）
├── webui-src/           # 前端源码（Vue3 + TS + Naive UI + ECharts）
├── tests/               # 自检脚本（纯逻辑，不需要 AstrBot 环境）
├── build_webui.ps1      # 构建前端（注入版本）
└── build_zip.ps1        # 打包（显式清单 + 漏包自检 + 撞版检测）
```

### 新增顶层模块时

必须同步做两件事，否则发布包会静默缺文件：

1. 加入 `build_zip.ps1` 的 `$includeList`；
2. 若该模块需要在插件热更新时生效，确认它被重新导入（见主 `main.py` 的导入区）。

## 存储要点

- 单文件库 `user_gateway.db`，**WAL 模式 + `synchronous=NORMAL` + `foreign_keys=ON`**；
- 时间统一用 **epoch 秒**（INTEGER），聚合按 SQLite 的 `localtime` 分桶；
- `usage_log` 是统计与明细的唯一事实来源：被拒的请求也写一条（`status='denied'`）；
- **额度与用量分离**：`llm_quota` 只存限额规则，用量在 `usage_counter`（三个记账维度：
  `user` / `group` / `member`，成员维度键为 `群号:QQ`）；
- schema 版本存在 `settings` 表，历史 v1 → v7 均有可重入迁移脚本。

## 里程碑历史（M0–M11，全部完成）

| 里程碑 | 内容 | 版本 |
| --- | --- | --- |
| M0 | 骨架（建库 / 配置 / 构建打包 / 控制台最小页面） | v0.1.0 |
| M1 | LLM 权限内核、好友群同步与真实数据 | v0.2.0（v0.2.1 修打包漏文件） |
| M2 | 自定义等级与三层限额、头像后端缓存、bot 最后回复 | v0.3.0 |
| M3 | 等级模型路由（主 / 备模型 + 熔断） | v0.4.0 / v0.4.1 |
| M4 | 指令权限（按指令名禁用 / 放行） | v0.5.0 |
| M5 | 指令权限补齐「对象 / 等级」维度（两段式判定） | v0.6.0 |
| M6 | 统计页（明细 + CSV 导出 + 指令榜单 + 热力图 + 审计） | v0.7.0 |
| M7 | 权限加「场景」维度（私聊 / 群聊分开管） | v0.8.0 |
| M8 | 群成员级管控 | v0.9.0 |
| M9 | 按成员的额度（第三个记账维度 `member`） | v0.10.0 |
| M10 | 规则导出 / 导入（等级 id 重映射） | v0.11.0 |
| M11 | 指令 × 对象矩阵（对象体检） | v0.12.0 |
| 1.0.0 | 发布评审：两处 Critical 修复、导入原子化、批量写两遍校验 | v1.0.0 |

后续维护版本（v1.0.1 起）见 CHANGELOG。
