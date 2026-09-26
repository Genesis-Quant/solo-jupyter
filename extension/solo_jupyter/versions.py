"""Jupyter 保存入口：冻结源码、构建候选 wheel，再提交正式任务。"""
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID
from urllib.parse import unquote, urlparse

import tomlkit
import tomllib
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError

from .dependencies import DependenciesHandler

_tasks: set[asyncio.Task] = set()


async def backend(path: str, body: dict | None = None) -> dict | list:
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(
            url + "/api/v1/projects/" + path,
            method="GET" if body is None else "POST",
            body=None if body is None else json.dumps(body),
            headers={"Content-Type": "application/json"}, request_timeout=90,
        )
        return json.loads(response.body)
    except HTTPClientError as error:
        message = "无法访问 Solo Backend"
        if error.response:
            message = json.loads(error.response.body).get("detail", message)
        raise web.HTTPError(error.code if error.code < 600 else 502, reason=str(message)) from error


def command(arguments: list[str], directory: Path, payload: dict | None = None) -> str:
    environment = {**os.environ, "UV_CACHE_DIR": "/tmp/solo-uv-cache", "UV_LINK_MODE": "copy", "GIT_TERMINAL_PROMPT": "0"}
    for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(name, None)
    result = subprocess.run(arguments, cwd=directory, env=environment,
                            input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=600, check=False)
    if result.returncode:
        message = (result.stderr or result.stdout)[-6000:]
        for key, value in os.environ.items():
            if value and any(word in key.upper() for word in ("TOKEN", "PASSWORD", "SECRET")):
                message = message.replace(value, "[REDACTED]")
        raise ValueError(message)
    return result.stdout


def parameters(directory: Path, values: dict | None = None, source: Path | None = None) -> dict:
    arguments = [str(directory / ".venv/bin/scheme"), "parameters", "--project", str(source or directory)]
    if values is not None:
        arguments.append("--validate")
    output = command(arguments, directory, values)
    return json.loads(output.splitlines()[-1])


def copy_source(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in {".venv", ".git", ".ipynb_checkpoints", "__pycache__", "dist", "build", ".pytest_cache", ".ruff_cache"}}

    # 冻结内容不跟随指向项目外部的符号链接。
    for directory, folders, files in os.walk(source):
        folders[:] = [name for name in folders if name not in ignore(directory, folders)]
        for name in folders + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ValueError(f"源码包含符号链接，请先移除：{path.relative_to(source)}")
    shutil.copytree(source, destination, ignore=ignore)


