# 因子评价与 Arena 对照

来源：Arena `backend/docs/factor/request.md`、`overview/dsl.md`（参考提交 `802cb0fd4ef45a0fc7db6168b96497de68eda537`）；Solo Scheme 提供 `period/price_column/max_lag` 的 `base/projects/factor/params.py` 与 `execute/factor/api.py`。这是口径说明，项目安装版本与实际报告参数优先。

## Solo 标签

新版 `period`（收益持有期）与 `max_lag`（入场延迟上限）默认均为1，要求严格正整数、不接受bool。`price_column` 可选 `open`、`close`、`open_hfq`、`close_hfq`，默认 `open_hfq`。对每个 `k=1..max_lag`：

```text
P{period}L{k}(t) = ln(price_column(t+k+period) / price_column(t+k))
```

`lag=k>=1`，`t+k` 按交易日而非自然日计。入场与退出使用同一价格字段，直接读取所选字段，后复权字段不再乘 `adj_factor`。当前所有因子评价收益均为log；起点最早为t+1，不允许使用因子当日收盘价起算隔日收益。评价标签口径不改变compute中的动量因子公式。

`period=5, price_column="open_hfq", max_lag=3` 依次输出：

| 列 | 后复权开盘价区间 | periods | lag | turnover_period |
| --- | --- | --- | --- | --- |
| `P5L1` | t+1 → t+6 | 5 | 1 | 5 |
| `P5L2` | t+2 → t+7 | 5 | 2 | 5 |
| `P5L3` | t+3 → t+8 | 5 | 3 | 5 |

`params.return_columns` 提供上述顺序；`params.return_specs` 每项包含 `kind="log"`、`periods=period`、`lag=k`、`price_column`、`turnover_period=period`。持有期不是入场延迟；换手按完整交易日历比较，不能因因子缺某一天而压缩日期轴。默认仅有 `P1L1`，需要t+1、t+2两个未来价格。

新因子接口采用闭区间 `[start,end]`，行情包含end当天，但不读取end之后的未来区间。收益按完整交易日网格位移，不能在稀疏因子面板上位移，缺价或停牌不跳日期；某列所需的入场或退出端点缺价或非正时为NULL。尾部 `P5L3` 需要t+8，较早lag仍可能有效；不要将NULL填0用于IC、均值、胜率或分组收益。这些行仍可保留因子值、分组和适用的换手信息。

历史 `r0o/r1/r1o/r2` 保持单段隔夜/日内收益口径，其中 `r0o` 从因子当日收盘起算，仅供旧报告解释，不用于新分析。旧版只含 `return_periods` 的参数和报告继续使用 `return_N(t)=C_adj(t+N)/C_adj(t)-1` 的close-to-close累计收益及原规格。桥接不将旧结果转换为新标签，不升级旧环境或篡改历史报告。

交易决策不能引用这些未来标签。回测中历史查询必须受当时可知时间约束；整个研究区间内“存在”不代表每个回调时刻都可见。

## 预处理

Factor 类唯一声明输出 `columns`，Form 不提供输入、AnalysisParams 无需手填；其默认空列表字段由执行流程按类声明自动写入报告元数据；在 `Factor.compute` 返回前调用 `self.preprocess(frame)` 显式执行预处理，不传 columns，分析器不重复处理。输入可以完全来自本地 DataFrame，不要求是 query 结果；也支持 `DosVar` 加所属 session。preprocess 始终另开自有（owned）query，按输入日期与证券获取动态行业/市值暴露，再 join 因子，不预取或沿用输入暴露，不改写原查询缓存。输出统一为 `(date,symbol)` 面板，数字处理由内联 DOS 完成，不加载 factor 模块；独立查询回收自己的 session，不关闭借用的因子 session。

每日截面按code排序、逐因子过滤有效输入，市值小于1截为1，对数市值用 `stdp` 标准化；原始因子做 median±3×MAD（无1.4826系数）去极值和 `stdp` 标准化；行业字符串排序后去首类构建哑变量，含截距SVD OLS；有效样本不足或残差低于浮点容限时留NULL，否则残差再标准化。默认使用动态 `industry_l0` 和 `circ_mv`，行业来源不同于旧应用静态Tushare映射，不能声称数值结果完全相同。

方法保留行、逐因子处理缺失，不像旧应用在调用前后额外取共同非NULL样本。单次 `CS controls.neutralize_by` 既不包含MAD/标准化，常量控制和小样本退化也不同，不能替代上述流程。空输入、所选因子全部NULL明确报错；常量因子、常量log市值及退化截面结果保留NULL，不补0。

## 比较清单

