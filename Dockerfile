ARG NOTEBOOK_IMAGE=quay.io/jupyter/scipy-notebook:latest
FROM ${NOTEBOOK_IMAGE} AS notebook-node

ENV PATH="/usr/local/bin:${PATH}" \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    npm_config_registry=https://registry.npmmirror.com \
    npm_config_prefix=/usr/local \
    CODEX_PATH=/usr/local/bin/codex \
    UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
    UV_PYTHON_INSTALL_MIRROR=https://registry.npmmirror.com/-/binary/python-build-standalone
USER root

RUN sed -i -e 's|http://archive.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' \
        -e 's|http://security.ubuntu.com/ubuntu|https://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        fonts-noto-cjk \
        pandoc \
        texlive-fonts-recommended \
        texlive-lang-chinese \
        texlive-plain-generic \
        texlive-xetex \
    && rm -rf /var/lib/apt/lists/*

RUN rm -f /usr/local/bin/node /usr/local/bin/npm /usr/local/bin/npx \
    && curl --fail --show-error --location --retry 3 https://deb.nodesource.com/setup_lts.x | bash - \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && node --version && npm --version && npx --version

FROM notebook-node AS solo-extension
USER root
WORKDIR /opt/solo-extension
COPY extension/package.json extension/package-lock.json extension/.npmrc ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY extension/ ./
RUN npm run build -- --core-package-file "$(python -c 'from pathlib import Path; import jupyterlab; print(Path(jupyterlab.__file__).parent / "staging" / "package.json")')" \
    && python -m pip wheel --no-cache-dir --no-deps --index-url https://pypi.tuna.tsinghua.edu.cn/simple --wheel-dir /tmp/solo-wheel .

FROM notebook-node
USER root
RUN rm -f /usr/local/bin/codex /usr/local/bin/codex-acp \
    && npm install --global --no-audit --no-fund \
        @openai/codex@latest \
        @agentclientprotocol/codex-acp@latest \
    && install -d -o "${NB_UID}" -g "${NB_GID}" -m 700 "/home/${NB_USER}/.codex" \
    && node --version && npm --version && npx --version \
    && codex --version && codex-acp --version

USER ${NB_UID}

COPY requirements-code-check.txt /tmp/requirements-code-check.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements-code-check.txt \
    && python -m pip check

COPY requirements-ai.txt /tmp/requirements-ai.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements-ai.txt \
    && python -m pip check

COPY scripts/patch_mcp_uuid.py /tmp/patch_mcp_uuid.py
RUN python -B /tmp/patch_mcp_uuid.py

RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: uv==0.12.10 \
    && uv --version \
    && uvx --version \
    && python -m pip check

COPY requirements-dolphindb.txt /tmp/requirements-dolphindb.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --requirement /tmp/requirements-dolphindb.txt \
    && python -m pip check \
    && python -B -c 'import dolphindb, dolphindb_extension; print("DolphinDB extension: " + dolphindb_extension.__version__)'

USER root
RUN printf '%s\n' \
        'deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu noble main' \
        'deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu noble-updates main' \
        'deb https://mirrors.tuna.tsinghua.edu.cn/ubuntu noble-security main' \
        > /tmp/keyring-apt.list \
    && apt-get -o Dir::Etc::sourcelist=/tmp/keyring-apt.list -o Dir::Etc::sourceparts=- \
        -o Acquire::https::Timeout=20 -o Acquire::Retries=1 update \
    && DEBIAN_FRONTEND=noninteractive apt-get \
        -o Dir::Etc::sourcelist=/tmp/keyring-apt.list -o Dir::Etc::sourceparts=- \
        -o Acquire::https::Timeout=20 -o Acquire::Retries=1 \
        install -y --no-install-recommends dbus-daemon dbus-x11 gnome-keyring \
    && rm -f /tmp/keyring-apt.list \
    && rm -rf /var/lib/apt/lists/*

RUN rm -f /usr/local/bin/start-jupyter.sh /usr/local/bin/with-keyring.sh
USER ${NB_UID}

RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: jupyterlab-execute-time==3.3.0 \
    && python -m pip check

RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: lckr-jupyterlab-variableinspector==3.2.4 \
    && python -m pip check

COPY --from=solo-extension /tmp/solo-wheel /tmp/solo-wheel
RUN python -m pip install --no-cache-dir --index-url https://pypi.tuna.tsinghua.edu.cn/simple 'tomlkit>=0.13,<1' \
    && python -m pip install --no-cache-dir --no-deps /tmp/solo-wheel/*.whl \
    && python -m pip check

COPY uv.toml /etc/uv/uv.toml
COPY solo_report_bridge.py /etc/ipython/solo_report_bridge.py
COPY codex/skills/ /etc/codex/skills/
COPY scripts/check_codex_skills.py /opt/jupyter-codex/check_skills.py
RUN python -B /opt/jupyter-codex/check_skills.py --cwd /tmp
