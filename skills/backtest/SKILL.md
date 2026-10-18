---
name: backtest
description: 在 Solo/Jupyter 中实现和审计策略回测 backtest、Algo callbacks、Model/Optimize/Control/Execution、订单成交与资金持仓；诊断无交易、拒单或收益异常时使用。
---

# 回测：默认 Solo 四环节

## 先分清入口

- 默认使用当前项目 Scheme 与 Python Backtest，沿已有 `Model -> Optimize -> Control -> Execution` 链路实现，不先另造引擎框架。
- Solo Python `backtest.Algo[Params, C]` 的公共回调只有 `initialize(self)`、`on_snapshot(self, msg: DosVar)`、`on_order(self, orders: Sequence[OrderReport])`、`on_trade(self, trades: Sequence[TradeReport])`。
- Python `ctx` 是共享模型，不自动同步到 DOS；`msg` 是本 session 的表名，不是 DataFrame、字典或 Arena callback message。
- Arena Backend 双源码请求、八个完整 DolphinScript 回调与 Runtime helpers 是另一套契约，不是 Solo 默认依赖。只有用户请求 Arena 且 MCP 实际连接时读 [Arena 分支](references/arena.md)。

## 工作顺序

1. 使用当前项目 `.venv` / kernel，先离线核对版本、安装路径、签名及分析参数/表单 schema。采用传统 `TypeVar`/`Generic` 写法，不默认升级或改用全局 Python 3.13。
2. 读取 [API 与示例](references/api-examples.md)，选当前 Model 报告表单或 `ExecutionReportForm` 路径。检查是否真的有 `form.run()`；当前 Scheme 1.5.0 基类是 `form.build().run(AlgoClass)`，不要凭名字添加方法。
3. 保留当前项目参数、上下文与四环节职责：Model 写信号，Optimize 写目标，Control 写订单，Execution 下单；长期状态放算法自身。检查额外的项目专用钩子，不把它们冒充四个公共回调。
4. 运行前读 [数据与最低审计](references/data-and-audit.md)，明确 `[start,end)`、信号可用时点、价格尺度、进程固定证券域与逐日成员、缺价和撮合假设。
5. 对外连接/运行仅限用户任务已授权的数据域与规模；复用现有环境凭据而不打印。先小样本，结果提取完成后由封装回收自己的 engine/session，不操作他人或生产引擎。
6. 结果至少核对收益、资金、持仓、成交和拒单。订单号不等于成交，程序完成不等于 QA 通过；无交易也可能是时间条件、缺价、取整或拒单问题。
7. 只在真实 Solo Jupyter 且显示桥接可用时调用 `report.show()`。仅写出该调用不能声称“已展示”；非 Notebook 环境提供结果摘要或按需 `to_html()`。

## 不能省的正确性检查

- Scheme 1.5.0 使用不含结束日的日期；原生插件配置的 `endDate` 可能含结束日，适配层只转换一次。
- 原始行情必须核对缺失字段、NULL/NaN/非有限值、非正价格与代码后缀；不沿用 Arena 自动复权或缺价回填。
- 查询与交易价格、分红除权、费用和基准口径分别确认；没有前视代码不代表供应商数据严格 point-in-time。
- 固定证券域不是当日有效池；`Universe` 单次 `dataset_query` 以 `codes=[]` 注入 `Universe.member`，不做 `codes_query` 预选、不默认过滤。`evaluate` 保留固定证券列，当日无成分为全 False；退出股票仍需行情、真实持仓与成交确认。
- 空持仓、空信号、缺行、零值和 NULL 分开处理。卖单已提交不代表现金可用，部分成交后重新对账账户与在途订单。
- 终态集合为 `{1,2,-1,-3}`，`-2` 撤单拒绝不是终态；终态也不等于全部成交。不要等待保证不存在的终态回调。
- `get_orders()` 是尚未消费的回报，不是活动订单查询。不要重复下同一目标造成超额交易。

## 输出要求

给出实际入口/改动、参数与数据边界、回测范围、五类审计摘要、期末持仓/未成交和未验证项。不要把订单事件行数当成交笔数，也不要仅凭净值曲线承诺实盘可复制。

## 渐进加载

| 任务 | 读取 |
| --- | --- |
| Python 回调、类型、表单运行与报告 | [api-examples.md](references/api-examples.md) |
| 日期/PIT、价格、数据域、订单与账户 QA | [data-and-audit.md](references/data-and-audit.md) |
| 用户明确要求 Arena，或比较两套契约 | [arena.md](references/arena.md) |

需要 DSL 元数据时统一使用 `../dsl/scripts/inspect_dsl.py`：运行前用项目解释器查看 `--help` 核对参数，仅离线 factors/operators/describe/query validation，不联网、不代替数据执行。其余数据库安全与 session 诊断读 `../dolphindb/SKILL.md`。
