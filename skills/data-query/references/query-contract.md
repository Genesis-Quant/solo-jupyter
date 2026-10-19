# Query 与 Universe

来源：Arena `backend/docs/query/request.md`、`overview/dsl.md`（参考提交 `802cb0fd4ef45a0fc7db6168b96497de68eda537`），Solo Scheme `data/dolphindb/api.py`、`universe.py`（编写基线1.5.0）。可用参数以项目锁定版本为准。

## 静态代码查询

```python
from scheme.data import query

request = {
    "start_date": "2024-06-03",
    "end_date": "2024-06-07",
    "lookback": "P0D",
    "codes": ["600000.SH", "000001.SZ"],
    "factors": ["close", "adj_factor"],
    "derivatives": {},
    "filters": [],
}
with query(request) as result:
    prices = result.data
```

直接查询读到6月7日，输出以 `time, code` 为轴。`factors` 是要求输出的基础列，不必重复添加仅被派生节点引用的基础输入。所有顶层命名derivatives也会输出；filters仅删除行，不删除其BOOL列。

先用相邻DSL skill的helper验证完整request，再决定是否执行。查询会访问现有DolphinDB，示例不能作为已经运行成功的证据。

## 指数动态池与过滤

```python
from datetime import date, timedelta
from scheme import StockPool, Universe

universe = Universe(pool=StockPool.CSI300)
start, end = date(2024, 6, 3), date(2024, 6, 8)
with universe.query(
    start, end,
    lookback=timedelta(days=90),
    factors=["close", "adj_factor"],
    filters=[Universe.member],
) as result:
    member_prices = result.data
```

此例`end`不含6月8日，并**显式选择**只保留当日成员。若要保留非成员行，移除filters；若只要求某个TS/CS节点在成员内计算，将 `Universe.member` 用于该节点的 `on`。

- `scheme.data.query` 是本地直接执行包装，不是 Runtime `execute_query` 的 alias。`codes=[]` 直接使用 Arena Runtime 的 `get_codes()`：首次从 Tushare `stock_basic` 合并 `L/D/P`（上市、退市、暂停上市）股票，去重排序并在进程内缓存；需要 `TUSHARE_TOKEN`，不扫描 CoreData，不按请求日期、lookback 或 `close` 筛代码。显式代码列表原样保留且不调用 Tushare，导入模块不联网。
- 全市场 `StockPool.ALL` 注入恒真成员列；指数池在同一固定证券域上注入每日成员判断，只执行一次查询，没有本地 `codes_query` 预筛选。远端 Arena 自身的 `codes_query` 契约不因此改变，不能把两者混用。
- 支持 `ALL/SSE50/CSI300/CSI500/CSI1000`；具体字段由当前实现决定，不手写指数权重替代Universe的公共契约。
- `Universe.member` 是类属性字符串，不是方法，也不是Pydantic输入字段。
- `universe.evaluate(start, end)` 返回交易日×固定代码域的 BOOL 面板（列为 `.XSHG/.XSHE`），非成员和缺失成员为 False；某天或整个区间无成分也保留全 False，不缩列，不返回某一天固定的股票列表。日期轴仍取请求区间内真实 `close` 交易日，不能将代码域不依赖 `close` 误解为无日期轴约束。
- `universe.dataset_query(...)` 只构造已注入成员节点、`codes=[]` 的完整查询，不连接数据库。它可直接交给 `scheme.data.query(request)` 执行；`Universe.query(...)` 正是单次执行该对象，空 `codes` 使用同一份进程级证券列表，不需要补候选代码。

## 会话与结果

QueryResult 的上下文管理器负责关闭会话。`.data` 将实际数据取回Python；`.data_ref` 是同一个session中的表名，不是持久表，也不是跨调用ID。读取完整数据可能占用较多内存；检查schema或少量结果时应在服务器端限列限行并先限制查询本身。

不把全市场多年数据取回本地后才使用 `.head()` 当作资源限制。要构造可比较的截面，限域必须符合业务参与域；不能为了少取数据破坏CS统计含义。

## 数据解释

财务公告事件、指数权重和行业可能按Runtime规则填充；close通常不随意前向填充。先核实query时轴和已安装实现，保留数据质量标记，不凭NULL/0反推供应商缺失原因。运行结果可复现当前库，不代表具有不可修订的point-in-time归档。

## 验收提示词

- “只读取2024-06-03至06-07沪深300当日成员的收盘价。”应选Universe闭开区间和显式成员filter。
- “查询沪深300逐日成员，保留非成员日用于之后退出。”应在进程固定证券域上保留成员列、不默认 filter，并解释固定证券域与当日成分的区别。
- “这次代码列表为空，可以照常传吗？”应明确全市场风险，而不是默默查询。
