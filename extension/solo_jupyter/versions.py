"""Jupyter 保存入口：冻结源码、构建候选 wheel，再提交正式任务。"""
import asyncio
import json
import os
import shutil
import subprocess
import tomllib
from copy import deepcopy
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID

import tomlkit
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError

from .dependencies import DependenciesHandler, check_project_release, project_operation
from .uv import uv_environment

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
            try:
                detail = json.loads(error.response.body).get("detail", message)
                message = detail.get("reason", message) if isinstance(detail, dict) else detail
            except (ValueError, AttributeError):
                pass
        raise web.HTTPError(error.code if error.code < 600 else 502, reason=str(message)) from error


def command(
    arguments: list[str], directory: Path, payload: dict | None = None, *, timeout: float | None = 600,
) -> str:
    environment = {**os.environ, **uv_environment(directory), "GIT_TERMINAL_PROMPT": "0"}
    for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(name, None)
    result = subprocess.run(arguments, cwd=directory, env=environment,
                            input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, timeout=timeout, check=False)
    if result.returncode:
        message = (result.stderr or result.stdout)[-6000:]
        for key, value in os.environ.items():
            if value and any(word in key.upper() for word in ("TOKEN", "PASSWORD", "SECRET")):
                message = message.replace(value, "[REDACTED]")
        raise ValueError(message)
    return result.stdout


def parameters(
    directory: Path, values: dict | None = None, source: Path | None = None, *,
    validate_factor: bool = False,
) -> dict:
    arguments = [str(directory / ".venv/bin/scheme"), "parameters", "--project", str(source or directory)]
    if values is not None:
        arguments.append("--validate")
    if validate_factor:
        arguments.append("--validate-factor")
    # 保存时可能执行完整区间和多个单日计算，不为用户的因子计算设置固定时限。
    output = command(arguments, directory, values, timeout=None if validate_factor else 600)
    return json.loads(output.splitlines()[-1])


def copy_source(source: Path, destination: Path) -> None:
    config = tomllib.loads((source / "pyproject.toml").read_text())
    inputs = {"pyproject.toml"}
    if (source / "uv.lock").exists():
        inputs.add("uv.lock")
    project = config["project"]
    for name in ("readme", "license"):
        value = project.get(name)
        if isinstance(value, dict) and "file" in value:
            inputs.add(value["file"])
        elif name == "readme" and isinstance(value, str):
            inputs.add(value)
    for pattern in project.get("license-files", []):
        inputs.update(path.relative_to(source).as_posix() for path in source.glob(pattern))
    destination.mkdir(parents=True)
    for name in inputs:
        target = (destination / name).resolve()
        if not target.is_relative_to(destination.resolve()):
            raise ValueError("构建输入路径不在项目内")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    shutil.copytree(source / "src", destination / "src")


