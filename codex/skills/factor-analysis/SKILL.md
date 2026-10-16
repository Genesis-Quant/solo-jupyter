---
name: factor-analysis
description: 在 Solo 创建或修改因子研究、Factor/FactorParams/ReportForm、IC/RankIC、分组收益、换手、收益标签或预处理时使用。也用于比较 Arena 与 Solo 报告口径，防止混淆默认预处理、收益时点和结束日截断。
---

# Solo 因子研究

默认使用项目现有 `Factor`、`FactorParams`、`FactorAnalysisParams` 和 `FactorReportForm`，不替换为未实现的 Dataset/DSL builder。先读当前项目源码、`pyproject.toml`、`uv.lock`，在项目解释器中核对已安装 Scheme；不升级旧项目来迎合技能示例。

接口与完整例子见 [因子接口](references/factor-contract.md)；口径比较见 [评价边界](references/evaluation.md)。构造派生节点时同时读取相邻 `dsl/SKILL.md`，股票池查询读取 `data-query/SKILL.md`。

## 实现步骤

1. 先定义经济含义、计算时点、股票池、因子方向、窗口和缺失规则。用户仅要求分析时，不擅自改代码或运行大型研究。
2. 使用传统 `TypeVar` / `Generic` 语法；具体项目因子直接继承 `Factor[ProjectParams]`。所有算法参数放入项目Params，不把报告设置藏进硬编码。
3. 实现 `compute(start, end)` 返回数值列DataFrame，唯一索引为 `(date, symbol)`，日期在 `[start,end)`。代码通过 `engine_symbol` 规范化，规范化后仍需唯一。
4. 查询按 `Universe.query` 两阶段接口；成员列不自动过滤。根据业务明确选择 `filters=[Universe.member]`、节点 `on` 或保留非成员记录。
5. 用项目 `FactorReportForm(...).build()` 构造分析参数；表单绑定项目AnalysisParams，不复制一套不一致的Notebook参数。新接口用 `max_lag=2`（默认1，严格正整数，不接受bool）替代 `return_periods`；旧项目按已安装版本保留原参数和收益定义。
6. 先离线核对DSL和参数，再按用户请求小范围 `params.run(Factor)`。检查实际面板、日期、NaN、样本数、标签有效率后扩展。
7. 只有真实Jupyter桥接上下文才使用 `report.show()`；它会保存报告，不再重复调用save造成双份输出。独立脚本使用 `report.save(新目录)`，不要声称浏览器展示已验证。

## 不可混用的口径

- 新版Solo按 `k=1..max_lag` 输出各单段simple收益：`r{k-1}o(t)=O_adj(t+k)/C_adj(t+k-1)-1` 为隔夜，`r{k}(t)=C_adj(t+k)/O_adj(t+k)-1` 为日内；不是close-to-close累计收益，也不是log收益。
- `max_lag=2` 涉及 `t,t+1,t+2` 三个交易日，依次输出 `r0o,r1,r1o,r2`。收益行情受 `[start,end)` 约束；尾部不可用的k对应列为NULL，不补0、不超界补行情，较早k仍可有效。
- 当前Solo分析不会自动执行Arena整套MAD、标准化、行业/市值中性化。需要这些步骤时在因子实现中明确构造，并核对相同截面。
- 多因子分析使用共同有效因子样本，不同分析列可能改变样本域；NULL标签仍不能用于相应IC/收益计算。
- 不同lag的隔夜/日内分组收益不是完整日收益，也不是每天可复投的组合净值；不能直接连乘后称为可交易年化。旧版 `return_N` 仍保持原close-to-close定义，不转换或改写历史报告。
- 相同因子名称不足以证明Solo/Arena结果可比。对齐收益价格/时点、simple或log、日期、成员、预处理、分组、权重和数据版本。

## 交付

提供最小源码/Notebook改动与可复核结果；列明依赖版本、计算区间、股票池、预处理、标签和有效样本。将定义不一致、数据缺失和程序错误分开报告。保留原Notebook自动重载，不添加 `sys.path`/清空 `sys.modules` 的兜底块；不改历史任务和报告。