def build_version(directory: Path, version: dict, values: dict) -> None:
    target = Path(os.environ.get("SOLO_SHARED_DIR", "/shared")) / "runs" / version["id"]
    target.mkdir(exist_ok=False)
    source = target / "source"
    copy_source(directory, source)
    config = tomlkit.parse((source / "pyproject.toml").read_text())
    config["project"]["version"] = version["packageVersion"]
    (source / "pyproject.toml").write_text(tomlkit.dumps(config))
    parameters_result = parameters(directory, values, source)
    wheels = target / "wheels"
    wheels.mkdir()
    command(["uv", "build", "--wheel", "--out-dir", str(wheels), str(source)], target)
    wheel = next(wheels.glob("*.whl"))
    package = config["project"]["name"]
    # 沿用工作区的确切 Git commit/版本；所有目录依赖冻结为候选 wheel。
    lock = tomllib.loads((source / "uv.lock").read_text())
    sources = {package: {"path": str(wheel)}}
    constraints = []
    for item in lock["package"]:
        if item["name"] == package:
            continue
        constraints.append(f'{item["name"]}=={item["version"]}')
        location = item["source"]
        if "git" in location:
            url, commit = location["git"].rsplit("#", 1)
            sources[item["name"]] = {"git": url.split("?", 1)[0], "rev": commit}
            from urllib.parse import parse_qs
            subdirectory = parse_qs(urlparse(url).query).get("subdirectory")
            if subdirectory:
                sources[item["name"]]["subdirectory"] = subdirectory[0]
        elif "directory" in location or "editable" in location:
            dependency = (directory / location.get("directory", location.get("editable"))).resolve()
            if not dependency.is_relative_to(Path("/shared/projects")):
                raise ValueError("本地源码依赖必须位于 /shared/projects")
            frozen = target / "dependencies" / item["name"]
            copy_source(dependency, frozen)
            destination = wheels / item["name"]
            command(["uv", "build", "--wheel", "--out-dir", str(destination), str(frozen)], target)
            sources[item["name"]] = {"path": str(next(destination.glob("*.whl")))}
        elif "path" in location:
            original = (directory / location["path"]).resolve()
            destination = wheels / original.name
            shutil.copy2(original, destination)
            sources[item["name"]] = {"path": str(destination)}
    # 若开发环境安装的是候选 Scheme wheel，保存相同安装内容，不能退回旧 Git 源。
    installed = json.loads(command([str(directory / ".venv/bin/python"), "-c",
        "import importlib.metadata as m,json; d=m.distribution('scheme'); print(json.dumps({'version':d.version,'direct':json.loads(d.read_text('direct_url.json') or '{}')}))"], directory))
    direct = installed["direct"].get("url", "")
    if direct.startswith("file:") and direct.endswith(".whl"):
        original = Path(unquote(urlparse(direct).path))
        destination = wheels / original.name
        shutil.copy2(original, destination)
        sources["scheme"] = {"path": str(destination)}
    environment = target / "environment"
    environment.mkdir()
    # 研究任务显式安装选中的 Algo；不要求当前交付 wheel 依赖上游研究包。
    research_dependencies = [f"{package}=={version['packageVersion']}"]
    research_dependencies.extend(
        f"{upstream['package']}=={upstream['version']}"
        for upstream in parameters_result.get("upstream", {}).values()
    )
    env_config = {
        "project": {"name": "research-environment", "version": "0.0.0", "requires-python": config["project"]["requires-python"], "dependencies": research_dependencies},
        "tool": {"uv": {"package": False, "sources": sources, "constraint-dependencies": constraints}},
    }
    (environment / "pyproject.toml").write_text(tomlkit.dumps(env_config))
    command(["uv", "lock", "--project", str(environment)], target)
    frozen_lock = tomllib.loads((environment / "uv.lock").read_text())
    if any({"directory", "editable"} & p["source"].keys() for p in frozen_lock["package"]):
        raise ValueError("正式环境仍包含未冻结的源码依赖")
    component = {"package": package, "version": version["packageVersion"], "wheel": str(wheel),
                 "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "entry": parameters_result["entry"]}
    data = {"environment": {"lockfile": str(environment / "uv.lock")}, "output": str(target / "report")}
    if "factor" in parameters_result:
        data.update(kind="factor", factor={**component, "params": parameters_result["factor"]},
                    analysis=parameters_result["analysis"])
    else:
        components = {parameters_result["project_kind"]: component}
        for kind, upstream in parameters_result.get("upstream", {}).items():
            location = sources.get(upstream["package"], {})
            if "path" not in location:
                raise ValueError(f"上游 {upstream['package']} 缺少冻结 wheel，请通过插件安装项目后重试")
            upstream_wheel = Path(location["path"])
            components[kind] = {**upstream, "wheel": str(upstream_wheel),
                                "sha256": hashlib.sha256(upstream_wheel.read_bytes()).hexdigest()}
        data.update(kind=parameters_result["project_kind"], algos=components, backtest=parameters_result["backtest"])
    (target / "input.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    (target / "build.json").write_text(json.dumps({"scheme_version": installed["version"]}))


async def save(directory: Path, project: dict, version: dict, values: dict) -> None:
    endpoint = f'{project["project_id"]}/versions/{version["id"]}/submit'
    try:
        await asyncio.to_thread(build_version, directory, version, values)
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
        await backend(endpoint, {"error": str(error)[-10000:]})
        return
    await backend(endpoint, {})


class VersionsHandler(DependenciesHandler):
    @web.authenticated
    async def get(self) -> None:
        project = self.current_project(self.get_query_argument("path", ""))
        if self.get_query_argument("parameters", "") == "1":
            directory = Path(self.settings["server_root_dir"]) / project["path"]
            try:
                self.finish(await asyncio.to_thread(parameters, directory))
            except (ValueError, OSError) as error:
                raise web.HTTPError(422, reason=str(error)) from error
        else:
            self.finish({"versions": await backend(f'{project["project_id"]}/versions')})

    @web.authenticated
    async def post(self) -> None:
        body = self.get_json_body()
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            raise web.HTTPError(400, reason="缺少当前项目路径")
        project = self.current_project(body["path"])
        action = body.get("action", "save")
        if action in {"cancel", "retry"}:
            try:
                version_id = UUID(body.get("version_id", ""))
            except (ValueError, TypeError, AttributeError) as error:
                raise web.HTTPError(400, reason="版本 ID 无效") from error
            endpoint = "cancel" if action == "cancel" else "submit"
            self.finish(await backend(f'{project["project_id"]}/versions/{version_id}/{endpoint}', {}))
            return
        if action != "save" or not isinstance(body.get("parameters"), dict):
            raise web.HTTPError(400, reason="请先填写研究参数")
        directory = Path(self.settings["server_root_dir"]) / project["path"]
        try:
            await asyncio.to_thread(parameters, directory, body["parameters"])
        except (ValueError, OSError) as error:
            raise web.HTTPError(422, reason=str(error)) from error
        if body.get("validate_only"):
            self.finish({"valid": True})
            return
        version = await backend(f'{project["project_id"]}/versions', {"note": body.get("note", "")})
        task = asyncio.create_task(save(directory, project, version, body["parameters"]))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        self.set_status(202)
        self.finish(version)
