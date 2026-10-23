# 当前 Solo Factor 契约

接口基线为 Scheme 1.12：base/projects/factor/{algo,params,form}.py、execute/factor/{api,result}.py 和项目模板。只使用当前接口，不回退已删除的方法，不改用户历史数据。

## 源码结构

项目导出 Factor、FactorParams、FactorAnalysisParams、FactorReportForm。FactorParams 保存 Universe、groups 与算法参数，不保存日期；AnalysisParams 继承项目参数并保存闭区间日期及报告设置。columns 唯一来自 Factor 类。groups 从表单进入分析参数后投影到 FactorParams，预处理与报告分组共用同一实际值。

```python
from datetime import date, timedelta

import pandas as pd
from pydantic import Field
from scheme import Factor as BaseFactor
from scheme import DosVar
from scheme.data import query
from scheme.data.dolphindb.universe import MEMBERS
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
        universe = self.params.universe
        codes = universe.codes(start, end + timedelta(days=1))
        if not codes:
            raise ValueError("所选区间没有股票池成员")
        with query({
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "lookback": timedelta(days=max(90, self.params.periods * 4)),
            "codes": codes,
            "derivatives": {universe.member: MEMBERS[universe.pool], **derivatives},
            "filters": [universe.member],
        }) as result:
            return self.preprocess(
                DosVar(result.data_ref), session=result.session,
            )
```

使用前核对当前 `close_hfq` 与 `unary.pct_change` 定义。示例只对输出应用成员filter，动量的历史输入不限制为成员日；是否应在成员子序列上计算是独立业务决定，不能随意添加 `on`。

lookback启发式只提供起点，不保证停牌证券有足够记录。需要完整20个有效交易日时验证样本窗口；缺失结果按明确规则保留/剔除，不能伪造值。

## 返回前预处理

`return self.preprocess(frame)` 接收 `time/code` 加因子列的 DataFrame，也接受 `(date,symbol)` 索引面板；数据可以完全来自本地计算，不要求先执行 query。`DosVar` 必须显式传所属 `session`，如上面的查询示例。输出列取自 Factor 类唯一的 `columns` 声明，不在 preprocess、AnalysisParams 或表单重复指定，也不从临时列推断；输出统一为唯一且排序的 `(date,symbol)` 面板。

预处理在同一会话中查询并连接所属行业和流通市值，然后单次调用原 factor::factorPreprocess；第三个参数是 groups，必须传实际参数而不是写死数值。数值流程仍由原 factor 模块维护，不按 Python 批次拆分，不下载行业、市值再上传。

DosVar 必须传所属会话，处理使用局部 DOS 对象并保留外层查询和日历；不传会话的 DataFrame 处理才创建自有连接。只输出声明的数值列；函数产生的临时分组列不下载，分析根据共同有效样本与相同 groups 确定最终分组。空输入、布尔/复数和全空因子明确报错，不伪造零值。

## Notebook 运行

```python
from scheme import StockPool

form = FactorReportForm(
    start="2024-06-03", end="2024-06-28",
    pool=StockPool.SSE50,
    periods=20,
    period=5, price_column="open_hfq", max_lag=3,
    groups=5, n_select=2, weight="equal",
)
params = form.build()
report = params.run(Factor)
```

示例至少需 `period+max_lag+1` 个真实交易日才能在端点价格有效时生成最大lag的首个标签（此处为9日）；该区间扣除2024-06-10端午休市仍有19个交易日，足够按报告首日固定锚点划出3个完整 P5 日期集合（末日分别为2024-06-07、2024-06-17、2024-06-24），尾部4日不输出。完整日期集合不代表标签全部有效：受未来端点限制，集合内任一标签NULL时该集合收益仍为NULL。lookback仅补因子历史，不补研究end后的未来报价。

只有用户请求实际运行时才执行最后一行。在Solo启动的Notebook里 `report.show()` 通过桥接保存并展示；不在普通Python脚本中假装存在该桥接。仅导出Parquet时，`report.save(output)` 会创建目录但也可覆盖同名文件，因此使用事先确认不存在的新输出目录。

## 收益参数与报告元数据

`period`（持有期）与 `max_lag`（入场延迟上限）默认均为1，只接受严格正整数（不接受bool、0、负数、float或字符串）。`price_column` 可选 `open`、`close`、`open_hfq`、`close_hfq`，默认 `open_hfq`；入场与退出使用同一价格字段，后复权字段直接读取，不再自行乘 `adj_factor`。当前所有因子评价收益均为log，对每个 `k=1..max_lag`：

```text
P{period}L{k}(t) = ln(price_column(t+k+period) / price_column(t+k))
```

`lag=k>=1`，`t+k` 是交易日，不是自然日；起点最早为t+1，不允许因子当日收盘价起算隔日收益。`period=5, price_column="open_hfq", max_lag=3` 生成 `P5L1/P5L2/P5L3`，对应 t+1→t+6、t+2→t+7、t+3→t+8。默认只生成 `P1L1`，需要 t+1 与 t+2 两个未来报价。这里定义的是评价标签，不改变上文compute中的动量因子公式。

行情限定在闭区间 `[start,end]`，包含end但不读取end之后的未来区间。按完整交易日网格位移，不能在稀疏因子面板上位移，停牌或缺价不跳日期；入场或退出端点缺价或非正时对应列为NULL，较早k仍可能有效，不延长读取、不补0、不删除仍有因子值的行。

`params.return_columns` 按lag递增，`params.return_specs` 每项包含 `kind="log"`、`periods=period`、`lag=k`、`price_column` 和 `turnover_period=period`。收益持有期与入场延迟不混用；换手比较期用完整交易日历，不用稀疏因子观测日期。

