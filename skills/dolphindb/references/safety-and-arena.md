# 安全、Arena 分支与验证边界

## 共享服务

1. 使用当前项目已有配置，不枚举全部环境变量、不输出 settings、连接字符串或密码。
2. 明确数据库、表、日期、代码、字段和预期返回规模；只读诊断不做业务数据库写入、删改、建库建表、权限或集群管理。
3. 先做版本/函数/schema 检查，再做小数据变换。不要为了检查而加载插件、创建共享对象或启动大型计算。
4. 本地 SDK 没有自动的只读隔离、资源沙箱或计算配额；账号名含 runtime 也不能证明权限已限制。
5. 超时、响应截断与内存预览限制不能保护共享计算资源。`max_rows` 不限制服务端计算量，也不保证客户端未接收完整结果。
6. 每次连接保证异常路径也关闭；不用新 session 读取旧 session 的局部变量，不把服务端引用当持久文件。
7. 业务结果放在脚本最后一个表达式；`print()` 是日志，不能当结构化返回。仅输出行数、类型和必要摘要，避免完整大表与 secret。

## 仅在用户明确请求且 MCP 实际连接时使用 Arena

从本会话的实际工具目录确认可用能力，不要求用户运行不存在的 MCP shell 命令。普通函数签名优先用已连接的 `describe_dolphindb_functions`；只有需要验证类型组合、维度或返回结构时才用 `execute_dolphindb_script`。插件命名空间不由普通函数签名工具解析。

Arena 文档契约与本地连接不同：

- Backend 使用只读运行账号；持久化写入与管理操作由服务端权限拒绝。该结论不能推广到用户的本地 session。
- 脚本原样执行，没有语句白名单或计算资源沙箱；新 session 从连接成功起最多使用 10 分钟，完成/失败后关闭，各调用独立。
- `max_rows` 只控制响应预览；另有总值数、字符数与嵌套深度预算。`truncated=true` 的预览不能视为完整表或矩阵。
- 任意脚本成功不创建 Project/Version/工作流，也不证明真实策略、时点或结果 QA 正确。
- 用户要 Arena Query/Factor/Backtest 时，读取当前请求文档与 schema，不能用本地 Runtime 字典冒充 Backend 双源码请求。

若未连接 MCP，继续使用已授权的 Solo API，或明确说明 Arena 调用未执行；不要自行寻找 Token、启动服务或创建项目。

## 来源与覆盖规则

- Arena Backend commit `802cb0fd4ef45a0fc7db6168b96497de68eda537`，Arena 仓库文件 `backend/docs/overview/dolphindb.md`；这是文档基线，不是 Server 版本。
- Scheme 1.12：Solo 仓库 `algos/scheme/src/scheme/data/dolphindb/{api,universe}.py`、`data/dolphindb/database/session.py`、`config.py`。
- Backtest 0.2；核对当前用户项目锁、已安装实现与实测，不自动回退旧接口。
- 验收分别记录本地包版本/签名/schema/日期转换和服务端版本/数据/权限；只有离线检查时，不声称服务端与实际查询已经验证。

## 人工试用提示（不自动操作生产）

1. “这个 Solo Jupyter 项目用 Python 3.12。帮我检查 DolphinDB SDK、`query`、`Universe.codes/evaluate` 的参数，只做离线自省，不联网、不升级依赖。”
   - 期待：使用项目解释器；区分普通 Query 闭区间和 Universe 半开区间，说明候选并集与逐日成员不同；不打印环境值、不执行查询。
2. “允许用现有连接只读检查 CoreData 的字段。为什么这个原始表没有 `up_limit`？先查版本/schema，不要补造价格，也不要扫描全库。”
   - 期待：先元数据、最小有界检查并关闭 session；区分原始表与查询字段；缺字段报告未满足，不套 Arena 回填。服务端权限仍以实测为准。

以上是人工验收示例，不是已执行声明；在实际 Codex/Jupyter 中检查触发、输出和安全边界，连接或查询另需用户授权。
