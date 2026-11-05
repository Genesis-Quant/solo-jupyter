"""将容器中的研究环境传给SSH会话，不依赖交互式shell启动文件。"""

import os
import tempfile
from pathlib import Path

ENVIRONMENT_VARIABLES = (
    "HOME", "PATH", "LANG", "LC_ALL", "TZ",
    "TUSHARE_TOKEN", "DATA_START_DATE", "INDEX_CODES",
    "DOLPHINDB_ENDPOINTS", "DOLPHINDB_USERNAME", "DOLPHINDB_PASSWORD",
    "DOLPHINDB_WORKER_USERNAME", "DOLPHINDB_WORKER_PASSWORD",
    "DOLPHIN_HOST", "DOLPHIN_PORT", "DOLPHIN_RUNTIME_USERNAME", "DOLPHIN_RUNTIME_PASSWORD",
    "DOLPHIN_WORKER_USERNAME", "DOLPHIN_WORKER_PASSWORD",
    "DOLPHIN_CORE_DATABASE", "DOLPHIN_CORE_TABLE", "DOLPHIN_BARRA_DATABASE",
    "DOLPHIN_BARRA_TABLE", "DOLPHIN_BARRA_COV_TABLE", "DOLPHIN_BARRA_SP_TABLE",
    "DOLPHIN_SNAPSHOT_DATABASE", "DOLPHIN_SNAPSHOT_TABLE",
    "SOLO_BACKEND_URL", "SOLO_SHARED_DIR", "JUPYTER_PATH", "JP_LSP_VIRTUAL_DIR",
    "PIP_INDEX_URL", "UV_DEFAULT_INDEX", "UV_PYTHON_INSTALL_MIRROR", "UV_LINK_MODE",
    "npm_config_registry", "GITEE_TOKEN", "GITEE_USERNAME", "GIT_TERMINAL_PROMPT",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
)


def write_environment(path: Path, environment: dict[str, str]) -> None:
    """生成仅root可读的sshd配置，密码和令牌不进入镜像、命令参数或日志。"""
    assignments = []
    for name in ENVIRONMENT_VARIABLES:
        if name not in environment:
            continue
        value = environment[name]
        if any(character in value for character in ("\r", "\n", "\0")):
            raise ValueError(f"SSH环境变量{name}不能包含换行或NUL")
        value = value.replace("\\", "\\\\").replace('"', '\\"')
        assignments.append(f'"{name}={value}"')
    descriptor, temporary = tempfile.mkstemp(prefix=".ssh-environment-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            # sshd仅采用首条SetEnv，所有变量必须写在同一条指令中。
            if assignments:
                stream.write("SetEnv " + " ".join(assignments) + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    write_environment(Path("/run/sshd/solo-environment.conf"), dict(os.environ))
