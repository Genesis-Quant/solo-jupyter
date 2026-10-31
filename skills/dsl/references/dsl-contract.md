# Runtime JSON DSL 规则

参考 Arena `backend/docs/overview/dsl.md`、`query/request.md`，参考提交 `802cb0fd4ef45a0fc7db6168b96497de68eda537`。下述是可用于 Solo 的组合规则，不是 Arena 应用请求封装。精确算符以项目锁定 Runtime 的本地 catalog 为准。

## 节点与查询

```json
{
  "start_date": "2024-06-03",
  "end_date": "2024-06-07",
  "lookback": "P90D",
  "codes": ["600000.SH"],
  "factors": [],
  "derivatives": {
    "momentum": {
      "type": "TS",
      "op": "unary.pct_change",
      "fields": {"col": "close_hfq"},
      "params": {"periods": 20}
    }
  },
  "filters": []
}
```

这是 Runtime JSON 示例；使用前仍执行 `describe unary.pct_change` 和 `validate`。不要给 Solo Runtime 增加 `dsl_source`。实际类型默认值与可选字段以 `schema` 为准。

节点的 `fields` 保存操作数，`params` 保存窗口、方法等非列参数，`type` 与算符定义一致。无参数可用 `{}`。`on` 只允许 TS/CS，接受定义允许的 BOOL 引用、BOOL 常量或嵌套 BOOL 节点。

`factors` 只列需要直接输出的基础列；依赖读取会自动补齐其它基础字段。顶层命名 `derivatives` 都会成为完整结果列。`factors` 与 `derivatives` 至少一项非空；保留名 `time`、`code` 不可当自定义输出列，名称不能空、重复或冲突。命名节点按依赖拓扑执行，不依赖 JSON 键顺序，循环引用拒绝。

## 掩码、筛选和 NULL

- `TS` 使用代码内按时间排序的序列；`CS` 使用当日完整截面；不要用缩小后的代码域冒充原截面。
- `on` 为 false/NULL 时该节点结果为 NULL，不删行，也不会替代最终 `filters`。
- `filters` 必须是顶层命名 BOOL 节点，多个项做 AND。OR 逻辑应由一个 BOOL 节点明确表达。
- 比较算符产生 BOOL；价格、权重和状态的数值列不能直接冒充条件。
- 零尺度/零分母的归一化可能得到整段 DOUBLE NULL；不能未经业务说明填零或回填原值。
- 同一嵌套表达式多次出现不保证自动消除重复计算。昂贵的共享 TS/CS 结果可命名复用。

仅给一个截面节点使用的条件可以嵌套进 `on`。若同时要删除不满足条件的输出，则创建命名 BOOL 节点，并显式列入 `filters`。不要为了看起来“分步骤”输出全部临时列。

## 截面 filter 与时序 filter

`on` 使用的 BOOL 按作用对象分两类，各自只放进对应算子的 `on`：

| 类型 | 决定什么 | 放在哪里 | 例子 |
|---|---|---|---|
| 截面 filter | 当天哪些股票组成截面 | CS 的 `on`；需删行时再列入 `filters` | `valid` |
| 时序 filter | 每只股票哪些观测组成时间序列 | TS 的 `on` | `is_week_end` |

CS 的 `on` 只让 true 行进入当日截面；TS 的 `on` 只让 true 行进入该股票的序列，`window`、`periods` 都按 on=true 的观测计数。两者的 false/NULL 行结果都是 NULL。

### 截面 filter：valid

```json
"valid": {
  "type": "DIRECT",
  "op": "multiary.and",
  "fields": {"cols": [
    "stock_pool_member",
    {"type": "DIRECT", "op": "binary.eq", "fields": {"left": "is_st", "right": 0}},
    {"type": "DIRECT", "op": "binary.eq", "fields": {"left": {"type": "TS", "op": "unary.shift", "fields": {"col": "is_st"}, "params": {"periods": -1}}, "right": 0}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": "open"}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": "high"}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": "low"}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": "close"}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": {"type": "TS", "op": "unary.shift", "fields": {"col": "open"}, "params": {"periods": -1}}}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": {"type": "TS", "op": "unary.shift", "fields": {"col": "high"}, "params": {"periods": -1}}}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": {"type": "TS", "op": "unary.shift", "fields": {"col": "low"}, "params": {"periods": -1}}}},
    {"type": "DIRECT", "op": "unary.not_null", "fields": {"col": {"type": "TS", "op": "unary.shift", "fields": {"col": "close"}, "params": {"periods": -1}}}},
    {"type": "DIRECT", "op": "unary.isin", "fields": {"col": {"type": "TS", "op": "unary.shift", "fields": {"col": "limit_status"}, "params": {"periods": -1}}}, "params": {"values": [0, 1, 4]}}
  ]}
}
```

