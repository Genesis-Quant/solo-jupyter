"""构建或下载项目 wheel，安装失败时恢复配置并重新同步环境。"""

import asyncio
import json
import os
import re
import shutil
import tempfile
from contextlib import ExitStack, contextmanager
from email.parser import BytesParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from uuid import UUID, uuid4
from zipfile import ZipFile

import tomlkit
import tomllib
from jupyter_server.base.handlers import APIHandler
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError

from .handlers import environment_project, read_project
from .uv import uv_environment

PROJECT_KINDS = ("factor", "model", "optimize", "control", "execution")
project_busy: set[str] = set()
deletion_leases: dict[str, str] = {}
METADATA_SCRIPT = """import importlib.metadata as m,json,sys,platform
print(json.dumps({'python':sys._base_executable,'markers':{'python_version':'.'.join(platform.python_version_tuple()[:2]),'python_full_version':platform.python_version()},'packages':[{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()]}))
"""


@contextmanager
def project_operation(identifier: str):
    """安装、参数读取、保存和删除按项目排他，不影响运行中的 Kernel。"""
    key = operation_identifier(identifier)
    if key in project_busy:
        raise web.HTTPError(
            409, reason="当前项目正在安装依赖、保存版本或删除，请稍后重试"
        )
    project_busy.add(key)
    try:
        yield
    finally:
        project_busy.discard(key)


async def project_catalog() -> list[dict[str, Any]]:
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(
            url + "/api/v1/projects", request_timeout=30
        )
        return json.loads(response.body)
    except (HTTPClientError, ValueError) as error:
        raise web.HTTPError(502, reason="无法读取 Solo 项目列表") from error


async def artifact_api(
    path: str, body: dict | None = None, *, binary: bool = False
) -> Any:
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(
            url + "/api/v1/" + path,
            method="GET" if body is None else "POST",
            body=None if body is None else json.dumps(body),
            headers={"Content-Type": "application/json"},
            request_timeout=90,
        )
        return response.body if binary else json.loads(response.body)
    except HTTPClientError as error:
        message = "无法访问 Solo artifact registry"
        if error.response:
            try:
                detail = json.loads(error.response.body).get("detail")
                if isinstance(detail, dict):
                    detail = detail.get("reason")
                if isinstance(detail, str) and detail:
                    message = detail
            except (ValueError, TypeError, AttributeError):
                pass
        raise web.HTTPError(
            error.code if error.code < 600 else 502, reason=redact(message)
        ) from error


async def artifact_catalog(*, published: bool | None = None) -> list[dict[str, Any]]:
    query = (
        "?" + urlencode({"published": str(published).lower()})
        if published is not None
        else ""
    )
    return await artifact_api("artifacts" + query)


def artifact_records(value: dict | list) -> list[dict]:
    return list(value.values()) if isinstance(value, dict) else value


async def register_wheel(project: dict, wheel: Path) -> dict:
    return await artifact_api(
        "artifacts/register", {"project_id": project["project_id"], "wheel": str(wheel)}
    )


def redact(message: str) -> str:
    message = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@", r"\1[REDACTED]@", message
    )
    for key, value in os.environ.items():
        if value and any(
            part in key.upper() for part in ("TOKEN", "PASSWORD", "SECRET")
        ):
            message = message.replace(value, "[REDACTED]")
    return message


async def check_project_release(identifier: str) -> None:
    """版本准入只由 Backend 决定，不在 Jupyter 复制退役策略。"""
    identifier = operation_identifier(identifier)
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(
            f"{url}/api/v1/version-policy/projects/{identifier}/check",
            method="POST",
            body="{}",
            headers={"Content-Type": "application/json"},
            request_timeout=30,
        )
        result = json.loads(response.body)
        if not isinstance(result, dict) or not isinstance(result.get("allowed"), bool):
            raise TypeError("Invalid admission response")
        if not result["allowed"]:
            raise web.HTTPError(422, reason="项目版本不允许新使用")
    except HTTPClientError as error:
        message = "无法确认项目版本准入"
        if error.response:
            try:
                detail = json.loads(error.response.body).get("detail")
                if isinstance(detail, dict):
                    detail = detail.get("reason")
                if isinstance(detail, str) and detail:
                    message = detail
            except (ValueError, TypeError, AttributeError):
                pass
        raise web.HTTPError(
            error.code if error.code < 600 else 502, reason=redact(message)
        ) from error
    except (ValueError, TypeError, AttributeError) as error:
        raise web.HTTPError(502, reason="项目版本准入响应无效") from error


def same_scheme_series(current: Any, candidate: Any) -> bool:
    try:
        current_version, candidate_version = Version(current), Version(candidate)
        return (current_version.major, current_version.minor) == (
            candidate_version.major,
            candidate_version.minor,
        )
    except (InvalidVersion, TypeError):
        return False


