"""安装项目 wheel；先在隔离环境验证，再以目录备份提交安装事务。"""

import asyncio
from contextlib import contextmanager
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID
from zipfile import ZipFile

from jupyter_server.base.handlers import APIHandler
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
import tomlkit
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError

from .handlers import read_project

PROJECT_KINDS = ("factor", "model", "optimize", "control", "execution")
_installing: set[str] = set()
_project_busy: set[str] = set()


@contextmanager
def project_operation(identifier: str):
    """Jupyter 同进程安装与参数/保存排他；不阻塞，也不影响运行中的 Kernel。"""
    try:
        key = str(UUID(identifier))
    except (ValueError, TypeError, AttributeError) as error:
        raise web.HTTPError(422, reason="项目 ID 无效") from error
    if key in _project_busy:
        raise web.HTTPError(409, reason="当前项目正在安装依赖或保存版本，请稍后重试")
    _project_busy.add(key)
    try:
        yield
    finally:
        _project_busy.discard(key)


_SOURCE_IGNORES = {".venv", ".git", ".solo-wheels", ".ipynb_checkpoints", "__pycache__", "dist", "build", ".pytest_cache", ".ruff_cache"}
_METADATA = """import importlib.metadata as m,json,sys,platform
print(json.dumps({'python':sys._base_executable,'markers':{'python_version':'.'.join(platform.python_version_tuple()[:2]),'python_full_version':platform.python_version()},'packages':[{'name':d.metadata['Name'],'version':d.version,'requires':d.requires or []} for d in m.distributions()]}))
"""


async def project_catalog() -> list[dict[str, Any]]:
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(url + "/api/v1/projects", request_timeout=30)
        return json.loads(response.body)
    except (HTTPClientError, ValueError) as error:
        raise web.HTTPError(502, reason="无法读取 Solo 项目列表") from error


def _redact(message: str) -> str:
    message = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@", r"\1[REDACTED]@", message)
    for key, value in os.environ.items():
        if value and any(part in key.upper() for part in ("TOKEN", "PASSWORD", "SECRET")):
            message = message.replace(value, "[REDACTED]")
    return message


async def check_project_release(identifier: str) -> None:
    """版本准入只由 Backend 决定，不在 Jupyter 复制退役策略。"""
    try:
        identifier = str(UUID(identifier))
    except (ValueError, TypeError, AttributeError) as error:
        raise web.HTTPError(422, reason="项目 ID 无效") from error
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(
            f"{url}/api/v1/version-policy/projects/{identifier}/check",
            method="POST", body="{}", headers={"Content-Type": "application/json"}, request_timeout=30,
        )
        result = json.loads(response.body)
        if not isinstance(result, dict) or not isinstance(result.get("allowed"), bool):
            raise ValueError("Invalid admission response")
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
        raise web.HTTPError(error.code if error.code < 600 else 502, reason=_redact(message)) from error
    except (ValueError, TypeError, AttributeError) as error:
        raise web.HTTPError(502, reason="项目版本准入响应无效") from error


def _same_scheme_series(current: Any, candidate: Any) -> bool:
    try:
        current_version, candidate_version = Version(current), Version(candidate)
        return (current_version.major, current_version.minor) == (candidate_version.major, candidate_version.minor)
    except (InvalidVersion, TypeError):
        return False


