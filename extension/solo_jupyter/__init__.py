"""Solo 的 Jupyter Server 与预构建 JupyterLab 扩展。"""

from pathlib import Path
from typing import Any

from jupyter_server.serverapp import ServerApp
from jupyter_server.utils import url_path_join

from .dependencies import DependenciesHandler, ProjectOperationsHandler, RecoveringProjectHandler, start_installation_recovery
from .versions import VersionsHandler


def _jupyter_server_extension_points() -> list[dict[str, str]]:
    return [{"module": "solo_jupyter"}]


def _load_jupyter_server_extension(server_app: ServerApp) -> None:
    web_app: Any = server_app.web_app
    route = url_path_join(web_app.settings["base_url"], "solo", "project")
    web_app.add_handlers(".*$", [
        (route, RecoveringProjectHandler),
        (route + "/dependencies", DependenciesHandler),
        (route + "/versions", VersionsHandler),
        (route + "/operations", ProjectOperationsHandler),
    ])
    if "solo_installation_recovery" not in web_app.settings:
        web_app.settings["solo_installation_recovery"] = start_installation_recovery(Path(server_app.root_dir))
