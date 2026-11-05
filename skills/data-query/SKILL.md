---
name: data-query
description: 在 Solo Notebook 查询行情、财务、行业和指数成分，处理 Universe 历史候选证券、逐日成员、日期区间、lookback/lookforward 或缺失数据时使用。核对项目 Runtime 和完整参与域，避免空代码列表意外查询全市场。
---

# Solo 数据查询

本技能对应 Scheme 1.12 接口。先读取项目 `pyproject.toml`、`uv.lock` 与源码，以项目 `.venv/bin/python` 和实际安装包核对；版本不符时明确报错，不添加旧接口适配。`from scheme.data import query` 导入独立Query包；`query.execute_query` 使用 `query.FactorQuery` 校验请求，支持可选 `lookforward`，默认前视为0，不导出同名 `query` 函数。先核对项目实际 schema，旧安装版本不宣称支持新字段。查询在当前 DolphinDB 会话中直接执行，不是 Arena Backend 请求，不需要 `dsl_source` 或项目 ID。

## 选择接口

- 静态证券列表和字段读取：`from scheme.data import query`，调用 `query.execute_query`。
- 历史候选证券：`Universe.codes(start, end, session=session)`，从已经过滤的每日成员记录取并集，返回 `.SH/.SZ`。
- 每日成员面板：`Universe.evaluate(start, end, session=session)`，返回交易日 × 区间历史成员的 BOOL 面板，列为 `.XSHG/.XSHE`。
- 因子和其它数据查询：在取得候选代码后调用普通 `query.execute_query`，用 `MEMBERS[universe.pool]` 定义 `universe.member`，仍按每日成员过滤或限定计算参与域。
- 派生节点：读取相邻 `dsl/SKILL.md`，核对算符定义并离线校验完整请求。
- 原始 DolphinScript：读取相邻 `dolphindb/SKILL.md`，使用集中封装的 `upload`、`download`。

签名、返回值及例子见 [查询契约](references/query-contract.md)。

## 工作步骤

1. 确定日期、证券域、输出列和用途。用户只要求解释时不要执行查询。
2. 核对基础字段白名单；供应商文档不能替代项目实际字段映射。
3. 普通 Query 的 `start_date/end_date` 为闭区间；Universe 和策略日期是 `[start,end)`；因子计算和分析为 `[start,end]`，调用 Universe 时给结束日加一天。
4. `lookback`/`lookforward` 是非负日历时长，不是交易日数，接受 `timedelta` 或 TimeDelta 字符串（如 `P20D`），默认均为 0。计算可读取原区间前后的数据，但 `.data`、`.calendar` 都只返回原闭区间；结合节假日检查窗口，缺数据不补值、不跳停牌。前视只用于明确允许未来信息的研究/标签，不用于当日交易信号；分析收益标签查询仍默认前视 0。
5. Universe 先在全市场输入域上读取成员字段并应用成员过滤；只传回候选代码或成员面板，不传回全市场因子表。后续查询使用明确候选代码，不能把区间并集当作每日成员。
6. 普通 `query.execute_query` 不自动注入成员节点。需要指数成员数据时显式定义该 BOOL 派生列，再用于 `filters=[universe.member]`，或并入截面 filter（如 `valid`）作为 CS 节点的 `on`。成员不是时序 filter，不放进 TS 的 `on`；两类 filter 见相邻 `dsl/references/dsl-contract.md`。filters 在派生计算后删行，on 仅限制参与计算。
7. 在 `with` 内读取 `.data`、`.calendar`，或以 `result.data_ref`、`result.calendar_ref` 做服务器端运算。结果退出后不可再使用这些引用。
8. 仅在用户要求运行时执行有界检查，核对日期、证券、NULL、历史成员变化，再按请求扩展。

## 数据域与会话约束

- 普通 `codes=[]` 表示全市场，不表示没有候选。它复用 Arena Runtime `get_codes()`，首次通过 Tushare `stock_basic` 合并上市、退市、暂停上市股票，需要 `TUSHARE_TOKEN`，随后使用进程缓存；显式代码不调用 Tushare。
- 候选为空时跳过后续查询或明确报错，不能把空候选传成全市场请求。
- Universe 面板只含区间内曾为成员的列；非成员日为 False，无成员日仍保留真实交易日历。整个区间无成员时为有日期、零列的面板。
- 不能用当前指数成分回填历史，也不能按某天有行情的代码替代历史成员并集。
- 查询、分析和回测只使用同一套 `DOLPHINDB_ENDPOINTS/USERNAME/PASSWORD`。新连接按端点顺序尝试，连接成功后不在任务中途切换节点。
- 调用方建连后手动调用 `query.initialize_query(session)`，再使用同一会话执行 `query.execute_query`、Universe 和预处理。查询过程中不自动初始化、不检查指纹、不记忆会话；显式再次调用初始化就再次执行 DOS。`query.execute_query(..., session=session, settings=...)`必须传入已有会话；查询API不创建或关闭连接，关闭结果只回收本次查询引用。Scheme研究使用其`create_session()`并由外层`with`或`close()`回收；独立Query也接受原生SDK会话，不依赖Scheme。独立`query workers`命令只在CLI边界使用SDK建连并注入Worker，不由Scheme接管。结果支持`with`和`close()`；同一会话只允许一个未关闭查询结果。
- `.data` 下载完整最终结果，之后调用 `.head()` 不会减少传输。先限制查询范围，少量预览使用 `download` 在服务器端投影。
- `.SH/.SZ` 与 `.XSHG/.XSHE` 使用 `source_symbol`、`engine_symbol` 转换，不能混写造成重复键。
- 价格不随意前向填充；财务、行业、权重填充也不证明供应商数据严格 point-in-time。缺行、NULL、零值和填充值分别解释。
- 缩小代码域会改变 CS 结果。候选必须覆盖业务所需参与域，不能只查询最终入选证券再重算原排名。

## 交付

说明实际接口、日期口径、候选域与逐日成员条件、字段和 DSL；运行后报告真实行数、证券数和缺失情况。不改用户公式、参数、锁文件或历史任务来迁就示例。
