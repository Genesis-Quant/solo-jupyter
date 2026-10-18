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

## 时间和股票池

Runtime `start_date..end_date` 为闭区间，`lookback` 只扩展内部历史，最终输出仍截回请求区间。`P90D` 是90个日历日，不保证90条交易记录，停牌、上市时间和缺行情需另查。

Solo `Universe.query(start, end)` 采用结束日不含，并在内部转成 Runtime `end_date=end-1日`。指数池先查区间候选并集，第二阶段注入 `Universe.member`；是否删非成员行由调用者决定。不要用第二阶段过滤后的少量证券重新定义原 CS 截面。

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

1. 未知字段：读取 `factors` 全白名单及 `configuration`。对于指数权重，先确认显式 `--index-codes` 或已导出的 `INDEX_CODES` 与项目有效配置一致；`runtime-defaults` 不是项目 `.env` 的证明。未显式配置时采用 Runtime 默认目录并提示，目录中已有指数权重字段正常校验；其它指数通过非敏感参数选择，不读取 `.env`。静态字段名称错误与实际数据为空分别处理。
2. 未知算符/参数：使用 `operators` 搜索后 `describe`，不猜参数名。
3. 缺少成员节点：使用 `Universe.dataset_query` 的注入后对象，或对普通 Query 显式定义自己的 BOOL 条件；不要把托管字符串当到处可用。
4. 筛选后排名变化：检查完整代码域、on、lookback、上游列和数据版本，filters不提前参与排名。
5. 末尾未来收益为空：检查评价窗口和截止日，不读取结束日后的数据去补齐。

## 验收提示词

- “在当前项目写20日复权动量，再按当日沪深300成员做百分位排名；保留非成员行。”应读取算符定义，用 `on=Universe.member`，不加成员filter。
- “把20日滚动窗口参数写成 period=20 可以吗？”应使用 `describe` 精确核实，不凭常识答复。
- “这一天没有候选股票，传 codes=[] 跳过查询吧。”应指出会扩大到全市场，保持空池语义而不执行该查询。
