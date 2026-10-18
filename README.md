# Jupyter

Solo 本地部署使用父目录的 `docker-compose.yml`，在父目录执行 `docker compose up -d --build --wait`。共享工作区为 `/shared/projects`，运行结果为 `/shared/runs`；凭据库使用命名卷，解锁密码统一配置在父目录 `.env` 的 `JUPYTER_KEYRING_PASSWORD`。代理为可选配置，不依赖 nj 的外部网络。具体配置见父目录 README。

## Solo 插件

源码在 `extension/`，包含 JupyterLab 预构建扩展和 Jupyter Server 扩展，随本目录镜像安装。
左侧 Solo 面板跟随当前文件或文件浏览器目录，向上查找项目根目录的 `.solo`，显示项目类型及 Scheme / Algo 版本。
工具栏提供刷新、打开项目目录和打开 `research.ipynb`；命令面板的 Solo 分类还可打开 `pyproject.toml`。
非项目目录显示空状态，错误在面板内显示，打开项目不弹出流程对话框。

界面使用 Jupyter 的 `SidePanel`、`CommandToolbarButton`、`Select` 和 `Button`，只读项目信息与状态提示使用普通文本；
主题、图标、命令与文件打开均使用 Jupyter 原生能力。CSS 只设置间距和滚动。
服务端提供需登录的 `GET <base_url>/solo/project?path=<相对路径>`，读取项目描述，不开放隐藏文件通用读取。

### 文件读取和下载

Solo Server Extension 在 Jupyter 启动时同时注册文件后端，与 Jupyter 共用进程、端口、登录 Cookie/token 和 Contents 授权，不需要额外启动服务：

- 读取原始文件：`GET <base_url>/solo/files/<工作区相对路径>`。
- 下载文件：同一 URL 加 `?download=1`；中文文件名按标准编码，Notebook 文件默认作为附件。
- `HEAD` 和 HTTP `Range` 沿用 Jupyter/Tornado 的文件传输实现，可读取 JSON、Parquet、源码或其他普通文件，不要求运行成功或存在报告清单。
- 目录浏览继续使用 Jupyter 自带的 `/api/contents`，文件接口不生成目录列表，不提供写入、删除或执行操作。

“所有文件”指 `ServerApp.root_dir` 内的文件。Solo 主部署的根目录是 `/shared`，因此可以读取 `projects/...` 和 `runs/...`，不是容器根目录或宿主全盘。路径按 URL 分段编码；拒绝 `..`、绝对路径、反斜杠、盘符和逃出工作区的符号链接。隐藏文件及指向隐藏目标的链接遵循当前 ContentsManager 的 `allow_hidden` 设置；文件接口不另行启用隐藏访问，也不扩大到凭据目录或打开全局跨域访问。

例如，报告保存到 `/shared/projects/factor/Test/reports/<id>/information_coefficient.parquet` 后，读取地址为：

```text
http://127.0.0.1:8888/solo/files/projects/factor/Test/reports/<id>/information_coefficient.parquet
```

iframe 应传浏览器可访问的页面 URL；文件 URL 中不放登录 token。页面读取数据仍须满足 Jupyter 的登录、Origin/Referer 和授权检查，跨端口不能仅凭 Cookie 假定能够读取。HTML/SVG 原始文件保留 Jupyter 的 sandbox CSP；这个文件后端本身不会把旧 Scheme `srcdoc` 报告转换成前端 URL，也不会修复旧独立 HTML 的 DuckDB 扩展加载。

Jupyter 原生 `/files/<工作区相对路径>` 和 `?download=1` 已提供相同的文件传输基础。新 `/solo/files` 路由在更新扩展后的下一次 Server 启动生效；仅安装 wheel 或刷新浏览器不会热加载路由。已有活跃 Kernel 时不为此重启、清空工作区或重新执行 Notebook。

### Notebook 报告 URL

`ipython_config.py` 在项目 Kernel 启动时加载 `/etc/ipython/solo_report_bridge.py`，注册 `solo_report_display`。该桥接只在 Kernel 内存中把已安装 Scheme 的 `Report.show()` 接到 URL 展示，不改项目源码、依赖、锁文件或旧 wheel；`to_html()`、`save()` 和 `preview()` 保持原行为。

