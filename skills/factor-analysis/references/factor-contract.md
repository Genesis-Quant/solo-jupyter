# 当前 Solo Factor 契约

接口基线：Scheme 的 `base/projects/factor/{algo,params,form}.py`、`execute/factor/{api,result}.py`，以及项目factor模板；以下示例面向提供 `period/price_column/max_lag` 的新版接口。参考 Arena `backend/docs/factor/request.md` 的概念与审计规则，但不套用其请求封装。项目安装版本优先，不要求旧项目升级或改写历史报告。

## 源码结构

项目导出 `Factor`、`FactorParams`、`FactorAnalysisParams`、`FactorReportForm`。Params 从 `scheme.base.FactorParams` 继承 Universe，不保存 `start/end`；AnalysisParams 同时继承项目 Params 与 Scheme 分析 Params，由分析参数保存日期并传给 `compute`；表单绑定具体项目 AnalysisParams。因子输出只在 Factor 类上声明 `columns`，不在项目 AnalysisParams 重复声明、表单不提供该输入，也不传给 preprocess；Analysis 的同名字段默认为空列表，由保存/run/analyze 按类声明自动绑定为报告元数据，不需手填。以下示例使用闭区间 `[start,end]` 的新接口，旧环境不自动改写日期语义。

```python
from datetime import date, timedelta

import pandas as pd
from pydantic import Field
from scheme import Factor as BaseFactor
from scheme import DosVar, Universe
from scheme.base import FactorParams as BaseParams
from scheme.base import FactorAnalysisParams as BaseAnalysis
from scheme.base import FactorReportForm as BaseForm


class FactorParams(BaseParams):
    periods: int = Field(default=20, ge=1)


class FactorAnalysisParams(FactorParams, BaseAnalysis):
    pass


class FactorReportForm(BaseForm[FactorAnalysisParams]):
    pass


class Factor(BaseFactor[FactorParams]):
    columns = ["momentum"]

    def compute(self, start: date, end: date) -> pd.DataFrame:
        if start > end:
            raise ValueError("因子日期区间必须满足 start <= end")
        derivatives = {
            "momentum": {
                "type": "TS",
                "op": "unary.pct_change",
                "fields": {"col": "close_hfq"},
                "params": {"periods": self.params.periods},
            }
        }
        with self.params.universe.query(
            start, end + timedelta(days=1),
            lookback=timedelta(days=max(90, self.params.periods * 4)),
            derivatives=derivatives,
            filters=[Universe.member],
        ) as result:
            return self.preprocess(
                DosVar(result.data_ref), session=result.session,
            )
```

使用前核对当前 `close_hfq` 与 `unary.pct_change` 定义。示例只对输出应用成员filter，动量的历史输入不限制为成员日；是否应在成员子序列上计算是独立业务决定，不能随意添加 `on`。

lookback启发式只提供起点，不保证停牌证券有足够记录。需要完整20个有效交易日时验证样本窗口；缺失结果按明确规则保留/剔除，不能伪造值。

## 返回前预处理

`return self.preprocess(frame)` 接收 `time/code` 加因子列的 DataFrame，也接受 `(date,symbol)` 索引面板；数据可以完全来自本地计算，不要求先执行 query。`DosVar` 必须显式传所属 `session`，如上面的查询示例。输出列取自 Factor 类唯一的 `columns` 声明，不在 preprocess、AnalysisParams 或表单重复指定，也不从临时列推断；输出统一为唯一且排序的 `(date,symbol)` 面板。

预处理 DOS 在自有或借用的计算 session 中内联；暴露从另一个独立 query session 获取，不执行 `use factor`。每个交易日内按证券代码排序、逐因子裁剪至 median±3×原始MAD，使用总体标准差 `stdp` 做标准化；对标准化的对数流通市值和行业哑变量做含截距 SVD OLS，残差再标准化。默认 `market_value_column="circ_mv"`、`industry_column="industry_l0"`，动态行业来自查询，不使用旧应用的静态 Tushare 行业映射。市值小于1截为1，常量因子、常量log市值、有效样本不足或退化残差保留NULL，不假造零值。

preprocess 始终另开自有（owned）query，按输入日期与证券获取行业/市值暴露后 join 因子；因子原查询不预取暴露，输入已有同名列也不替代这次独立读取。独立查询负责关闭自己的 session，借用的因子 session 不关闭，不覆盖原输入变量或查询缓存；返回的是已下载 DataFrame，可以安全退出因子查询的 with。空输入及声明因子全部 NULL 明确报错。这里只处理数值、不生成分组、不在分析器重复执行；不扩大输入日期/证券域，原有行和各因子的 NULL 位置保留，分析器随后取共同有效样本。

## Notebook 运行

```python
from scheme import StockPool

form = FactorReportForm(
    start="2024-06-03", end="2024-06-08",
    pool=StockPool.SSE50,
    periods=20,
    period=5, price_column="open_hfq", max_lag=3,
    groups=5, n_select=2, weight="equal",
)
params = form.build()
report = params.run(Factor)
```

只有用户请求实际运行时才执行最后一行。在Solo启动的Notebook里 `report.show()` 通过桥接保存并展示；不在普通Python脚本中假装存在该桥接。仅导出Parquet时，`report.save(output)` 会创建目录但也可覆盖同名文件，因此使用事先确认不存在的新输出目录。

## 收益参数与报告元数据