def compatible(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if current.get("retired") or candidate.get("retired") or candidate.get("archived") or candidate["id"] == current["project_id"]:
        return False
    if current.get("kind") not in PROJECT_KINDS or candidate.get("kind") not in PROJECT_KINDS[: PROJECT_KINDS.index(current["kind"]) + 1]:
        return False
    return _same_scheme_series(current.get("scheme_version"), candidate.get("schemeVersion"))


def _safe_path(root: Path, path: Path) -> Path:
    """在 resolve 前拒绝链接（包括目录链接）和逃出 workspace 的路径。"""
    root = root.resolve()
    path = path.absolute()
    if not path.is_relative_to(root):
        raise web.HTTPError(403, reason="依赖路径不在 Jupyter workspace 内")
    for item in (path, *path.parents):
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
            raise web.HTTPError(403, reason="依赖路径不能包含符号链接")
    normalized = Path(os.path.abspath(path))
    if not normalized.is_relative_to(root):
        raise web.HTTPError(403, reason="依赖路径不在 Jupyter workspace 内")
    return normalized


def _project_directory(root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or "\\" in raw or ":" in raw:
        raise web.HTTPError(400, reason="Invalid project path")
    return _safe_path(root, root / path)


def project_package(root: Path, project: dict[str, Any]) -> tuple[Path, str]:
    directory = _project_directory(root, project["path"])
    path = _safe_path(root, directory / "pyproject.toml")
    try:
        name = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["name"]
        return directory, canonicalize_name(name, validate=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise web.HTTPError(422, reason="项目缺少有效的 Python 包配置") from error


def _allows_scheme_series(raw: str, requirement: Requirement, version: Version) -> bool:
    specifier = raw.strip()[len(requirement.name):].strip()
    if specifier.startswith("(") and specifier.endswith(")"):
        specifier = specifier[1:-1]
    bounds = specifier.split(",")
    return (not requirement.url and not requirement.marker and not requirement.extras and len(bounds) == 2
            and all(re.fullmatch(r"\s*(?:>=|<)\s*[0-9]+(?:\.[0-9]+){0,2}\s*", item) for item in bounds)
            and {(item.operator, Version(item.version)) for item in requirement.specifier} == {
                (">=", Version(f"{version.major}.{version.minor}.0")),
                ("<", Version(f"{version.major}.{version.minor + 1}.0")),
            })


def _validate_project(version: str, requirements: list[str], scheme: Version) -> None:
    try:
        if not isinstance(version, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", version) is None or not _same_scheme_series(version, str(scheme)):
            raise web.HTTPError(422, reason="项目包版本的主版本和次版本必须与当前 Scheme 一致")
        parsed = [(raw, Requirement(raw)) for raw in requirements]
        matches = [(raw, item) for raw, item in parsed if canonicalize_name(item.name) == "scheme"]
        if len(matches) != 1 or not _allows_scheme_series(*matches[0], scheme):
            raise ValueError("Expected the entire Scheme series")
    except (ValueError, TypeError) as error:
        raise web.HTTPError(422, reason="项目 Scheme 依赖必须允许当前主次版本的全部 patch 版本") from error


def _locked_scheme(lock: dict, current: dict) -> Version:
    try:
        packages = [item for item in lock.get("package", []) if canonicalize_name(item["name"]) == "scheme"]
        if len(packages) != 1 or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", packages[0]["version"]) is None:
            raise ValueError("Expected one locked Scheme release")
        version = Version(packages[0]["version"])
        if not _same_scheme_series(current.get("scheme_version"), str(version)):
            raise ValueError("Locked Scheme series differs")
        return version
    except (ValueError, TypeError, KeyError) as error:
        raise web.HTTPError(422, reason="当前项目缺少有效的同主次版本 Scheme 锁定记录，请先重建环境") from error


def _python(directory: Path) -> Path:
    return directory / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


async def _run(arguments: list[str], directory: Path, environment: dict[str, str]) -> str:
    process = await asyncio.create_subprocess_exec(*arguments, cwd=directory, env=environment,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=600)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.communicate()
        raise
    message = output.decode(errors="replace")
    if process.returncode:
        raise web.HTTPError(422, reason="uv 安装失败：\n" + _redact(message[-3000:]))
    return message


async def _metadata(directory: Path, environment: dict[str, str]) -> dict:
    try:
        return json.loads(await _run([str(_python(directory)), "-I", "-B", "-c", _METADATA], directory, environment))
    except (ValueError, TypeError) as error:
        raise web.HTTPError(422, reason="无法读取项目环境中的实际包元数据") from error


def _actual_scheme(metadata: dict) -> str:
    matches = [item["version"] for item in metadata["packages"] if canonicalize_name(item["name"]) == "scheme"]
    if len(matches) != 1:
        raise web.HTTPError(422, reason="项目环境必须实际安装唯一 Scheme")
    return matches[0]


async def _verify_environment(directory: Path, environment: dict[str, str], before: str, lock: dict, markers: dict) -> None:
    await _run(["uv", "pip", "check", "--python", str(_python(directory))], directory, environment)
    metadata = await _metadata(directory, environment)
    if _actual_scheme(metadata) != before:
        raise web.HTTPError(422, reason="安装未保留当前 Scheme 实际版本")
    locked: dict[str, set[str]] = {}
    for item in lock["package"]:
        locked.setdefault(canonicalize_name(item["name"]), set()).add(item["version"])
    actual = {canonicalize_name(item["name"]): item["version"] for item in metadata["packages"]}
    for item in metadata["packages"]:
        if item["version"] not in locked.get(canonicalize_name(item["name"]), set()):
            raise web.HTTPError(422, reason=f"实际包 {item['name']} 不在项目锁文件中")
        for raw in item["requires"]:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate(markers):
                continue
            name = canonicalize_name(requirement.name)
            if name not in actual or not requirement.specifier.contains(actual[name], prereleases=True):
                raise web.HTTPError(422, reason=f"传递依赖不满足：{item['name']} requires {raw}")


def _copy_source(root: Path, source: Path, destination: Path) -> None:
    for directory, folders, files in os.walk(source):
        folders[:] = [name for name in folders if name not in _SOURCE_IGNORES]
        for name in folders + files:
            _safe_path(root, Path(directory) / name)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*_SOURCE_IGNORES))


def _wheel_metadata(wheel: Path, seen: set[Path] | None = None) -> tuple[str, str, list[str]]:
    try:
        with ZipFile(wheel) as archive:
            entries = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(entries) != 1:
                raise ValueError("Expected one wheel METADATA")
            metadata = BytesParser().parsebytes(archive.read(entries[0]))
        name = canonicalize_name(metadata["Name"], validate=True)
        version = metadata["Version"]
        Version(version)
        requirements = metadata.get_all("Requires-Dist", [])
        seen = seen if seen is not None else set()
        seen.add(wheel)
        for raw in requirements:
            requirement = Requirement(raw)
            # 直接本地目录 URL 不参与 PEP 517/uv 解析，避免绕过隔离构建与准入。
            if requirement.url and urlparse(requirement.url).scheme == "file":
                url = urlparse(requirement.url)
                dependency = Path(unquote(url.path))
                if url.netloc or dependency.is_symlink() or dependency.suffix != ".whl" or not dependency.is_file():
                    raise web.HTTPError(422, reason="不支持 Requires-Dist 中的本地目录或非 wheel file URL")
                if dependency not in seen:
                    _wheel_metadata(dependency, seen)
        return name, version, requirements
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise web.HTTPError(422, reason="构建 wheel 的包元数据无效") from error


def _store_wheel(root: Path, directory: Path, wheel: Path, name: str, created: list[Path]) -> Path:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    target = _safe_path(root, directory / ".solo-wheels" / name / digest / wheel.name)
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise web.HTTPError(422, reason="持久 wheel 的内容与地址哈希不一致")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 不覆盖既有 wheel；记录本事务创建的文件供失败清理。
        with target.open("xb") as stream:
            created.append(target)
            stream.write(wheel.read_bytes())
    return target


def _relocate_sources(config: dict, root: Path, origin: Path, destination: Path | None = None) -> None:
    for source in config.get("tool", {}).get("uv", {}).get("sources", {}).values():
        for record in source if isinstance(source, list) else [source]:
            if "path" in record:
                path = _safe_path(root, origin / record["path"])
                record["path"] = Path(os.path.relpath(path, destination)).as_posix() if destination else str(path)


def _relocate_lock(lock: dict, root: Path, origin: Path, destination: Path, name: str) -> None:
    def relocate(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"path", "directory", "editable"} and isinstance(item, str):
                    value[key] = Path(os.path.relpath(_safe_path(root, origin / item), destination)).as_posix()
                else:
                    relocate(item)
        elif isinstance(value, list):
            for item in value:
                relocate(item)
    for package in lock.get("package", []):
        source = package.get("source", {})
        local_root = canonicalize_name(package["name"]) == name and source in ({"editable": "."}, {"virtual": "."}, {"directory": "."})
        if local_root:
            del package["source"]
        relocate(package)
        if local_root:
            package["source"] = source
    for key in lock:
        if key != "package":
            relocate(lock[key])


async def _freeze_runtime(root: Path, directory: Path, source: Path, temporary: Path, scheme: Version,
                          environment: dict[str, str], markers: dict, created: list[Path]) -> tuple[Path, dict[str, str]]:
    """只沿 wheel 的 runtime Requires-Dist 访问本地 source，不遍历 dev/workspace 图。"""
    frozen: dict[str, str] = {}
    pending: dict[Path, str] = {}
    built: dict[Path, tuple[Path, list[str], dict]] = {}
    git_wheels: dict[tuple[str, str, str | None], Path] = {}
    visiting: set[Path] = set()
    checked: set[tuple[Path, frozenset[str]]] = set()

    def record(name: str, requirement: str) -> None:
        if name in frozen and frozen[name] != requirement:
            raise web.HTTPError(422, reason=f"运行依赖 {name} 声明了冲突的来源")
        frozen[name] = requirement

    async def build(path: Path, extras: frozenset[str] = frozenset(), expected: str | None = None) -> Path:
        extras = frozenset(canonicalize_name(extra) for extra in extras)
        path = _safe_path(root, path)
        if path in visiting or len(built) >= 64:
            raise web.HTTPError(422, reason="本地运行依赖存在循环或项目过多")
        visiting.add(path)
        try:
            if path not in built:
                config = tomllib.loads(_safe_path(root, path / "pyproject.toml").read_text(encoding="utf-8"))
                project = config["project"]
                name = canonicalize_name(project["name"], validate=True)
                if expected is not None and name != expected:
                    raise web.HTTPError(422, reason="本地依赖包名与 source 声明不一致")
                research = path == source or (path / ".solo").exists()
                if research:
                    metadata = read_project(root, path.relative_to(root).as_posix())
                    if metadata is None or metadata["path"] != path.relative_to(root).as_posix():
                        raise web.HTTPError(422, reason="本地项目元数据无效")
                    if path != source:
                        await check_project_release(metadata["project_id"])
                    if not _same_scheme_series(metadata.get("scheme_version"), str(scheme)):
                        raise web.HTTPError(422, reason="本地项目 Scheme 主版本和次版本不一致")
                    _validate_project(project["version"], project.get("dependencies", []), scheme)
                copied = temporary / "sources" / str(len(built))
                copied.parent.mkdir(exist_ok=True)
                _copy_source(root, path, copied)
                output = temporary / "wheels" / str(len(built))
                output.mkdir(parents=True)
                await _run(["uv", "build", "--wheel", "--no-sources", "--out-dir", str(output), str(copied)], temporary, environment)
                wheels = list(output.glob("*.whl"))
                if len(wheels) != 1:
                    raise web.HTTPError(422, reason="项目必须构建唯一 wheel")
                wheel_name, version, requirements = _wheel_metadata(wheels[0])
                if wheel_name != name or version != project["version"]:
                    raise web.HTTPError(422, reason="构建 wheel 的名称或版本与源码项目不一致")
                if research:
                    _validate_project(version, requirements, scheme)
                asset = wheels[0]
                pending[asset] = name
                built[path] = asset, requirements, config
            asset, requirements, config = built[path]
            if (path, extras) in checked:
                return asset
            checked.add((path, extras))
            sources = {canonicalize_name(name): value for name, value in config.get("tool", {}).get("uv", {}).get("sources", {}).items()}
            for raw in requirements:
                requirement = Requirement(raw)
                if requirement.marker and not any(requirement.marker.evaluate({**markers, "extra": extra}) for extra in {"", *extras}):
                    continue
                name = canonicalize_name(requirement.name)
                # 上游的 Scheme source 永远不能传播到当前项目。
                if name == "scheme":
                    continue
                location = sources.get(name)
                if location is None:
                    if requirement.url and requirement.url.startswith("file:"):
                        _safe_path(root, Path(unquote(urlparse(requirement.url).path)))
                    continue
                records = location if isinstance(location, list) else [location]
                for location in records:
                    if location.get("marker") and not Requirement(f"{name}; {location['marker']}").marker.evaluate(markers):
                        continue
                    if location.get("extra") and canonicalize_name(location["extra"]) not in extras:
                        continue
                    if "path" in location:
                        dependency = _safe_path(root, path / location["path"])
                        if dependency.is_dir():
                            wheel = await build(dependency, frozenset(requirement.extras), name)
                        elif dependency.suffix == ".whl":
                            wheel_name, _, _ = _wheel_metadata(dependency)
                            if wheel_name != name:
                                raise web.HTTPError(422, reason="wheel 依赖包名与 source 不一致")
                            wheel = dependency
                            pending[wheel] = name
                        else:
                            raise web.HTTPError(422, reason="本地依赖必须是项目目录或 wheel")
                        record(name, f"{name} @ {wheel.as_uri()}")
                    elif "git" in location:
                        # 使用源项目 lock 中的精确 commit，不解析/传播其 Scheme 来源。
                        source_lock = tomllib.loads(_safe_path(root, path / "uv.lock").read_text(encoding="utf-8"))
                        matches = [item for item in source_lock["package"] if canonicalize_name(item["name"]) == name]
                        if len(matches) != 1 or "git" not in matches[0].get("source", {}):
                            raise web.HTTPError(422, reason="Git 运行依赖缺少唯一冻结 commit")
                        url, commit = matches[0]["source"]["git"].rsplit("#", 1)
                        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
                            raise web.HTTPError(422, reason="Git 运行依赖 commit 无效")
                        from urllib.parse import parse_qs
                        subdirectory = parse_qs(urlparse(url).query).get("subdirectory", [location.get("subdirectory")])[0]
                        url = url.split("?", 1)[0]
                        if url.rstrip("/") != location["git"].rstrip("/"):
                            raise web.HTTPError(422, reason="Git 运行依赖锁定来源与 source 不一致")
                        if url.startswith("file:"):
                            _safe_path(root, Path(unquote(urlparse(url).path)))
                        identity = (url, commit, subdirectory)
                        if identity not in git_wheels:
                            checkout = temporary / "git" / str(len(git_wheels))
                            checkout.parent.mkdir(exist_ok=True)
                            # Git 包也先变成仅含原 Requires-Dist 的 wheel，防止 uv 读其 tool.uv.sources。
                            await _run(["git", "clone", "--no-checkout", "--", url, str(checkout)], temporary, environment)
                            await _run(["git", "-C", str(checkout), "checkout", "--detach", commit], temporary, environment)
                            actual_commit = (await _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], temporary, environment)).strip()
                            if actual_commit != commit:
                                raise web.HTTPError(422, reason="Git 构建 commit 与源锁定记录不一致")
                            git_project = _safe_path(checkout, checkout / (subdirectory or "."))
                            copied_git = temporary / "git-build" / str(len(git_wheels))
                            copied_git.parent.mkdir(exist_ok=True)
                            _copy_source(checkout, git_project, copied_git)
                            output_git = temporary / "git-wheels" / str(len(git_wheels))
                            output_git.mkdir(parents=True)
                            await _run(["uv", "build", "--wheel", "--no-sources", "--out-dir", str(output_git), str(copied_git)], temporary, environment)
                            wheels_git = list(output_git.glob("*.whl"))
                            if len(wheels_git) != 1:
                                raise web.HTTPError(422, reason="Git 运行依赖必须构建唯一 wheel")
                            git_name, git_version, _ = _wheel_metadata(wheels_git[0])
                            if git_name != name or git_version != matches[0]["version"]:
                                raise web.HTTPError(422, reason="Git wheel 名称或版本与源锁定记录不一致")
                            git_wheels[identity] = wheels_git[0]
                            pending[wheels_git[0]] = name
                        record(name, f"{name} @ {git_wheels[identity].as_uri()}")
                    elif "url" in location:
                        record(name, f"{name} @ {location['url']}")
                    elif location.get("workspace"):
                        raise web.HTTPError(422, reason="运行依赖请声明 workspace 内的明确本地项目路径")
            return asset
        finally:
            visiting.discard(path)

    selected = await build(source)
    # 先完成整个 runtime graph 的 admission/build，再发布任何真实项目资产。
    published = {wheel: _store_wheel(root, directory, wheel, name, created) for wheel, name in pending.items()}
    for wheel, asset in published.items():
        for name, requirement in frozen.items():
            frozen[name] = requirement.replace(wheel.as_uri(), asset.as_uri())
    return published[selected], frozen


