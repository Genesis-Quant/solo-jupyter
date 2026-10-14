# 因子评价与 Arena 对照

来源：Arena `backend/docs/factor/request.md`、`overview/dsl.md`（参考提交 `802cb0fd4ef45a0fc7db6168b96497de68eda537`）；Solo Scheme 1.5.0 `execute/factor/api.py`。这是口径说明，不代表未来版本保持不变。

## Solo 标签

当前实现使用 `adjusted_close = close * adj_factor`，N交易日simple标签：

```text
return_N(t) = adjusted_close(t+N) / adjusted_close(t) - 1
```

`[start,end)` 的行情截止到 `end-1日`，不额外读取结束日之后的数据。末尾不足N个交易日的标签为NULL。不要将NULL填0用于IC、均值、胜率或分组收益；这些行仍可保留因子值、分组和适用的换手信息。

交易决策不能引用这个未来标签。回测中历史查询必须受当时可知时间约束；整个研究区间内“存在”不代表每个回调时刻都可见。

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
- 多期标签彼此重叠，逐日均值或连乘不是可交易组合净值。收益年化需清楚的组合持有/换仓方案。
- 分组/集合换手衡量样本集合变化，不自动等于真实成交额换手；费用与冲击要通过交易模拟另验。
- 小样本、零方差、空截面导致的NULL应披露，不转换成看似良好的0风险指标。

## 远端 Arena

如用户明确使用Arena且MCP已接入，先读取当前 `factor/request` 与 `overview/dsl`。双源码、托管 `return_specs/return_columns`、指数成员自动过滤和默认预处理属于该服务请求规则。未经对齐，不能直接把Arena请求字段传入SoloAnalysisParams，也不能重写现有Solo接口去“兼容”远端文档。