`period`（持有期）与 `max_lag`（入场延迟上限）默认均为1，只接受严格正整数（不接受bool、0、负数、float或字符串）。`price_column` 可选 `open`、`close`、`open_hfq`、`close_hfq`，默认 `open_hfq`；入场与退出使用同一价格字段，后复权字段直接读取，不再自行乘 `adj_factor`。当前所有因子评价收益均为log，对每个 `k=1..max_lag`：

```text
P{period}L{k}(t) = ln(price_column(t+k+period) / price_column(t+k))
```

`lag=k>=1`，`t+k` 是交易日，不是自然日；起点最早为t+1，不允许因子当日收盘价起算隔日收益。`period=5, price_column="open_hfq", max_lag=3` 生成 `P5L1/P5L2/P5L3`，对应 t+1→t+6、t+2→t+7、t+3→t+8。默认只生成 `P1L1`，需要 t+1 与 t+2 两个未来报价。这里定义的是评价标签，不改变上文compute中的动量因子公式。

行情限定在闭区间 `[start,end]`，包含end但不读取end之后的未来区间。按完整交易日网格位移，不能在稀疏因子面板上位移，停牌或缺价不跳日期；入场或退出端点缺价或非正时对应列为NULL，较早k仍可能有效，不延长读取、不补0、不删除仍有因子值的行。

`params.return_columns` 按lag递增，`params.return_specs` 每项包含 `kind="log"`、`periods=period`、`lag=k`、`price_column` 和 `turnover_period=period`。收益持有期与入场延迟不混用；换手比较期用完整交易日历，不用稀疏因子观测日期。

桥接校验完整 `period/price_column/max_lag` 后，直接使用报告参数的 `return_specs`，不凭参数形状强制改写收益类型。Scheme 1.10 起生成log标签，1.8/1.9的simple结果保留其实际规格。旧版仅有 `max_lag` 的对象仍生成 `r0o/r1/r1o/r2` 单段隔夜/日内规格；只有 `return_periods` 的旧对象仍输出原 `return_N` 和 `{"kind": "simple", "periods": N}` 的close-to-close累计收益规格。不把旧数据重新解释成新标签，不改旧环境或历史报告。因子实现和手写分组逻辑不因此新增预处理。

## 报告有效收益与曲线

各组raw收益仍是当日有效log标签的加权均值（按所选等权或市值权），不先对单个标签除以持有期；IC/RankIC直接使用当日因子与原始log标签，不平滑。

对每个因子、收益列和分组，在报告完整交易日轴上计算：

```text
effective(t) = mean(raw(t), ..., raw(t-period+1))
```

这是当前及前 `period-1` 个交易期raw log收益的算术平均；序列开头不足period时使用已有样本，不额外除以period，不平均不同lag。先补齐完整交易日轴，缺失日期保留NULL；窗口中任一raw为NULL，当期effective即为NULL，不跳过NULL求均值。日期选择先在报告全表计算窗口再截取，不能在所选起点重启窗口；`period=1`使用同一规则。

分组净值为 `exp(cumsum(effective))`。多空先取同日 `high-low` raw log收益，再使用同一窗口及NULL规则；时序 `value=effective` 为log，`cumulative=cumsum(effective)` 为logsum，不是简单收益或净值。

均值、年化、波动率与Sharpe都使用同一有效序列的非NULL观测：均值为logmean，年化为 `logmean*252`，波动率为 `std(effective)*sqrt(252)`，零无风险利率下Sharpe为 `logmean/std(effective)*sqrt(252)`。所有持有期统一生成曲线和统计；NULL日期留断点、不补零，不可定义的指标保留NULL。

## 返回与校验

`factor.validate(start,end,n_sample=5)` 计算一次完整闭区间因子表，从输出的唯一日期无放回抽样，逐日原样调用 `compute(d,d)`。全部证券、列和NULL位置必须匹配，数值使用 `rtol=1e-7, atol=1e-10`，行列排列不同可以对齐；日期少于样本数时全部检查。通过返回 `None`，不一致抛出含日期的错误，空表或重复索引不能通过。不要把抽样通过说成穷尽证明。

Jupyter 保存时只在冻结源码阶段自动调用一次；打开表单、普通参数校验、正式任务及重试提交不重复运行。校验失败会阻断提交，不生成报告来冒充验证。旧Schema或旧compute尚未迁移时明确报错，不静默跳过。

FactorAnalysisResult字段：

- `processed_data`
- `execution_statistics`
- `information_coefficient`
- `group_returns`
- `group_turnover`
- `parameters`

`compute` 的返回列须与 Factor.columns 完全一致，不能夹带暴露或未声明中间列；可以返回 `(date,symbol)` 唯一 MultiIndex，也可返回 `time/code` 平表，由包装层转为同一面板。所有声明因子列必须为数值；日期不得越界。规范化代码之后重复也无效。无穷因子转换为NaN，分析器取共同有效因子样本。不得以空表/0替代报错来假造成功。

市值加权要求有效样本具有正数 `circ_mv`；等权不需要把市值作为因子输出列。不要把因子分组变化当作真实订单换手。

## 验收提示词

- “在当前因子项目增加可调20日动量，不改报告协议。”应沿用这四类接口/原文件结构，通过本地DSL定义校验。
- “为末尾 `period=5, max_lag=3` 的 `P5L3` 补0，不然报告不好看。”应保留NULL并说明结束日约束，不能延长行情读取。
- “用相同因子复现Arena默认报告。”应先对齐收益和预处理，不能以列名相同声称一致。