def _constraints(config: dict, directory: Path, scheme: Version, frozen: dict[str, str]) -> None:
    uv = config.setdefault("tool", {}).setdefault("uv", {})
    existing = uv.get("constraint-dependencies", [])
    retained = []
    for raw in existing:
        requirement = Requirement(raw)
        name = canonicalize_name(requirement.name)
        managed = requirement.url and requirement.url.startswith("file:") and Path(unquote(urlparse(requirement.url).path)).is_relative_to(directory / ".solo-wheels")
        if name not in frozen or not managed:
            retained.append(raw)
    exact = f"scheme=={scheme}"
    if exact not in retained:
        retained.append(exact)
    for constraint in frozen.values():
        if constraint not in retained:
            retained.append(constraint)
    uv["constraint-dependencies"] = retained


def _cleanup_assets(created: list[Path], directory: Path) -> None:
    references = b"".join(path.read_bytes() for path in (directory / "pyproject.toml", directory / "uv.lock") if path.exists())
    for path in reversed(created):
        if path.parent.name.encode() in references:
            continue
        path.unlink(missing_ok=True)
        for parent in (path.parent, path.parent.parent, path.parent.parent.parent):
            try:
                parent.rmdir()
            except OSError:
                break


async def install_project(root: Path, current: dict[str, Any], candidate: dict[str, Any], *, dev: bool = True) -> str:
    with project_operation(current["project_id"]):
        return await _install_project(root, current, candidate, dev=dev)


