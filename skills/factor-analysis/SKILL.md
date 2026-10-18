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
3. 新因子接口的 `compute(start, end)` 只返回类声明 columns 的数值列，支持 `(date,symbol)` 唯一索引或 `time/code` 平表（包装层转为面板），不返回暴露或未声明列。日期在闭区间 `[start,end]`，`compute(d,d)` 返回当天；旧项目仍以安装版本的区间约定为准。代码通过 `engine_symbol` 规范化，规范化后仍需唯一。
4. `Universe.query` 单次执行 `dataset_query`（`codes=[]` 并注入每日成员列），不再两阶段预筛选。底层 `scheme.data.query` 是本地直接执行包装，不是 Runtime alias；每进程首次空 `codes` 从 CoreData 全历史所有记录读取 distinct codes 缓存为 tuple，不受日期或 `close` 限制；后续空列表复用，显式 `codes` 不改。`evaluate` 返回交易日×固定证券域 BOOL 面板，无成分日为全 False、不因缺行情缩列。共享查询仍为半开区间，因此闭区间因子传入 `end + timedelta(days=1)`。成员列不自动过滤。根据业务明确选择 `filters=[Universe.member]`、节点 `on` 或保留非成员记录。
5. 用项目 `FactorReportForm(...).build()` 构造分析参数；表单绑定项目AnalysisParams，不复制一套不一致的Notebook参数。新接口用 `period`（持有期，默认1）、`max_lag`（入场延迟上限，默认1），均为严格正整数、不接受bool；`price_column` 可选 `open/close/open_hfq/close_hfq`，默认 `open_hfq`。旧项目按已安装版本保留原参数和收益定义。
6. 先离线核对DSL和参数，再按用户请求小范围 `params.run(Factor)`。需要分段一致性检查时调用因子对象的 `validate(start,end,n_sample)`；保存版本会自动对冻结源码校验一次，默认抽最多5个唯一日期，逐个用 `compute(d,d)` 对照整表全部证券和列。抽样通过不是全日期的证明。检查实际面板、日期、NaN、样本数、标签有效率后扩展。
7. 只有真实Jupyter桥接上下文才使用 `report.show()`；它会保存报告，不再重复调用save造成双份输出。独立脚本使用 `report.save(新目录)`，不要声称浏览器展示已验证。

## 不可混用的口径

- 新版Solo按 `k=1..max_lag` 输出 `P{period}L{k}(t)=price_column(t+k+period)/price_column(t+k)-1` 的simple收益，t+k按交易日计。`period=5, price_column="open_hfq", max_lag=3` 输出 `P5L1/P5L2/P5L3`，价格区间为 t+1→t+6、t+2→t+7、t+3→t+8。直接使用所选后复权或原始字段，不重复乘复权因子，不使用因子当日收盘价作起点。
- 行情受 `[start,end]` 约束，包含end当天；尾部未来价格不足时对应列为NULL，不补0、不超界补行情，较早k仍可有效。`return_specs` 使用 `periods=period`、`lag=k`、`price_column`、`turnover_period=period`；持有期不是入场延迟。
- 在 Factor 类上声明输出 columns，在 `compute` 的 return 前调用 `self.preprocess(frame)`；传 DosVar 时另给所属 session，不传 columns。因子可完全来自本地 DataFrame，不要求来自 query，也不预取中性化暴露。preprocess 始终另开自有（owned）query，按输入日期/证券读取动态 `industry_l0/circ_mv` 后 join 因子；内联 DOS 执行 3MAD、标准化、行业与 log 流通市值 OLS、残差再标准化，不依赖 `use factor`。借用的因子 session 不关闭，独立查询负责回收；分析器不重复预处理。保留行和 NULL，不把退化截面伪造为 0。
- 多因子分析使用共同有效因子样本，不同分析列可能改变样本域；NULL标签仍不能用于相应IC/收益计算。
- `period>1` 时逐日标签是重叠持有收益，不能直接连乘后称为可交易年化。旧版 `r0o/r1/r1o/r2` 保持单段隔夜/日内口径，`return_N` 保持原close-to-close累计收益定义；不转换或改写历史报告。
- 相同因子名称不足以证明Solo/Arena结果可比。对齐收益价格/时点、simple或log、日期、成员、预处理、分组、权重和数据版本。

## 交付

提供最小源码/Notebook改动与可复核结果；列明依赖版本、计算区间、股票池、预处理、标签和有效样本。将定义不一致、数据缺失和程序错误分开报告。保留原Notebook自动重载，不添加 `sys.path`/清空 `sys.modules` 的兜底块；不改历史任务和报告。
