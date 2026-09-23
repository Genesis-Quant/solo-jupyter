ARG NOTEBOOK_IMAGE=quay.io/jupyter/scipy-notebook:latest
FROM node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS codex-runtime

WORKDIR /opt/jupyter-codex
COPY codex/package.json codex/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

FROM ${NOTEBOOK_IMAGE}

USER root

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        pandoc \
        texlive-fonts-recommended \
        texlive-lang-chinese \
        texlive-plain-generic \
        texlive-xetex \
    && rm -rf /var/lib/apt/lists/*

USER ${NB_UID}

COPY requirements-code-check.txt /tmp/requirements-code-check.txt
RUN python -m pip install --no-cache-dir --requirement /tmp/requirements-code-check.txt \
    && python -m pip check

COPY requirements-ai.txt /tmp/requirements-ai.txt
RUN python -B -c 'import importlib.metadata as m; print("\n".join(sorted(d.metadata["Name"] + "==" + d.version for d in m.distributions())))' > /tmp/requirements-existing.txt \
    && python -m pip install --no-cache-dir \
        --constraint /tmp/requirements-existing.txt \
        --requirement /tmp/requirements-ai.txt \
    && python -m pip check \
    && rm /tmp/requirements-existing.txt

COPY --from=codex-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=codex-runtime /opt/jupyter-codex /opt/jupyter-codex

USER root
RUN ln -sf /opt/jupyter-codex/node_modules/.bin/codex /usr/local/bin/codex \
    && ln -sf /opt/jupyter-codex/node_modules/.bin/codex-acp /usr/local/bin/codex-acp \
    && install -d -o "${NB_UID}" -g "${NB_GID}" -m 700 "/home/${NB_USER}/.codex"

USER ${NB_UID}
RUN node --version && codex --version && codex-acp --version

COPY --chmod=755 start-jupyter.sh /usr/local/bin/start-jupyter.sh

RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: uv==0.12.10 \
    && uv --version \
    && uvx --version \
    && python -m pip check

COPY requirements-dolphindb.txt /tmp/requirements-dolphindb.txt
RUN python -B -c 'import importlib.metadata as m; print("\n".join(sorted(d.metadata["Name"] + "==" + d.version for d in m.distributions())))' > /tmp/requirements-existing.txt \
    && python -m pip install --no-cache-dir --only-binary=:all: \
        --constraint /tmp/requirements-existing.txt \
        --requirement /tmp/requirements-dolphindb.txt \
    && python -m pip check \
    && python -B -c 'import dolphindb, dolphindb_extension; print("DolphinDB extension: " + dolphindb_extension.__version__)' \
    && rm /tmp/requirements-existing.txt

USER root
RUN printf '%s\n' \
        'deb https://archive.ubuntu.com/ubuntu noble main' \
        'deb https://archive.ubuntu.com/ubuntu noble-updates main' \
        'deb https://security.ubuntu.com/ubuntu noble-security main' \
        > /tmp/keyring-apt.list \
    && apt-get -o Dir::Etc::sourcelist=/tmp/keyring-apt.list -o Dir::Etc::sourceparts=- \
        -o Acquire::https::Timeout=20 -o Acquire::Retries=1 update \
    && DEBIAN_FRONTEND=noninteractive apt-get \
        -o Dir::Etc::sourcelist=/tmp/keyring-apt.list -o Dir::Etc::sourceparts=- \
        -o Acquire::https::Timeout=20 -o Acquire::Retries=1 \
        install -y --no-install-recommends dbus-daemon dbus-x11 gnome-keyring \
    && rm -f /tmp/keyring-apt.list \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=755 with-keyring.sh /usr/local/bin/with-keyring.sh
USER ${NB_UID}

RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: jupyterlab-execute-time==3.3.0 \
    && python -m pip check

RUN python -m pip install --no-cache-dir --no-deps --only-binary=:all: lckr-jupyterlab-variableinspector==3.2.4 \
    && python -m pip check
