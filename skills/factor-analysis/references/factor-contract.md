# 当前 Solo Factor 契约

接口基线：Scheme 的 `base/projects/factor/{algo,params,form}.py`、`execute/factor/{api,result}.py`，以及项目factor模板；以下示例面向提供 `period/price_column/max_lag` 的新版接口。参考 Arena `backend/docs/factor/request.md` 的概念与审计规则，但不套用其请求封装。项目安装版本优先，不要求旧项目升级或改写历史报告。

## 源码结构

项目导出 `Factor`、`FactorParams`、`FactorAnalysisParams`、`FactorReportForm`。Params从 `scheme.base.FactorParams` 继承Universe，不保存 `start/end`；AnalysisParams同时继承项目Params与Scheme分析Params，由分析参数保存日期并传给 `compute`；表单绑定具体项目AnalysisParams。以下示例使用闭区间 `[start,end]` 的新接口，旧环境不自动改写日期语义。

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
    columns: list[str] = Field(default_factory=lambda: ["momentum"], min_length=1)


class FactorReportForm(BaseForm[FactorAnalysisParams]):
    pass


class Factor(BaseFactor[FactorParams]):
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
            factors=["industry_l0", "circ_mv"],
            derivatives=derivatives,
            filters=[Universe.member],
        ) as result:
            return self.preprocess(
                DosVar(result.data_ref), session=result.session, columns=["momentum"],
            )
```

使用前核对当前 `close_hfq` 与 `unary.pct_change` 定义。示例只对输出应用成员filter，动量的历史输入不限制为成员日；是否应在成员子序列上计算是独立业务决定，不能随意添加 `on`。

lookback启发式只提供起点，不保证停牌证券有足够记录。需要完整20个有效交易日时验证样本窗口；缺失结果按明确规则保留/剔除，不能伪造值。

## 返回前预处理

`return self.preprocess(frame, columns=["momentum"])` 接收 `time/code` 加因子列的 DataFrame，也接受 `(date,symbol)` 索引面板；`DosVar` 必须显式传所属 `session`，如上面的查询示例。输出统一为唯一且排序的 `(date,symbol)` 面板，只含所选因子。多个因子可同时传入，省略 `columns` 时排除日期、代码、股票池成员和暴露列后推断；有中间计算列时明确指定，避免误处理。

预处理 DOS 直接内联在 session 中，不执行 `use factor`。每个交易日内按证券代码排序、逐因子裁剪至 median±3×原始MAD，使用总体标准差 `stdp` 做标准化；对标准化的对数流通市值和行业哑变量做含截距 SVD OLS，残差再标准化。默认 `market_value_column="circ_mv"`、`industry_column="industry_l0"`，动态行业来自查询，不使用旧应用的静态 Tushare 行业映射。市值小于1截为1，常量因子、常量log市值、有效样本不足或退化残差保留NULL，不假造零值。

暴露列已存在就原样使用，包括NULL；仅缺列时按输入日期/证券补查，不扩大样本或日期。借用session不关闭，不覆盖原输入变量或查询缓存；返回的是已下载DataFrame，可以安全退出查询的with。空输入及所选因子全部NULL明确报错。这里只处理数值、不生成分组、不在分析器重复执行；原有行和各因子的NULL位置保留，分析器随后取共同有效样本。

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

`period`（持有期）与 `max_lag`（入场延迟上限）默认均为1，只接受严格正整数（不接受bool、0、负数、float或字符串）。`price_column` 可选 `open`、`close`、`open_hfq`、`close_hfq`，默认 `open_hfq`；后复权字段直接读取，不再自行乘 `adj_factor`。对每个 `k=1..max_lag`：

```text
P{period}L{k}(t) = price_column(t+k+period) / price_column(t+k) - 1
```

`t+k` 是交易日，不是自然日；起点最早为t+1，不允许因子当日收盘价起算隔日收益。`period=5, price_column="open_hfq", max_lag=3` 生成 `P5L1/P5L2/P5L3`，对应 t+1→t+6、t+2→t+7、t+3→t+8。默认只生成 `P1L1`，需要 t+1 与 t+2 两个未来报价。

行情限定在闭区间 `[start,end]`，包含end但不读取end之后的价格。按完整交易日网格位移，停牌或缺价不跳日期；未来端点缺价时对应列为NULL，较早k仍可能有效，不延长读取、不补0、不删除仍有因子值的行。

`params.return_columns` 按lag递增，`params.return_specs` 每项包含 `kind="simple"`、`periods=period`、`lag=k`、`price_column` 和 `turnover_period=period`。收益持有期与入场延迟不混用；换手比较期用完整交易日历，不用稀疏因子观测日期。`period>1` 的逐日收益相互重叠，不可直接连乘冒充可交易组合净值。

桥接按完整 `period/price_column/max_lag` 构造对应收益规格，不依赖导入新版Scheme。旧版仅有 `max_lag` 的对象仍生成 `r0o/r1/r1o/r2` 单段隔夜/日内规格；只有 `return_periods` 的旧对象仍输出原 `return_N` 和 `{"kind": "simple", "periods": N}` 的close-to-close累计收益规格。不把旧数据重新解释成新标签，不改旧环境或历史报告。因子实现和手写分组逻辑不因此新增预处理。

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

`compute` 要求 `(date,symbol)` 唯一MultiIndex；所有请求因子列必须为数值；日期不得越界。规范化代码之后重复也无效。无穷因子转换为NaN，分析器取共同有效因子样本。不得以空表/0替代报错来假造成功。

市值加权要求有效样本具有正数 `circ_mv`；等权不需要把市值作为因子输出列。不要把因子分组变化当作真实订单换手。

## 验收提示词

- “在当前因子项目增加可调20日动量，不改报告协议。”应沿用这四类接口/原文件结构，通过本地DSL定义校验。
- “为末尾 `period=5, max_lag=3` 的 `P5L3` 补0，不然报告不好看。”应保留NULL并说明结束日约束，不能延长行情读取。
- “用相同因子复现Arena默认报告。”应先对齐收益和预处理，不能以列名相同声称一致。