def compatible(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if (
        current.get("retired")
        or candidate.get("retired")
        or candidate.get("archived")
        or candidate["id"] == current["project_id"]
    ):
        return False
    if (
        current.get("kind") not in PROJECT_KINDS
        or candidate.get("kind")
        not in PROJECT_KINDS[: PROJECT_KINDS.index(current["kind"]) + 1]
    ):
        return False
    return same_scheme_series(
        current.get("scheme_version"), candidate.get("schemeVersion")
    )


def compatible_artifact(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if (
        current.get("retired")
        or candidate.get("retired")
        or not candidate.get("publishedAt")
        or candidate.get("sourceProjectId") == current["project_id"]
    ):
        return False
    if (
        current.get("kind") not in PROJECT_KINDS
        or candidate.get("kind")
        not in PROJECT_KINDS[: PROJECT_KINDS.index(current["kind"]) + 1]
    ):
        return False
    return same_scheme_series(
        current.get("scheme_version"), candidate.get("schemeVersion")
    )


def safe_path(root: Path, path: Path) -> Path:
    path = path.resolve()
    if not path.is_relative_to(root.resolve()):
        raise web.HTTPError(403, reason="依赖路径不在 Jupyter workspace 内")
    return path


def project_directory(root: Path, raw: str) -> Path:
    path = Path(raw)
    if (
        path.is_absolute()
        or raw.startswith("/")
        or ".." in path.parts
        or "\\" in raw
        or ":" in raw
    ):
        raise web.HTTPError(400, reason="Invalid project path")
    return safe_path(root, root / path)


def project_package(root: Path, project: dict[str, Any]) -> tuple[Path, str]:
    directory = project_directory(root, project["path"])
    try:
        name = canonicalize_name(
            tomllib.loads(
                safe_path(root, directory / "pyproject.toml").read_text(
                    encoding="utf-8"
                )
            )["project"]["name"],
            validate=True,
        )
        if (
            project.get("package_name") is not None
            and canonicalize_name(project["package_name"]) != name
        ):
            raise web.HTTPError(422, reason="项目包名与 .solo 显式身份不一致")
        return directory, name
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise web.HTTPError(422, reason="项目缺少有效的 Python 包配置") from error


def allows_scheme_series(requirement: Requirement, version: Version) -> bool:
    # PEP 440 范围在声明的 patch 边界变化，同时检查边界及其下一 patch。
    patches = {0}
    for specifier in requirement.specifier:
        boundary = Version(specifier.version.removesuffix(".*"))
        if (boundary.major, boundary.minor) == (version.major, version.minor):
            patches.update((boundary.micro, boundary.micro + 1))
    return not requirement.url and all(
        requirement.specifier.contains(
            f"{version.major}.{version.minor}.{patch}", prereleases=True
        )
        for patch in patches
    )


def validate_project(version: str, requirements: list[str], scheme: Version) -> None:
    if not same_scheme_series(version, str(scheme)):
        raise web.HTTPError(
            422, reason="项目包版本的主版本和次版本必须与当前 Scheme 一致"
        )
    try:
        matches = [
            item
            for raw in requirements
            if canonicalize_name((item := Requirement(raw)).name) == "scheme"
        ]
        if not matches or not all(
            allows_scheme_series(item, scheme) for item in matches
        ):
            raise ValueError("Expected the entire Scheme series")
    except (ValueError, TypeError) as error:
        raise web.HTTPError(
            422, reason="项目 Scheme 依赖必须允许当前主次版本的全部 patch 版本"
        ) from error


def locked_scheme(lock: dict, current: dict) -> Version:
    try:
        packages = [
            item
            for item in lock["package"]
            if canonicalize_name(item["name"]) == "scheme"
        ]
        if len(packages) != 1:
            raise ValueError("Expected one locked Scheme release")
        version = Version(packages[0]["version"])
        if not same_scheme_series(current.get("scheme_version"), str(version)):
            raise ValueError("Locked Scheme series differs")
        return version
    except (ValueError, TypeError, KeyError) as error:
        raise web.HTTPError(
            422, reason="当前项目缺少有效的同主次版本 Scheme 锁定记录，请先重建环境"
        ) from error


def project_python(directory: Path) -> Path:
    return (
        directory
        / ".venv"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )


async def run_command(
    arguments: list[str], directory: Path, environment: dict[str, str]
) -> str:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        cwd=directory,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output = (await asyncio.wait_for(process.communicate(), timeout=600))[0]
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise
    message = output.decode(errors="replace")
    if process.returncode:
        raise web.HTTPError(422, reason="uv 安装失败：\n" + redact(message[-3000:]))
    return message


async def install_project_environment(root: Path, body: Any) -> dict[str, str]:
    directory, project, mode = environment_project(root, body)
    environment = {**os.environ, **uv_environment(), "GIT_TERMINAL_PROMPT": "0"}
    for key in (
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PROJECT",
        "UV_WORKING_DIR",
    ):
        environment.pop(key, None)
    with project_operation(project["project_id"]):
        try:
            await run_command(
                [
                    "uv",
                    "sync",
                    "--project",
                    str(directory),
                    "--python",
                    "3.12",
                    "--managed-python",
                    *(["--locked"] if body.get("locked", False) else []),
                ],
                directory,
                environment,
            )
            if mode == "prepare":
                python = project_python(directory)
                await asyncio.wait_for(
                    run_command(
                        [str(python), "-c", "import ipykernel"], directory, environment
                    ),
                    timeout=30,
                )
                kernel_name = f"solo-{project['project_id']}"
                kernel = root.resolve() / ".jupyter" / "kernels" / kernel_name
                if kernel.is_symlink() or not kernel.resolve().is_relative_to(
                    root.resolve()
                ):
                    raise web.HTTPError(
                        403, reason="Kernel directory is outside the workspace"
                    )
                kernel.mkdir(parents=True, exist_ok=True)
                if (kernel / "kernel.json").is_symlink():
                    raise web.HTTPError(
                        403, reason="Kernel specification is redirected"
                    )
                (kernel / "kernel.json").write_text(
                    json.dumps(
                        {
                            "argv": [
                                str(python),
                                "-m",
                                "ipykernel_launcher",
                                "-f",
                                "{connection_file}",
                            ],
                            "display_name": project["name"],
                            "language": "python",
                            "env": {"UV_PROJECT_ENVIRONMENT": str(directory / ".venv")},
                            "metadata": {
                                "solo": {
                                    "project_id": project["project_id"],
                                    "path": project["path"],
                                }
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                notebook_path = directory / "research.ipynb"
                notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
                notebook.setdefault("metadata", {})["kernelspec"] = {
                    "name": kernel_name,
                    "display_name": project["name"],
                    "language": "python",
                }
                notebook_path.write_text(
                    json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except (asyncio.TimeoutError, OSError, ValueError) as error:
            raise web.HTTPError(
                502,
                reason="Project environment installation failed: " + redact(str(error)),
            ) from None
    return {"project_id": project["project_id"], "mode": mode}


class ProjectEnvironmentHandler(APIHandler):
    @web.authenticated
    async def post(self) -> None:
        self.finish(
            await install_project_environment(
                Path(self.settings["server_root_dir"]), self.get_json_body()
            )
        )


async def installed_metadata(directory: Path, environment: dict[str, str]) -> dict:
    return json.loads(
        await run_command(
            [str(project_python(directory)), "-I", "-B", "-c", METADATA_SCRIPT],
            directory,
            environment,
        )
    )


def actual_scheme(metadata: dict) -> str:
    matches = [
        item["version"]
        for item in metadata["packages"]
        if canonicalize_name(item["name"]) == "scheme"
    ]
    if len(matches) != 1:
        raise web.HTTPError(422, reason="项目环境必须实际安装唯一 Scheme")
    return matches[0]


def verify_environment(lock: dict, before: str) -> None:
    if (
        next(
            item["version"]
            for item in lock["package"]
            if canonicalize_name(item["name"]) == "scheme"
        )
        != before
    ):
        raise web.HTTPError(422, reason="安装未保留当前 Scheme 实际版本")


def wheel_metadata(wheel: Path) -> tuple[str, str, list[str]]:
    with ZipFile(wheel) as archive:
        (entry,) = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        metadata = BytesParser().parsebytes(archive.read(entry))
    return (
        canonicalize_name(metadata["Name"]),
        metadata["Version"],
        metadata.get_all("Requires-Dist", []),
    )


def store_wheel(
    root: Path,
    directory: Path,
    wheel: Path,
    name: str,
    identifier: str,
    created: list[Path],
) -> Path:
    parent = safe_path(root, directory / ".solo-wheels" / name / str(UUID(identifier)))
    target = safe_path(root, parent / wheel.name)
    if not target.exists():
        parent.mkdir(parents=True, exist_ok=False)
        created.append(parent)
        with target.open("xb") as stream:
            stream.write(wheel.read_bytes())
    return target


async def stage_artifact_graph(
    root: Path,
    directory: Path,
    selected: list[dict],
    scheme: Version,
    created: list[Path],
    *,
    known: dict[str, dict] | None = None,
) -> tuple[dict[str, Path], dict[str, dict]]:
    staged, snapshots, seen = {}, {}, set()
    pending = [(item, True) for item in selected]
    while pending:
        item, expand = pending.pop(0)
        identifier = str(UUID(item["id"]))
        if identifier in seen or item["package"] == "scheme":
            continue
        seen.add(identifier)
        snapshot = known.get(identifier) if known is not None else None
        if snapshot is None:
            snapshot = await artifact_api(f"artifacts/{identifier}")
        if snapshot.get("retired"):
            raise web.HTTPError(422, reason="Artifact 闭包包含已退役版本")
        if snapshot["kind"] in PROJECT_KINDS and not same_scheme_series(
            str(scheme), snapshot.get("schemeVersion")
        ):
            raise web.HTTPError(422, reason="Artifact 闭包 Scheme 主版本和次版本不一致")
        package = snapshot["package"]
        if package in snapshots and snapshots[package]["id"] != identifier:
            raise web.HTTPError(422, reason=f"Artifact 闭包包 {package} 的冻结来源冲突")
        snapshots[package] = snapshot
        if expand:
            pending.extend(
                (item, not bool(snapshot.get("publishedAt")))
                for item in artifact_records(snapshot.get("dependencies", {}))
            )
    for package, snapshot in snapshots.items():
        parent = safe_path(
            root, directory / ".solo-wheels" / package / str(UUID(snapshot["id"]))
        )
        target = safe_path(root, parent / snapshot["filename"])
        if not target.exists():
            parent.mkdir(parents=True, exist_ok=False)
            created.append(parent)
            with target.open("xb") as stream:
                stream.write(
                    await artifact_api(f"artifacts/{snapshot['id']}/wheel", binary=True)
                )
        staged[package] = target
    return staged, snapshots


async def wheel_artifact(
    wheel: Path,
    package: str,
    known: dict[Path, dict],
    projects: dict[str, dict],
    *,
    project_id: str | None = None,
    version: str | None = None,
) -> dict:
    """旧目录只通过所属项目已接受的 UUID 及 registry 目录映射重绑定。"""
    package = canonicalize_name(package)
    reason = f"受管 wheel {package} 缺少匹配的已注册 Artifact，无法确认精确身份"
    if (
        wheel.parent.parent.parent.name != ".solo-wheels"
        or wheel.parent.parent.name != package
    ):
        raise web.HTTPError(422, reason=reason)
    try:
        directory_id = str(UUID(wheel.parent.name))
    except ValueError:
        directory_id = None
    snapshot = known.get(wheel)
    identifier = directory_id
    if snapshot is None:
        if identifier is None:
            if project_id is None:
                raise web.HTTPError(422, reason=reason)
            if project_id not in projects:
                projects[project_id] = await artifact_api(f"projects/{project_id}")
            accepted = [
                item
                for item in artifact_records(
                    projects[project_id].get("artifactDependencies", {})
                )
                if item.get("package") == package
            ]
            if len(accepted) != 1:
                raise web.HTTPError(422, reason=reason)
            try:
                identifier = str(
                    UUID(accepted[0].get("artifact_id") or accepted[0].get("id"))
                )
            except (ValueError, TypeError, AttributeError) as error:
                raise web.HTTPError(422, reason=reason) from error
        try:
            snapshot = await artifact_api(f"artifacts/{identifier}")
        except web.HTTPError as error:
            if error.status_code == 404:
                raise web.HTTPError(422, reason=reason) from error
            raise
    else:
        identifier = snapshot["id"]
    if (
        snapshot["id"] != identifier
        or snapshot["package"] != package
        or snapshot["filename"] != wheel.name
        or (version is not None and snapshot["version"] != version)
        or (directory_id is not None and directory_id != identifier)
        or (
            directory_id is None
            and snapshot.get("storageDirectory") != wheel.parent.name
        )
    ):
        raise web.HTTPError(422, reason=reason)
    known[wheel] = snapshot
    return snapshot


def workspace_member(directory: Path, name: str) -> Path:
    for parent in (directory, *directory.parents):
        manifest = parent / "pyproject.toml"
        if not manifest.is_file():
            continue
        workspace = (
            tomllib.loads(manifest.read_text(encoding="utf-8"))
            .get("tool", {})
            .get("uv", {})
            .get("workspace")
        )
        if workspace is None:
            continue
        excluded = {
            path.resolve()
            for pattern in workspace.get("exclude", [])
            for path in parent.glob(pattern)
        }
        for pattern in workspace["members"]:
            for member in parent.glob(pattern):
                if (
                    member.resolve() not in excluded
                    and canonicalize_name(
                        tomllib.loads(
                            (member / "pyproject.toml").read_text(encoding="utf-8")
                        )["project"]["name"]
                    )
                    == name
                ):
                    return member
        break
    raise ValueError(f"Workspace 缺少依赖项目 {name}")


async def freeze_runtime(
    root: Path,
    directory: Path,
    source: Path,
    temporary: Path,
    scheme: Version,
    environment: dict[str, str],
    markers: dict,
    created: list[Path],
    current: dict,
    selected_extras: frozenset[str],
) -> tuple[Path, dict[str, str], dict[str, dict]]:
    """冻结实际 runtime sources，不传播上游 Scheme source 或开发依赖。"""
    frozen, pending, built, copied_artifacts, git_wheels = {}, {}, {}, {}, {}
    wheel_artifacts, projects = {}, {}
    visiting, checked = set(), set()
    operations = ExitStack()
    guarded = {
        str(UUID(current["project_id"])),
    }

    def record(name: str, requirement: str) -> None:
        if name in frozen and frozen[name] != requirement:
            raise web.HTTPError(422, reason=f"运行依赖 {name} 声明了冲突的来源")
        frozen[name] = requirement

    async def build(path: Path, extras: frozenset[str] = frozenset()) -> Path:
        path = safe_path(root, path)
        extras = frozenset(canonicalize_name(extra) for extra in extras)
        if path in visiting:
            raise web.HTTPError(422, reason="本地运行依赖存在循环")
        visiting.add(path)
        try:
            if path not in built:
                metadata = (
                    read_project(root, path.relative_to(root).as_posix())
                    if (path / ".solo").exists()
                    else None
                )
                if metadata is not None:
                    identifier = str(UUID(metadata["project_id"]))
                    if identifier not in guarded and path != source:
                        operations.enter_context(project_operation(identifier))
                    guarded.add(identifier)
                    if path != source:
                        await check_project_release(identifier)
                    if not same_scheme_series(
                        metadata.get("scheme_version"), str(scheme)
                    ):
                        raise web.HTTPError(
                            422, reason="本地项目 Scheme 主版本和次版本不一致"
                        )
                config = tomllib.loads(
                    safe_path(root, path / "pyproject.toml").read_text(encoding="utf-8")
                )
                project = config["project"]
                name = canonicalize_name(project["name"])
                if metadata is not None:
                    if (
                        metadata.get("package_name") is not None
                        and canonicalize_name(metadata["package_name"]) != name
                    ):
                        raise web.HTTPError(
                            422, reason="本地项目包名与 .solo 显式身份不一致"
                        )
                    if path != source:
                        validate_project(
                            project["version"], project.get("dependencies", []), scheme
                        )
                output = safe_path(
                    root, directory / ".solo-wheels" / name / str(uuid4())
                )
                output.mkdir(parents=True)
                created.append(output)
                await run_command(
                    [
                        "uv",
                        "build",
                        "--wheel",
                        "--no-sources",
                        "--out-dir",
                        str(output),
                        str(path),
                    ],
                    temporary,
                    environment,
                )
                (wheel,) = output.glob("*.whl")
                requirements = wheel_metadata(wheel)[2]
                pending[wheel] = (name, metadata or current)
                built[path] = wheel, requirements, config, metadata
            wheel, requirements, config, metadata = built[path]
            if (path, extras) in checked:
                return wheel
            checked.add((path, extras))
            sources = {
                canonicalize_name(name): value
                for name, value in config.get("tool", {})
                .get("uv", {})
                .get("sources", {})
                .items()
            }
            for raw in requirements:
                requirement = Requirement(raw)
                if requirement.marker and not any(
                    requirement.marker.evaluate({**markers, "extra": extra})
                    for extra in {"", *extras}
                ):
                    continue
                name = canonicalize_name(requirement.name)
                if name == "scheme" or name not in sources:
                    continue
                records = sources[name]
                for location in records if isinstance(records, list) else [records]:
                    if location.get("marker") and not Requirement(
                        f"{name}; {location['marker']}"
                    ).marker.evaluate(markers):
                        continue
                    if (
                        location.get("extra")
                        and canonicalize_name(location["extra"]) not in extras
                    ):
                        continue
                    if "path" in location or location.get("workspace"):
                        dependency = safe_path(
                            root,
                            path / location["path"]
                            if "path" in location
                            else workspace_member(path, name),
                        )
                        if dependency.is_dir():
                            asset = await build(
                                dependency, frozenset(requirement.extras)
                            )
                        else:
                            asset = dependency
                            if ".solo-wheels" in dependency.parts:
                                snapshot = await wheel_artifact(
                                    dependency,
                                    name,
                                    wheel_artifacts,
                                    projects,
                                    project_id=metadata["project_id"]
                                    if metadata is not None
                                    else None,
                                )
                                copied_artifacts[snapshot["id"]] = snapshot
                                continue
                            else:
                                pending[asset] = (name, current)
                        record(name, f"{name} @ {asset.as_uri()}")
                    elif "git" in location:
                        source_lock = tomllib.loads(
                            safe_path(root, path / "uv.lock").read_text(
                                encoding="utf-8"
                            )
                        )
                        (selected,) = [
                            item
                            for item in source_lock["package"]
                            if canonicalize_name(item["name"]) == name
                        ]
                        url, commit = selected["source"]["git"].rsplit("#", 1)
                        subdirectory = parse_qs(urlparse(url).query).get(
                            "subdirectory", [location.get("subdirectory")]
                        )[0]
                        url = url.split("?", 1)[0]
                        identity = (url, commit, subdirectory)
                        if identity not in git_wheels:
                            checkout = temporary / "git" / str(len(git_wheels))
                            checkout.parent.mkdir(exist_ok=True)
                            await run_command(
                                [
                                    "git",
                                    "clone",
                                    "--no-checkout",
                                    "--",
                                    url,
                                    str(checkout),
                                ],
                                temporary,
                                environment,
                            )
                            await run_command(
                                [
                                    "git",
                                    "-C",
                                    str(checkout),
                                    "checkout",
                                    "--detach",
                                    commit,
                                ],
                                temporary,
                                environment,
                            )
                            git_project = safe_path(
                                checkout, checkout / (subdirectory or ".")
                            )
                            output = safe_path(
                                root, directory / ".solo-wheels" / name / str(uuid4())
                            )
                            output.mkdir(parents=True)
                            created.append(output)
                            await run_command(
                                [
                                    "uv",
                                    "build",
                                    "--wheel",
                                    "--no-sources",
                                    "--out-dir",
                                    str(output),
                                    str(git_project),
                                ],
                                temporary,
                                environment,
                            )
                            (asset,) = output.glob("*.whl")
                            git_wheels[identity] = asset
                            pending[asset] = (name, current)
                        record(name, f"{name} @ {git_wheels[identity].as_uri()}")
                    elif "url" in location:
                        record(name, f"{name} @ {location['url']}")
            return wheel
        finally:
            visiting.remove(path)

    with operations:
        selected = await build(source, selected_extras)
        published, snapshots = {}, {}
        for wheel, (name, project) in reversed(list(pending.items())):
            stored = (
                wheel
                if wheel.parent in created
                else store_wheel(root, directory, wheel, name, str(uuid4()), created)
            )
            snapshot = await register_wheel(project, stored)
            registered = safe_path(
                root, stored.parent.with_name(str(UUID(snapshot["id"])))
            )
            if registered.exists():
                raise FileExistsError(registered)
            stored.parent.rename(registered)
            created[created.index(stored.parent)] = registered
            published[wheel] = registered / stored.name
            snapshots[name] = snapshot
        for wheel, stored in published.items():
            for name, requirement in frozen.items():
                frozen[name] = requirement.replace(wheel.as_uri(), stored.as_uri())
        if copied_artifacts:
            closure, copied = await stage_artifact_graph(
                root,
                directory,
                list(copied_artifacts.values()),
                scheme,
                created,
                known=copied_artifacts,
            )
            for name, asset in closure.items():
                record(name, f"{name} @ {asset.as_uri()}")
            snapshots.update(copied)
        return published[selected], frozen, snapshots


def apply_constraints(
    config: dict, directory: Path, scheme: Version, frozen: dict[str, str]
) -> None:
    uv = config.setdefault("tool", {}).setdefault("uv", {})
    retained = []
    for raw in uv.get("constraint-dependencies", []):
        requirement = Requirement(raw)
        managed = (
            requirement.url
            and requirement.url.startswith("file:")
            and Path(unquote(urlparse(requirement.url).path)).is_relative_to(
                directory / ".solo-wheels"
            )
        )
        if canonicalize_name(requirement.name) not in frozen or not managed:
            retained.append(raw)
    for constraint in [f"scheme=={scheme}", *frozen.values()]:
        if constraint not in retained:
            retained.append(constraint)
    uv["constraint-dependencies"] = retained


def update_dependency(
    config: dict, package: str, version: str, wheel: Path, directory: Path, dev: bool
) -> None:
    runtime = config["project"].setdefault("dependencies", [])
    development = config.setdefault("dependency-groups", {}).setdefault("dev", [])
    replacements = []
    for group in (runtime, development):
        for index in range(len(group) - 1, -1, -1):
            if not isinstance(group[index], str):
                continue
            requirement = Requirement(group[index])
            if canonicalize_name(requirement.name) != package:
                continue
            extras = (
                "[" + ",".join(sorted(requirement.extras)) + "]"
                if requirement.extras
                else ""
            )
            marker = f"; {requirement.marker}" if requirement.marker else ""
            replacements.append(f"{package}{extras}=={version}{marker}")
            del group[index]
    (development if dev else runtime).extend(
        dict.fromkeys(replacements or [f"{package}=={version}"])
    )
    config.setdefault("tool", {}).setdefault("uv", {}).setdefault("sources", {})[
        package
    ] = {
        "path": Path(os.path.relpath(wheel, directory)).as_posix(),
    }


async def installed_artifacts(
    root: Path,
    directory: Path,
    lock: dict,
    known: dict[Path, dict],
    *,
    project_id: str,
    projects: dict[str, dict],
    legacy_only: bool = False,
) -> list[dict]:
    accepted = []
    for item in lock["package"]:
        raw = item.get("source", {}).get("path")
        if not isinstance(raw, str) or canonicalize_name(item["name"]) == "scheme":
            continue
        wheel = safe_path(root, directory / raw)
        if ".solo-wheels" not in wheel.parts:
            continue
        if legacy_only:
            try:
                UUID(wheel.parent.name)
            except ValueError:
                pass
            else:
                continue
        snapshot = await wheel_artifact(
            wheel,
            item["name"],
            known,
            projects,
            project_id=project_id,
            version=item["version"],
        )
        accepted.append(snapshot)
    return accepted


async def install_project(
    root: Path, current: dict[str, Any], candidate: dict[str, Any], *, dev: bool = True
) -> str:
    if not compatible(current, candidate):
        raise web.HTTPError(
            422, reason="仅可安装同 Scheme 主版本和次版本的同类或上游项目"
        )
    with project_operation(current["project_id"]), project_operation(candidate["id"]):
        await check_project_release(current["project_id"])
        await check_project_release(candidate["id"])
        project_directory(root, candidate["directory"])
        source = read_project(root, candidate["directory"])
        if (
            source is None
            or source["project_id"] != candidate["id"]
            or source["path"] != candidate["directory"]
        ):
            raise web.HTTPError(409, reason="待安装项目目录与项目记录不一致")
        if not same_scheme_series(
            current.get("scheme_version"), source.get("scheme_version")
        ):
            raise web.HTTPError(
                422, reason="仅可安装同 Scheme 主版本和次版本的同类或上游项目"
            )
        source_directory, package = project_package(root, source)
        return await install_common(
            root.resolve(), current, package, source_directory=source_directory, dev=dev
        )


async def install_artifact(
    root: Path, current: dict[str, Any], candidate: dict[str, Any], *, dev: bool = True
) -> str:
    if not compatible_artifact(current, candidate):
        raise web.HTTPError(
            422, reason="仅可安装已发布的同 Scheme 主次版本的同类或上游 artifact"
        )
    with project_operation(current["project_id"]):
        await check_project_release(current["project_id"])
        return await install_common(
            root.resolve(), current, candidate["package"], artifact=candidate, dev=dev
        )


async def install_common(
    root: Path,
    current: dict,
    package: str,
    *,
    source_directory: Path | None = None,
    artifact: dict | None = None,
    dev: bool = True,
) -> str:
    directory, name = project_package(root, current)
    if name == package:
        raise web.HTTPError(422, reason="不能安装与当前项目同包名的项目")
    safe_path(root, directory / ".venv")
    if not project_python(directory).is_file():
        raise web.HTTPError(409, reason="当前项目环境尚未创建")
    snapshots = {
        directory / name: safe_path(root, directory / name).read_bytes()
        for name in ("pyproject.toml", "uv.lock")
    }
    config = tomlkit.parse(snapshots[directory / "pyproject.toml"].decode("utf-8"))
    original_lock = tomllib.loads(snapshots[directory / "uv.lock"].decode("utf-8"))
    scheme = locked_scheme(original_lock, current)
    validate_project(
        config["project"]["version"], config["project"].get("dependencies", []), scheme
    )
    if source_directory is not None:
        source_project = tomllib.loads(
            (source_directory / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        validate_project(
            source_project["version"], source_project.get("dependencies", []), scheme
        )
    environment = {**os.environ, **uv_environment(), "GIT_TERMINAL_PROMPT": "0"}
    for variable in (
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "PYTHONHOME",
        "UV_PROJECT_ENVIRONMENT",
        "UV_WORKING_DIR",
        "UV_PROJECT",
    ):
        environment.pop(variable, None)
    before = await installed_metadata(directory, environment)
    installed_scheme = actual_scheme(before)
    if Version(installed_scheme) != scheme:
        raise web.HTTPError(
            422, reason="当前 Scheme 实际版本与 uv.lock 不一致，请先重建环境"
        )
    wheel_artifacts, projects = {}, {}
    await installed_artifacts(
        root,
        directory,
        original_lock,
        wheel_artifacts,
        project_id=current["project_id"],
        projects=projects,
        legacy_only=True,
    )
    created = []
    changed = False
    try:
        with tempfile.TemporaryDirectory(prefix="solo-install-") as temporary:
            temporary = Path(temporary)
            if artifact is not None:
                closure, known = await stage_artifact_graph(
                    root, directory, [artifact], scheme, created
                )
                if name in closure:
                    raise web.HTTPError(422, reason="Artifact 闭包不能覆盖当前项目包")
                wheel = closure[package]
                frozen = {
                    name: f"{name} @ {asset.as_uri()}"
                    for name, asset in closure.items()
                    if name != package
                }
            else:
                existing = [
                    *config["project"].get("dependencies", []),
                    *config.get("dependency-groups", {}).get("dev", []),
                ]
                extras = frozenset(
                    extra
                    for raw in existing
                    if isinstance(raw, str)
                    and canonicalize_name((requirement := Requirement(raw)).name)
                    == package
                    for extra in requirement.extras
                )
                wheel, frozen, known = await freeze_runtime(
                    root,
                    directory,
                    source_directory,
                    temporary,
                    scheme,
                    environment,
                    {**default_environment(), **before["markers"]},
                    created,
                    current,
                    extras,
                )
            apply_constraints(config, directory, scheme, frozen)
            update_dependency(
                config, package, known[package]["version"], wheel, directory, dev
            )
            changed = True
            (directory / "pyproject.toml").write_text(
                tomlkit.dumps(config), encoding="utf-8"
            )
            await run_command(
                [
                    "uv",
                    "lock",
                    "--project",
                    str(directory),
                    "--upgrade-package",
                    package,
                ],
                directory,
                environment,
            )
            await run_command(
                ["uv", "sync", "--project", str(directory), "--frozen"],
                directory,
                environment,
            )
            lock = tomllib.loads((directory / "uv.lock").read_text(encoding="utf-8"))
            verify_environment(lock, installed_scheme)
            for snapshot in known.values():
                wheel_artifacts[
                    safe_path(
                        root,
                        directory
                        / ".solo-wheels"
                        / snapshot["package"]
                        / str(UUID(snapshot["id"]))
                        / snapshot["filename"],
                    )
                ] = snapshot
            accepted = await installed_artifacts(
                root,
                directory,
                lock,
                wheel_artifacts,
                project_id=current["project_id"],
                projects=projects,
            )
            await artifact_api(
                f"projects/{current['project_id']}/dependencies",
                {"artifacts": list(dict.fromkeys(item["id"] for item in accepted))},
            )
            return package
    except BaseException:
        if changed:
            for path, content in snapshots.items():
                path.write_bytes(content)
            await run_command(
                ["uv", "sync", "--project", str(directory), "--frozen"],
                directory,
                environment,
            )
        for parent in reversed(created):
            shutil.rmtree(parent)
        raise


def operation_identifier(raw: Any) -> str:
    try:
        return str(UUID(raw))
    except (ValueError, TypeError, AttributeError) as error:
        raise web.HTTPError(422, reason="项目 ID 无效") from error


def deletion_nonce(body: Any) -> str:
    lease = body.get("lease") if isinstance(body, dict) else None
    if not isinstance(lease, str) or not lease.strip():
        raise web.HTTPError(400, reason="缺少有效项目操作 lease")
    return lease


class ProjectOperationsHandler(APIHandler):
    @web.authenticated
    async def get(self) -> None:
        identifier = operation_identifier(self.get_query_argument("project_id", ""))
        self.finish({"project_id": identifier, "busy": identifier in project_busy})

    @web.authenticated
    async def post(self) -> None:
        body = self.get_json_body()
        if not isinstance(body, dict) or body.get("operation") != "delete":
            raise web.HTTPError(400, reason="缺少有效项目操作（delete）")
        identifier = operation_identifier(body.get("project_id"))
        lease = deletion_nonce(body)
        existing = deletion_leases.get(identifier)
        if existing is not None:
            if existing != lease:
                raise web.HTTPError(409, reason="项目操作 lease 不属于当前请求")
        elif identifier in project_busy:
            raise web.HTTPError(
                409, reason="当前项目正在安装依赖、保存版本或删除，请稍后重试"
            )
        project_busy.add(identifier)
        deletion_leases[identifier] = lease
        self.finish({"project_id": identifier, "lease": lease})

    @web.authenticated
    async def delete(self) -> None:
        body = self.get_json_body()
        requested = deletion_nonce(body)
        identifier = operation_identifier(body.get("project_id"))
        lease = deletion_leases.get(identifier)
        if lease is not None:
            if lease != requested:
                raise web.HTTPError(409, reason="项目操作 lease 不属于当前请求")
            del deletion_leases[identifier]
            project_busy.discard(identifier)
        self.set_status(204)
        self.finish()


class DependenciesHandler(APIHandler):
    def current_project(self, path: str) -> dict[str, Any]:
        root = Path(self.settings["server_root_dir"]).resolve()
        project_directory(root, path)
        project = read_project(root, path)
        if project is None:
            raise web.HTTPError(404, reason="当前目录不是 Solo 项目")
        return project

    @web.authenticated
    async def get(self) -> None:
        current = self.current_project(self.get_query_argument("path", ""))
        candidates = [
            project
            for project in await project_catalog()
            if compatible(current, project)
        ]
        artifacts = [
            item
            for item in await artifact_catalog(published=True)
            if compatible_artifact(current, item)
        ]
        self.finish({"projects": candidates, "artifacts": artifacts})

    @web.authenticated
    async def post(self) -> None:
        body = self.get_json_body()
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            raise web.HTTPError(400, reason="缺少当前项目路径")
        live, artifact = body.get("live_project_id"), body.get("artifact_id")
        if (
            bool(live) == bool(artifact)
            or (live is not None and not isinstance(live, str))
            or (artifact is not None and not isinstance(artifact, str))
        ):
            raise web.HTTPError(
                400, reason="请选择唯一的 live_project_id 或 artifact_id"
            )
        if not isinstance(body.get("dev", True), bool):
            raise web.HTTPError(400, reason="dev 必须为布尔值")
        current = self.current_project(body["path"])
        root = Path(self.settings["server_root_dir"]).resolve()
        if artifact is not None:
            candidate = await artifact_api(
                f"artifacts/{operation_identifier(artifact)}"
            )
            package = await install_artifact(
                root, current, candidate, dev=body.get("dev", True)
            )
        else:
            candidate = next(
                (
                    project
                    for project in await project_catalog()
                    if project["id"] == live
                ),
                None,
            )
            if candidate is None:
                raise web.HTTPError(404, reason="待安装源码项目不存在")
            package = await install_project(
                root, current, candidate, dev=body.get("dev", True)
            )
        self.finish({"package": package, "dev": body.get("dev", True)})