`stock_pool_member` 先由 `MEMBERS[universe.pool]` 定义。`limit_status` 来自 Tushare `daily_basic` 的收盘涨跌状态：0 平盘、1 上涨（不含涨停）、2 涨停（不含一字）、3 一字涨停、4 下跌（不含跌停）、5 跌停（不含一字）、6 一字跌停。模板检查**下一交易日**的该状态，只保留 0/1/4；状态缺失（停牌行也缺失）时 `isin` 为 false。`valid(t)` 只要求当日为 member，同时要求 t、t+1 的 `is_st=0` 且 OHLC 完整；不检查 t+1 是否为 member，不剔除 t 日涨跌停。

`shift(-1)` 不加 on，按完整交易日轴取下一日而不跳过停牌。查询使用 `lookforward: "P20D"`，计算多读至 end+20 个自然日，但数据和日历只输出原区间；无下一日或缺字段时不通过过滤。ST 为稀疏字段，沿用查询层缺失填 0 的既有含义，不能据此证明供应商记录完整。

该 valid 及其限定的截面排名含未来信息，只用于事后样本研究，不能作当日可交易信号；即使 `compute(d,d)` 与整段一致也不能证明无未来依赖。

`Universe.codes` 是区间成员并集，查询中同一天也有非成员、未上市和停牌行。CS 算子不加 `on: "valid"` 时，这些股票会改变排名、均值和标准差；`compute(d,d)` 的候选又与整段不同，导致逐日与整段结果不一致。因子模板对 CS 排名使用 `on: "valid"`，并用 `filters: ["valid"]` 删除其余输出行。

### 时序 filter：is_week_end

因子模板的默认因子：按周 resample 计算近 5 周收益波动率，日频对齐后在有效股票中排名。下面用 `is_week_end` 演示周五时序 filter，模板中同一字段命名为 `is_friday`；仅名称不同，节点含义相同。模板 `factor.py` 逐节点、逐字段注释同一写法：

```json
"is_week_end": {
  "type": "DIRECT",
  "op": "binary.eq",
  "fields": {"left": {"type": "DIRECT", "op": "unary.weekday", "fields": {"col": "time"}}, "right": 4}
},
"volatility": {
  "type": "CS",
  "op": "unary.rank_pct",
  "fields": {"col": {
    "type": "TS",
    "op": "unary.ffill",
    "fields": {"col": {
      "type": "TS",
      "op": "unary.rolling_std",
      "fields": {"col": {
        "type": "TS",
        "op": "unary.log_return",
        "fields": {"col": {"type": "DIRECT", "op": "binary.mul", "fields": {"left": "close", "right": "adj_factor"}}},
        "params": {"periods": 1},
        "on": "is_week_end"
      }},
      "params": {"window": 5},
      "on": "is_week_end"
    }},
    "params": {"limit": 8}
  }},
  "params": {"ascending": true, "ties_method": "average"},
  "on": "valid"
}
```

由内向外：

1. `binary.mul`（DIRECT）：后复权收盘价 `close × adj_factor`，逐行计算。
2. `unary.log_return`（TS，`on: is_week_end`）：只在周五子序列上取相邻两期，即相邻两个周五之间的周对数收益。`unary.weekday` 以周一为 0，`time` 可直接作为日期输入。
3. `unary.rolling_std`（TS，`on: is_week_end`）：`window=5` 按周五观测计数，即最近 5 条周收益的样本标准差。没有时序 filter 时，同样的 `window=5` 只是最近 5 个交易日。
4. `unary.ffill`（TS，无 on）：前两步只在周五有值。因子报告按日计算分组收益，若只有周五有值，多数收益集合会成为 NULL，所以把周五的值沿用到之后的交易日。2010 年以来任一交易日距最近交易周五最多 8 个交易日，`limit=8` 覆盖节假日周；更长的空缺（如长期停牌）保持 NULL。
5. `unary.rank_pct`（CS，`on: valid`）：每天只在 valid 股票中排名，取值 (0, 1]，并列取平均名次。

- 周五只是近似每周最后一个交易日：周五休市的节假日周没有采样点（如 2026-04-27 当周最后交易日为周四），下一条周收益跨两周。严格的“每周最后一个交易日”需要交易日历；不能用负 `shift` 查看下一交易日，那是未来观测，查询末日为 NULL，`compute(d,d)` 也会与整段结果不一致。
- 时序 filter 只用日历条件 `is_week_end`，周序列就是日历中的交易周五，窗口最多回看约 10 周，110 天 lookback 足够，`compute(d,d)` 与整段结果一致。周五停牌行仍在周序列中且价格为 NULL，会使相邻周收益及随后 5 周的波动率为 NULL；日频沿用期间最多继续使用上一个有效周五的值 8 个交易日。
- 不要把“价格非空”并入时序 filter 来跳过停牌周：周序列会变成该股票有成交的周五，长期停牌后窗口回溯到任意早的周五，lookback 无法覆盖，`compute(d,d)` 与整段结果可能不一致。

