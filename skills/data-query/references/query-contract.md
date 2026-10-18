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

- 全市场 `StockPool.ALL` 没有第一阶段，第二阶段注入恒真成员列。
- 指数池第一阶段查询区间内候选并集，第二阶段只读取候选代码，再按每日成员判断。
- 支持 `ALL/SSE50/CSI300/CSI500/CSI1000`；具体字段由当前实现决定，不手写指数权重替代Universe的公共契约。
- `Universe.member` 是类属性字符串，不是方法，也不是Pydantic输入字段。
- `universe.evaluate(start, end)` 返回日期×候选代码的BOOL面板，非成员False，不返回某一天固定的股票列表。
- `universe.dataset_query(...)` 用于构造/检查第二阶段规范，尚未填入第一阶段候选代码，**不要直接执行它来跳过两阶段流程**。

## 会话与结果

QueryResult 的上下文管理器负责关闭会话。`.data` 将实际数据取回Python；`.data_ref` 是同一个session中的表名，不是持久表，也不是跨调用ID。读取完整数据可能占用较多内存；检查schema或少量结果时应在服务器端限列限行并先限制查询本身。

不把全市场多年数据取回本地后才使用 `.head()` 当作资源限制。要构造可比较的截面，限域必须符合业务参与域；不能为了少取数据破坏CS统计含义。

## 数据解释

财务公告事件、指数权重和行业可能按Runtime规则填充；close通常不随意前向填充。先核实query时轴和已安装实现，保留数据质量标记，不凭NULL/0反推供应商缺失原因。运行结果可复现当前库，不代表具有不可修订的point-in-time归档。

## 验收提示词

- “只读取2024-06-03至06-07沪深300当日成员的收盘价。”应选Universe闭开区间和显式成员filter。
- “查询沪深300期间所有候选股票，保留非成员日用于之后退出。”应保留成员列、不默认filter，并解释候选与当日成分区别。
- “这次代码列表为空，可以照常传吗？”应明确全市场风险，而不是默默查询。
