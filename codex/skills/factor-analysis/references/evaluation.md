# 因子评价与 Arena 对照

来源：Arena `backend/docs/factor/request.md`、`overview/dsl.md`（参考提交 `802cb0fd4ef45a0fc7db6168b96497de68eda537`）；Solo Scheme 提供 `max_lag` 的 `base/projects/factor/params.py` 与 `execute/factor/api.py`。这是口径说明，项目安装版本与实际报告参数优先。

## Solo 标签

新版 `max_lag` 默认1，要求严格正整数，不接受bool；不再用 `return_periods` 配置新分析。使用 `O_adj = open * adj_factor`、`C_adj = close * adj_factor`，对每个 `k=1..max_lag` 生成两列单段simple收益：

```text
r{k-1}o(t) = O_adj(t+k) / C_adj(t+k-1) - 1  # overnight
r{k}(t)   = C_adj(t+k) / O_adj(t+k) - 1    # intraday
```

`t+k` 按交易日而非自然日计。各列既不是close-to-close累计收益，也不是log收益。`max_lag=2` 用到 `t,t+1,t+2` 三个交易日，依次输出：

| 列 | 单段价格区间 | lag | session | turnover_period |
| --- | --- | --- | --- | --- |
| `r0o` | `C_adj(t) -> O_adj(t+1)` | 0 | overnight | 1 |
| `r1` | `O_adj(t+1) -> C_adj(t+1)` | 1 | intraday | 1 |
| `r1o` | `C_adj(t+1) -> O_adj(t+2)` | 1 | overnight | 2 |
| `r2` | `O_adj(t+2) -> C_adj(t+2)` | 2 | intraday | 2 |

默认 `max_lag=1` 仅有 `r0o,r1`。`params.return_columns` 提供上述顺序；`params.return_specs` 每项均为 `kind="simple"`、`periods=1`，加上表中的lag、session和turnover_period。标签数字是lag，不能把它当成累计持有期。

新因子接口采用闭区间 `[start,end]`，行情包含end当天，但不读取end之后的数据；旧项目及其报告仍按各自安装版本的区间解释。尾部某个k不可用时，其对应两列为NULL，较早k仍可能有效。例如只剩 `t,t+1` 时，`r0o/r1` 可有效而 `r1o/r2` 为NULL；最后一个交易日的未来标签全部为NULL。不要将NULL填0用于IC、均值、胜率或分组收益；这些行仍可保留因子值、分组和适用的换手信息。

旧版只含 `return_periods` 的参数和报告继续使用 `return_N(t)=C_adj(t+N)/C_adj(t)-1` 及原规格；桥接不将它们转换为新标签，不升级旧环境或篡改历史。

交易决策不能引用这些未来标签。回测中历史查询必须受当时可知时间约束；整个研究区间内“存在”不代表每个回调时刻都可见。

## 预处理

Solo当前分析器没有隐式执行Arena整套MAD、z-score、log市值与行业哑变量回归、残差再标准化。若业务需要，可在Factor.compute明确实现或调用实际安装DSL支持的算符，但应记录顺序、参数、完整截面和NULL策略。

单次 `CS controls.neutralize_by` 残差不等于完整预处理链。不要因为使用同一个DolphinDB factor模块，就认定Solo分析前的数据处理相同。

## 比较清单

1. 样本区间、lookback和是否含结束日；收益是否读取超界。
2. 动态池候选并集、逐日成员和实际参与CS的截面。
3. 因子价格尺度、窗口、信号方向、NaN/无穷处理。
4. 标签使用open/close、当日或下一日、simple/log、真正持有期。
5. 共同因子有效域、标签有效域、停牌或缺行情行。
6. 预处理顺序、行业与市值时点、分组数、极端选股数量、等权/市值权。
7. 相同数据版本和供应商历史修订；point-in-time归档是否存在。
8. 各结果表的键、日期轴、NULL掩码和数值容差，不只比较一个汇总指标。

执行统计的初始代码数可能因候选域定义不同而不同；应区分初始覆盖数和最终有效样本，而非改统计数掩盖差异。

## 指标解释

- IC是当日因子与未来收益的相关；RankIC使用排序相关，具体ties/缺失规则核对当前实现。
- ICIR按当前模块定义的均值/样本标准差，不无依据乘 `sqrt(252)`。
- 不同lag的单段隔夜/日内标签不是完整日收益；逐日均值或连乘不是可交易组合净值，旧版重叠 `return_N` 也不例外。收益年化需清楚的组合持有/换仓方案。
- 分组/集合换手衡量样本集合变化，不自动等于真实成交额换手；费用与冲击要通过交易模拟另验。
- 小样本、零方差、空截面导致的NULL应披露，不转换成看似良好的0风险指标。

## 远端 Arena

如用户明确使用Arena且MCP已接入，先读取当前 `factor/request` 与 `overview/dsl`。双源码、托管 `return_specs/return_columns`、指数成员自动过滤和默认预处理属于该服务请求规则。未经对齐，不能直接把Arena请求字段传入SoloAnalysisParams，也不能重写现有Solo接口去“兼容”远端文档。