async def _install_project(root: Path, current: dict[str, Any], candidate: dict[str, Any], *, dev: bool = True) -> str:
    if not compatible(current, candidate):
        raise web.HTTPError(422, reason="仅可安装同 Scheme 主版本和次版本的同类或上游项目")
    # 所有写入/安装状态之前由中央 API 校验当前和上游的退役 tag 身份。
    await check_project_release(current["project_id"])
    await check_project_release(candidate["id"])
    root = root.resolve()
    _project_directory(root, candidate["directory"])
    source = read_project(root, candidate["directory"])
    if source is None or source["project_id"] != candidate["id"] or source["path"] != candidate["directory"]:
        raise web.HTTPError(409, reason="待安装项目目录与项目记录不一致")
    if not _same_scheme_series(current.get("scheme_version"), source.get("scheme_version")):
        raise web.HTTPError(422, reason="仅可安装同 Scheme 主版本和次版本的同类或上游项目")
    directory, name = project_package(root, current)
    source_directory, package = project_package(root, source)
    if name == package:
        raise web.HTTPError(422, reason="不能安装与当前项目同包名的项目")
    _safe_path(root, directory / ".venv")
    if not _python(directory).exists():
        raise web.HTTPError(409, reason="当前项目环境尚未创建")
    key = str(directory)
    if key in _installing:
        raise web.HTTPError(409, reason="当前项目正在安装依赖")
    snapshots = {}
    for file in ("pyproject.toml", "uv.lock"):
        path = _safe_path(root, directory / file)
        if not path.is_file():
            raise web.HTTPError(422, reason="当前项目缺少有效的同主次版本 Scheme 锁定记录，请先重建环境")
        snapshots[path] = path.read_bytes()
    try:
        config = tomlkit.parse(snapshots[directory / "pyproject.toml"].decode("utf-8"))
        lock = tomllib.loads(snapshots[directory / "uv.lock"].decode("utf-8"))
        scheme = _locked_scheme(lock, current)
        uv = config.get("tool", {}).get("uv", {})
        for item in lock.get("package", []):
            location = item.get("source", {})
            own_root = canonicalize_name(item["name"]) == name and location in ({"editable": "."}, {"directory": "."})
            if {"editable", "directory"} & location.keys() and not own_root:
                raise web.HTTPError(422, reason="请先将当前项目的本地目录依赖冻结为 wheel，或移除该开发依赖")
        for location in uv.get("sources", {}).values():
            for record in location if isinstance(location, list) else [location]:
                if "path" in record and _safe_path(root, directory / record["path"]).is_dir():
                    raise web.HTTPError(422, reason="请先将当前项目的本地目录依赖冻结为 wheel，或移除该开发依赖")
        if any(canonicalize_name(Requirement(raw).name) == "scheme" for raw in uv.get("override-dependencies", [])):
            raise web.HTTPError(422, reason="请移除 Scheme override，使用正常依赖范围与 constraint")
        if uv.get("workspace"):
            raise web.HTTPError(422, reason="请使用独立项目安装，不支持 workspace 根安装")
        existing = [*config["project"].get("dependencies", []), *config.get("dependency-groups", {}).get("dev", [])]
        for raw in existing:
            if isinstance(raw, str):
                requirement = Requirement(raw)
                if canonicalize_name(requirement.name) == package and (requirement.extras or requirement.marker):
                    raise web.HTTPError(422, reason="安装接口暂不支持保留已有上游依赖的 extras 或 marker，请先明确移除该选择")
        _validate_project(config["project"]["version"], config["project"].get("dependencies", []), scheme)
        source_config = tomllib.loads((source_directory / "pyproject.toml").read_text(encoding="utf-8"))
        _validate_project(source_config["project"]["version"], source_config["project"].get("dependencies", []), scheme)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise web.HTTPError(422, reason="项目包配置或锁文件无效") from error
    environment = {**os.environ, "UV_LINK_MODE": "copy", "GIT_TERMINAL_PROMPT": "0"}
    for variable in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "UV_WORKING_DIR", "UV_PROJECT"):
        environment.pop(variable, None)
    _installing.add(key)
    temporary = None
    backup = None
    live_started = False
    committed = False
    created: list[Path] = []
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".solo-install-", dir=directory.parent))
        stage = temporary / "stage"
        stage.mkdir()
        for path, content in snapshots.items():
            (stage / path.name).write_bytes(content)
        before = await _metadata(directory, environment)
        actual_scheme = _actual_scheme(before)
        if actual_scheme != str(scheme):
            raise web.HTTPError(422, reason="当前 Scheme 实际版本与 uv.lock 不一致，请先重建环境")
        markers = {**default_environment(), **before["markers"]}
        stage_environment = {**environment, "UV_PROJECT_ENVIRONMENT": str(stage / ".venv"), "UV_PYTHON": before["python"]}
        original_sources = tomllib.loads(snapshots[directory / "pyproject.toml"].decode("utf-8")).get("tool", {}).get("uv", {}).get("sources", {})
        _relocate_sources(config, root, directory)
        _relocate_lock(lock, root, directory, stage, name)
        (stage / "uv.lock").write_text(tomlkit.dumps(lock), encoding="utf-8")
        wheel, frozen = await _freeze_runtime(root, directory, source_directory, temporary, scheme, stage_environment, markers, created)
        _constraints(config, directory, scheme, frozen)
        # 在另一分组删除旧声明，不触碰真实环境；目标分组由 uv add 更新。
        other = config["project"].get("dependencies", []) if dev else config.get("dependency-groups", {}).get("dev", [])
        for index in range(len(other) - 1, -1, -1):
            if isinstance(other[index], str) and canonicalize_name(Requirement(other[index]).name) == package:
                del other[index]
        (stage / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
        await _run(["uv", "add", "--project", str(stage), "--no-workspace", "--no-sync",
                    *(["--dev"] if dev else []), "--upgrade-package", package, "--", str(wheel)], stage, stage_environment)
        await _run(["uv", "lock", "--project", str(stage)], stage, stage_environment)
        stage_lock = tomllib.loads((stage / "uv.lock").read_text(encoding="utf-8"))
        if _locked_scheme(stage_lock, current) != scheme:
            raise web.HTTPError(422, reason="安装未保留当前 Scheme 锁定版本")
        await _run(["uv", "sync", "--project", str(stage), "--frozen", "--no-install-project", "--no-editable"], stage, stage_environment)
        await _verify_environment(stage, stage_environment, actual_scheme, stage_lock, markers)
        final_config = tomlkit.parse((stage / "pyproject.toml").read_text(encoding="utf-8"))
        _relocate_sources(final_config, root, stage, directory)
        final_sources = final_config.get("tool", {}).get("uv", {}).get("sources", {})
        for source_name, location in original_sources.items():
            if canonicalize_name(source_name) != package:
                final_sources[source_name] = location
        _relocate_lock(stage_lock, root, stage, directory, name)
        for path, content in snapshots.items():
            if path.read_bytes() != content:
                raise web.HTTPError(409, reason="安装预检期间项目配置发生变化，请重试")
        _safe_path(root, directory / ".venv")
        backup = temporary / "original-venv"
        (directory / ".venv").rename(backup)
        live_started = True
        (directory / "pyproject.toml").write_text(tomlkit.dumps(final_config), encoding="utf-8")
        (directory / "uv.lock").write_text(tomlkit.dumps(stage_lock), encoding="utf-8")
        live_environment = {**environment, "UV_PROJECT_ENVIRONMENT": str(directory / ".venv"), "UV_PYTHON": before["python"]}
        await _run(["uv", "sync", "--project", str(directory), "--frozen", "--no-editable"], directory, live_environment)
        await _verify_environment(directory, live_environment, actual_scheme, stage_lock, markers)
        committed = True
        shutil.rmtree(backup, ignore_errors=True)
        return package
    except BaseException as error:
        if live_started:
            for path, content in snapshots.items():
                path.write_bytes(content)
            partial = directory / ".venv"
            if partial.is_symlink():
                partial.unlink()
            elif partial.exists():
                shutil.rmtree(partial)
            if backup is not None and backup.exists():
                backup.rename(partial)
        _cleanup_assets(created, directory)
        if isinstance(error, TimeoutError):
            raise web.HTTPError(504, reason="uv 安装超时，请检查依赖下载网络") from error
        raise
    finally:
        # 恢复失败时留下本事务的备份，绝不能把唯一原环境当临时垃圾删除。
        if temporary is not None and (committed or backup is None or not backup.exists()):
            shutil.rmtree(temporary, ignore_errors=True)
        _installing.discard(key)


class DependenciesHandler(APIHandler):
    def current_project(self, path: str) -> dict[str, Any]:
        root = Path(self.settings["server_root_dir"]).resolve()
        _project_directory(root, path)
        project = read_project(root, path)
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
        if not isinstance(body.get("dev", True), bool):
            raise web.HTTPError(400, reason="dev 必须为布尔值")
        current = self.current_project(body["path"])
        candidate = next((project for project in await project_catalog() if project["id"] == body["project_id"]), None)
        if candidate is None:
            raise web.HTTPError(404, reason="待安装项目不存在或已归档")
        package = await install_project(Path(self.settings["server_root_dir"]).resolve(), current, candidate, dev=body.get("dev", True))
        self.finish({"package": package, "dev": body.get("dev", True)})
