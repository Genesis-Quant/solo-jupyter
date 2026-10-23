# Query 与 Universe

接口基线：Scheme 1.12 的 `data/dolphindb/api.py`、`universe.py`、`database/session.py`。请求节点以项目锁定的 Arena Runtime schema 为准；Solo 的 `solo-runtime` 只负责启动冻结任务，不提供数据查询。

## 静态代码查询

```python
from scheme.data import query

request = {
    "start_date": "2024-06-03",
    "end_date": "2024-06-07",
    "codes": ["600000.SH", "000001.SZ"],
    "factors": ["close", "adj_factor"],
}
with query(request) as result:
    prices = result.data
    trading_days = result.calendar
```

普通请求读取闭区间，输出以 `time/code` 为轴。`factors` 只列需要输出的基础字段；派生计算所需字段会自动读取，所有顶层命名派生列都会输出，filters 只删行。先用 DSL helper 离线校验，只有用户授权实际查询时才运行。

## 历史候选与每日成员

```python
from datetime import date, timedelta

from scheme import StockPool, Universe
from scheme.data import query
from scheme.data.dolphindb.universe import MEMBERS

universe = Universe(pool=StockPool.CSI300)
start, end = date(2024, 6, 3), date(2024, 6, 8)
codes = universe.codes(start, end)
if not codes:
    raise ValueError("所选区间没有股票池成员")
with query({
    "start_date": start.isoformat(),
    "end_date": (end - timedelta(days=1)).isoformat(),
    "lookback": "P90D",
    "codes": codes,
    "factors": ["close", "adj_factor"],
    "derivatives": {universe.member: MEMBERS[universe.pool]},
    "filters": [universe.member],
}) as result:
    member_prices = result.data
```

- `codes(start,end)` 已应用每日成员过滤，再对结果取代码并集；不是把全市场输入代码直接返回。只传回候选列表，后续因子查询使用该列表。
- 区间并集不等于某一天的指数成分。普通 Query 必须再次定义成员 BOOL 并选择 filters 或 on；不自动继承上一次 Universe 条件。
- `MEMBERS` 提供 `ALL/SSE50/CSI300/CSI500/CSI1000` 的成员定义；`universe.member` 是列名字符串。全市场定义为恒真。
- `evaluate(start,end)` 直接返回每日成员 BOOL 面板，列是区间历史成员，使用 `.XSHG/.XSHE`。非成员日为 False，空成员日仍保留真实交易日；空区间成员结果为有日期零列面板。
- 普通 `codes=[]` 有意请求全市场，复用 Tushare `stock_basic` 的 L/D/P 股票缓存；需要 `TUSHARE_TOKEN`。不能将空候选交给它来表示不查询。
- Universe 的日期是 `[start,end)`，普通 Query 和因子分析的结束日包含当天，不重复转换。

## 会话、数据与日历

`query(request, *, session=None, settings=None)` 返回 `QueryResult`。数据与日历各有按需下载属性 `.data`、`.calendar`，服务器引用为 `.data_ref`、`.calendar_ref`；没有源表、计算中间表等额外下载接口。

```python
from scheme.data import query
from scheme.data.dolphindb.database import download

with query(request) as result:
    preview = download(
        result.session,
        f"select top 5 time, code from {result.data_ref}",
        "查询键预览",
    )
```

`.data` 会下载全部最终结果，`.head()` 在其后执行不能限制网络或内存。查询本身先限制日期、代码和列，预览再使用有界服务器投影。

省略 session 时结果拥有连接，退出时关闭；传入 session 时退出只回收本次结果引用，不关闭调用方连接。同一会话的存活查询结果不能重叠。需要在该结果内执行因子预处理时，传同一 session；预处理使用局部 DOS 对象，不覆盖外层结果及日历。结果关闭后不能继续下载或使用引用。

`upload` 只上传必要参数或本地数据；`download` 在 DOS 侧计算表达式一次，先打印类型、行列或项数，再在一次调用中返回数据，局部对象随作用域回收。禁止直接以 `session.run` 下载业务结果；只计算不传回的数据脚本以 NULL 结束。

## 连接与数据解释

只接受一套 `DOLPHINDB_ENDPOINTS`、`DOLPHINDB_USERNAME`、`DOLPHINDB_PASSWORD`。多个 `host:port` 以逗号分隔，IPv6 使用 `[地址]:端口`；新连接按顺序尝试，不重连、重放查询或委托，不回填旧环境变量。

查询日历来自区间内真实 close 记录。行业使用历史种子再按证券前向填充；财务按已读取公告事件前向填充；指数权重在完整观测日给未出现证券补零并前向填充，包含查询开始前快照和非交易日调整。价格不能擅自补值。可重算当前库不等于有不可修订的历史时点归档。

## 验收提示

- “读取 6 月 3 日至 7 日沪深 300 成员收盘价”：先取历史候选，后续 Query 明确定义并过滤每日成员。
- “保留指数退出后的估值和交易数据”：候选并集保留，Query 不删除需要继续处理的非成员行；成员条件与持仓退出分别处理。
- “没有候选，传空代码就行”：拒绝扩大为全市场，跳过或明确返回空结果。
