"""安装项目 wheel；先在隔离环境验证，再以目录备份提交安装事务。"""

import asyncio
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from email.parser import BytesParser
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import tomllib
from typing import Any
from urllib.parse import unquote, urlencode, urlparse
from uuid import UUID, uuid4
from zipfile import ZipFile

from jupyter_server.base.handlers import APIHandler
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
import tomlkit
from tornado import web
from tornado.httpclient import AsyncHTTPClient, HTTPClientError
from tornado.ioloop import IOLoop, PeriodicCallback

from . import handlers
from .handlers import read_project
from .uv import uv_environment

PROJECT_KINDS = ("factor", "model", "optimize", "control", "execution")
_installing: set[str] = set()
_project_busy: set[str] = set()
_deletion_leases: dict[str, str] = {}
# An untrustworthy/missing intent must not permit further local source operations.
_recovery_blocked: set[str] = set()
_recovering_roots: set[str] = set()
_INSTALLATION_INTENT = ".solo-installation.json"
_INSTALL_RECOVERY_SECONDS = 60


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


_SOURCE_IGNORES = {".venv", ".git", ".solo-wheels", ".ipynb_checkpoints", "__pycache__", "dist", "build", ".pytest_cache", ".ruff_cache", _INSTALLATION_INTENT, _INSTALLATION_INTENT + ".*.tmp"}
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


async def _artifact_api(path: str, body: dict | None = None, *, binary: bool = False) -> Any:
    url = os.environ.get("SOLO_BACKEND_URL", "http://backend:8000").rstrip("/")
    try:
        response = await AsyncHTTPClient().fetch(
            url + "/api/v1/" + path, method="GET" if body is None else "POST",
            body=None if body is None else json.dumps(body),
            headers={"Content-Type": "application/json"}, request_timeout=90,
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
        raise web.HTTPError(error.code if error.code < 600 else 502, reason=_redact(message)) from error
    except (ValueError, TypeError) as error:
        raise web.HTTPError(502, reason="Solo artifact registry 响应无效") from error


async def artifact_catalog(*, published: bool | None = None, sha256: str | None = None) -> list[dict[str, Any]]:
    query = {}
    if published is not None:
        query["published"] = str(published).lower()
    if sha256 is not None:
        query["sha256"] = sha256
    result = await _artifact_api("artifacts" + ("?" + urlencode(query) if query else ""))
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise web.HTTPError(502, reason="Solo artifact 列表响应无效")
    return result


def _artifact_identity(value: Any) -> dict[str, Any]:
    """只接受能独立下载且能与原始 wheel 严格比较的 registry snapshot。"""
    try:
        if not isinstance(value, dict):
            raise ValueError("Expected an artifact snapshot")
        identifier = str(UUID(value["id"]))
        package = canonicalize_name(value["package"], validate=True)
        version = value["version"]
        Version(version)
        digest, filename = value["sha256"], value["filename"]
        if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                or not isinstance(filename, str) or not filename.endswith(".whl")
                or "/" in filename or "\\" in filename or ":" in filename
                or value.get("kind") not in (*PROJECT_KINDS, "dependency")):
            raise ValueError("Invalid artifact identity")
        return {**value, "id": identifier, "package": package, "version": version}
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise web.HTTPError(502, reason="Artifact snapshot 缺少有效的独立 wheel 身份") from error


def _artifact_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise web.HTTPError(502, reason="Artifact 依赖闭包响应无效")
    return value


def _same_artifact(first: dict, second: dict) -> bool:
    return all(first[key] == second[key] for key in ("id", "package", "version", "sha256", "filename"))


_INSTALL_RENEW_SECONDS = 300


async def _join_shielded(task: asyncio.Task) -> Any:
    """Drain shared work without forwarding caller cancellation; then re-raise that cancellation."""
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                break
            if cancellation is None:
                cancellation = error
        except BaseException:
            break
    if cancellation is not None:
        # Retrieve any exception without replacing the owner's original CancelledError.
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()


