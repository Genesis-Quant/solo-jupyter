"""只读当前文件所属项目；不开放隐藏文件的通用访问。"""

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from jupyter_server.base.handlers import APIHandler
from packaging.utils import canonicalize_name
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
                package_name = metadata.get("package_name")
                if package_name is not None:
                    if not isinstance(package_name, str):
                        raise ValueError("Invalid package name")
                    package_name = canonicalize_name(package_name, validate=True)
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
                "package_name": package_name,
                **versions,
            }
        if current == root:
            return None
        current = current.parent


def environment_project(root: Path, body: Any) -> tuple[Path, dict[str, Any], str]:
    """Only operate on an owned project root, or its direct upgrade staging directory."""
    if not isinstance(body, dict) or not isinstance(body.get("path"), str):
        raise web.HTTPError(400, reason="Invalid environment request")
    raw = body["path"]
    relative = Path(raw)
    mode = body.get("mode", "prepare")
    if (mode not in ("prepare", "sync") or type(body.get("locked", False)) is not bool
            or relative.is_absolute() or ".." in relative.parts or "\\" in raw or ":" in raw
            or relative.as_posix() != raw):
        raise web.HTTPError(400, reason="Invalid project environment path or mode")
    parts = relative.parts
    if (len(parts) not in (3, 4) or parts[0] != "projects" or parts[1] not in KINDS
            or (len(parts) == 4 and (mode != "sync" or not (parts[3] == ".stage" or parts[3].startswith(".stage-"))))):
        raise web.HTTPError(400, reason="Environment must belong to a project")
    root = root.resolve()
    project_path = Path(*parts[:3]).as_posix()
    project = read_project(root, project_path)
    if (project is None or project["path"] != project_path
            or project["kind"] != parts[1] or project["name"] != parts[2]
            or project["project_id"] != body.get("project_id") or project["name"] != body.get("name")):
        raise web.HTTPError(409, reason="Project identity does not match .solo")
    current = read_project(root, raw)
    if (current is None or any(current[key] != project[key] for key in ("project_id", "name", "kind"))):
        raise web.HTTPError(409, reason="Staging directory belongs to another project")
    directory = (root / relative).resolve()
    if not directory.is_dir() or directory != root / relative:
        raise web.HTTPError(403, reason="Project environment path is redirected")
    for name in (".solo", "pyproject.toml", "uv.lock", ".venv", "research.ipynb"):
        path = directory / name
        if path.is_symlink() or not path.resolve().is_relative_to(directory):
            raise web.HTTPError(403, reason="Project environment file is redirected")
    return directory, project, mode


async def install_project_environment(root: Path, body: Any) -> dict[str, str]:
    # Lazy imports avoid the existing dependencies -> handlers reader import cycle.
    from .dependencies import _python, _redact, _run, project_operation
    from .uv import uv_environment

    directory, project, mode = environment_project(root, body)
    environment = {**os.environ, **uv_environment(directory, shared_root=root), "GIT_TERMINAL_PROMPT": "0"}
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "UV_PROJECT", "UV_WORKING_DIR"):
        environment.pop(key, None)
    with project_operation(project["project_id"]):
        try:
            await _run(["uv", "sync", "--project", str(directory), "--python", "3.12", "--managed-python",
                        *(["--locked"] if body.get("locked", False) else [])], directory, environment)
            if mode == "prepare":
                python = _python(directory)
                await asyncio.wait_for(_run([str(python), "-c", "import ipykernel"], directory, environment), timeout=30)
                kernel_name = f"solo-{project['project_id']}"
                kernel = root.resolve() / ".jupyter" / "kernels" / kernel_name
                if kernel.is_symlink() or not kernel.resolve().is_relative_to(root.resolve()):
                    raise web.HTTPError(403, reason="Kernel directory is outside the workspace")
                kernel.mkdir(parents=True, exist_ok=True)
                if (kernel / "kernel.json").is_symlink():
                    raise web.HTTPError(403, reason="Kernel specification is redirected")
                (kernel / "kernel.json").write_text(json.dumps({
                    "argv": [str(python), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                    "display_name": project["name"], "language": "python",
                    "env": {"UV_PROJECT_ENVIRONMENT": str(directory / ".venv")},
                    "metadata": {"solo": {"project_id": project["project_id"], "path": project["path"]}},
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                notebook_path = directory / "research.ipynb"
                notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
                notebook.setdefault("metadata", {})["kernelspec"] = {
                    "name": kernel_name, "display_name": project["name"], "language": "python",
                }
                notebook_path.write_text(json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8")
        except (asyncio.TimeoutError, OSError, ValueError) as error:
            raise web.HTTPError(502, reason="Project environment installation failed: " + _redact(str(error))) from None
    return {"project_id": project["project_id"], "mode": mode}


class ProjectEnvironmentHandler(APIHandler):
    @web.authenticated
    async def post(self) -> None:
        self.finish(await install_project_environment(Path(self.settings["server_root_dir"]), self.get_json_body()))


class ProjectHandler(APIHandler):
    @web.authenticated
    def get(self) -> None:
        root = Path(self.settings["server_root_dir"])
        project = read_project(root, self.get_query_argument("path", ""))
        self.finish({"project": project})
