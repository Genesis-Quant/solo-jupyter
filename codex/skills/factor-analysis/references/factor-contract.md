# 当前 Solo Factor 契约

编写基线：Scheme 1.5.0 的 `base/projects/factor/{algo,params,form}.py`、`execute/factor/{api,result}.py`，以及五类模板1.5.0中的factor。参考 Arena `backend/docs/factor/request.md` 的概念与审计规则，但不套用其请求封装。项目安装版本优先。

## 源码结构

项目导出 `Factor`、`FactorParams`、`FactorAnalysisParams`、`FactorReportForm`。Params从 `scheme.base.FactorParams` 继承基础日期/Universe；AnalysisParams同时继承项目Params与Scheme分析Params；表单绑定具体项目AnalysisParams。

```python
from datetime import date, timedelta

import pandas as pd
from pydantic import Field
from scheme import Factor as BaseFactor
from scheme import Universe
from scheme.base import FactorParams as BaseParams
from scheme.base import FactorAnalysisParams as BaseAnalysis
from scheme.base import FactorReportForm as BaseForm
from scheme.data.dolphindb.symbols import engine_symbol


class FactorParams(BaseParams):
    periods: int = Field(default=20, ge=1)


class FactorAnalysisParams(FactorParams, BaseAnalysis):
    columns: list[str] = Field(default_factory=lambda: ["momentum"], min_length=1)


class FactorReportForm(BaseForm[FactorAnalysisParams]):
    pass


class Factor(BaseFactor[FactorParams]):
    def compute(self, start: date, end: date) -> pd.DataFrame:
        derivatives = {
            "momentum": {
                "type": "TS",
                "op": "unary.pct_change",
                "fields": {"col": "close_hfq"},
                "params": {"periods": self.params.periods},
            }
        }
        with self.params.universe.query(
            start, end,
            lookback=timedelta(days=max(90, self.params.periods * 4)),
            derivatives=derivatives,
            filters=[Universe.member],
        ) as result:
            panel = result.data[["time", "code", "momentum"]].copy()
        panel = panel.rename(columns={"time": "date", "code": "symbol"})
        panel["date"] = pd.to_datetime(panel["date"])
        panel["symbol"] = panel["symbol"].map(engine_symbol)
        return panel.set_index(["date", "symbol"]).sort_index()
```

使用前核对当前 `close_hfq` 与 `unary.pct_change` 定义。示例只对输出应用成员filter，动量的历史输入不限制为成员日；是否应在成员子序列上计算是独立业务决定，不能随意添加 `on`。

lookback启发式只提供起点，不保证停牌证券有足够记录。需要完整20个有效交易日时验证样本窗口；缺失结果按明确规则保留/剔除，不能伪造值。

## Notebook 运行

```python
from scheme import StockPool

form = FactorReportForm(
    start="2024-06-03", end="2024-06-08",
    pool=StockPool.SSE50,
    periods=20,
    return_periods=[1, 2], groups=5, n_select=2, weight="equal",
)
params = form.build()
report = params.run(Factor)
```

只有用户请求实际运行时才执行最后一行。在Solo启动的Notebook里 `report.show()` 通过桥接保存并展示；不在普通Python脚本中假装存在该桥接。仅导出Parquet时，`report.save(output)` 会创建目录但也可覆盖同名文件，因此使用事先确认不存在的新输出目录。

## 返回与校验

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
- “为末尾5日收益补0，不然报告不好看。”应保留NULL并说明结束日约束，不能延长行情读取。
- “用相同因子复现Arena默认报告。”应先对齐收益和预处理，不能以列名相同声称一致。
