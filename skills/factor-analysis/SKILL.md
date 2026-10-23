---
name: factor-analysis
description: 在 Solo 创建或修改因子研究、Factor/FactorParams/ReportForm、IC/RankIC、分组收益、换手、收益标签或预处理时使用。也用于比较 Arena 与 Solo 报告口径，防止混淆默认预处理、收益时点和结束日截断。
---

# Solo 因子研究

默认使用项目现有 `Factor`、`FactorParams`、`FactorAnalysisParams` 和 `FactorReportForm`，不替换为未实现的 Dataset/DSL builder。先读当前项目源码、`pyproject.toml`、`uv.lock`，在项目解释器中核对已安装 Scheme；不升级旧项目来迎合技能示例。

接口与完整例子见 [因子接口](references/factor-contract.md)；口径比较见 [评价边界](references/evaluation.md)。构造派生节点时同时读取相邻 `dsl/SKILL.md`，股票池查询读取 `data-query/SKILL.md`。

## 实现步骤

1. 先定义经济含义、计算时点、股票池、因子方向、窗口和缺失规则。用户仅要求分析时，不擅自改代码或运行大型研究。
2. 使用传统 `TypeVar` / `Generic` 语法；具体项目因子直接继承 `Factor[ProjectParams]`，在 Factor 类上唯一声明 `columns = ["momentum"]` 等输出列，项目 AnalysisParams 不重复声明、表单不提供 columns 输入；Analysis 保留默认空列表的内部字段，由执行流程按类声明自动填入报告，不需手填。`FactorParams` 保存股票池与算法字段，不含 `start/end`；日期放在 `FactorAnalysisParams`，由调用者传入 `compute`，不把报告设置藏进硬编码。
3. 新因子接口的 `compute(start, end)` 只返回类声明 columns 的数值列，支持 `(date,symbol)` 唯一索引或 `time/code` 平表（包装层转为面板），不返回行业、市值或未声明列。日期在闭区间 `[start,end]`，`compute(d,d)` 返回当天；旧项目仍以安装版本的区间约定为准。代码通过 `engine_symbol` 规范化，规范化后仍需唯一。
4. 先用 `universe.codes(start,end + timedelta(days=1))` 取得已过滤的历史成员并集；候选为空时停止。普通 `query` 显式传候选代码，并以 `MEMBERS[universe.pool]` 定义 `universe.member`，按每日成员 filters 或 on 处理。`evaluate` 返回真实交易日 × 历史成员面板，非成员日 False、整个区间无成员时为有日历零列。普通 `codes=[]` 是全市场，不是空候选；Tushare L/D/P 股票列表只用于该输入域。
5. 用项目 `FactorReportForm(...).build()` 构造分析参数；表单绑定项目AnalysisParams，不复制一套不一致的Notebook参数。新接口用 `period`（持有期，默认1）、`max_lag`（入场延迟上限，默认1），均为严格正整数、不接受bool；`price_column` 可选 `open/close/open_hfq/close_hfq`，默认 `open_hfq`。旧项目按已安装版本保留原参数和收益定义。
6. 先离线核对DSL和参数，再按用户请求小范围 `params.run(Factor)`。把因子对象的 `validate(start,end,n_sample)` 放在因子Notebook最后一个代码单元，供用户手动执行；保存版本、构建和正式任务不自动调用，也不以抽样一致性作为提交前置条件。手动校验默认抽最多5个唯一日期，逐个用 `compute(d,d)` 对照整表全部证券和列，抽样通过不是全日期的证明。检查实际面板、日期、NaN、样本数、标签有效率后扩展。
7. `params.run(Factor)` 返回持有服务器结果和会话的 FactorAnalysisResult，不自动下载五张表。属性访问才读取对应表，`preview(name,rows)` 在服务器限行；以 with 或 close 结束生命周期。show/save/to_html 是用户显式导出行为，按表读取，不缓存全部结果。只有真实 Jupyter 桥接才调用 show。

## 不可混用的口径