### 不要混用

- 截面 filter 放进 TS 的 `on`，会把涨跌停日、非成员日和停牌日从历史序列中抽掉：涨停日收益直接消失，窗口跨越不连续的日期。周收益、20 日动量等 TS 节点都不能用 `on: "valid"`。
- 时序 filter 放进 CS 的 `on`，只会让非采样日截面为空，不能代替股票池和交易状态。
- 两类条件都需要时，分别用在各自算子上，不要合成一个 `on` 同时给 TS 和 CS。

## 时间和股票池

`start_date..end_date` 为输出闭区间。`lookback` 只扩展内部历史；Solo Scheme 新增的 `lookforward` 只扩展内部未来数据，默认 0，不属于原 Runtime/远端请求契约。两者都是非负日历时长（`timedelta` 或 TimeDelta 字符串）；日频计算边界归一到日期，最终 `.data` 和 `.calendar` 都截回原区间。`P90D` 是90个自然日，不保证90条交易记录；停牌、上市时间和缺行情需另查。扩展前视不改变因子分析收益标签的 end 截止，标签查询仍默认前视 0。

Universe.codes/evaluate 使用 [start,end)，普通 Query 使用闭区间。codes 在成员过滤后取历史证券并集；后续字段查询显式传候选，并通过 MEMBERS[universe.pool] 定义每日成员 BOOL。filters 删除输出行，on 只限制计算参与，二者不能混用。evaluate 保留历史成员列，非成员日 False，空成员区间保留日历和零列。普通 codes=[] 仍为 Tushare L/D/P 股票列表的全市场输入域，不是空候选。

`codes=[]` 请求全市场计算域，不能作为空池“跳过查询”的方式。证券是否有数据和市场后缀支持应查当前源数据，不把 `.SH/.SZ` 与 `.XSHG/.XSHE` 混写。

## 基础字段填充与历史时点

Arena Runtime 的当前规则需要结合项目安装版本核实：

- `weight_*` 在某日有权重观察时，为该日其它查询代码的缺失权重填0，再按代码前向填充。
- 财务字段按已写入查询时间轴的公告事件前向填充，不以报告期结束日直接代表当时可知。
- 行业字段使用查询开始前的历史事件作为种子并前向填充；0表示未知。
- `is_st` 的缺失可能被固定填0，其它字段不会一律隐式前向填充。

查询轴连接后，源记录不存在与源值本身为NULL都可表现为NULL；填充后更不能反推来源。字段白名单只保证字段可被请求，不证明数据完整、无修订或严格 point-in-time。

指数权重、财务与行业数据可能被供应商回溯修订。可重复计算当前数据库快照，不等于具有不可修改的历史时点证据；审计级复现需要数据版本、抓取时间和原始响应归档。

## 常见排查

1. 未知字段：读取 Worker 的 `factors` 目录及 `configuration`，或核对指定窄表的实际 factor。Worker 目录不限制自定义表的字段；目录外字段仅提示，不拒绝查询。对于指数权重，先确认显式 `--index-codes` 或已导出的 `INDEX_CODES` 与项目有效配置一致；`query-defaults` 不是项目 `.env` 的证明。未显式配置时采用 Query 默认目录并提示；其它指数通过非敏感参数选择，不读取 `.env`。静态字段名称错误与实际数据为空分别处理。
2. 未知算符/参数：使用 `operators` 搜索后 `describe`，不猜参数名。
3. 缺少成员节点：普通 Query 显式定义 derivatives[universe.member] = MEMBERS[universe.pool]，再用于 filters 或 on；候选代码本身不会定义任何 DSL 列。
4. 筛选后排名变化：检查完整代码域、on、lookback、上游列和数据版本，filters不提前参与排名。
5. 末尾未来收益为空：检查评价窗口和截止日，不读取结束日后的数据去补齐。

## 验收提示词

- “在当前项目写20日复权动量，再按当日沪深300成员做百分位排名；保留非成员行。”应读取算符定义，用 `on=Universe.member`，不加成员filter。
- “把20日滚动窗口参数写成 period=20 可以吗？”应使用 `describe` 精确核实，不凭常识答复。
- “把 valid 也放进20日动量的 TS on，这样更干净。”应拒绝：valid 是截面 filter，会把涨跌停日和非成员日从时间序列中抽掉；动量使用完整交易日序列，排名用 `on: "valid"`。
- “计算近5周股价波动率。”应以 `is_week_end` 作为周收益和滚动标准差两个 TS 节点的 `on`，用 `unary.ffill(limit=8)` 日频对齐，再用 `on: "valid"` 排名；说明周五近似和节假日周的边界，不用负 shift 判断周末。
- “这一天没有候选股票，传 codes=[] 跳过查询吧。”应指出会扩大到全市场，保持空池语义而不执行该查询。
