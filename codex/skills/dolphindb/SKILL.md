---
name: dolphindb
description: 在 Solo/Jupyter 中编写与诊断 DolphinDB、DolphinScript/DOS、session、函数签名和表 schema；涉及连接、原始行情或数据库脚本时使用。
---

# DolphinDB：默认走 Solo

## 适用界面

- 默认使用当前 Solo 项目的 Python API 与已配置 DolphinDB，不需要 Arena MCP。
- 先区分需求：数据库脚本与连接诊断用本技能；DSL 定义、数据查询业务和因子分析交给对应技能；策略撮合与结果审计读 `../backtest/SKILL.md`。
- 只有用户明确请求 Arena，且本会话实际连接了 Arena MCP，才切换到 [Arena 与安全边界](references/safety-and-arena.md)。不要编造 MCP 命令或自动连接另一个服务。

## 最短工作流程

1. 确定当前项目与 kernel 的解释器、锁文件及目标数据域。使用该项目 `.venv`；不要默认升级依赖，也不要拿全局 Python 3.13 代替项目 Python 3.12。
2. 先读 [本地 API 与最小示例](references/local-api.md)，离线核对 `inspect.signature`、模型 schema、安装路径和版本。名称相同不代表接口相同。
3. 仅在任务需要连接时复用现有环境凭据；先取实际 `version()`、已加载插件信息、所需函数的 `defs()` 信息和对象 `schema`。尚未连接就标记服务端部分未验证。
4. 对共享实例先限定日期、代码、列和查询规模，再运行小样本。响应截断、`head()`、`max_rows`、客户端超时都不是计算配额。
5. 按实际类型处理 TABLE、DICTIONARY、VECTOR、NULL 与空结果；确认字段、时间粒度和价格口径后再写业务计算。
6. 用 `with` 或 `finally` 关闭 session；会话表名只在所属 session 存活期间可用。保留需要的有界结果，不下载整库。
7. 返回脚本/API、检查依据、结果摘要和未验证项；不要把编译成功说成数据正确或生产可用。

## 必守边界

- 不打印凭据、Token、完整配置或环境变量；不 `dumpenv`、不读取秘密文件找密码、不把 secret 写入脚本、Notebook 或日志。
- 本地 SDK 连接不保证只读权限；实际权限由目标账号与服务端决定。诊断中不做数据库持久化写入、删改、管理操作或共享对象/插件管理。
- 策略引擎的创建与回收交给已有回测封装；不要清理、停止或删除别人的引擎。
- 普通函数签名先看部署 `defs()`；插件命名空间不能仅凭导出名字推断能力。
- 原始行情先检查真实 schema、缺失字段、NULL/NaN/非有限值与有效价格。不要套用 Arena 的自动复权或缺价回填。
- 不能从“回调只读历史”推断供应商数据严格 point-in-time；另查发布时间、修订和填充规则。

## 与查询 API 的关键边界

- `scheme.data.query` 直接导出 `runtime.apps.query.execute_query`，接收 Runtime `FactorQuery` 或字典；不是 Arena Backend 双源码请求。
- `Universe.query(start, end, ...)` 使用 `[start, end)`，内部转为 inclusive `end - 1 天`；不要再减一次。
- `Universe.member` 是注入的逐日 BOOL 节点，不默认加入 `filters`。期间候选并集不等于每日成员集合。
- DSL 元数据统一使用 `../dsl/scripts/inspect_dsl.py`，提供离线 factors/operators/describe/query validation，不联网；运行前用项目解释器查看 `--help` 核对参数，不把它当数据执行器。

## 渐进加载

| 需要做什么 | 读取 |
| --- | --- |
| 项目环境、SDK、查询/Universe、session 最小示例 | [local-api.md](references/local-api.md) |
| 共享服务安全、Arena 条件分支、来源与试用提示 | [safety-and-arena.md](references/safety-and-arena.md) |

文档基线为 Scheme 1.5.0 / Backtest 0.1.1；以当前项目锁与实测覆盖静态说明。
