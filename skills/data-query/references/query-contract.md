# Query 与 Universe

接口按职责导入：`scheme.data.query` 为独立 Query 包；股票池定义在 `scheme.data.universe`，DolphinDB 会话及数据交换定义在 `scheme.data.dolphindb.database.session`。请求节点以项目实际安装的 Query schema 为准；Solo 的 `solo-runtime` 只负责启动冻结任务，不提供数据查询。

## 静态代码查询

```python
from scheme.data import create_session, query

request = {
    "start_date": "2024-06-03",
    "end_date": "2024-06-07",
    "codes": ["600000.SH", "000001.SZ"],
    "factors": ["close", "adj_factor"],
}
with create_session() as session:
    query.initialize_query(session)
    with query.execute_query(request, session=session) as result:
        prices = result.data
        trading_days = result.calendar
```

普通请求读取闭区间，输出以 `time/code` 为轴。`factors` 只列需要输出的基础字段；派生计算所需字段会自动读取，所有顶层命名派生列都会输出，filters 只删行。先用 DSL helper 离线校验，只有用户授权实际查询时才运行。

## lookback 与 lookforward：计算域不等于输出域

`query.FactorQuery` 是独立Query包的查询模型，支持 `lookforward`，默认 `timedelta(0)`；`query.execute_query` 接受此模型或请求字典。两个时长都接受非负 `timedelta` / TimeDelta 字符串，拒绝负数、bool、数值和未知字段，仍沿用 Runtime 的算符、依赖和 BOOL 校验。

```python
with create_session() as session:
    query.initialize_query(session)
    with query.execute_query({
        "start_date": "2026-04-30",
        "end_date": "2026-04-30",
        "lookback": "P110D",
        "lookforward": "P20D",
        "codes": ["600000.SH"],
        "derivatives": {
            "next_close": {
                "type": "TS", "op": "unary.shift",
                "fields": {"col": "close"}, "params": {"periods": -1},
            }
        },
    }, session=session) as result:
        next_close = result.data
        trading_days = result.calendar
```

- `lookback` 扩展开始日前的计算域；`lookforward` 扩展结束日后的计算域。日频边界归一到日期，内部从 `(start-lookback).normalize()` 到 `(end+lookforward).normalize()`，两端包含；返回数据和日历仍仅 `[start_date,end_date]`。不足一天的前视不会凭空多读一个自然日。
- 先在完整扩展交易日轴上计算派生列、filters，再裁回原区间；代码列表不自动扩大。下一交易日依据真实 close 日历，`shift(-1)` 不加 on，不跳过停牌或 NULL。上例只返回 4 月 30 日，下一日价格可以来自劳动节之后；20 天是自然日窗口，不保证有 20 个交易日。
- 窗口内缺少下一交易日、停牌或字段缺失时相应值为 NULL，不向更晚的有效交易日跳转。启用前视不会填充价格。
- 前视取到的值和基于它的筛选都带未来信息。模板 valid 明确是事后研究筛选；即使自身波动率公式没有未来值，`CS on valid` 的排名也受未来样本域影响，不能视作当日可用信号。
- `lookforward=0` 维持旧行为。因子分析的收益标签查询不启用前视，仍不读取报告 end 以后的标签端点；不要为了补齐标签擅自启用它。
- `lookforward` 是 Solo Scheme 扩展，不是 Arena Runtime 或远端双源码请求已经支持的字段。离线检查脚本在项目 Scheme 导出扩展模型时使用它；旧项目按安装版本拒绝该字段，不自动升级。

## 历史候选与每日成员

```python
from datetime import date, timedelta

from scheme.data import MEMBERS, StockPool, Universe, create_session, query

universe = Universe(pool=StockPool.CSI300)
start, end = date(2024, 6, 3), date(2024, 6, 8)
with create_session() as session:
    query.initialize_query(session)
    codes = universe.codes(start, end, session=session)
    if not codes:
        raise ValueError("所选区间没有股票池成员")
    with query.execute_query({
        "start_date": start.isoformat(),
        "end_date": (end - timedelta(days=1)).isoformat(),
        "lookback": "P90D",
        "codes": codes,
        "factors": ["close", "adj_factor"],
        "derivatives": {universe.member: MEMBERS[universe.pool]},
        "filters": [universe.member],
    }, session=session) as result:
        member_prices = result.data
```