- 当前Solo所有因子收益均为log，按 `k=1..max_lag` 输出 `P{period}L{k}(t)=ln(price_column(t+k+period)/price_column(t+k))`，`lag=k>=1`，t+k按完整交易日轴计，不在稀疏因子面板上位移。`period=5, price_column="open_hfq", max_lag=3` 输出 `P5L1/P5L2/P5L3`，价格区间为 t+1→t+6、t+2→t+7、t+3→t+8。入场与退出使用同一价格字段，直接读取所选后复权或原始字段，不重复乘复权因子，不使用因子当日收盘价作起点；评价标签的log口径不要求修改compute动量因子公式。
- 行情受 `[start,end]` 约束，包含end当天、不读取结束日后的未来区间；端点缺价或非正时对应列为NULL，不跳日期、不补0、不超界补行情，较早k仍可有效。`return_specs` 使用 `kind="log"`、`periods=period`、`lag=k`、`price_column`、`turnover_period=period`；持有期不是入场延迟。
- Factor 唯一声明输出 columns。`self.preprocess` 在 return 前调用，DosVar 必须传所属 session；列从类声明取得，分组数从 `self.params.groups` 传入原 `factor::factorPreprocess`，不写死分组值。所属行业和流通市值在同一会话内查询并连接，不下载再上传，不分批中性化，不另写算法。只返回数值因子列，不覆盖外层查询。
- 多因子分析使用共同有效因子样本，不同分析列可能改变样本域；NULL标签仍不能用于相应IC/收益计算。各组raw收益是当日有效log标签的加权均值；IC/RankIC直接使用当日因子与原始log标签，不平滑。
- 令 `P=period`，以 full report 的完整交易日轴首日为固定 anchor，每个因子、收益列和分组独立按连续 P 个真实交易日切成不相交集合，每集合恰有 P 条 raw P 日标签。`collectionlog(C)=sum(rawlog[i] for i in C)/P` 是算术平均，只做一次 `/P`，不是 mean 后再除以 P；对应 simple 集合为 `collection_simple(C)=prod(1+R[i] for i in C)**(1/P)-1`，即增长因子的几何平均减一。不使用逐日 rolling average 或 expanding prefix，不平均不同 lag。
- 只有完整 P 日期集合输出一个点，`time` 为集合末日的原观测日期；开头 P-1 个交易日没有集合点，尾部不足 P 个交易日不输出。先按完整真实交易日历补齐缺交易行并填 NULL，任一 raw 为 NULL 则整个集合为 NULL，不跳过 NULL 凑 P。日期选择先在 full report 全表固定集合后，再按集合末日筛选，不按所选起点重切集合或改变 anchor；`period=1` 使用同一规则。历史 simple 展示时先转 `ln(1+r)`，`r<=-1` 或非有限值转 NULL；旧 simple 多期数据使用 `group_turnover` 的完整日历补 NULL，缺少时明确报“缺少完整交易日历”；新版 log 的 `group_returns` 已含完整日历，没有 `group_turnover` 仍支持，不改写历史文件。
- 分组净值为 `exp(cumsum(collectionlog))`，正序/倒序都沿同一集合序列每集合只累计一次。多空取同集合 top-bottom（`high-low`）的集合 log 收益之差；时序 `value=collectionlog` 为 log，`cumulative=cumsum(collectionlog)` 为 logsum。分组均值、t 检验/p 值、年化、波动率与 Sharpe 都基于非 NULL 完整集合，每集合为一次 observation，不把 P 条 raw 当 P 次样本；年化频率为 `252/P`，均值为 logmean，年化为 `logmean*252/P`，波动率为 `std(collectionlog)*sqrt(252/P)`，零无风险利率下 Sharpe 为 `logmean/std(collectionlog)*sqrt(252/P)`。所有 period 统一生成曲线与统计，NULL 保留断点、不补零，不可定义的指标留 NULL；原始日频标签、IC/RankIC、换手的原 period 和未来端点公式不变。
- 例如 P20 的 20 条 raw 都对应 10% 的 20 日收益，只输出一个收益 10%、NAV 1.1 的集合点；40 条只输出两个点，NAV 为 1.1、1.21，绝不是 `1.1^40`。先划定完整日期集合再检查 NULL，不能用下一有效行补足集合，也不能逐日重复累计同一段持有收益。
- 旧版 `r0o/r1/r1o/r2` 保持单段隔夜/日内口径，`return_N` 保持原close-to-close累计收益定义；不转换或改写历史报告。
- 相同因子名称不足以证明Solo/Arena结果可比。对齐收益价格/时点、log口径、日期、成员、预处理、分组、权重和数据版本；不把Solo规则改写为远端Arena契约。

## 交付

提供最小源码/Notebook改动与可复核结果；列明依赖版本、计算区间、股票池、预处理、标签和有效样本。将定义不一致、数据缺失和程序错误分开报告。保留原Notebook自动重载，不添加 `sys.path`/清空 `sys.modules` 的兜底块；不改历史任务和报告。
