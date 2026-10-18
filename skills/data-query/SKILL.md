---
name: data-query
description: 在 Solo Notebook 查询行情、财务、行业和指数成分，处理 Universe 股票池、日期区间、固定证券域、成员列、lookback 或缺失数据时使用。核对项目 Runtime 和完整数据域，避免空代码列表意外查询全市场。
---

# Solo 数据查询

先读取当前项目 `pyproject.toml`、`uv.lock` 与源码，以项目 `.venv/bin/python` 和已安装 Scheme 为准。`scheme.data.query` 是使用 Runtime 请求/结果类型的本地直接执行包装，不再是 `runtime.apps.query.execute_query` 的别名；也不是 Arena 后端 MCP 请求，无需 `dsl_source`、项目 ID 或调度工作流。

## 选择接口

- 已知静态证券列表、简单字段读取：`from scheme.data import query`。
- 全市场或指数动态成员：`from scheme import Universe, StockPool`，使用 `Universe.query` 或 `evaluate`。
- 新建/修改派生列：先读取相邻 `dsl/SKILL.md`，用其 `scripts/inspect_dsl.py` 查询字段、算符并离线校验。
- 直接 DolphinScript：读取相邻 `dolphindb/SKILL.md`，不要绕过会话和安全边界。

签名、返回值及示例见 [查询契约](references/query-contract.md)。在不确定当前版本时，用 `inspect.signature`、`inspect.getfile` 和 Pydantic schema查看安装包，不自动升级项目来匹配文档。

## 工作步骤

1. 确定需要的日期、证券域、最终输出列与用途。用户只要解释时不要执行数据查询。
2. 核实基础字段在当前 Runtime 白名单里；供应商文档只能解释单位、频率和来源，不能替代实际字段映射。
3. 明确边界：直接 Query 的 `end_date` **含当日**；Universe 和策略研究参数的 `end` **不含当日**；新版因子计算/分析是闭区间，调用 Universe 时传 `end + timedelta(days=1)`。避免多减或少减一天。
4. 将窗口换成足够的日历 lookback；考虑节假日、停牌和上市时间，不能把20日历日当作20交易日。
5. 每进程首次 `codes=[]` 从 CoreData 全历史所有记录取 distinct codes 缓存为 tuple，不受请求日期或 `close` 限制；后续空列表复用该域，显式 `codes` 不改。指数池单次 `dataset_query` 保留 `codes=[]` 并注入 `Universe.member` 逐日判断，不再预筛选候选；筛选、截面参与和持仓退出分别处理。
6. `with` 内读取 `.data` 或在同一会话访问 `.data_ref`。离开会话后只使用已取回的 DataFrame，不再引用会话变量。
7. 用户要求运行时先验证小范围，检查列类型、日期范围、代码数、NULL率及成员变化，再按请求扩展；不要无界扫描生产库。

## 常见错误

- 本地 `codes=[]` **使用进程固定的全市场证券域**，不是空池。业务上无候选时应明确跳过或返回按契约构造的空结果，不能把空列表交给查询当作“无数据”。
- `Universe.query` 输出 `Universe.member`，**不会自动删除非成员行**。需要成员内运算用 DSL `on`；需要只保留成员输出再显式 `filters=[Universe.member]`。`evaluate` 返回交易日×固定证券域的 BOOL 面板，缺失/非成员为 False，无成分日保留全 False，不因缺行情缩列；日期轴仍按区间内真实 `close` 交易日确定。
- 固定证券列表不等于每一天的指数成分股。不要以最终成员快照回填历史，也不要按某一天有收盘价的代码重新缩小股票域。
- 市场字段、缺行、NULL、0 和填充值不同；别将缺行情自动解释为停牌，也别擅自前向填充价格。
- 查询示例常用 `.SH/.SZ`，回测和因子面板常用 `.XSHG/.XSHE`；调用 `source_symbol` / `engine_symbol` 转换，不靠字符串猜市场。
- 只取最终选中代码重新算CS排名会改变结果。要复现先前截面，需要原始参与域、掩码、窗口和数据版本。

## 交付

给出实际使用接口、日期闭开口径、证券域、字段和必要的DSL；运行后报告真实行数/代码数/缺失情况，区分推测和观察。只按用户要求保存输出，不覆盖已有报告、项目依赖或冻结任务。
