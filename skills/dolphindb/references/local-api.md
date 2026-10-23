# Solo 本地 DolphinDB API

## 解释器与离线核对

接口基线为 Scheme 1.12、Backtest 0.2，研究项目使用 Python 3.12。先读项目 manifest、锁文件和源码，不使用全局 Server Python 代替项目解释器，不回退旧接口或旧环境变量。

```python
import inspect
import sys
from importlib.metadata import version

from runtime.apps.query import FactorQuery
from scheme import Universe
from scheme.data import query

print(sys.executable, sys.version.split()[0])
for name in ("scheme", "backtest", "dolphindb", "arena-runtime"):
    print(name, version(name))
print(query.__module__, inspect.getfile(query))
print(inspect.signature(query))
print(inspect.signature(Universe.codes))
print(inspect.signature(Universe.evaluate))
print(FactorQuery.model_json_schema())
```

这些检查不连接数据库。缺包或接口不符就报告，不擅自安装、升级，也不输出 settings 或秘密。

## 查询与股票池

| 接口 | 日期与作用 | 结果 |
| --- | --- | --- |
| `query(request, *, session=None, settings=None)` | `start_date/end_date` 闭区间；字段和 DSL 查询 | 持有服务端结果的 QueryResult |
| `Universe.codes(start,end, *, session=None, settings=None)` | `[start,end)`；过滤每日成员后取代码并集 | `.SH/.SZ` 列表 |
| `Universe.evaluate(start,end, *, session=None, settings=None)` | `[start,end)`；每日成员 | 真实交易日 × 区间历史成员 BOOL 面板 |

Universe 只提供候选证券与成员面板；普通因子、行情和派生字段统一调用 `query`。指数候选并集不能替代逐日成员判断：后续 Query 以 `MEMBERS[universe.pool]` 定义 `universe.member`，按业务使用 filters 或 on。完整例子见相邻 `data-query/references/query-contract.md`。

普通 `codes=[]` 表示全市场输入域，通过 Arena Runtime `get_codes()` 缓存 Tushare `stock_basic` 的 L/D/P 股票，需要 `TUSHARE_TOKEN`。不能将空候选直接交给它表示无数据。显式代码不会调用 Tushare。日历取真实 close 日期，候选不是按今日成分或当日有价证券缩成的列表。

`result.data_ref`、`result.calendar_ref` 是所属会话的引用；`.data`、`.calendar` 访问时才下载。同一会话只保留一个未关闭查询结果。借用 session 的结果退出时仅回收自己的引用；自有 session 随结果关闭。中性化在调用方 session 的局部 DOS 作用域内处理，不覆盖外层数据和日历。

## 一套连接配置

查询、分析和回测只读取：

```dotenv
DOLPHINDB_ENDPOINTS=node1:8848,node2:8848
DOLPHINDB_USERNAME=your-account
DOLPHINDB_PASSWORD=your-password
```

多个端点以逗号分隔，IPv6 为 `[地址]:端口`。仅在连接建立时按顺序尝试；成功后不切节点、不重放查询或委托。端点须提供同一套数据、模块和回测插件。省略显式 settings 时读取上述环境，不映射旧名称；不打印凭据。

## 上传、下载与元信息

业务数据只能通过 `upload`、`download` 交换。`download` 表达式在 DOS 侧求值一次，在局部函数中打印类型、行列或项数，再由一次调用返回数据；无会话级下载临时变量。只计算而不下载的脚本以 NULL 结束，函数局部对象不显式置 NULL。

以下函数只有用户要求连接检查时才调用：

```python
from scheme.config import DolphinSettings
from scheme.data.dolphindb.database import create_session, download, upload


def inspect_server():
    settings = DolphinSettings.from_env()
    with create_session(settings) as session:
        server_version = download(session, "version()", "服务端版本")
        plugins = download(session, "getLoadedPlugins()", "已加载插件")
        functions = download(
            session,
            'select name, syntax from defs() where name in ["schema", "mavg", "version"]',
            "指定函数定义",
        )
        upload(session, {"metaDb": settings.database, "metaTable": settings.table}, "源表位置")
        columns = download(session, "schema(loadTable(metaDb, metaTable)).colDefs", "源表字段")
    return server_version, plugins, functions, columns
```

缺少函数定义时先核实服务端版本及命名空间；Backtest 插件导出不等于普通内置函数。原始源表不必包含查询输出的宽表列，不能据此伪造字段。

- TABLE 用 `schema(ref).colDefs`，DICTIONARY 用 keys，数组列检查维度和方向。
- 空表、空字典、NULL、缺行和零值分别解释。持仓均价不是当前估值价。
- SQL 标识符来自已经核对的 schema 或程序定义，参数通过 upload 传递。
- 读取前先限定实际分区、日期、代码和列；`.data.head()` 在全量下载后才执行，不是资源限制。
- 因子分析 Result 同样持有服务端结果：访问某属性才下载该表，`preview(name,rows)` 在服务器限行，完成后显式 close 或使用 with。保存与展示是显式导出行为，不在分析函数返回前偷偷下载所有表。
- 凭据、完整环境、用户数据不进入日志；不加载插件、不管理共享对象，也不清理他人的连接或引擎。
