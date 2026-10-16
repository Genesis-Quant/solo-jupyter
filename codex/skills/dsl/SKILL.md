---
name: dsl
description: 编写、解释和检查 Solo/Arena Factor Query DSL。用户提到派生因子、JSON DSL、DIRECT/TS/CS、滚动窗口、截面排名、on、filters、基础字段或算符参数时使用；先从当前项目 Runtime 查询准确定义，不凭名称猜接口。
---

# Factor Query DSL

## 先确定执行环境

默认服务 Solo Jupyter 研究项目。先读取项目 `pyproject.toml`、`uv.lock`、算法源码，使用该项目 `.venv/bin/python`，不要用 Jupyter Server 的全局 Python 或自动升级依赖。

- **Solo 本地**：`scheme.data.query`、`Universe.query` 使用 Runtime JSON 节点。在 Python 文件中写字典也是 JSON DSL，不是 Arena 的 Python DSL 编译器。
- **Arena 远端**：只有用户明确操作 Arena 且当前工具确实提供对应 MCP 时，才使用双源码和 `compile_python_dsl`。见 [Arena 适配边界](references/arena-adapter.md)。
- Skill 是本地文档和检查程序，不会凭空提供 Arena MCP，不改变运行账号权限。

## 按需读取

- 首次构造节点或解释 `on`、BOOL、依赖：读 [DSL 规则](references/dsl-contract.md)。
- 查询日期、股票池、数据读取：同时读相邻 `data-query/SKILL.md`。
- 生成因子报告或回测：分别读相邻 `factor-analysis/SKILL.md`、`backtest/SKILL.md`。

## 发现与校验

将 `HELPER` 设为本 skill 的 `scripts/inspect_dsl.py` 绝对路径；相对路径以本 `SKILL.md` 所在目录为基准，不是用户工作目录。在项目目录运行：

```bash
.venv/bin/python "$HELPER" version
.venv/bin/python "$HELPER" factors
.venv/bin/python "$HELPER" operators --search pct_change
.venv/bin/python "$HELPER" describe unary.pct_change
.venv/bin/python "$HELPER" schema
.venv/bin/python "$HELPER" validate request.json
```

这些命令只读取已安装 Runtime 的模型与注册表，不连接 DolphinDB、不写数据库、不安装依赖，也不加载 `.env`。先用 `--help` 核对可选参数。Windows 项目把解释器换为 `.venv/Scripts/python.exe`。

字段目录同时返回 `configuration`，先检查 `source`：`option` 为显式参数，`environment` 为已导出的 `INDEX_CODES`，`runtime-defaults` 只表示包的默认值，不代表项目 `.env`。未显式配置时直接使用 Runtime 默认目录并给出提示；目录中已有指数权重字段正常校验。需要选择其它指数时，取得项目已确认的非敏感配置，通过 `--index-codes` 在子命令**之前**传入（优先于环境变量），例如：

```bash
.venv/bin/python "$HELPER" --index-codes '000016.SH,000300.SH,000905.SH,000852.SH' factors
.venv/bin/python "$HELPER" --index-codes '123456.SH' validate request.json
```

例子不是所有项目的默认配置；不要仅为通过校验把请求中出现的指数拼进配置。不需要改变项目 `.env` 或导出密码。`factors`／`operators` 和校验结果的默认配置提示不阻止已有字段使用；默认目录不证明项目 `.env` 的有效配置，目录中不存在的字段仍返回未知字段。

1. 明确最终需要的列、日期、代码范围、最长窗口；不要把每步中间值变成输出列。
2. 读取基础字段白名单。字段可用不代表所有代码和日期都有非空数据。
3. 对每个不同算符取得准确 `definition`，逐项核对计算上下文、`fields`、`params`、输出类型及 `on`。不要凭函数名推断 `window`、`periods`、`ddof` 等参数。
4. 在节点中区分列输入和参数。单消费者中间表达式优先嵌套；昂贵且被复用的表达式可以命名一次。
5. 对完整 Runtime Query 离线校验。使用 `Universe` 时先构造它的 `dataset_query`，再校验注入成员节点后的对象；不要把缺少托管节点的片段当完整 Query。
6. 只有用户要求计算时才运行小范围数据验证；检查实际字段、日期边界、NULL 和样本数。离线校验通过不等于实际数据或收益有效。

## 必守语义

- `DIRECT` 逐行计算且禁止 `on`；`TS` 按代码/日期；`CS` 按日期截面。
- `on` 限定参与计算的行，未参与行产生 NULL，**不删行**。`filters` 在派生计算后按 AND 删行，只能引用顶层 BOOL 派生列。
- 数值字段不是 BOOL。对价格、`is_st`、权重先使用比较或类型明确的 BOOL 算符。
- JSON 中字符串可以引用基础列或命名派生列；Arena Python 源码对本地派生列必须引用 OP 变量。
- Solo 的 `Universe.member` 是 `"stock_pool_member"`；`Universe.query` 注入它，但不自动加入 `filters`。不要自行重定义。
- Runtime Query 的 `end_date` 包含该日；Solo `Universe.query(start, end)` 和研究参数是 `[start, end)`。
- `codes=[]` 表示全市场，不表示空股票池；空候选不能直接当空列表查询。
- `lookback` 是日历时长，不是交易日数；负 shift 和未来标签只能用于评价，不能作为交易信号。
- 截面计算必须保留完整参与域。只查最终入选股票不能复现原排名或标准化结果。

## 交付

提供可运行的最小 DSL/项目改动，说明当前 Runtime 来源、验证方式、日期/股票池/输出列及 NULL 处理。需要数据才能确认的内容明确标注，不杜撰算符或成功结果。不要为此修改项目锁文件、已有版本或历史报告。