桥接校验 period/price_column/max_lag，并直接使用 parameters.return_specs；不创建旧版参数适配或改写已有文件。

## 报告集合收益与曲线

各组 raw 收益仍是当日有效 log 标签的加权均值（按所选等权或市值权），不先对单个标签除以持有期；IC/RankIC 直接使用当日因子与原始 log 标签，不平滑。原始日频标签、换手的原 period 和未来端点公式不变，不把信号因子 momentum 改为 log。

令 `P=period`，以 full report 的完整交易日轴首日为固定 anchor，每个因子、收益列和分组独立按连续 P 个真实交易日切成不相交集合，每集合恰有 P 条 raw P 日标签。若交易日为 d1…dN，则第 j 个集合为 `C_j={d_{jP+1},...,d_{(j+1)P}}`（j 从 0 开始），只保留 `(j+1)P<=N` 的完整日期集合。

- 集合 log 收益：`collectionlog(C)=sum(rawlog[i] for i in C)/P`。这是算术平均，只做一次 `/P`，不是 mean 后再除以 P。
- 对应 simple 集合收益：`collection_simple(C)=prod(1+R[i] for i in C)**(1/P)-1`，即这 P 条 P 日收益增长因子的几何平均减一。

不使用逐日 rolling average 或 expanding prefix，不平均不同 lag。只有完整 P 日期集合输出一个点，`time` 为集合末日的原观测日期；开头 P-1 个交易日没有集合点，尾部不足 P 个交易日不输出。先按完整真实交易日历补齐缺交易行并填 NULL，任一 raw 为 NULL 则整个集合为 NULL，在集合末日保留断点，不跳过 NULL 凑 P。日期选择先在 full report 全表固定集合后，再按集合末日筛选，不按所选起点重切集合或改变 anchor；`period=1` 使用同一规则。

历史 simple 展示时先转 `ln(1+r)`，`r<=-1` 或非有限值转 NULL；旧 simple 多期数据使用 `group_turnover` 的完整日历补 NULL，缺少时明确报“缺少完整交易日历”，不按已有行压缩；新版 log 的 `group_returns` 已含完整日历，没有 `group_turnover` 仍支持。原收益 metadata 和历史文件不改写。

分组净值为 `exp(cumsum(collectionlog))`，正序/倒序都沿同一集合序列每集合只累计一次。多空取同集合 top-bottom（`high-low`）的集合 log 收益之差；时序 `value=collectionlog` 为 log，`cumulative=cumsum(collectionlog)` 为 logsum，不是简单收益或净值。

分组均值、t 检验/p 值、年化、波动率与 Sharpe 都基于非 NULL 完整集合，每集合为一次 observation，不把 P 条 raw 当 P 次样本；年化频率为 `252/P`，均值为 logmean，年化为 `logmean*252/P`，波动率为 `std(collectionlog)*sqrt(252/P)`，零无风险利率下 Sharpe 为 `logmean/std(collectionlog)*sqrt(252/P)`。所有持有期统一生成曲线与统计；NULL 保留断点、不补零，不可定义的指标保留 NULL。

例如 P20 的 20 条 raw 都对应 10% 的 20 日收益，只输出一个收益 10%、NAV 1.1 的集合点；40 条只输出两个点，NAV 为 1.1、1.21，绝不是 `1.1^40`。完整日期集合中即使只有一条 raw 为 NULL，也不输出伪造的 10% 或跳过缺失另凑 20 条。

## 返回与校验

`factor.validate(start,end,n_sample=5)` 计算一次完整闭区间因子表，从输出的唯一日期无放回抽样，逐日原样调用 `compute(d,d)`。全部证券、列和NULL位置必须匹配，数值使用 `rtol=1e-7, atol=1e-10`，行列排列不同可以对齐；日期少于样本数时全部检查。通过返回 `None`，不一致抛出含日期的错误，空表或重复索引不能通过。不要把抽样通过说成穷尽证明。

`validate` 只在因子Notebook最后一个代码单元中显式调用，供用户手动检查；分析和报告单元在前，不因该抽样检查失败而阻断保存版本。打开表单、保存构建、正式任务及重试提交均不自动调用。参数合法性、因子类输出列、索引和实际计算错误仍正常校验；旧Schema或旧compute不符合所安装版本接口时明确报错，不静默跳过。

FactorAnalysisResult 是持有服务端结果和会话的对象，不是预先下载五张表的数据容器。创建和 repr 不下载；属性访问只读取对应表，preview(name,rows) 在服务器限行。with 或 close 释放会话；关闭后不再读取。save、show、to_html 是显式导出请求，按表读取，不在 analyze_factors 返回前下载。

按需数据属性：

- `processed_data`
- `execution_statistics`
- `information_coefficient`
- `group_returns`
- `group_turnover`
- `parameters`

`compute` 的返回列须与 Factor.columns 完全一致，不能夹带行业、市值或未声明中间列；可以返回 `(date,symbol)` 唯一 MultiIndex，也可返回 `time/code` 平表，由包装层转为同一面板。所有声明因子列必须为数值；日期不得越界。规范化代码之后重复也无效。无穷因子转换为NaN，分析器取共同有效因子样本。不得以空表/0替代报错来假造成功。

市值加权要求有效样本具有正数 `circ_mv`；等权不需要把市值作为因子输出列。不要把因子分组变化当作真实订单换手。

## 验收提示词

- “在当前因子项目增加可调20日动量，不改报告协议。”应沿用这四类接口/原文件结构，通过本地DSL定义校验。
- “为末尾 `period=5, max_lag=3` 的 `P5L3` 补0，不然报告不好看。”应保留NULL并说明结束日约束，不能延长行情读取。
- “用相同因子复现Arena默认报告。”应先对齐收益和预处理，不能以列名相同声称一致。
