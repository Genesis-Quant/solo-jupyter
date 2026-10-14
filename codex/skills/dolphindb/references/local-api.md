# 本地 API：先离线核对，再按需连接

## 解释器与自省

版本基线为 Scheme 1.5.0、Backtest 0.1.1、DolphinDB SDK 3.0.4.2；使用当前项目 `.venv` 或实际 kernel 的 Python 3.12，核对锁文件、安装版本和路径，不固定机器路径。

```python
import inspect
import sys
from importlib.metadata import version
from scheme import Universe
from scheme.data import query
from runtime.apps.query import FactorQuery, execute_query

print(sys.executable, sys.version.split()[0])
for name in ("scheme", "backtest", "dolphindb", "arena-runtime"):
    print(name, version(name))
print(query is execute_query)
print(inspect.signature(query))
print(inspect.signature(Universe.query))
print(FactorQuery.model_json_schema())
```

这些操作不连接数据库。缺包就报告环境不完整，不自动安装或更新。只输出必要的 schema/版本，不输出模型实例里的配置或秘密。

## 三个不同的接口

| 接口 | 输入与日期 | 结果 |
| --- | --- | --- |
| `scheme.data.query(request, *, session=None)` | Runtime `FactorQuery` 或字典；`start_date/end_date` 闭区间 | `QueryResult`，惰性下载并持有 session |
| `Universe.query(start, end, *, lookback, factors, derivatives, filters)` | Python `date` 的 `[start, end)` | 同一 session 内完成候选域与数据查询 |
| `DolphinDB session.run(script)` | 原样 DolphinScript | 脚本最后一个表达式的值；不是工作流结果 |

`Universe` 从 `scheme` 或 `scheme.data.dolphindb.universe` 导入，不能假定 `scheme.data` 导出它。

### 只离线构造，不执行未填候选域的请求

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
assert request.codes == []  # 尚未注入第一阶段候选代码。
assert request.filters == []
assert Universe.member in request.derivatives
```

此处 `end_date` 在版本基线中是字符串。`dataset_query` 只构造第二阶段请求，指数池尚未注入候选 codes；不要直接执行 `query(request)`，因为 Runtime 的 `codes=[]` 表示全市场，不是指数候选池。

### 实际指数池查询走 Universe.query

以下函数只有被显式调用时才连接服务；先确认用户授权、区间、股票池和返回规模，再调用。两个阶段由 `Universe.query` 在同一 session 中执行。

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

成员节点不能重定义；全市场没有第一阶段，成员为恒真。指数池第一阶段取期间代码并集，第二阶段输出逐日成员，不自动删行。需要成员门控时显式选择参与域或 `filters`；详见 DSL/数据查询技能。

`.data` 会下载完整最终结果，`.head()` 发生在下载之后；不要用大范围请求做预览。需要少量列/行时，在结果块内用 `result.session` 对 `result.data_ref` 做有界投影。`source_ref/computed_ref/filtered_ref/data_ref` 是 session 引用，退出后不可继续查询。即使传入已有 session，也核对结果对象的所有权与关闭规则，避免多个存活结果交叉覆盖引用。只有独立、明确限定 codes 的 Runtime 查询才直接使用 `scheme.data.query`，不要绕开 Universe 的候选域步骤。

## 已有环境凭据与关闭 session

当前 Scheme 使用 `DOLPHIN_HOST`、`DOLPHIN_PORT`、`DOLPHIN_RUNTIME_USERNAME`、`DOLPHIN_RUNTIME_PASSWORD`，以及可选的数据库/表配置。只复用现有环境，不打印这些值；缺失时只报告缺失变量名。`scheme.data.query` 的连接由安装的 Runtime 配置负责，不把下面的 settings 参数硬塞给它。

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