1. 样本区间、lookback和是否含结束日；收益是否读取超界。
2. Solo 的进程固定证券域、Arena 的候选域、逐日成员与实际参与 CS 的截面；本地 Universe 单次注入成员但不自动过滤，不套用远端的 codes_query 流程。
3. 因子价格尺度、窗口、信号方向、NaN/无穷处理。
4. `period`、`price_column`、入场lag、Solo的log收益口径与对端实际定义、复权数据来源。
5. 共同因子有效域、标签有效域、停牌或缺行情行。
6. 预处理顺序、行业与市值时点、分组数、极端选股数量、等权/市值权。
7. 相同数据版本和供应商历史修订；point-in-time归档是否存在。
8. 各结果表的键、日期轴、NULL掩码和数值容差，不只比较一个汇总指标。

Solo `codes=[]` 的证券域直接复用 Arena Runtime 从 Tushare `stock_basic` 的 `L/D/P` 股票合并得到的进程缓存，需要 `TUSHARE_TOKEN`；不扫描 CoreData，不按日期或 `close` 缩小，显式代码不改且不调用 Tushare；`evaluate` 保留交易日×固定代码域的 BOOL 面板，无成分为 False。执行统计的初始代码数可能因两端数据域定义不同而不同；应区分初始覆盖数和最终有效样本，而非改统计数掩盖差异。

## 指标解释

- IC 是当日因子与原始未来 log 标签的相关；RankIC 使用排序相关，两者不平滑，具体 ties/缺失规则核对当前实现。ICIR 按当前模块定义的均值/样本标准差，不无依据乘 `sqrt(252)`。原始日频标签、换手的原 period 和未来端点公式不变，不把信号因子 momentum 改为 log。
- 各组 raw 收益仍为当日有效 log 标签的加权均值。令 `P=period`，以 full report 的完整交易日轴首日为固定 anchor，每个因子、收益列和分组独立按连续 P 个真实交易日切成不相交集合，每集合恰有 P 条 raw P 日标签。集合 `collectionlog(C)=sum(rawlog[i] for i in C)/P` 是算术平均，只做一次 `/P`，不是 mean 后再除以 P；对应 simple 集合为 `collection_simple(C)=prod(1+R[i] for i in C)**(1/P)-1`，即增长因子的几何平均减一。不使用逐日 rolling average 或 expanding prefix，不平均不同 lag。
- 只有完整 P 日期集合输出一个点，`time` 为集合末日的原观测日期；开头 P-1 个交易日没有集合点，尾部不足 P 个交易日不输出。先按完整真实交易日历补齐缺交易行并填 NULL，任一 raw 为 NULL 则整个集合为 NULL，在集合末日保留断点，不跳过 NULL 凑 P。日期选择先在 full report 全表固定集合后，再按集合末日筛选，不按所选起点重切集合或改变 anchor；`period=1` 使用同一规则。
- 历史 simple 展示时先转 `ln(1+r)`，`r<=-1` 或非有限值转 NULL；旧 simple 多期数据使用 `group_turnover` 的完整日历补 NULL，缺少时明确报“缺少完整交易日历”，不按已有行压缩；新版 log 的 `group_returns` 已含完整日历，没有 `group_turnover` 仍支持。保留原 metadata，不改写历史文件。
- 分组净值为 `exp(cumsum(collectionlog))`，正序/倒序都沿同一集合序列每集合只累计一次。多空取同集合 top-bottom（`high-low`）的集合 log 收益之差；时序 `value=collectionlog` 为 log，`cumulative=cumsum(collectionlog)` 为 logsum，不是简单收益或净值。所有持有期统一生成曲线与统计，NULL 保留断点、不补零。
- 分组均值、t 检验/p 值、年化、波动率与 Sharpe 都基于非 NULL 完整集合，每集合为一次 observation，不把 P 条 raw 当 P 次样本；年化频率为 `252/P`，均值为 logmean，年化为 `logmean*252/P`，波动率为 `std(collectionlog)*sqrt(252/P)`，零无风险利率下 Sharpe 为 `logmean/std(collectionlog)*sqrt(252/P)`。报告统计不等同于真实成交组合收益，费用与冲击需另做交易模拟。
- 例如 P20 的 20 条 raw 都对应 10% 的 20 日收益，只输出一个收益 10%、NAV 1.1 的集合点；40 条只输出两个点，NAV 为 1.1、1.21，绝不是 `1.1^40`，不能将同一段持有收益逐日重复累计。
- 分组/集合换手衡量样本集合变化，不自动等于真实成交额换手；历史标签按原始规格解释。
- 小样本、零方差、空截面导致的 NULL 应披露，不转换成看似良好的 0 风险指标。

## 远端 Arena

如用户明确使用Arena且MCP已接入，先读取当前 `factor/request` 与 `overview/dsl`。双源码、托管 `return_specs/return_columns`、指数成员自动过滤和默认预处理属于该服务请求规则。未经对齐，不能直接把Arena请求字段传入SoloAnalysisParams，也不能重写现有Solo接口去“兼容”远端文档。
