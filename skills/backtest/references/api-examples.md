# Solo Python API 与示例

## 项目环境先行

版本基线为 Scheme 1.12、Backtest 0.2、DolphinDB SDK 3.0.4.2。使用当前项目 `.venv` / kernel 的 Python 3.12，核对锁与安装实现；不要使用另一个项目或全局 Python 3.13 环境替代它。

```python
import inspect
import sys
from importlib.metadata import version
from backtest import Algo, Backtest, OrderReport, TradeReport
from scheme.base import ModelReportForm, ExecutionReportForm

print(sys.executable, sys.version.split()[0])
print({name: version(name) for name in ("scheme", "backtest", "dolphindb")})
print(inspect.getfile(Algo))
for name in ("initialize", "on_snapshot", "on_order", "on_trade"):
    print(name, inspect.signature(getattr(Algo, name)))
print(inspect.signature(Backtest.submit_order))
print(ModelReportForm.model_json_schema())
print(ExecutionReportForm.model_json_schema())
print(OrderReport.model_json_schema(), TradeReport.model_json_schema())
print("form.run", hasattr(ModelReportForm, "run"))
```

只做离线自省，不实例化 Backtest；它的构造函数会连接并创建引擎。

## 公共回调骨架（本身不连接、不下单）

```python
from collections.abc import Sequence
from typing import Generic, TypeVar
from backtest import Algo, DosVar, OrderReport, TradeReport
from pydantic import BaseModel

class Params(BaseModel):
    pass

class State(BaseModel):
    filled_shares: int = 0

C = TypeVar("C", bound=State)

class Observe(Algo[Params, C], Generic[C]):
    def initialize(self) -> None:
        self.last_status: dict[int, int] = {}

    def on_snapshot(self, msg: DosVar) -> None:
        # msg 是驱动器提供的可信 DOS 表引用，不拼接外部用户文本。
        # 必要时对它运行 schema，而不是 msg["timestamp"]。
        positions = self.backtest.get_position()
        self.held_symbols = [
            code for code, p in positions.items() if p.longPosition > 0
        ]

    def on_order(self, orders: Sequence[OrderReport]) -> None:
        for event in orders:  # 同一批可含同一订单的多个变化。
            self.last_status[event.orderId] = int(event.status)

    def on_trade(self, trades: Sequence[TradeReport]) -> None:
        for event in trades:
            self.ctx.filled_shares += event.tradeQty  # 当次成交量。
```

`backtest` 与 `ctx` 由驱动器绑定；所有 Algo 共用同一个 Python ctx。`msg` 可被后续行情覆盖，跨回调留存时复制必要数据。`get_context()` 返回 DOS 状态快照，修改它不会写回服务端。初始化正序，事件通知逆序；Scheme 随后正序消费 `signal -> target -> orders`，消费后清空，长期状态留在算法属性。

## 报告入口：Model 优先

当前项目通常从自身包导出专门的 `ModelAlgo`、`ModelReportForm`。以下以仓库 Model 模板的导出名为例；替换为当前项目真实包名，不自动安装模板。

```python
from datetime import date
from model import ModelAlgo, ModelReportForm
from scheme import StockPool

form = ModelReportForm(
    start=date(2026, 1, 5), end=date(2026, 1, 10),
    pool=StockPool.CSI300, market_data="stock_daily",
    cash=500_000, benchmark=None,
)
parameters = form.build()  # 构造/校验；不执行研究。
# 仅在授权实际回测且数据就绪后运行下一行：
# report = parameters.run(ModelAlgo)
```

Scheme 1.12 的 Model 表单默认后续环节是 `risk_parity`、`no_control`、`direct_execution`。`no_control` 不等于生产风险控制已完整实现；默认执行也不意味着所有订单立即成交。只改信号时优先保留这条组装链路。

基类表单没有 `form.run()`，实际是 `.build()` 得到分析参数再 `.run(AlgoClass, ctx=..., settings=...)`。如果项目自定义提供了便捷方法，先 inspect 后使用。不要把算法实例或普通 `backtest.Algo` 直接传给要求 Scheme `ModelAlgo` 的研究入口。

## ExecutionReportForm 路径

执行项目使用其自身的 `ExecutionReportForm` / `ExecutionAlgo`；表单要求 `model`、`optimize`、`control` 三个上游入口。先用 `scheme.algo_options(kind)` 列出当前环境兼容的已安装入口（形式如 `module:ModelAlgo`），按任务选择，不猜包名或自动安装。选项为空时报告缺少兼容上游，不自行替换策略。

```python
from scheme import algo_options
from scheme.base import ExecutionReportForm

# entries 是已选择的真实入口映射，不是下列代码自动选的默认值。
def build_execution_form(entries: dict[str, str]) -> ExecutionReportForm:
    for kind in ("model", "optimize", "control"):
        if entries[kind] not in algo_options(kind):
            raise ValueError(f"未安装或不兼容的上游：{kind}")
    return ExecutionReportForm(
        start="2026-01-05", end="2026-01-10", benchmark=None,
        model=entries["model"], optimize=entries["optimize"],
        control=entries["control"],
    )
```

当前项目应使用自己的专门表单以纳入 Execution 参数。得到 `parameters = form.build()` 后调用 `parameters.run(CurrentExecutionAlgo)`；上游查找失败就是环境/入口问题，不应默默切换另一套策略。

## 下单与读取边界

- `Backtest.submit_order(LimitOrder(...)) -> int` 是 Python 单单封装；原生 `Backtest::submitOrder` 返回 LONG VECTOR。都不代表成交。
- `LimitOrder` 的字段是 `symbol,time,quantity,direction,label,price`；用当前时间与可见报价，数量/价格有限且合法，不能硬编码未来价格。
- `get_position() -> dict[str, Position]` 允许空字典；均价不是现价。活动订单需按当前封装/插件的真实接口核对，不能用 `get_orders()` 替代。
- 手动使用底层引擎时，`load_messages(start,end)` 是 `[start,end)`；原生 config 的 `endDate` 含结束日。`run()` 完成 finish，退出 `with` 关闭引擎，不自动补做 finish。完整结果在 finish 后、close 前提取。
- Scheme 返回脱离 session 的 `BacktestResult`：四张交易/账户 DataFrame，另有 `allocation_diagnostics` 和参数快照。按实际列审计，不假定只有四个文件。
- `report.show()` 需要实际 IPython shell 上可调用的 `solo_report_display`；普通脚本/CLI 中不声称展示成功。按需 `report.preview(name)`、`report.to_html()` 或 `report.save(path)`；没有要求就不额外写文件。

## 验收标准与人工试用提示

分别检查实际签名、Pydantic schema、表单 build/run 分界、默认组装、结果审计与显示桥接。离线检查不代表数据库回测、真实撮合或 Jupyter 显示已通过。

1. “改当前 Model 项目，用昨收信号在下一交易日执行。先给我 `[2026-01-05,2026-01-10)` 的小样本表单与四环节接入，不连接生产、不升级依赖。”
   - 期待：项目解释器、四个正确回调、传统 TypeVar、`build().run(AlgoClass)` 分界；历史时点与结束日正确；不虚构已跑/已展示。
2. “回测完成但一单没成交。请只审计已有 report/Parquet：收益、资金、空持仓、拒单、部分成交和期末挂单；不要重跑服务。”
   - 期待：先实际 schema/日期；订单号与成交分开，检查终态与交易统计，不累计事件表的重复数量，说明数据不足。

以上提示是人工验收示例，不是已执行声明；先检查技能触发与离线输出，真实回测或显示另需在授权环境验证。
