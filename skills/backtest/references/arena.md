# Arena 条件分支与来源

## 何时切换

只有用户明确要求 Arena 且本会话实际连接其 MCP，才使用该分支。确认真实工具目录与当前请求/schema；不要要求不存在的 MCP CLI 命令，也不自行寻找 Token。没有连接时说明未执行，不创建项目、提交工作流或以本地脚本冒充 Arena 成功。

Solo `scheme.data.query` 使用 Runtime 的查询请求和结果类型。Solo 四环节策略通过 `scheme.execute.strategy.run_backtest` 和独立的 `backtest` 包驱动 `Backtest` 插件，不在数据层提供另一套 callback 回测，也不依赖小写 backtest 模块。Arena Backend Query 支持 JSON/Python 双源码，活动语言选择、完整 Backtest 请求与本地 Runtime JSON 是不同契约，按目标工具的当前文档构造，不抄一本固定请求协议。

## 八个完整 DolphinScript 回调

Arena callbacks 必须恰好包含下列八项，值是带完整函数定义的 DolphinScript，不是 Python 方法体。当前股票快照模式 onBar 不触发但仍要定义。

| 名称 | 签名 |
| --- | --- |
| initialize | `def initialize(mutable context)` |
| beforeTrading | `def beforeTrading(mutable context)` |
| onBar | `def onBar(mutable context, message, indicator)` |
| onSnapshot | `def onSnapshot(mutable context, message, indicator)` |
| onOrder | `def onOrder(mutable context, orders)` |
| onTrade | `def onTrade(mutable context, trades)` |
| afterTrading | `def afterTrading(mutable context)` |
| finalize | `def finalize(mutable context)` |

Arena message 是 TABLE，orders/trades 是 ANY VECTOR，元素为事件字典；遍历全部元素并按 key 读取，不能把事件当固定位置数组。带价格量纲的历史字段与执行价格先统一尺度，message 不自动含 DSL derivatives。

`backtest::getLastData/getHistoryData`、`order_target*`、`getParam/getParams`、`getTradeDates`、`getIndustry` 等 helpers 是 Arena Runtime 能力，不是 Solo 默认 API。Arena 历史 helper 限制 `date(time) < 当前消息日期`，不代表源数据严格 PIT；不要访问内部全区间表绕过边界。

Arena daily 的执行复权与缺价回退、snapshot 的五档/分钟历史/报价缓存，以及策略接口白名单，以该部署文档与实测为准，不推广到 Solo。Arena helpers 和普通插件 `Backtest::` 接口也不能混为一套。

## 最低执行与 QA

- 由 Arena Runtime 管理引擎与数据回放，策略不得自行创建、删除、停止或持久化引擎。
- 工作流提交返回不表示执行完成；通过真实返回的 workspace/attempt/workflow 标识核对当前执行，失败时看实际任务与完整日志。
- SUCCESS 后核对四张标准结果 schema、订单终态、成交统计、持仓、现金/收益/费用，完成 QA 后再保存版本；再次确认绑定的当前 workflow。
- `submitOrder` 的订单号向量不代表成交。`getPosition`、`getTotalPortfolios`、挂单的返回 form 可能不同；先自省，空对象单独处理。文档之间示例有表/字典差异时，部署实测优先，不靠猜测强转。
- 文档基线 Backtest 2.00.16.32 的 `-3` 可能无 onOrder 回调、15:00 新单可能拒绝；这不是所有插件版本的保证。Server、Backtest、MatchingEngineSimulator 必须分别查询实际版本，不能假定版本号相同；部署变化后复测。

本技能不自动运行、保存、重试或修改生产项目。MCP 脚本安全与响应截断见 `../../dolphindb/references/safety-and-arena.md`。

## 来源与覆盖顺序

Arena Backend commit `802cb0fd4ef45a0fc7db6168b96497de68eda537`，Arena 仓库相对路径：

- `backend/docs/overview/dolphindb.md`
- `backend/docs/backtest/dolphindb.md`
- `backend/docs/backtest/callback-data.md`
- `backend/docs/backtest/qa.md`
- `backend/docs/backtest/results.md`
- `backend/docs/backtest/dynamic-pool.md`
- `backend/docs/backtest/interfaces.md`

Solo 基线 Scheme 1.12 / Backtest 0.2，Solo 仓库实现为 `algos/scheme/src/scheme/{base,execute/strategy,data/dolphindb}` 与 `backtest/src/backtest`，并对照当前项目 `.venv` 的安装签名/schema。

当前项目锁与安装实现确定本地 API，实际 Server/插件版本与 schema 确定运行能力；静态 Arena 文档只描述其特定 Runtime，不能覆盖另一部署。验收分别记录离线检查、真实请求/撮合、结果 QA 与 Jupyter 显示，不将一个环节的成功描述成全部已验证。
