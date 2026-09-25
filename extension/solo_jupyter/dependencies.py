"""从后端项目目录选择本地包，用 uv 安装到当前项目。"""

import asyncio
import json
import os
from pathlib import Path
import tomllib
from typing import Any

from jupyter_server.base.handlers import APIHandler
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError

from .handlers import read_project

PROJECT_KINDS = ("factor", "model", "optimize", "control", "execution")
_installing: set[str] = set()


async def project_catalog() -> list[dict[str, Any]]:
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(url + "/api/v1/projects", request_timeout=30)
        return json.loads(response.body)
    except (HTTPClientError, ValueError) as error:
        raise web.HTTPError(502, reason="无法读取 Solo 项目列表") from error


def compatible(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if candidate.get("archived") or candidate["id"] == current["project_id"]:
        return False
    if candidate["kind"] not in PROJECT_KINDS[: PROJECT_KINDS.index(current["kind"]) + 1]:
        return False
    try:
        return Version(current["scheme_version"]).major == Version(candidate["schemeVersion"]).major
    except (InvalidVersion, TypeError):
        return False


def project_package(root: Path, project: dict[str, Any]) -> tuple[Path, str]:
    directory = (root / project["path"]).resolve()
    path = directory / "pyproject.toml"
    if not path.resolve().is_relative_to(directory):
        raise web.HTTPError(403, reason="包配置不在项目目录内")
    try:
        name = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["name"]
        return directory, canonicalize_name(name, validate=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise web.HTTPError(422, reason="项目缺少有效的 Python 包配置") from error


async def install_project(root: Path, current: dict[str, Any], candidate: dict[str, Any]) -> str:
    if not compatible(current, candidate):
        raise web.HTTPError(422, reason="仅可安装同 Scheme 大版本的同类或上游项目")
    source = read_project(root, candidate["directory"])
    if (source is None or source["project_id"] != candidate["id"]
            or source["path"] != candidate["directory"]):
        raise web.HTTPError(409, reason="待安装项目目录与项目记录不一致")
    directory, name = project_package(root, current)
    source_directory, package = project_package(root, source)
    if name == package:
        raise web.HTTPError(422, reason="不能安装与当前项目同包名的项目")
    if not (directory / ".venv" / "bin" / "python").exists():
        raise web.HTTPError(409, reason="当前项目环境尚未创建")
    key = str(directory)
    if key in _installing:
        raise web.HTTPError(409, reason="当前项目正在安装依赖")
    _installing.add(key)
    snapshots: dict[Path, bytes | None] = {}
    process = None
    try:
        for file in ("pyproject.toml", "uv.lock"):
            path = directory / file
            if not path.resolve().is_relative_to(directory):
                raise web.HTTPError(403, reason="依赖文件不在项目目录内")
            snapshots[path] = path.read_bytes() if path.exists() else None
        env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(directory / ".venv"),
            "UV_CACHE_DIR": "/tmp/solo-uv-cache",
            "UV_PYTHON_INSTALL_DIR": str(Path.home() / ".python"),
            "UV_LINK_MODE": "copy",
            "GIT_TERMINAL_PROMPT": "0",
        }
        process = await asyncio.create_subprocess_exec(
            "uv", "add", "--project", str(directory), "--no-workspace",
            "--upgrade-package", package, "--reinstall-package", package,
            "--", str(source_directory),
            cwd=directory, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=600)
        if process.returncode:
            message = output.decode(errors="replace")[-3000:]
            for key, value in os.environ.items():
                if value and any(part in key.upper() for part in ("TOKEN", "PASSWORD", "SECRET")):
                    message = message.replace(value, "[REDACTED]")
            raise web.HTTPError(422, reason="uv 安装失败：\n" + message)
        return package
    except BaseException as error:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        for path, content in snapshots.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        if isinstance(error, TimeoutError):
            raise web.HTTPError(504, reason="uv 安装超时，请检查依赖下载网络") from error
        raise
    finally:
        _installing.discard(str(directory))


class DependenciesHandler(APIHandler):
    def current_project(self, path: str) -> dict[str, Any]:
        project = read_project(Path(self.settings["server_root_dir"]), path)
        if project is None:
            raise web.HTTPError(404, reason="当前目录不是 Solo 项目")
        return project

    @web.authenticated
    async def get(self) -> None:
        current = self.current_project(self.get_query_argument("path", ""))
        candidates = [project for project in await project_catalog() if compatible(current, project)]
        self.finish({"projects": candidates})

    @web.authenticated
    async def post(self) -> None:
        body = self.get_json_body()
        if not isinstance(body, dict) or not isinstance(body.get("path"), str) or not isinstance(body.get("project_id"), str):
            raise web.HTTPError(400, reason="缺少当前项目路径或待安装项目 ID")
        current = self.current_project(body["path"])
        candidate = next((project for project in await project_catalog() if project["id"] == body["project_id"]), None)
        if candidate is None:
            raise web.HTTPError(404, reason="待安装项目不存在或已归档")
        package = await install_project(Path(self.settings["server_root_dir"]).resolve(), current, candidate)
        self.finish({"package": package})