- `codes(start,end)` 已应用每日成员过滤，再对结果取代码并集；不是把全市场输入代码直接返回。只传回候选列表，后续因子查询使用该列表。
- 区间并集不等于某一天的指数成分。普通 Query 必须再次定义成员 BOOL 并选择 filters 或 on；不自动继承上一次 Universe 条件。
- `MEMBERS` 提供 `ALL/SSE50/CSI300/CSI500/CSI1000` 的成员定义；`universe.member` 是列名字符串。全市场定义为恒真。
- `evaluate(start,end)` 直接返回每日成员 BOOL 面板，列是区间历史成员，使用 `.XSHG/.XSHE`。非成员日为 False，空成员日仍保留真实交易日；空区间成员结果为有日期零列面板。
- 普通 `codes=[]` 有意请求全市场，复用 Tushare `stock_basic` 的 L/D/P 股票缓存；需要 `TUSHARE_TOKEN`。不能将空候选交给它来表示不查询。
- Universe 的日期是 `[start,end)`，普通 Query 和因子分析的结束日包含当天，不重复转换。

## 会话、数据与日历

`query.execute_query(request, *, session, settings=...)`强制使用已有会话并返回`QueryResult`，不创建或关闭连接。Scheme研究代码通过`scheme.data.dolphindb`管理会话；独立使用Query时也可传入DolphinDB SDK会话，不要求安装Scheme。独立`query workers`命令使用自己的`WorkerSettings`在CLI边界建连，再把连接注入Worker；这不是查询API的隐式会话工厂。`from scheme.data import query` 导入实际Query包，不是函数；执行入口仅为 `execute_query`。数据与日历有按需下载属性 `.data`、`.calendar`；还可读取 `.source_data`、`.computed_data`、`.filtered_data`，对应引用为 `.source_ref`、`.computed_ref`、`.filtered_ref`、`.data_ref`、`.calendar_ref`。

```python
from scheme.data import create_session, download, query

with create_session() as session:
    query.initialize_query(session)
    with query.execute_query(request, session=session) as result:
        preview = download(
            session,
            f"select top 5 time, code from {result.data_ref}",
            "查询键预览",
        )
```

`.data` 会下载全部最终结果，`.head()` 在其后执行不能限制网络或内存。查询本身先限制日期、代码和列，预览再使用有界服务器投影。

结果支持直接`with`或显式`.close()`，不使用`closing`。Scheme的`create_session()`返回支持`with`和`.close()`的会话，包含可选`max_time`总寿命限制；它不加载Query脚本。调用方手动调用`query.initialize_query(session)`后再查询；Query、Universe和预处理不自动初始化、不检查指纹或记忆会话，显式重复初始化会再次执行DOS。Query禁止省略或传入空session，正常、失败及结果退出都不关闭调用方连接，只回收本次结果引用。会话由外层Scheme上下文回收。同一会话的存活查询结果不能重叠。需要在该结果内执行因子预处理时，传同一 session；预处理使用局部 DOS 对象，不覆盖外层结果及日历。结果关闭后不能继续下载或使用引用。

`upload` 只上传必要参数或本地数据；`download` 在 DOS 侧计算表达式一次，先打印类型、行列或项数，再在一次调用中返回数据，局部对象随作用域回收。禁止直接以 `session.run` 下载业务结果；只计算不传回的数据脚本以 NULL 结束。

## 连接与数据解释

只接受一套 `DOLPHINDB_ENDPOINTS`、`DOLPHINDB_USERNAME`、`DOLPHINDB_PASSWORD`。多个 `host:port` 以逗号分隔，IPv6 使用 `[地址]:端口`；新连接按顺序尝试，不重连、重放查询或委托，不回填旧环境变量。

查询日历来自区间内真实 close 记录。Query 0.1.1 的行业、财务只对计算区间内已读取事件前向填充；指数权重只在区间内快照日给未出现证券补零并前向填充，包括区间内非交易日调整。全部取消计算起点前的 seed 和历史快照补值，头部缺失保留NULL；需要更多历史时显式扩大 lookback。固定证券字符串引用（如 close@000852.SH、pct_chg@000852.SH）使用相同规则并按日期广播，不增加股票行，价格不能擅自补值。旧冻结环境仍按其安装版本解释。可重算当前库不等于有不可修订的历史时点归档。

## 验收提示

- “读取 6 月 3 日至 7 日沪深 300 成员收盘价”：先取历史候选，后续 Query 明确定义并过滤每日成员。
- “保留指数退出后的估值和交易数据”：候选并集保留，Query 不删除需要继续处理的非成员行；成员条件与持仓退出分别处理。
- “没有候选，传空代码就行”：拒绝扩大为全市场，跳过或明确返回空结果。
