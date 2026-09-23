#!/usr/bin/env bash
set -euo pipefail

# Fail early if a rebuild omitted the package required by the saved zh_CN locale.
python -B -c 'from importlib.metadata import version; print("Chinese language pack: " + version("jupyterlab-language-pack-zh-CN"), flush=True)'

exec dbus-run-session -- /usr/local/bin/with-keyring.sh /usr/local/bin/start-notebook.py "$@"
