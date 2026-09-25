"""只读当前文件所属项目；不开放隐藏文件的通用访问。"""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from jupyter_server.base.handlers import APIHandler
from tornado import web

KINDS = {"factor", "model", "optimize", "control", "execution"}


def read_project(root: Path, path: str) -> dict[str, Any] | None:
    root = root.resolve()
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or "\\" in path or ":" in path:
        raise web.HTTPError(400, reason="Invalid project path")
    current = (root / relative).resolve()
    if not current.is_relative_to(root):
        raise web.HTTPError(403, reason="Path is outside the Jupyter workspace")
    if not current.exists():
        raise web.HTTPError(404, reason="File or directory does not exist")
    if current.is_file():
        current = current.parent
    while True:
        marker = current / ".solo"
        if marker.exists():
            if not marker.resolve().is_relative_to(root):
                raise web.HTTPError(403, reason="Project metadata is outside the workspace")
            try:
                if marker.stat().st_size > 65536:
                    raise ValueError("Metadata too large")
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                UUID(metadata["project_id"])
                if not isinstance(metadata["name"], str) or not metadata["name"].strip():
                    raise ValueError("Invalid name")
                if metadata["kind"] not in KINDS:
                    raise ValueError("Invalid kind")
                fields = ("scheme_version", "scheme_commit", "algo_version", "algo_commit")
                versions = {field: metadata.get(field) for field in fields}
                if any(value is not None and not isinstance(value, str) for value in versions.values()):
                    raise ValueError("Invalid version")
            except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
                raise web.HTTPError(422, reason="Invalid .solo project metadata") from error
            return {
                "path": "" if current == root else current.relative_to(root).as_posix(),
                "project_id": metadata["project_id"],
                "name": metadata["name"],
                "kind": metadata["kind"],
                **versions,
            }
        if current == root:
            return None
        current = current.parent


class ProjectHandler(APIHandler):
    @web.authenticated
    def get(self) -> None:
        root = Path(self.settings["server_root_dir"])
        project = read_project(root, self.get_query_argument("path", ""))
        self.finish({"project": project})