class _ArtifactInstallation:
    """本次安装的 durable GC 引用；不提前替换 consumer 已接受的依赖。"""

    def __init__(self, current: dict):
        self.current = current
        self.install_id = str(uuid4())
        self.path = f'projects/{current["project_id"]}/dependencies/installations'
        self.protected: dict[str, dict] = {}
        self.failure: Exception | None = None
        self.failure_cancelled_owner = False
        self.begun = False
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._heartbeat: asyncio.Task | None = None

    async def protect(self, snapshots: list[dict] | None = None) -> None:
        async with self._lock:
            requested = {item["id"]: item for item in map(_artifact_identity, snapshots or [])}
            expected = {**self.protected, **requested}
            result = await _artifact_api(self.path, {
                "install_id": self.install_id, "artifacts": list(requested), "renew": self.begun,
            })
            try:
                if not isinstance(result, dict) or str(UUID(result["install_id"])) != self.install_id:
                    raise ValueError("Wrong installation receipt")
                expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
                if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                    raise ValueError("Expired installation receipt")
                returned = [_artifact_identity(item) for item in _artifact_records(result["artifacts"])]
                protected = {item["id"]: item for item in returned}
                if any(identifier not in protected or not _same_artifact(item, protected[identifier])
                       for identifier, item in expected.items()):
                    raise ValueError("Missing exact protected artifact")
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise web.HTTPError(502, reason="安装保护响应缺少有效的 nonce、TTL 或确切 artifact") from error
            self.protected = protected
            self.begun = True

    def start(self) -> None:
        if self._heartbeat is not None or self._stopping.is_set():
            return
        owner = asyncio.current_task()

        async def renew() -> None:
            try:
                while True:
                    try:
                        await asyncio.wait_for(self._stopping.wait(), timeout=_INSTALL_RENEW_SECONDS)
                        return
                    except TimeoutError:
                        await self.protect()
            except Exception as error:
                self.failure = error
                # _run 的取消路径会先 kill/wait 子进程，才允许恢复正式文件和环境。
                if not self._stopping.is_set() and owner is not None and not owner.cancelling():
                    self.failure_cancelled_owner = True
                    owner.cancel()

        self._heartbeat = asyncio.create_task(renew())

    async def stop(self) -> None:
        self._stopping.set()
        if self._heartbeat is not None:
            try:
                await _join_shielded(self._heartbeat)
            except asyncio.CancelledError:
                # A heartbeat cancelled elsewhere must not make every later cleanup join fail.
                if not self._heartbeat.cancelled() or asyncio.current_task().cancelling():
                    raise
                if self.failure is None:
                    self.failure = web.HTTPError(502, reason="安装保护续期任务已取消，请重试")


async def _artifact_by_sha256(wheel: Path, installation: _ArtifactInstallation, *, required: bool = False) -> dict[str, Any] | None:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    results = await artifact_catalog(sha256=digest)
    if not results:
        if required:
            raise web.HTTPError(422, reason="冻结研究 wheel 缺少 registry snapshot，请先完成 artifact 迁移")
        return None
    if len(results) != 1:
        raise web.HTTPError(502, reason="Artifact 哈希必须对应唯一记录")
    snapshot = _artifact_identity(results[0])
    await installation.protect([snapshot])
    name, version, _ = _wheel_metadata(wheel)
    if (snapshot["sha256"] != digest or snapshot["package"] != name
            or snapshot["version"] != version or snapshot["filename"] != wheel.name):
        raise web.HTTPError(422, reason="冻结 wheel 与 artifact snapshot 不一致")
    return snapshot


async def _register_wheel(project: dict, current: dict, wheel: Path,
                          installation: _ArtifactInstallation, *, asset: bool = False) -> dict[str, Any]:
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    snapshot = _artifact_identity(await _artifact_api("artifacts/register", {
        "project_id": project["project_id"], "consumer_project_id": current["project_id"],
        "install_id": installation.install_id, "wheel": str(wheel), "sha256": digest,
    }))
    name, version, _ = _wheel_metadata(wheel)
    if (snapshot["sha256"] != digest or snapshot["package"] != name
            or snapshot["version"] != version or snapshot["filename"] != wheel.name
            or (snapshot["kind"] != "dependency" if asset else snapshot.get("sourceProjectId") != project["project_id"])):
        raise web.HTTPError(502, reason="新构建 wheel 的 registry 身份与实际源码项目不一致")
    return snapshot


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


