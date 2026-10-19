# 本地 API：先离线核对，再按需连接

## 解释器与自省

版本基线为 Scheme 1.5.0、Backtest 0.1.1、DolphinDB SDK 3.0.4.2；使用当前项目 `.venv` 或实际 kernel 的 Python 3.12，核对锁文件、安装版本和路径，不固定机器路径。

```python
import inspect
import sys
from importlib.metadata import version
from scheme import Universe
from scheme.data import query
from runtime.apps.query import FactorQuery

print(sys.executable, sys.version.split()[0])
for name in ("scheme", "backtest", "dolphindb", "arena-runtime"):
    print(name, version(name))
print(query.__module__, inspect.getfile(query))
print(inspect.signature(query))
print(inspect.signature(Universe.query))
print(FactorQuery.model_json_schema())
```

这些操作不连接数据库。缺包就报告环境不完整，不自动安装或更新。只输出必要的 schema/版本，不输出模型实例里的配置或秘密。

## 三个不同的接口

| 接口 | 输入与日期 | 结果 |
| --- | --- | --- |
| `scheme.data.query(request, *, session=None)` | Runtime `FactorQuery` 或字典；`start_date/end_date` 闭区间 | `QueryResult`，惰性下载并持有 session |
| `Universe.query(start, end, *, lookback, factors, derivatives, filters)` | Python `date` 的 `[start, end)` | 单次执行注入成员节点、`codes=[]` 的 `dataset_query` |
| `DolphinDB session.run(script)` | 原样 DolphinScript | 脚本最后一个表达式的值；不是工作流结果 |

`Universe` 从 `scheme` 或 `scheme.data.dolphindb.universe` 导入，不能假定 `scheme.data` 导出它。

### 离线构造完整的单次查询请求

```python
from datetime import date
from scheme import StockPool, Universe

universe = Universe(pool=StockPool.CSI300)
# 仅检查参数：2026-01-05 <= 日期 <= 2026-01-09。
request = universe.dataset_query(
    date(2026, 1, 5), date(2026, 1, 10), factors=["close"],
)
assert isinstance(request.end_date, str)
assert request.end_date == "2026-01-09"
assert request.codes == []  # 执行时由本地 query 补齐进程固定证券域。
assert request.filters == []
assert Universe.member in request.derivatives
```

此处 `end_date` 在版本基线中是字符串。`dataset_query` 只构造已注入成员节点的完整请求，不连接数据库；它保留 `codes=[]`，可在确认运行范围后直接交给 `scheme.data.query(request)` 执行，不需要补入候选代码。`scheme.data.query` 是本地直接执行包装，不是 Runtime `execute_query` 的别名：空 `codes` 复用 Arena Runtime 的 `get_codes()`，首次通过 Tushare `stock_basic` 合并 `L/D/P` 股票并在进程内缓存，需要 `TUSHARE_TOKEN`；不扫描 CoreData，不按日期、lookback 或 `close` 缩小代码域。显式 `codes` 原样保留且不调用 Tushare，导入不联网。证券域不依赖 `close`，但每次查询的日期轴仍取区间内真实 `close` 交易日。

### 用 Universe.query 执行同一请求

以下函数只有被显式调用时才连接服务；先确认用户授权、区间、股票池和返回规模，再调用。`Universe.query` 单次执行 `dataset_query`，不进行本地 `codes_query` 预选。若要缩小最终返回规模，按业务显式加成员 filters；短日期区间本身不会缩小固定证券域。

```python
from datetime import date
from scheme import StockPool, Universe

def query_small_index_window():
    universe = Universe(pool=StockPool.CSI300)
    with universe.query(
        date(2026, 1, 5), date(2026, 1, 10), factors=["close"],
    ) as result:
        return result.data
```

成员节点不能重定义；全市场成员为恒真。指数池在同一固定证券域上逐日判断成员，成员只作为 BOOL 列输出，不自动删行。需要成员门控时显式选择参与域 `on` 或 `filters`。`evaluate(start,end)` 返回交易日×固定证券域的 BOOL 面板（`.XSHG/.XSHE` 列），非成员及缺失成员为 False，某天或整个区间无成分也保留全 False，不因缺行情缩列；详见 DSL/数据查询技能。

`.data` 会下载完整最终结果，`.head()` 发生在下载之后；不要用大范围请求做预览。需要少量列/行时，在结果块内用 `result.session` 对 `result.data_ref` 做有界投影。`source_ref/computed_ref/filtered_ref/data_ref` 是 session 引用，退出后不可继续查询。即使传入已有 session，也核对结果对象的所有权与关闭规则，避免多个存活结果交叉覆盖引用。`scheme.data.query` 可直接执行静态代码请求或 Universe 构造的完整请求；`codes=[]` 有意使用进程固定全市场域，不是空池或待补候选，不存在需要绕开的本地两阶段步骤。

## 已有环境凭据与关闭 session

当前 Scheme 查询使用 `SOLO_DOLPHIN_HOST/PORT/USERNAME/PASSWORD`，回测引擎使用 `BACKTEST_DOLPHIN_HOST/PORT/USERNAME/PASSWORD`；`codes=[]` 另需 `TUSHARE_TOKEN` 获取 `stock_basic` 股票列表。只复用现有环境，不打印凭据，缺失时只报告变量名，不回退旧变量或另一套连接。`scheme.data.query(request, settings=...)` 可显式指定数据连接，省略时读取 SOLO 配置。

以下示例会连接服务，只有用户任务确实需要时才运行；只取所需元数据，不下载整个 defs 列表。

```python
from scheme.config import DolphinSettings
from scheme.data.dolphindb.database.session import create_session

settings = DolphinSettings.from_env()
with create_session(settings) as session:
    server_version = session.run("version()")
    plugins = session.run("getLoadedPlugins()")
    definition_columns = session.run("schema(defs()).colDefs")
    if not {"name", "syntax"}.issubset(set(definition_columns["name"])):
        raise RuntimeError("defs schema 已变化，先核对返回字段")
    function_defs = session.run(
        'select top 5 name, syntax from defs() '
        'where name in ["schema", "mavg", "version"]'
    )
    session.upload({"metaDb": settings.database, "metaTable": settings.table})
    columns = session.run("schema(loadTable(metaDb, metaTable)).colDefs")
```

目标函数未出现在 `defs()` 结果时，核对部署版本的内置函数包含选项和命名空间规则，不把空结果当函数不存在。`Backtest::` 插件导出不等于普通内置函数。`columns` 是实际源表 schema，不保证与 Runtime 查询的 `time/code/close` 列同名。SDK、Server、Backtest、MatchingEngineSimulator 的版本分别记录；`getLoadedPlugins()` 未提供的版本另行按部署接口核实，不编造“当前版本”。

## 对象自省

- 表：`schema(tableRef).colDefs`；先确认引用合法且对象为表。
- 字典：先看 `keys()`，不套用表的 schema 或位置索引。
- 数组列：检查类型与维度，小样本确认档位/行方向后再访问。
- 空表、空字典、NULL 和缺行分别处理；持仓均价不是当前估值价。
- 用 `session.upload` 传递非秘密的日期、代码和配置值；SQL 标识符只来自已验证的 schema/程序引用，不拼接任意用户输入。

原始行情查询先限定实际日期/代码分区、列及结果行数；`top N` 也不能替代分区和数据域约束。不要在生产共享实例执行全库 scan、全量下载或无边界矩阵计算。
