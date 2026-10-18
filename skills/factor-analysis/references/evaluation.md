# 因子评价与 Arena 对照

来源：Arena `backend/docs/factor/request.md`、`overview/dsl.md`（参考提交 `802cb0fd4ef45a0fc7db6168b96497de68eda537`）；Solo Scheme 提供 `period/price_column/max_lag` 的 `base/projects/factor/params.py` 与 `execute/factor/api.py`。这是口径说明，项目安装版本与实际报告参数优先。

## Solo 标签

新版 `period`（收益持有期）与 `max_lag`（入场延迟上限）默认均为1，要求严格正整数、不接受bool。`price_column` 可选 `open`、`close`、`open_hfq`、`close_hfq`，默认 `open_hfq`。对每个 `k=1..max_lag`：

```text
P{period}L{k}(t) = price_column(t+k+period) / price_column(t+k) - 1
```

`t+k` 按交易日而非自然日计。直接读取所选字段，后复权字段不再乘 `adj_factor`。收益是simple而非log；起点最早为t+1，不允许使用因子当日收盘价起算隔日收益。

`period=5, price_column="open_hfq", max_lag=3` 依次输出：

| 列 | 后复权开盘价区间 | periods | lag | turnover_period |
| --- | --- | --- | --- | --- |
| `P5L1` | t+1 → t+6 | 5 | 1 | 5 |
| `P5L2` | t+2 → t+7 | 5 | 2 | 5 |
| `P5L3` | t+3 → t+8 | 5 | 3 | 5 |

`params.return_columns` 提供上述顺序；`params.return_specs` 每项包含 `kind="simple"`、`periods=period`、`lag=k`、`price_column`、`turnover_period=period`。持有期不是入场延迟；换手按完整交易日历比较，不能因因子缺某一天而压缩日期轴。默认仅有 `P1L1`，需要t+1、t+2两个未来价格。

新因子接口采用闭区间 `[start,end]`，行情包含end当天，但不读取end之后的数据。收益按完整交易日网格位移，缺价或停牌不跳日期；某列所需的入场或退出端点缺价时为NULL。尾部 `P5L3` 需要t+8，较早lag仍可能有效；不要将NULL填0用于IC、均值、胜率或分组收益。这些行仍可保留因子值、分组和适用的换手信息。

历史 `r0o/r1/r1o/r2` 保持单段隔夜/日内收益口径，其中 `r0o` 从因子当日收盘起算，仅供旧报告解释，不用于新分析。旧版只含 `return_periods` 的参数和报告继续使用 `return_N(t)=C_adj(t+N)/C_adj(t)-1` 的close-to-close累计收益及原规格。桥接不将旧结果转换为新标签，不升级旧环境或篡改历史报告。

交易决策不能引用这些未来标签。回测中历史查询必须受当时可知时间约束；整个研究区间内“存在”不代表每个回调时刻都可见。

## 预处理

在 `Factor.compute` 返回前调用 `self.preprocess(...)` 显式执行预处理，分析器不重复处理。该方法接收 `DosVar` 加所属session，或 `time/code/因子列` DataFrame，统一返回 `(date,symbol)` 面板；数字处理由内联DOS完成，不加载factor模块。

每日截面按code排序、逐因子过滤有效输入，市值小于1截为1，对数市值用 `stdp` 标准化；原始因子做 median±3×MAD（无1.4826系数）去极值和 `stdp` 标准化；行业字符串排序后去首类构建哑变量，含截距SVD OLS；有效样本不足或残差低于浮点容限时留NULL，否则残差再标准化。默认使用动态 `industry_l0` 和 `circ_mv`，行业来源不同于旧应用静态Tushare映射，不能声称数值结果完全相同。

方法保留行、逐因子处理缺失，不像旧应用在调用前后额外取共同非NULL样本。单次 `CS controls.neutralize_by` 既不包含MAD/标准化，常量控制和小样本退化也不同，不能替代上述流程。空输入、所选因子全部NULL明确报错；常量因子、常量log市值及退化截面结果保留NULL，不补0。

## 比较清单

1. 样本区间、lookback和是否含结束日；收益是否读取超界。
2. 动态池候选并集、逐日成员和实际参与CS的截面。
3. 因子价格尺度、窗口、信号方向、NaN/无穷处理。
4. `period`、`price_column`、入场lag、simple/log和复权数据来源。
5. 共同因子有效域、标签有效域、停牌或缺行情行。
6. 预处理顺序、行业与市值时点、分组数、极端选股数量、等权/市值权。
7. 相同数据版本和供应商历史修订；point-in-time归档是否存在。
8. 各结果表的键、日期轴、NULL掩码和数值容差，不只比较一个汇总指标。

执行统计的初始代码数可能因候选域定义不同而不同；应区分初始覆盖数和最终有效样本，而非改统计数掩盖差异。

## 指标解释

- IC是当日因子与未来收益的相关；RankIC使用排序相关，具体ties/缺失规则核对当前实现。
- ICIR按当前模块定义的均值/样本标准差，不无依据乘 `sqrt(252)`。
- `period>1` 的逐日P日收益相互重叠，不是每天可复投的组合净值；不能直接连乘后称为可交易年化。旧版重叠 `return_N` 和隔夜/日内片段也应按其原始规格解释。收益年化需要清楚的组合持有/换仓方案。
- 分组/集合换手衡量样本集合变化，不自动等于真实成交额换手；费用与冲击要通过交易模拟另验。
- 小样本、零方差、空截面导致的NULL应披露，不转换成看似良好的0风险指标。

## 远端 Arena

如用户明确使用Arena且MCP已接入，先读取当前 `factor/request` 与 `overview/dsl`。双源码、托管 `return_specs/return_columns`、指数成员自动过滤和默认预处理属于该服务请求规则。未经对齐，不能直接把Arena请求字段传入SoloAnalysisParams，也不能重写现有Solo接口去“兼容”远端文档。
