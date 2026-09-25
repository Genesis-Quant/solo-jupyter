# Jupyter

Solo 本地部署使用父目录的 `docker-compose.yml`，在父目录执行 `docker compose up -d --build --wait`。共享工作区为 `/shared/projects`，运行结果为 `/shared/runs`；凭据库使用命名卷，密钥位于父目录 `.secrets/jupyter-keyring-password`。代理为可选配置，不依赖 nj 的外部网络。具体配置见父目录 README。

## Solo 插件

源码在 `extension/`，包含 JupyterLab 预构建扩展和 Jupyter Server 扩展，随本目录镜像安装。
左侧 Solo 面板跟随当前文件或文件浏览器目录，向上查找项目根目录的 `.solo`，显示项目类型及 Scheme / Algo 版本。
工具栏提供刷新、打开项目目录和打开 `research.ipynb`；命令面板的 Solo 分类还可打开 `pyproject.toml`。
非项目目录显示空状态，错误在面板内显示，打开项目不弹出流程对话框。

界面使用 Jupyter 的 `SidePanel`、`CommandToolbarButton`、`Select` 和 `Button`，只读项目信息与状态提示使用普通文本；
主题、图标、命令与文件打开均使用 Jupyter 原生能力。CSS 只设置间距和滚动。
服务端提供需登录的 `GET <base_url>/solo/project?path=<相对路径>`，读取项目描述，不开放隐藏文件通用读取。

“安装已有项目”从 Solo 后端读取未归档项目，仅列出当前类型及其上游类型、且 Scheme 大版本相同的其他项目。
类型顺序为 factor → model → optimize → control → execution；同类项目可以相互选择，自身不能安装。
点击安装会执行 `uv add --no-workspace --upgrade-package <包名> --reinstall-package <包名> <项目目录>`，将对方当前已保存的源码构建安装到当前项目的 `.venv`，同步更新 `pyproject.toml` 和 `uv.lock`。
安装的是本地项目包，不是已发布版本，也不是 editable 安装；源码修改后可再次点击安装更新。
已被 Kernel 导入的包需要重启 Kernel 才能加载新代码。项目改名导致路径变化时，需重新选择安装。
插件通过 `SOLO_BACKEND_URL`（默认 `http://backend:8000`）连接后端，列表与安装接口为 `GET/POST <base_url>/solo/project/dependencies`。

后续提交和上游管理插件通过 `IProjectContext` token 获取当前项目、监听 `changed` 或调用 `refresh()`。
因子项目支持填写研究参数后保存版本。构建中断时可点击“取消构建”解除保存锁定；
调度服务明确未接收任务时可点击“重试提交”，复用该版本的构建产物。
提交响应丢失时通过“核实提交”查找已有任务，不重复提交工作流。发布接口尚未接入。

开发环境需要 Node.js、npm、Python 及 `jupyterlab>=4.6,<5`，在 `extension/` 执行：

```bash
npm ci
npm run build
python -m pip install --no-deps .
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

启动时使用 root 修正挂载目录归属，然后由镜像切换为 `jovyan`（UID 1000、GID 100）。`start-jupyter.sh` 检查中文语言包已安装，再调用镜像原有的 `start-notebook.py` 运行 Jupyter。

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
构建时约束已有 Python 包的版本。

刷新 JupyterLab 后，点击侧栏的 DolphinDB 连接按钮，或在命令面板运行
`DolphinDB: 管理连接`。通过该面板填写连接地址、端口、账号和密码。
可从启动器新建 DOS 文件；Python Notebook 支持 `%ddb` / `%%ddb`。

连接配置默认保存到 `/home/jovyan/.jupyter/dolphindb-extension/connections.json`，
对应宿主机 `config/dolphindb-extension/`，随现有配置目录持久化。

项目说明：[dolphindb-extension 0.1.0b3](https://pypi.org/project/dolphindb-extension/0.1.0b3/)。

### 系统凭据库

容器内使用 GNOME Keyring 的 Secret Service。启动脚本在同一个 D-Bus 会话中解锁凭据库并启动 Jupyter；
在 DolphinDB 连接编辑界面输入密码、勾选“记住密码”并保存后，密码会写入加密凭据库。

`keyring-data/` 持久化加密凭据，`secrets/jupyter-keyring-password` 保存自动解锁密钥，
通过 Compose secret 只读挂载到容器。解锁密钥不进入镜像，也不通过命令行参数或环境变量传递。
这两个路径位于 Notebook 文件根目录之外，权限分别为 `0700` 和 `0600`；应限制宿主机上的读取权限。
备份时需要保留解锁密钥和加密凭据，并分别妥善保存；直接替换密钥会导致已有凭据无法解锁。

重建会沿用现有密钥和凭据文件。未勾选“记住密码”的连接仍使用临时密码。
参考：[keyring 的无桌面 Linux 配置](https://keyring.readthedocs.io/en/latest/#using-keyring-on-headless-linux-systems)。

## uv

镜像通过[官方 PyPI 安装方式](https://docs.astral.sh/uv/getting-started/installation/#pypi)预置 uv 0.12.10 和 uvx，命令位于 `/opt/conda/bin`。版本固定在 `Dockerfile` 中，容器重建后仍可使用。

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

镜像预置 Jupyter AI 3.2.0、Codex CLI 0.153.4 和 `@agentclientprotocol/codex-acp` 1.10.0，在 JupyterLab 的 Chat 界面中使用 Codex 读取和编辑文件、执行命令以及处理 Notebook。

Python 新增依赖固定在 `requirements-ai.txt`；构建时约束已有包的版本。Codex 的 npm 依赖固定在 `codex/package.json` 和 `codex/package-lock.json`，使用 Node.js 24.20.0，运行组件随镜像安装。

首次使用时，打开 JupyterLab 的终端，执行：

```bash
codex login --device-auth
```

按照终端提示在浏览器完成登录，再从启动器打开 Chat。新聊天默认选择 Codex，此设置保存在 `config/jupyter_server_config.json` 的 `PersonaManager.default_persona_id` 中，也可在输入框的代理选择菜单中选择 Codex。设备登录若不可用，请检查 ChatGPT 账户的安全设置是否允许设备代码登录。

Codex 的配置、登录信息和会话保存在 Docker 命名卷 `codex-home`，挂载至 `/home/jovyan/.codex`，重建容器后保留。Jupyter AI 的聊天记录以文件形式保存在工作目录中。普通 `docker compose down` 保留命名卷，`docker compose down -v` 会删除其中的 Codex 数据。

使用说明：[Jupyter AI](https://jupyter-ai.readthedocs.io/en/latest/getting-started.html)、[Codex 登录](https://learn.chatgpt.com/docs/auth)。

## Python 代码检查

镜像通过 `requirements-code-check.txt` 固定 `jupyterlab-lsp`、`jupyter-lsp`、`python-lsp-server`、`python-lsp-ruff` 和 `ruff`。检查设置保存在 `config/lab/user-settings/@jupyter-lsp/jupyterlab-lsp/plugin.jupyterlab-settings`，优先使用 `pylsp`，由 Ruff 提供检查并关闭重复的检查器。

首次安装语言服务器后，需要重启 Jupyter 服务并刷新浏览器，才能在 Notebook 和 Python 文件中看到检查提示。重启服务会结束当前内核并清空内存变量，请先保存工作。后续可在“设置 → 设置编辑器 → Language Server”中调整检查配置。

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
