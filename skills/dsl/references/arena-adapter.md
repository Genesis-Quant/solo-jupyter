# Arena MCP 适配：仅显式远端任务使用

本地 skills 不提供 MCP 服务器。只有当前工具列表确实包含 Arena 工具且用户要求操作 Arena 时才执行本节；否则使用 Solo 本地 JSON 和项目 Runtime，不虚构工具调用或认证配置。

来源：Arena `backend/docs/overview/dsl.md`、`query/request.md`、`factor/request.md`、`backtest/request.md`，参考提交 `802cb0fd4ef45a0fc7db6168b96497de68eda537`。远端服务可能更新，连接后先读取当前文档。

## 发现入口

- `read_arena_document(name="overview/dsl")`：组合规则。
- `list_dsl_operators` / `describe_dsl_operator`：基础字段与精确算符 schema。
- 资源 `arena://dsl/catalog`：完整 catalog（仅客户端实际支持 resource 读取时）。
- `compile_python_dsl`：编辑源码离线于数据库的编译验证；不创建项目或任务。

不要将没有接入的工具替换为未认证 HTTP 猜测接口。凭据不放进源码、技能文件或请求示例。

## 双源码属于 Arena Backend

Arena MCP 每个 Query 同时保存 `dsl_source.language`、`json_source`、`python_source`。活动语言生成执行快照，另一份可以是未完成草稿；不能相互覆盖。新编辑默认使用 Python，用户明确JSON或延续JSON记录时保留选择。

Python源码必须定义 `FACTORS` 与 `FILTERS`，使用三级路径 `DIRECT.binary.gt`、`TS.unary.pct_change`、`CS.unary.rank_pct`。命名 OP 自动收集，不需要 `DERIVATIVES`；Python 关键字算符使用尾下划线，如 `and_`。

```python
valid = DIRECT.binary.gt("valid", left="close_hfq", right=0)
momentum = TS.unary.pct_change("momentum", col="close_hfq", periods=20, on=valid)
rank = CS.unary.rank_pct("rank", col=momentum, ascending=True, ties_method="average", on=valid)
FACTORS = []
FILTERS = [valid]
```

本地派生依赖必须传 OP 变量，字符串只表示基础字段。Factor/Backtest dataset 的 `stock_pool_member` 是特定编译上下文允许的外部托管例外，不能自行定义或当作任意Query已有节点。

编译 `application="query"` 用于 Query 及所有 codes_query；Factor/Backtest dataset 分别用 `factor`/`backtest`。先编译再提交完整请求，编译成功不证明日期、收益标签、股票池或回调已全部有效。

Arena Factor 的收益标签是托管状态，回填 compiled_json 不能删除 return_columns 对应节点。Arena 自动注入成员与过滤属于远端请求契约；Solo 普通 Query 则显式定义 MEMBERS[universe.pool]，不能把候选代码视为托管节点。

## 不得混用的层次

- Backend 保存双源码；Worker/Runtime 执行剥除双源码后的 JSON。Solo直接调用该Runtime，不具备Backend Python编译器全局变量。
- Arena `run_query(project_id, request=...)` 和 `run_factor_analysis/run_backtest(parameters=...)` 会创建服务任务；本地 `scheme.data.query` 只是当前会话计算。
- Arena callbacks是DolphinScript函数；Solo主研究接口是Python `Algo`。阅读相邻 `backtest` skill的适配说明。
- 本地 schema应以项目锁定Runtime为准；不能因为远端支持新算符就自动升级历史项目。