def build_version(
    directory: Path, version: dict, values: dict, *, validate_factor: bool = False,
) -> None:
    target = Path(os.environ.get("SOLO_SHARED_DIR", "/shared")) / "runs" / version["id"]
    target.mkdir(exist_ok=False)
    source = target / "source"
    copy_source(directory, source)
    config = tomlkit.parse((source / "pyproject.toml").read_text())
    config["project"]["version"] = version["packageVersion"]
    (source / "pyproject.toml").write_text(tomlkit.dumps(config))
    parameters_result = parameters(directory, values, source, validate_factor=validate_factor)
    wheels = target / "wheels"
    wheels.mkdir()
    command(["uv", "build", "--wheel", "--out-dir", str(wheels), str(source)], target)
    wheel = next(wheels.glob("*.whl"))
    package = config["project"]["name"]
    # 沿用工作区的确切 Git commit/版本；所有目录依赖冻结为候选 wheel。
    lock = tomllib.loads((source / "uv.lock").read_text())
    sources = {package: {"path": str(wheel)}}
    project_uv = config.get("tool", {}).get("uv", {})
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
            destination = wheels / item["name"]
            command(["uv", "build", "--wheel", "--out-dir", str(destination), str(dependency)], target)
            sources[item["name"]] = {"path": str(next(destination.glob("*.whl")))}
        elif "path" in location:
            original = (directory / location["path"]).resolve()
            destination = wheels / original.name
            shutil.copy2(original, destination)
            sources[item["name"]] = {"path": str(destination)}
        elif "registry" in location:
            selected = project_uv.get("sources", {}).get(item["name"])
            if isinstance(selected, dict) and "index" in selected:
                sources[item["name"]] = deepcopy(selected)
            elif isinstance(selected, list) and all("index" in entry for entry in selected):
                sources[item["name"]] = deepcopy(selected)
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
    for name in ("index", "index-url", "extra-index-url", "index-strategy"):
        if name in project_uv:
            env_config["tool"]["uv"][name] = deepcopy(project_uv[name])
    if "index-url" not in project_uv and not any(index.get("default") for index in project_uv.get("index", [])):
        env_config["tool"]["uv"].setdefault("index", []).append({
            "url": "https://pypi.tuna.tsinghua.edu.cn/simple", "default": True,
        })
    (environment / "pyproject.toml").write_text(tomlkit.dumps(env_config))
    command(["uv", "lock", "--project", str(environment)], target)
    frozen_lock = tomllib.loads((environment / "uv.lock").read_text())
    if any({"directory", "editable"} & p["source"].keys() for p in frozen_lock["package"]):
        raise ValueError("正式环境仍包含未冻结的源码依赖")
    component = {"package": package, "version": version["packageVersion"], "wheel": str(wheel),
                 "entry": parameters_result["entry"]}
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
            components[kind] = {**upstream, "wheel": str(upstream_wheel)}
        data.update(kind=parameters_result["project_kind"], algos=components, backtest=parameters_result["backtest"])
    (target / "input.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    (target / "build.json").write_text(json.dumps({"scheme_version": installed["version"]}))


async def _project_thread(function, *arguments, **keywords):
    """请求取消也等待已开始的源码读取结束，再释放项目排他入口。"""
    task = asyncio.create_task(asyncio.to_thread(function, *arguments, **keywords))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def _save(directory: Path, project: dict, version: dict, values: dict) -> None:
    endpoint = f'{project["project_id"]}/versions/{version["id"]}/submit'
    try:
        await check_project_release(project["project_id"])
        await _project_thread(
            build_version, directory, version, values, validate_factor=project["kind"] == "factor",
        )
    except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError, web.HTTPError) as error:
        message = error.reason if isinstance(error, web.HTTPError) else str(error)
        await backend(endpoint, {"error": message[-10000:]})
        return
    await backend(endpoint, {})


async def save(directory: Path, project: dict, version: dict, values: dict) -> None:
    with project_operation(project["project_id"]):
        await _save(directory, project, version, values)


async def _save_owned(operation, directory: Path, project: dict, version: dict, values: dict) -> None:
    try:
        await _save(directory, project, version, values)
    finally:
        operation.__exit__(None, None, None)


class VersionsHandler(DependenciesHandler):
    @web.authenticated
    async def get(self) -> None:
        project = self.current_project(self.get_query_argument("path", ""))
        if self.get_query_argument("parameters", "") == "1":
            with project_operation(project["project_id"]):
                await check_project_release(project["project_id"])
                directory = Path(self.settings["server_root_dir"]) / project["path"]
                try:
                    self.finish(await _project_thread(parameters, directory))
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
        operation = project_operation(project["project_id"])
        operation.__enter__()
        transferred = False
        try:
            await check_project_release(project["project_id"])
            directory = Path(self.settings["server_root_dir"]) / project["path"]
            try:
                await _project_thread(parameters, directory, body["parameters"])
            except (ValueError, OSError) as error:
                raise web.HTTPError(422, reason=str(error)) from error
            if body.get("validate_only"):
                self.finish({"valid": True})
                return
            version = await backend(f'{project["project_id"]}/versions', {"note": body.get("note", "")})
            task = asyncio.create_task(_save_owned(operation, directory, project, version, body["parameters"]))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)
            # 若 task 在启动前即被取消，done callback 也释放已有入口。
            task.add_done_callback(lambda _: operation.__exit__(None, None, None))
            transferred = True
            self.set_status(202)
            self.finish(version)
        finally:
            if not transferred:
                operation.__exit__(None, None, None)