`report.show()` 将已计算的 DataFrame 保存到当前项目 `reports/<uuid>/`，写入一个带 Scheme 版本、文件名和显示参数的 `report.json`，然后输出 `iframe src="<前端>/embed/report?source=jupyter&path=..."`。不会重新运行研究，不嵌入脚本、Worker、WASM、Parquet 或登录 token；每次保存使用独立目录，不覆盖历史运行报告。前端按同源 `/jupyter/solo/files/...` 读取 JSON 和 Parquet，经过固定目标代理复用 Jupyter 登录 Cookie 和 XSRF 校验，无服务端 token 注入或全局 CORS 放行。

`SOLO_REPORT_FRONTEND_URL` 必须是浏览器可访问的 HTTP(S) 前端地址，默认 `http://127.0.0.1:5174`，不能包含凭据、查询参数或片段。浏览器应使用与 `SOLO_JUPYTER_BROWSER_URL` 相同的主机名登录 Jupyter；生产部署同样需把前端的 `/jupyter` 反向代理到受认证的 Jupyter 服务。新 Kernel 自动加载桥接，已打开的 Kernel 不会被自动重启或执行。

“安装已有项目”从 Solo 后端读取未归档且未退役的项目，仅列出当前类型及其上游类型、且 Scheme 主版本和次版本相同的其他项目，补丁版本不必相同。1.2.0 与 1.2.7 可双向安装，1.1.x、1.3.x 不混用；安装接口也执行同一校验，不能绕过候选列表。项目包的实际版本须属于同一系列，并声明整个系列的依赖范围，如 `scheme>=1.2.0,<1.3.0`。类型顺序为 factor → model → optimize → control → execution；同类项目可以相互选择，自身不能安装。

安装前由 Backend 中央政策检查当前项目、候选项目和递归本地运行时依赖的发布来源。源码及其必要本地依赖构建为 wheel，注册后按成果 UUID 保存在当前项目的 `.solo-wheels/<包名>/<成果UUID>/<文件名>`；已发布成果按 UUID 下载，不计算文件摘要。上游开发依赖不递归，上游 Scheme source 不传播。extras 按 PEP 685 规范化，wheel 原始 `Requires-Dist` 交给 uv 解析；已有依赖更新时保留 extras 和 marker，也允许已有目录、editable、Scheme override 和 uv workspace 配置。

安装在内存中保存原 `pyproject.toml` 和 `uv.lock`，准备 wheel 后修改依赖，执行 `uv lock`、`uv sync` 和 `uv pip check`。安装不能改变当前 Scheme 的实际版本；失败写回原配置、锁文件并重新 sync，成功后向 Backend 接受实际使用的成果 UUID。不会移动或删除原 `.venv`，也没有安装事务、续期或恢复日志。重新同步失败会直接报错，需修复依赖来源后再同步环境。

安装、参数读取和版本快照在 Jupyter Server 同一进程内按项目 UUID 排他，重叠请求立即返回 409；后端项目操作锁只用于删除。源码内链接正常跟随，依赖路径解析后仍须位于 workspace 内。固定 Git 运行时来源在系统临时目录检出源 lock 的 commit 并构建 wheel，不传播对方 Scheme source。
已被 Kernel 导入的包需要重启 Kernel 才能加载新代码。项目改名导致路径变化时，需重新选择安装。
插件通过 `SOLO_BACKEND_URL`（默认 `http://backend:8000`）连接后端，列表与安装接口为 `GET/POST <base_url>/solo/project/dependencies`。

后续提交和上游管理插件通过 `IProjectContext` token 获取当前项目、监听 `changed` 或调用 `refresh()`。
五类项目均支持填写研究参数后保存版本。表单由 Scheme 对应的 ReportForm 生成；
Model 选择默认后续算法，Optimize、Control、Execution 从已安装项目中选择上游，补齐后续默认环节。
保存时仅复制 `pyproject.toml`、`uv.lock`、清单引用的 readme/license 和 `src`；`src` 内链接按普通复制跟随，不复制研究数据、Notebook 或环境。冻结当前包及上游包，按实际项目类型提交 factor、model、optimize、control 或 execution 工作流；后三类和 model 当前均展示完整回测报告。打开参数面板、验证和保存前检查中央准入，后台构建开始前再次检查；退役项目保留源码、Notebook 与历史版本查看，但不能新保存或安装为上游。
因子版本在冻结源码之后、构建 wheel 之前调用一次 `scheme parameters --validate --validate-factor`。Scheme 用分析参数的闭区间 `[start,end]` 先计算完整因子表，再默认抽取最多 5 个不同日期调用 `compute(d,d)` 对照；全部一致才继续构建并提交，失败沿用构建失败回调。`factor.params` 不再保存日期，日期只在 `analysis` 中。打开表单、保存前的普通 `--validate` 及 `validate_only` 不运行计算；正式任务和“重试提交”不会重复此校验。该计算调用不使用普通命令的 600 秒超时；项目需配套安装 Scheme 1.7.0 或支持该接口的后续版本；1.6.x 及更早版本的因子保存会因不支持新开关而失败，不会静默跳过验证。旧项目的交互研究和历史报告不因此改写，升级前仍须迁移自定义 `compute` 的闭区间处理。