def compatible_artifact(current: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if (current.get("retired") or candidate.get("retired") or not candidate.get("publishedAt")
            or candidate.get("sourceProjectId") == current["project_id"]):
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
    if path.is_absolute() or raw.startswith("/") or ".." in path.parts or "\\" in raw or ":" in raw:
        raise web.HTTPError(400, reason="Invalid project path")
    return _safe_path(root, root / path)


def _regular_bytes(root: Path, path: Path, *, limit: int | None = None, sync: bool = False) -> bytes:
    path = _safe_path(root, path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Installation recovery requires an unlinked regular file")
    if info.st_uid != path.parent.stat().st_uid:
        raise ValueError("Installation recovery file ownership differs from its project")
    if limit is not None and info.st_size > limit:
        raise ValueError("Installation recovery metadata is too large")
    with path.open("r+b" if sync else "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Installation recovery file changed while opening")
        content = stream.read() if limit is None else stream.read(limit + 1)
        if limit is not None and len(content) > limit:
            raise ValueError("Installation recovery metadata is too large")
        if sync:
            os.fsync(stream.fileno())
    return content


def _unique_json(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate installation recovery field")
        result[key] = value
    return result


def _intent_owner(root: Path, project: dict) -> Path:
    directory = _project_directory(root, project["path"])
    _regular_bytes(root, directory / ".solo", limit=65536)
    owner = handlers.read_project(root, project["path"])
    if (owner is None or owner["path"] != project["path"]
            or str(UUID(owner["project_id"])) != str(UUID(project["project_id"]))
            or (project.get("kind") is not None and owner["kind"] != project["kind"])):
        raise ValueError("Installation recovery metadata does not own this project directory")
    _safe_path(root, directory / ".venv")
    return directory


def _intent_hashes(root: Path, directory: Path, *, sync: bool = False) -> dict[str, str]:
    return {name: hashlib.sha256(_regular_bytes(root, directory / name, sync=sync)).hexdigest()
            for name in ("pyproject.toml", "uv.lock")}


def _read_installation_intent(root: Path, project: dict) -> tuple[dict, bytes]:
    directory = _intent_owner(root, project)
    content = _regular_bytes(root, directory / _INSTALLATION_INTENT, limit=16384)
    intent = json.loads(content, object_pairs_hook=_unique_json)
    if (not isinstance(intent, dict) or set(intent) != {"format", "project_id", "install_id", "operation", "hashes"}
            or type(intent["format"]) is not int or intent["format"] != 1
            or intent["operation"] not in {"complete", "abort"}
            or not isinstance(intent["project_id"], str) or not isinstance(intent["install_id"], str)
            or str(UUID(intent["project_id"])) != intent["project_id"]
            or str(UUID(intent["install_id"])) != intent["install_id"]
            or intent["project_id"] != str(UUID(project["project_id"]))
            or not isinstance(intent["hashes"], dict) or set(intent["hashes"]) != {"pyproject.toml", "uv.lock"}
            or any(not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                   for digest in intent["hashes"].values())):
        raise ValueError("Invalid installation finalization intent")
    if intent["hashes"] != _intent_hashes(root, directory):
        raise ValueError("Installation finalization intent no longer matches the project files")
    return intent, content


def _sync_directory(directory: Path) -> None:
    # Windows cannot open a directory for fsync; the file itself is always flushed.
    if os.name != "nt":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_installation_intent(root: Path, project: dict, install_id: str, operation: str) -> None:
    key = str(UUID(project["project_id"]))
    temporary = None
    try:
        directory = _intent_owner(root, project)
        path = _safe_path(root, directory / _INSTALLATION_INTENT)
        if path.exists():
            raise ValueError("An installation finalization intent already exists")
        if operation not in {"complete", "abort"} or str(UUID(install_id)) != install_id:
            raise ValueError("Invalid installation finalization operation")
        intent = {"format": 1, "project_id": key, "install_id": install_id, "operation": operation,
                  "hashes": _intent_hashes(root, directory, sync=True)}
        temporary = _safe_path(root, directory / f"{_INSTALLATION_INTENT}.{uuid4()}.tmp")
        with temporary.open("xb") as stream:
            stream.write(json.dumps(intent, sort_keys=True).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        # Recheck before publishing, without following any newly introduced links.
        _intent_owner(root, project)
        if _safe_path(root, path).exists():
            raise ValueError("Installation finalization intent appeared while writing")
        os.replace(temporary, path)
        _sync_directory(directory)
        _read_installation_intent(root, project)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, web.HTTPError) as error:
        _recovery_blocked.add(key)
        raise web.HTTPError(409, reason="无法持久记录安装最终状态；保留 Backend 保护，请先修复项目恢复记录") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def recover_installation(root: Path, project: dict) -> None:
    """Caller holds project_operation; only replay a validated final state, never infer it from TTL."""
    root = root.resolve()
    key = str(UUID(project["project_id"]))
    path = _safe_path(root, _project_directory(root, project["path"]) / _INSTALLATION_INTENT)
    try:
        path.lstat()
    except FileNotFoundError:
        if key in _recovery_blocked:
            raise web.HTTPError(409, reason="项目安装恢复状态不可信；保留 Backend 保护，请先修复恢复记录")
        return
    try:
        intent, content = _read_installation_intent(root, project)
        result = await _artifact_api(
            f'projects/{key}/dependencies/installations/{intent["install_id"]}/{intent["operation"]}', {})
        flag = "completed" if intent["operation"] == "complete" else "aborted"
        if (not isinstance(result, dict) or result.get("install_id") != intent["install_id"]
                or result.get(flag) is not True or result.get("aborted" if flag == "completed" else "completed", False) is not False):
            raise ValueError("Installation finalization response does not match the intent")
        # An RPC may have committed despite a lost reply. Never discard a changed intent/files.
        current, current_content = _read_installation_intent(root, project)
        if current != intent or current_content != content:
            raise ValueError("Installation finalization intent changed during recovery")
        _safe_path(root, path).unlink()
        _sync_directory(path.parent)
        _recovery_blocked.discard(key)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, web.HTTPError) as error:
        _recovery_blocked.add(key)
        raise web.HTTPError(409, reason="项目安装最终确认尚未恢复；请先恢复 Backend 连接或修复项目恢复记录") from error


async def recover_installations(root: Path) -> None:
    """Only registered, owned root projects; no notebook/source execution or recursive crawling."""
    root = root.resolve()
    key = str(root)
    if key in _recovering_roots:
        return
    _recovering_roots.add(key)
    try:
        for record in await project_catalog():
            try:
                identifier = str(UUID(record["id"]))
                relative = Path(record["directory"])
                if (record["kind"] not in PROJECT_KINDS or len(relative.parts) != 3
                        or relative.parts[:2] != ("projects", record["kind"]) or identifier in _project_busy):
                    continue
                directory = _project_directory(root, record["directory"])
                marker = _safe_path(root, directory / _INSTALLATION_INTENT)
                if not marker.exists():
                    continue
                project = handlers.read_project(root, record["directory"])
                if (project is None or project["path"] != record["directory"]
                        or str(UUID(project["project_id"])) != identifier or project["kind"] != record["kind"]
                        or project["name"] != record["name"]):
                    continue
                with project_operation(identifier):
                    await recover_installation(root, project)
            except (OSError, ValueError, KeyError, TypeError, AttributeError, web.HTTPError) as error:
                logging.getLogger(__name__).warning("Artifact install recovery retained its protection: %s", _redact(str(error)))
    except Exception as error:
        logging.getLogger(__name__).warning("Artifact install recovery cannot read project catalog: %s", _redact(str(error)))
    finally:
        _recovering_roots.discard(key)


def start_installation_recovery(root: Path) -> PeriodicCallback:
    callback = PeriodicCallback(lambda: recover_installations(root), _INSTALL_RECOVERY_SECONDS * 1000)
    callback.start()
    IOLoop.current().spawn_callback(recover_installations, root)
    return callback


class RecoveringProjectHandler(handlers.ProjectHandler):
    @web.authenticated
    async def get(self) -> None:
        root = Path(self.settings["server_root_dir"])
        path = self.get_query_argument("path", "")
        _project_directory(root.resolve(), path)
        project = handlers.read_project(root, path)
        if project is not None:
            with project_operation(project["project_id"]):
                await recover_installation(root, project)
        self.finish({"project": project})


def project_package(root: Path, project: dict[str, Any]) -> tuple[Path, str]:
    directory = _project_directory(root, project["path"])
    path = _safe_path(root, directory / "pyproject.toml")
    try:
        name = canonicalize_name(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["name"], validate=True)
        if project.get("package_name") is not None and canonicalize_name(project["package_name"]) != name:
            raise web.HTTPError(422, reason="项目包名与 .solo 显式身份不一致")
        return directory, name
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


def _wheel_metadata(wheel: Path, seen: set[Path] | None = None, *, local_urls: bool = True) -> tuple[str, str, list[str]]:
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
            if local_urls and requirement.url and urlparse(requirement.url).scheme == "file":
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


async def _stage_artifact_graph(root: Path, directory: Path, selected: list[dict], temporary: Path,
                                scheme: Version, created: list[Path],
                                installation: _ArtifactInstallation) -> tuple[dict[str, Path], dict[str, dict]]:
    """下载独立闭包，不读取 originProject 目录，也不传播任何上游 Scheme source。"""
    staged: dict[str, Path] = {}
    snapshots: dict[str, dict] = {}
    identifiers: dict[str, dict] = {}
    pending = [(item, True) for item in selected]
    while pending:
        item, expand = pending.pop(0)
        advertised = _artifact_identity(item)
        if advertised["package"] == "scheme":
            continue
        if advertised["id"] in identifiers:
            if not _same_artifact(identifiers[advertised["id"]], advertised):
                raise web.HTTPError(422, reason="Artifact 闭包同一 ID 的快照冲突")
            continue
        if len(identifiers) >= 64:
            raise web.HTTPError(422, reason="Artifact 运行依赖闭包过大")
        await installation.protect([advertised])
        snapshot = _artifact_identity(await _artifact_api(f'artifacts/{advertised["id"]}'))
        if not _same_artifact(advertised, snapshot):
            raise web.HTTPError(422, reason="Artifact 详情与冻结快照不一致")
        if snapshot.get("retired"):
            raise web.HTTPError(422, reason="Artifact 闭包包含已退役版本")
        if snapshot["kind"] in PROJECT_KINDS and not _same_scheme_series(str(scheme), snapshot.get("schemeVersion")):
            raise web.HTTPError(422, reason="Artifact 闭包 Scheme 主版本和次版本不一致")
        package = snapshot["package"]
        if package in snapshots and not _same_artifact(snapshots[package], snapshot):
            raise web.HTTPError(422, reason=f"Artifact 闭包包 {package} 的冻结来源冲突")
        identifiers[snapshot["id"]] = snapshot
        snapshots[package] = snapshot
        if expand:
            # A published root has a sealed, flattened exact closure. A descendant's
            # historical source/dev graph is not part of that installation.
            pending.extend((item, not bool(snapshot.get("publishedAt")))
                           for item in _artifact_records(snapshot.get("dependencies", {})))
    # Backend 只保护提交的确切 ID；先遍历/保护完整已知闭包，再取任何 wheel 字节。
    for package, snapshot in snapshots.items():
        download = temporary / "downloads" / snapshot["sha256"] / snapshot["filename"]
        download.parent.mkdir(parents=True, exist_ok=True)
        content = await _artifact_api(f'artifacts/{snapshot["id"]}/wheel', binary=True)
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != snapshot["sha256"]:
            raise web.HTTPError(422, reason="下载 wheel 的内容与 artifact 哈希不一致")
        download.write_bytes(content)
        name, version, requirements = _wheel_metadata(download, local_urls=False)
        if any(Requirement(raw).url and urlparse(Requirement(raw).url).scheme == "file" for raw in requirements):
            raise web.HTTPError(422, reason="发布 wheel 的 Requires-Dist 不能引用原始本地路径，请使用独立 artifact 闭包")
        if name != package or version != snapshot["version"]:
            raise web.HTTPError(422, reason="下载 wheel 的 METADATA 与 artifact snapshot 不一致")
        if snapshot["kind"] in PROJECT_KINDS:
            _validate_project(version, requirements, scheme)
        staged[package] = _store_wheel(root, directory, download, name, created)
    return staged, snapshots


async def _freeze_runtime(root: Path, directory: Path, source: Path, temporary: Path, scheme: Version,
                          environment: dict[str, str], markers: dict, created: list[Path],
                          current: dict, installation: _ArtifactInstallation) -> tuple[Path, dict[str, str], dict[str, dict]]:
    """只沿 wheel 的 runtime Requires-Dist 访问本地 source，不遍历 dev/workspace 图。"""
    frozen: dict[str, str] = {}
    pending: dict[Path, str] = {}
    built: dict[Path, tuple[Path, list[str], dict, dict | None]] = {}
    copied_artifacts: dict[Path, dict] = {}
    git_wheels: dict[tuple[str, str, str | None], Path] = {}
    visiting: set[Path] = set()
    checked: set[tuple[Path, frozenset[str]]] = set()
    operations = ExitStack()
    guarded = {str(UUID(current["project_id"])): directory}

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
                research = path == source or (path / ".solo").exists()
                metadata = None
                if research:
                    metadata = read_project(root, path.relative_to(root).as_posix())
                    if metadata is None or metadata["path"] != path.relative_to(root).as_posix():
                        raise web.HTTPError(422, reason="本地项目元数据无效")
                    identifier = str(UUID(metadata["project_id"]))
                    if path == source:
                        guarded[identifier] = path  # install_project 已持有选中 root 的入口。
                    elif identifier not in guarded:
                        operations.enter_context(project_operation(identifier))
                        guarded[identifier] = path
                    elif guarded[identifier] != path:
                        raise web.HTTPError(422, reason="运行依赖存在重复项目身份")
                    if path != source:
                        await recover_installation(root, metadata)
                        await check_project_release(identifier)
                    if not _same_scheme_series(metadata.get("scheme_version"), str(scheme)):
                        raise web.HTTPError(422, reason="本地项目 Scheme 主版本和次版本不一致")
                config = tomllib.loads(_safe_path(root, path / "pyproject.toml").read_text(encoding="utf-8"))
                project = config["project"]
                name = canonicalize_name(project["name"], validate=True)
                if expected is not None and name != expected:
                    raise web.HTTPError(422, reason="本地依赖包名与 source 声明不一致")
                if metadata is not None:
                    if metadata.get("package_name") is not None and canonicalize_name(metadata["package_name"]) != name:
                        raise web.HTTPError(422, reason="本地项目包名与 .solo 显式身份不一致")
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
                built[path] = asset, requirements, config, metadata
            asset, requirements, config, metadata = built[path]
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
                            copied = await _artifact_by_sha256(wheel, installation)
                            if copied is not None:
                                copied_artifacts[wheel] = copied
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

    with operations:
        selected = await build(source)
        # 先完成整个 runtime graph 的 admission/build/copy，才注册实际源码身份。
        published = {wheel: _store_wheel(root, directory, wheel, name, created) for wheel, name in pending.items()}
        for wheel, asset in published.items():
            for name, requirement in frozen.items():
                frozen[name] = requirement.replace(wheel.as_uri(), asset.as_uri())
        snapshots: dict[str, dict] = {}
        for _, (wheel, _, _, metadata) in reversed(list(built.items())):
            if metadata is not None:
                snapshot = await _register_wheel(metadata, current, published[wheel], installation)
                if snapshot["package"] in snapshots and not _same_artifact(snapshots[snapshot["package"]], snapshot):
                    raise web.HTTPError(422, reason="源码运行闭包包身份冲突")
                snapshots[snapshot["package"]] = snapshot
        if copied_artifacts:
            closure, copied_snapshots = await _stage_artifact_graph(
                root, directory, list(copied_artifacts.values()), temporary, scheme, created, installation,
            )
            for name, asset in closure.items():
                record(name, f"{name} @ {asset.as_uri()}")
            for name, snapshot in copied_snapshots.items():
                if name in snapshots and not _same_artifact(snapshots[name], snapshot):
                    raise web.HTTPError(422, reason="源码和冻结 artifact 闭包包身份冲突")
                snapshots[name] = snapshot
        return published[selected], frozen, snapshots


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


async def _installed_artifacts(root: Path, directory: Path, lock: dict, known: dict[str, dict],
                               installation: _ArtifactInstallation) -> list[dict]:
    accepted = []
    for item in lock.get("package", []):
        name = canonicalize_name(item["name"])
        raw = item.get("source", {}).get("path")
        if name == "scheme" or not isinstance(raw, str):
            continue
        wheel = _safe_path(root, directory / raw)
        if wheel.suffix != ".whl":
            raise web.HTTPError(422, reason="正式锁文件的本地依赖必须为冻结 wheel")
        snapshot = known.get(name)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if snapshot is None or snapshot["sha256"] != digest:
            # 显式 kind-4hex 研究包不能在旧副本缺失迁移 snapshot 时悄悄降级为系统包。
            research = re.fullmatch(r"(?:factor|model|optimize|control|execution)-[0-9a-f]{4}", name) is not None
            snapshot = await _artifact_by_sha256(wheel, installation, required=research or name in known)
        if snapshot is None:
            # 非研究 local/Git wheel 也用 consumer-owned nonce 原子注册，最终接受不能漏掉实际本地资产。
            snapshot = await _register_wheel(installation.current, installation.current, wheel, installation, asset=True)
        wheel_name, version, _ = _wheel_metadata(wheel)
        if (snapshot["package"] != wheel_name or snapshot["version"] != version
                or snapshot["version"] != item["version"] or snapshot["filename"] != wheel.name
                or snapshot["sha256"] != digest):
            raise web.HTTPError(422, reason="已安装 artifact 与最终锁文件或 wheel 不一致")
        accepted.append(snapshot)
    return accepted


async def _accept_dependencies(current: dict, snapshots: list[dict], installation: _ArtifactInstallation) -> None:
    identifiers = list(dict.fromkeys(snapshot["id"] for snapshot in snapshots))
    result = await _artifact_api(f'projects/{current["project_id"]}/dependencies', {
        "install_id": installation.install_id, "artifacts": identifiers,
    })
    if not isinstance(result, dict) or "artifacts" not in result:
        raise web.HTTPError(502, reason="依赖接受响应无效")
    returned = [_artifact_identity(item) for item in _artifact_records(result["artifacts"])]
    accepted = {item["id"]: item for item in returned}
    if (set(accepted) != set(identifiers) or len(returned) != len(accepted)
            or any(not _same_artifact(snapshot, accepted[snapshot["id"]]) for snapshot in snapshots)):
        raise web.HTTPError(502, reason="依赖接受响应缺少已安装的确切 artifact snapshot")


async def install_project(root: Path, current: dict[str, Any], candidate: dict[str, Any], *, dev: bool = True) -> str:
    if not compatible(current, candidate):
        raise web.HTTPError(422, reason="仅可安装同 Scheme 主版本和次版本的同类或上游项目")
    with project_operation(current["project_id"]), project_operation(candidate["id"]):
        await recover_installation(root, current)
        await recover_installation(root, {"project_id": candidate["id"], "path": candidate["directory"], "kind": candidate["kind"]})
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
    source_directory, package = project_package(root, source)
    return await _install_common(root, current, package, source_directory=source_directory, dev=dev)


async def install_artifact(root: Path, current: dict[str, Any], candidate: dict[str, Any], *, dev: bool = True) -> str:
    if not compatible_artifact(current, candidate):
        raise web.HTTPError(422, reason="仅可安装已发布的同 Scheme 主次版本的同类或上游 artifact")
    snapshot = _artifact_identity(candidate)
    with project_operation(current["project_id"]):
        await recover_installation(root, current)
        await check_project_release(current["project_id"])
        return await _install_common(root.resolve(), current, snapshot["package"], artifact=snapshot, dev=dev)


async def _install_common(root: Path, current: dict, package: str, *, source_directory: Path | None = None,
                          artifact: dict | None = None, dev: bool = True) -> str:
    directory, name = project_package(root, current)
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
        if source_directory is not None:
            source_config = tomllib.loads((source_directory / "pyproject.toml").read_text(encoding="utf-8"))["project"]
            _validate_project(source_config["version"], source_config.get("dependencies", []), scheme)
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise web.HTTPError(422, reason="项目包配置或锁文件无效") from error
    environment = {**os.environ, **uv_environment(directory), "GIT_TERMINAL_PROMPT": "0"}
    for variable in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT", "UV_WORKING_DIR", "UV_PROJECT"):
        environment.pop(variable, None)
    _installing.add(key)
    temporary = None
    backup = None
    live_started = False
    committed = False
    created: list[Path] = []
    installation = _ArtifactInstallation(current)

    async def finalize(operation: str) -> None:
        try:
            await recover_installation(root, current)
        except Exception as finish_error:
            logging.getLogger(__name__).warning(
                "Artifact install %s awaiting durable replay: %s", operation, _redact(str(finish_error)))

    async def rollback() -> None:
        await installation.stop()
        if live_started:
            for path, content in snapshots.items():
                _safe_path(root, path).write_bytes(content)
            partial = directory / ".venv"
            if partial.is_symlink():
                partial.unlink()
            elif partial.exists():
                _safe_path(root, partial)
                shutil.rmtree(partial)
            if backup is None or not backup.exists():
                raise OSError("Original environment backup is missing; installation references retained")
            backup.rename(partial)
        _cleanup_assets(created, directory)
        # Only a completed filesystem restoration can authorize compensation.
        _write_installation_intent(root, current, installation.install_id, "abort")
        await finalize("abort")

    try:
        # 即使 begin 响应丢失，失败路径也只能在恢复文件之后 abort 同一 nonce。
        await installation.protect()
        installation.start()
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
        if artifact is not None:
            closure, artifact_snapshots = await _stage_artifact_graph(root, directory, [artifact], temporary, scheme, created, installation)
            if name in closure:
                raise web.HTTPError(422, reason="Artifact 闭包不能覆盖当前项目包")
            wheel = closure[package]
            frozen = {name: f"{name} @ {asset.as_uri()}" for name, asset in closure.items() if name != package}
            # 闭包全部本地 staging/约束，安装仍遵循 root 的 Requires-Dist 与用户原 dev/runtime 分组。
        else:
            wheel, frozen, artifact_snapshots = await _freeze_runtime(
                root, directory, source_directory, temporary, scheme, stage_environment, markers, created, current, installation,
            )
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
        installed_snapshots = await _installed_artifacts(root, directory, stage_lock, artifact_snapshots, installation)
        await installation.stop()
        if installation.failure is not None:
            raise installation.failure
        await _accept_dependencies(current, installed_snapshots, installation)
        # No await between acceptance and durable intent; keep the unique backup until this succeeds.
        committed = True
        _write_installation_intent(root, current, installation.install_id, "complete")
        shutil.rmtree(backup, ignore_errors=True)
        await _join_shielded(asyncio.create_task(finalize("complete")))
        return package
    except BaseException as error:
        if committed:
            # New FS stays live; an unwriteable intent also retains the unique old environment.
            raise
        if (isinstance(error, asyncio.CancelledError) and installation.failure is not None
                and installation.failure_cancelled_owner):
            error = installation.failure
        try:
            await _join_shielded(asyncio.create_task(rollback()))
        except BaseException as cleanup_error:
            _recovery_blocked.add(str(UUID(current["project_id"])))
            if isinstance(error, asyncio.CancelledError):
                logging.getLogger(__name__).error("Cancelled installation recovery remains blocked: %s", _redact(str(cleanup_error)))
                raise error
            raise cleanup_error from error
        if isinstance(error, TimeoutError):
            raise web.HTTPError(504, reason="uv 安装超时，请检查依赖下载网络") from error
        raise error
    finally:
        # 恢复/记录失败时留下备份，绝不能把唯一原环境当临时垃圾删除。
        if temporary is not None and (backup is None or not backup.exists()):
            shutil.rmtree(temporary, ignore_errors=True)
        _installing.discard(key)


def _operation_identifier(raw: Any) -> str:
    try:
        return str(UUID(raw))
    except (ValueError, TypeError, AttributeError) as error:
        raise web.HTTPError(422, reason="项目 ID 无效") from error


def _deletion_nonce(body: Any) -> str:
    # Keep legacy journal tokens recoverable; new Backend requests use UUID nonces.
    lease = body.get("lease") if isinstance(body, dict) else None
    if not isinstance(lease, str) or not lease.strip() or len(lease) > 128:
        raise web.HTTPError(400, reason="缺少有效删除 lease")
    return lease


class ProjectOperationsHandler(APIHandler):
    @web.authenticated
    async def get(self) -> None:
        identifier = _operation_identifier(self.get_query_argument("project_id", ""))
        self.finish({"project_id": identifier, "busy": identifier in _project_busy})

    @web.authenticated
    async def post(self) -> None:
        body = self.get_json_body()
        if not isinstance(body, dict) or body.get("operation") != "delete":
            raise web.HTTPError(400, reason="缺少 delete 操作")
        identifier = _operation_identifier(body.get("project_id"))
        lease = _deletion_nonce(body)
        existing = _deletion_leases.get(identifier)
        if existing is not None:
            if existing != lease:
                raise web.HTTPError(409, reason="删除 lease 不属于当前请求")
            # The original POST may have granted the lease before its response was lost.
        elif identifier in _project_busy:
            raise web.HTTPError(409, reason="当前项目正在安装依赖、保存版本或删除，请稍后重试")
        _project_busy.add(identifier)
        _deletion_leases[identifier] = lease
        self.finish({"project_id": identifier, "lease": lease})

    @web.authenticated
    async def delete(self) -> None:
        body = self.get_json_body()
        requested = _deletion_nonce(body)
        identifier = _operation_identifier(body.get("project_id"))
        lease = _deletion_leases.get(identifier)
        if lease is not None:
            if lease != requested:
                raise web.HTTPError(409, reason="删除 lease 不属于当前请求")
            del _deletion_leases[identifier]
            _project_busy.discard(identifier)
        # 服务重启/已释放均幂等；不能释放普通 project_operation 的入口。
        self.set_status(204)
        self.finish()


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
        with project_operation(current["project_id"]):
            await recover_installation(Path(self.settings["server_root_dir"]), current)
        candidates = [project for project in await project_catalog() if compatible(current, project)]
        artifacts = [item for item in await artifact_catalog(published=True) if compatible_artifact(current, item)]
        self.finish({"projects": candidates, "artifacts": artifacts})

    @web.authenticated
    async def post(self) -> None:
        body = self.get_json_body()
        if not isinstance(body, dict) or not isinstance(body.get("path"), str):
            raise web.HTTPError(400, reason="缺少当前项目路径")
        live = body.get("live_project_id", body.get("project_id"))
        artifact = body.get("artifact_id")
        if (bool(live) == bool(artifact) or (live is not None and not isinstance(live, str))
                or (artifact is not None and not isinstance(artifact, str))):
            raise web.HTTPError(400, reason="请选择唯一的 live_project_id 或 artifact_id")
        if not isinstance(body.get("dev", True), bool):
            raise web.HTTPError(400, reason="dev 必须为布尔值")
        current = self.current_project(body["path"])
        root = Path(self.settings["server_root_dir"]).resolve()
        if artifact is not None:
            identifier = _operation_identifier(artifact)
            candidate = await _artifact_api(f"artifacts/{identifier}")
            package = await install_artifact(root, current, candidate, dev=body.get("dev", True))
        else:
            candidate = next((project for project in await project_catalog() if project["id"] == live), None)
            if candidate is None:
                raise web.HTTPError(404, reason="待安装源码项目不存在")
            package = await install_project(root, current, candidate, dev=body.get("dev", True))
        self.finish({"package": package, "dev": body.get("dev", True)})