构建中断时可点击“取消构建”解除保存锁定；
调度服务明确未接收任务时可点击“重试提交”，复用该版本的构建产物。
提交响应丢失时通过“核实提交”查找已有任务，不重复提交工作流。发布接口尚未接入。

开发环境需要 Node.js、npm、Python 及 `jupyterlab>=4.6,<5`，在 `extension/` 执行：

```bash
npm ci
npm run build
python -m pip install --index-url https://pypi.tuna.tsinghua.edu.cn/simple --no-deps .
jupyter labextension list
jupyter server extension list
```

首次安装后重启 Jupyter Server、刷新页面。前端改动需重新构建、安装并刷新页面。
生产镜像会从源码构建，不依赖本地 `node_modules` 或构建产物。
结构遵循 [JupyterLab 预构建扩展](https://jupyterlab.readthedocs.io/en/stable/extension/extension_dev.html)的组织方式。

以下说明保留本目录独立 Compose 的 nj 部署方式；Solo 本地部署不使用这份独立 Compose。

项目目录：`/home/ubuntu/docker-compose/jupyter`。

- 容器名称：`jupyter`；宿主机仅发布 `127.0.0.1:8888`，映射至容器端口 `8888`。
- 内存上限：4096 MiB，禁止额外使用交换空间。
- HTTP/HTTPS 代理：`http://metacubexd:7891`。
- Notebook 根目录和工作目录：`/home/jovyan/work`。
- 工作文件：宿主机 `./data`，挂载到 `/home/jovyan/work`。
- Jupyter 设置：宿主机 `./config`，挂载到 `/home/jovyan/.jupyter`。
- IPython 默认配置：`./ipython_config.py`，只读挂载到 `/etc/ipython/ipython_config.py`，默认隐藏 `%` / `%%` Magic 补全候选。
- 登录令牌保存在 `.env` 的 `JUPYTER_TOKEN` 中；该文件不提交到 Git。

独立 Compose 启动时使用镜像原有入口修正挂载目录归属并切换为 `jovyan`（UID 1000、GID 100）。Compose 命令直接启动 D-Bus 会话、通过 `.env` 的 `JUPYTER_KEYRING_PASSWORD` 解锁凭据库，然后调用镜像原生 `start-notebook.py`；没有自定义启动脚本。中文语言包固定在镜像依赖中。

## 管理

在本项目目录执行：

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail 50
docker compose restart
```

服务器本机可访问 `http://127.0.0.1:8888`，远程访问使用已有的 Jupyter 反向代理域名，使用 `.env` 中的令牌登录。

`meta_default` 和 `1panel-network` 使用宿主机现有 Docker 网络。前者用于连接代理，后者保留与现有服务的容器网络连通性。

## 资源占用显示

镜像通过 `requirements-code-check.txt` 预置 `jupyter-resource-usage==1.3.0`。顶部资源指示器显示 Jupyter 服务及其子进程（内核、终端等）的内存占用，每 5 秒刷新。已有启用设置保存在 `config/lab/user-settings/@jupyter-server/resource-usage/topbar-item.jupyterlab-settings`，可在“设置 → 设置编辑器 → Resource Usage Indicator”中调整。

`config/jupyter_server_config.json` 中启用了 `ResourceUseDisplay.track_cpu_percent=true`，
顶部同时显示 Jupyter 服务及其子进程的 CPU 占用。修改此服务端选项后需重启 Jupyter。

同一配置设置 `ResourceUseDisplay.enable_prometheus_metrics=false`，关闭插件的 Prometheus 指标采集。该任务每秒在服务主线程上逐个采样服务及全部子进程的 CPU，每个进程阻塞 0.05 秒；子进程（内核、终端、omp 等）越多，阻塞越久，终端输入和 Notebook 输出会周期性卡顿。顶部指示器使用的 `/api/metrics/v1` 在后台线程计算，不受影响。

## 单元格执行耗时

镜像预置 `jupyterlab-execute-time==3.3.0`。刷新 JupyterLab 并运行 Notebook 单元格后，
单元格下方会显示执行时间和耗时。扩展默认启用，并自动将 Notebook 设置中的 `recordTiming` 设为 `true`；
计时数据随 Notebook 元数据保存。显示选项可在“设置 → 设置编辑器 → Execute Time”中调整。

项目说明：[jupyterlab-execute-time](https://github.com/deshaw/jupyterlab-execute-time)。

## 变量查看器

镜像预置 `lckr-jupyterlab-variableinspector==3.2.4`。刷新 JupyterLab 后，
在 Notebook 内右键选择 `Open Variable Inspector`，或在命令面板搜索同名命令。
运行单元格后，面板显示当前 Python 内核中的变量名、类型、大小和值；
NumPy 数组和 pandas DataFrame 可通过变量行的查看按钮打开表格视图。

已知问题：在当前 Python 3.13 环境中，3.2.4 对名为 `values` 的变量存在摘要显示冲突，
可能显示插件内部的变量名单；实测内核中的原变量值保持不变。

项目说明：[jupyterlab-variableInspector](https://github.com/jupyterlab-contrib/jupyterlab-variableInspector)。

## 中文界面

镜像通过 `requirements-code-check.txt` 安装并固定 `jupyterlab-language-pack-zh-CN==4.5.post3`，用于持久化设置中的 `zh_CN` 界面语言。语言包随镜像构建安装，容器重建后仍可使用。它与下述 PDF 中文字体分别配置。

## 中文 PDF 导出

自定义镜像通过 `Dockerfile` 安装 XeLaTeX、Pandoc、中文排版组件和 Noto CJK 字体。镜像构建使用宿主机代理 `http://127.0.0.1:7891`；更换服务器时可调整 Compose 中的构建代理。

中文模板位于 `config/nbconvert/templates/latex/index.tex.j2`，挂载到 Jupyter 用户模板目录。正文使用 Noto Serif CJK SC，无衬线文字使用 Noto Sans CJK SC，代码注释及文本输出使用 Noto Sans Mono CJK SC。JupyterLab 菜单中的 PDF 导出和 `jupyter nbconvert --to pdf` 都会自动使用该模板。

在 JupyterLab 中保存 Notebook 后，选择“文件 → 保存并导出 Notebook 为 → PDF”，或在容器终端执行：

```bash
jupyter nbconvert --to pdf "你的文件.ipynb"
```

中文配置和字体依赖已记录在挂载目录及镜像构建文件中，容器重建后仍可使用。图表内部的文字由绘图库绘制，需要在绘图代码中选择中文字体并重新生成图表。

## 表格查看

镜像通过 `requirements-code-check.txt` 预置 `jupyterlab-spreadsheet==0.4.2`，用于查看 XLS、XLSX、ODS 和 CSV 表格。安装后刷新浏览器，双击 Excel 文件打开；CSV 文件可右键选择“打开方式 → Spreadsheet”。该扩展主要提供表格查看，不是完整的 Excel 编辑器。

## DolphinDB

镜像通过 `requirements-dolphindb.txt` 安装 PyPI 发布的 `dolphindb-extension[notebook]==0.1.0b3`，
包含预构建的 JupyterLab 前端和 Notebook magic 支持。DolphinDB Python SDK 固定为 `3.0.6.0`，
构建时正常解析依赖，安装后运行 `pip check`。

刷新 JupyterLab 后，点击侧栏的 DolphinDB 连接按钮，或在命令面板运行
`DolphinDB: 管理连接`。通过该面板填写连接地址、端口、账号和密码。
可从启动器新建 DOS 文件；Python Notebook 支持 `%ddb` / `%%ddb`。

连接配置默认保存到 `/home/jovyan/.jupyter/dolphindb-extension/connections.json`，
对应宿主机 `config/dolphindb-extension/`，随现有配置目录持久化。

项目说明：[dolphindb-extension 0.1.0b3](https://pypi.org/project/dolphindb-extension/0.1.0b3/)。

### 系统凭据库

容器内使用 GNOME Keyring 的 Secret Service。Compose 在同一个 D-Bus 会话中解锁凭据库并启动 Jupyter；
在 DolphinDB 连接编辑界面输入密码、勾选“记住密码”并保存后，密码会写入加密凭据库。

解锁密码统一放在 `.env` 的 `JUPYTER_KEYRING_PASSWORD`，通过容器环境变量提供，解锁后从 Jupyter 进程环境中移除；
不需要单独密码文件、额外目录或 Compose secret。Docker 管理者仍可查看容器配置中的环境变量，应限制 `.env` 读取权限。
独立 Compose 的 `keyring-data/` 和 Solo 的 `jupyter-keyrings` 命名卷只保存加密凭据，继续持久化且不重建。
备份时需要同时保留凭据库与 `.env` 中的原解锁密码；已有凭据库不能随意更换密码。
迁移原密码时必须保留全部字节，包括原本参与解锁的换行；可在 `.env` 的双引号值中用 `\r`、`\n` 转义表示，不要去除它们。

重建会沿用现有密钥和凭据文件。未勾选“记住密码”的连接仍使用临时密码。
参考：[keyring 的无桌面 Linux 配置](https://keyring.readthedocs.io/en/latest/#using-keyring-on-headless-linux-systems)。

## uv

镜像通过[官方 PyPI 安装方式](https://docs.astral.sh/uv/getting-started/installation/#pypi)，从清华 PyPI 镜像预置 uv 0.12.10 和 uvx，命令位于 `/opt/conda/bin`。版本固定在 `Dockerfile` 中，容器重建后仍可使用。

构建时 pip / uv 默认使用 `https://pypi.tuna.tsinghua.edu.cn/simple`，APT 使用清华 Ubuntu 镜像；镜像的 npm 配置与插件的 `.npmrc` 将 npm 源设为 `https://registry.npmmirror.com`，插件本地 `npm ci` 也沿用此源。`uv.toml` 保留硬链接模式与清华默认索引，并将 uv 管理的 Python 下载源设为 `https://registry.npmmirror.com/-/binary/python-build-standalone`；容器中该配置位于 `/etc/uv/uv.toml`。基础镜像仍使用原始 Quay / Docker Hub 名称，仓库不替换为未经验证的镜像副本。

在 JupyterLab 的终端中可以直接执行：

```bash
uv --version
uvx --version
```

在项目目录中使用当前 Python 创建虚拟环境：

```bash
uv venv --python /opt/conda/bin/python .venv
```

## AI 助手

镜像预置 Jupyter AI 3.2.0、npm 当前 `latest` 版本的 Codex CLI 和 `@agentclientprotocol/codex-acp`，在 JupyterLab 的 Chat 界面中使用 Codex 读取和编辑文件、执行命令以及处理 Notebook。

Python 依赖配置保留在 `requirements-ai.txt`。Node/Codex 安装链先通过 NodeSource `setup_lts.x` 和系统包管理器安装当前 LTS 的完整 Node.js（包含 npm、npx），再通过 `npm install --global` 安装 `@openai/codex@latest` 与 `@agentclientprotocol/codex-acp@latest`。这条安装链不写死版本号或发行包 SHA，也不复制只在构建阶段安装的私有 `node_modules`。

Node/npm、Codex 和 ACP 在最终镜像中完整保留，npm 全局包安装前缀为 `/usr/local`。`CODEX_PATH=/usr/local/bin/codex` 让 ACP 调用同一个全局 Codex，而不是它自己的嵌套依赖副本。当前 Compose 以 root 运行 Jupyter，可在终端执行：

```bash
node --version
npm --version
npx --version
npm install -g @openai/codex@latest @agentclientprotocol/codex-acp@latest
codex --version
codex-acp --version
```

更新后重新打开 Codex 会话才会使用新进程；不需要重启 Notebook Kernel。容器内手动更新只改变当前容器；镜像构建执行安装时也使用 `latest`，不会回退到固定旧版。Docker 可能复用旧安装层；要强制拉取当前最新版，使用 `docker compose build --no-cache jupyter`，或在已有容器中执行上述更新命令。独立以非 root 运行镜像时，更新全局包需要相应的 `/usr/local` 写权限，不改用另一份用户级 Codex 掩盖权限问题。

`jupyterlab-commands-toolkit==0.2.0` 的前端在模块顶层直接调用 `crypto.randomUUID()`；部分浏览器或非安全上下文没有该方法，导致模块初始化失败，MCP 命令没有前端接收者。镜像安装 AI 依赖后运行 `scripts/patch_mcp_uuid.py`，按固定版本及 UUID/Lumino 特征字符串定位后改用已绑定的 `@lumino/coreutils.UUID.uuid4()`。补丁不增加全局 crypto polyfill，不调整认证或权限；保留原始资产，新 chunk 和 remote entry 使用确定性的 `solo-uuid` 文件名并同步更新加载清单，刷新页面即可加载修复，不需要重启 Server/Kernel。未来升级工具包若资产不同，构建会明确失败，必须重新验证补丁，不能静默替换未知版本。

首次使用时，打开 JupyterLab 的终端，执行：

```bash
codex login --device-auth
```

按照终端提示在浏览器完成登录，再从启动器打开 Chat。新聊天默认选择 Codex，此设置保存在 `config/jupyter_server_config.json` 的 `PersonaManager.default_persona_id` 中，也可在输入框的代理选择菜单中选择 Codex。设备登录若不可用，请检查 ChatGPT 账户的安全设置是否允许设备代码登录。

Codex 的配置、登录信息和会话保存在 Docker 命名卷 `codex-home`，挂载至 `/home/jovyan/.codex`，重建容器后保留。Jupyter AI 的聊天记录以文件形式保存在工作目录中。普通 `docker compose down` 保留命名卷，`docker compose down -v` 会删除其中的 Codex 数据。

使用说明：[Jupyter AI](https://jupyter-ai.readthedocs.io/en/latest/getting-started.html)、[Codex 登录](https://learn.chatgpt.com/docs/auth)。

### 内置研究 skills

技能的实际目录是 `/shared/skills/`，不再创建 `/etc/codex/skills` 链接。Jupyter 的 Codex/ACP 读取 `/opt/jupyter-codex/skills.json`，通过原有 `skills/extraRoots/set` 调用注册搜索目录（`user` scope）；这是 Solo 的集成配置，不是 Codex `config.toml` 的字段。不改动用户登录、会话、sandbox 权限或 `CODEX_HOME`。

仓库配置 `codex/skills.json`：

```json
{"extraRoots": ["/shared/skills"]}
```

`patch_codex_skills.py` 仅在已安装 ACP 的 `refreshSkills` 中加入该配置读取，与原有工作区技能根合并，不代理通信、不扩展可写工作区；上游刷新入口变化时构建明确失败。镜像安装时应用，手动 `npm install -g @agentclientprotocol/codex-acp@latest` 后需再运行 `python /opt/jupyter-codex/patch_skills.py`。

`/shared/skills/` 本身是独立数据卷（主 Compose 为 `solo-skills`，独立 Compose 为 `skills`，设置 `nocopy`，不预填镜像内的预置技能）。自建技能直接在其中新建文件夹和 `SKILL.md`，保存在数据卷中，不进入仓库，重建容器后保留，`docker compose down -v` 会删除。5 个预置技能分别从 `jupyter/skills/<名称>/` 读写挂载到 `/shared/skills/<名称>/`，在 Jupyter 中修改会直接写回仓库；新增预置技能时需在两份 Compose 中各加一行挂载。`jupyter/codex/skills.json` 只读挂载到 `/opt/jupyter-codex/skills.json`，目录配置修改仓库中的 JSON。首次调整挂载需重建容器，之后技能内容与配置均在刷新技能时重新读取，无需重建镜像或重启内核。独立部署的文件浏览器根目录为 `/home/jovyan/work`，可通过终端编辑 `/shared/skills/`。

| Skill | 用途 |
| --- | --- |
| `dsl` | Runtime JSON DSL、算符和基础字段发现、BOOL/on/filters、离线校验 |
| `data-query` | 行情和基础数据查询、Universe 两阶段动态池、日期与会话边界 |
| `factor-analysis` | Factor/Params/Form、收益标签、IC/分组/换手与 Arena 口径比较 |
| `dolphindb` | DolphinScript、部署版本、函数签名、类型/schema 与有界诊断 |
| `backtest` | Python Algo 四环节、回调/订单/持仓、回测数据与结果审计 |

内容参考 Arena MCP 文档并按 Solo 实际接口适配。默认使用项目锁定的 Runtime JSON、`Universe.member` 和 Python Algo，不把 Arena 双源码、DolphinScript callbacks 或未连接的 MCP 工具当作本地接口。主指引按主题触发，详细契约在各 skill 的 `references/` 按需读取，不把整套文档自动塞入每轮上下文。

在 Jupyter AI 的 Codex 聊天中可使用 `/skills` 查看技能，也可明确说“使用 `$dsl` 检查当前因子”或“使用 `$backtest` 检查回调”。ACP 会在新建/恢复会话以及处理普通提示前读取目录配置并刷新 skills；如果界面仍保留旧列表，新开 Codex 聊天即可，不必重启 Notebook Kernel。

此配置仅接入 Jupyter 使用的 `codex-acp`，终端直接执行原生 `codex` 不会自动读取该 JSON。Codex 0.162.1 没有持久化的额外技能目录 TOML 字段；`skills.config.path` 仅控制已发现技能的启停，不能替代搜索目录注册。

DSL skill 提供本地脚本，始终用研究项目的解释器运行：

```bash
.venv/bin/python /shared/skills/dsl/scripts/inspect_dsl.py version
.venv/bin/python /shared/skills/dsl/scripts/inspect_dsl.py operators --search pct_change
.venv/bin/python /shared/skills/dsl/scripts/inspect_dsl.py describe unary.pct_change
.venv/bin/python /shared/skills/dsl/scripts/inspect_dsl.py validate request.json
```

它读取已安装 Runtime 的注册表和 Pydantic 模型，校验依赖字段白名单，不连接数据库、不计算数据或安装依赖；不会加载 `.env`。字段目录和校验结果的 `configuration` 说明指数配置来源：显式 `--index-codes`（放在子命令前）优先于已导出的 `INDEX_CODES`，未提供时使用 Runtime 默认配置并警告。默认目录中的指数权重字段正常校验；默认配置不代表项目 `.env`，需要其它指数时显式传入非敏感配置。

项目若仅在 `.env` 中设置指数，用已确认的非敏感配置显式传入，不需要加载或导出密码。例如项目实际配置为 `123456.SH` 时：

```bash
.venv/bin/python /shared/skills/dsl/scripts/inspect_dsl.py --index-codes '123456.SH' factors
.venv/bin/python /shared/skills/dsl/scripts/inspect_dsl.py --index-codes '123456.SH' validate request.json
```

参数必须与运行项目的有效配置一致，不应仅为通过校验添加请求中的指数；空字符串表示明确没有配置指数。helper 按 Runtime 的映射函数更新权重字段，其余基础字段和算符仍来自已安装版本。算符通用 JSON Schema 不包含所有具体规则，实际验证使用 `FactorQuery.model_validate`。本地校验通过不等于数据完整或回测成功。

回归测试和真实 Codex 发现检查：

```bash
python -m pytest -q -p no:cacheprovider tests/test_codex_skills.py tests/test_codex_dsl.py
# 构建后的 Jupyter 容器内：
python /opt/jupyter-codex/check_skills.py --cwd /shared/projects
```

DSL 测试需要已安装 `arena-runtime` 的项目 Python；没有该包时相关用例明确跳过。发现检查只调用 Codex 本地 `initialize`、`skills/extraRoots/set` 和 `skills/list`，使用临时 `HOME/CODEX_HOME`，不发送模型请求、不读取真实登录或聊天记录。要证明无外部网络，放在 `docker run --network none` 的隔离容器执行；临时目录不等于网络隔离。

## Python 代码检查

镜像通过 `requirements-code-check.txt` 固定 `jupyterlab-lsp`、`jupyter-lsp`、`python-lsp-server`、`python-lsp-ruff` 和 `ruff`。检查设置保存在 `config/lab/user-settings/@jupyter-lsp/jupyterlab-lsp/plugin.jupyterlab-settings`，优先使用 `pylsp`，由 Ruff 提供检查并关闭重复的检查器。

首次安装语言服务器后，需要重启 Jupyter 服务并刷新浏览器，才能在 Notebook 和 Python 文件中看到检查提示。重启服务会结束当前内核并清空内存变量，请先保存工作。后续可在“设置 → 设置编辑器 → Language Server”中调整检查配置。

### 按项目绑定 Python 代码智能

`solo-jupyter` 同时注册 `solo_project_environment` pylsp 插件。插件在服务端初始化、配置更新和文档请求时自动启用 `solo` 配置源；前端 `pylsp.configurationSources` 保留 `pycodestyle` 等内置值，不添加前端 schema 不支持的 `solo`。每个文档按 `/shared/projects/<类型>/<项目>` 识别所属项目，校验 `.solo`、`pyproject.toml` 和项目解释器，分别使用项目 `.venv/bin/python` 与 `src`。不根据当前标签页或 Kernel 改写全局设置，多项目可同时补全、悬浮和 Alt+点击跳转。

Notebook 的 Python 虚拟文档保留原始相对路径；插件将 `JP_LSP_VIRTUAL_DIR` 下的文档映射回 `SOLO_SHARED_DIR`，再识别同一项目。非项目文件、无效标识或尚未创建的环境使用默认配置，不借用其他项目的依赖。

打开文档及代码智能请求前按文档识别所属项目，仅在项目解释器或 `uv.lock` 的修改时间变化时清除 pylsp 共享配置缓存，并仅移除受影响项目的 Jedi 环境缓存。定义与类型定义、补全、悬浮、引用、签名、检查、文档符号、高亮及重命名均在对应 pylsp hook 中刷新；动态增删工作区后，按文档实际持有的配置与工作区清除缓存，无需关闭重开文档。项目不要求存在 `src`，元数据名称不要求与目录名一致，环境目录可以是链接；文件所属项目仍须位于工作区内。支持当前固定的 `python-lsp-server==1.15.0`；配置源注册和环境缓存使用其私有接口，升级时应运行回归测试。

测试在 `extension/` 中执行：

```bash
uv run --group dev pytest -q
```

依赖源码定位由此恢复，但 Jedi 对部分泛型、动态属性的推断仍有限；编译库跳转到 `.pyi` 类型声明属于正常行为。

也可以在 Jupyter 终端中执行：

```bash
ruff check "你的文件.ipynb"
ruff format --check "你的文件.ipynb"
```

这两个命令只检查，不修改文件；执行 `ruff format "你的文件.ipynb"` 会格式化文件。项目专属规则可写入 `pyproject.toml` 或 `ruff.toml`。

### 格式化

镜像通过 `requirements-code-check.txt` 预置 `jupyter-ruff==0.3.1`。在 Notebook 中按 `Ctrl+Alt+L` 格式化全部代码单元格，在 `.py` 编辑器中按同一快捷键格式化整个文件。快捷键保存在 `config/lab/user-settings/@jupyterlab/shortcuts-extension/shortcuts.jupyterlab-settings`。

扩展默认关闭运行时和保存时自动格式化，可在“设置 → 设置编辑器 → Jupyter Ruff”中调整。容器中的 Ruff 命令行工具也可通过 `ruff format "文件名.py"` 或 `ruff format "文件名.ipynb"` 格式化文件。

已知问题：此前浏览器日志记录 `jupyter-ruff 0.3.1` 激活耗时约 11.4 秒，并出现 WASM 空指针错误。本次预置使用该版本的官方包，未包含这些问题的修复。

### 直接运行 Python 文件

打开 `.py` 文件后，编辑器顶部提供“创建控制台”和“运行整个文件”按钮。首次点击“创建控制台”并选择 Python 内核，随后点击“运行整个文件”，即可在下方控制台查看结果。关联的控制台仍打开时，后续只需点击运行按钮。

按钮调用 JupyterLab 内置命令，无需额外安装扩展；设置位于 `config/lab/user-settings/@jupyterlab/fileeditor-extension/plugin.jupyterlab-settings`，刷新页面后加载。运行的是编辑器中的全部代码，包括尚未保存的修改；控制台会保留上次运行的变量。这种交互执行方式与独立启动 `python 文件.py` 的进程不同，依赖 `__file__` 或命令行参数的脚本仍应使用终端运行。

在容器中临时安装的其他 Python 包不会随重建保留；需要固定依赖时，应写入镜像构建配置。`data` 和 `config` 目录中的内容会随重建保留。
