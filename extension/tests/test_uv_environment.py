import asyncio
import json
import os
from pathlib import Path
import sys
import tomllib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import tomlkit
from solo_jupyter import dependencies, versions
from solo_jupyter.uv import uv_environment


@pytest.mark.parametrize("relative, volume", [
    ("projects", "projects"),
    ("projects/model/current", "projects"),
    ("projects/model/.solo-install-test/stage", "projects"),
    ("runs", "runs"),
    ("runs/frozen-build", "runs"),
    ("runs/frozen-build/environment", "runs"),
    ("artifacts", "artifacts"),
    ("artifacts/release/environment", "artifacts"),
])
@pytest.mark.parametrize("root_setting", ["default", "environment", "explicit"])
def test_uv_environment_routes_to_target_mount(tmp_path, monkeypatch, relative, volume, root_setting):
    if root_setting == "default":
        monkeypatch.delenv("SOLO_SHARED_DIR", raising=False)
        root = Path("/shared").resolve()
        options = {}
    else:
        root = tmp_path / "shared"
        monkeypatch.setenv("SOLO_SHARED_DIR", str(root if root_setting == "environment" else tmp_path / "other"))
        options = {"shared_root": root} if root_setting == "explicit" else {}
    assert uv_environment(root / relative, **options) == {
        "UV_LINK_MODE": "hardlink", "UV_CACHE_DIR": str(root / volume / ".uv-cache"),
    }


@pytest.mark.parametrize("relative", [
    "outside/project", "shared/projects-other/project", "shared/runs-other/environment",
    "shared/artifacts-other/environment", "shared/unknown/projects/project",
])
def test_uv_environment_outside_mount_uses_directory_parent_without_creating_cache(tmp_path, monkeypatch, relative):
    monkeypatch.setenv("SOLO_SHARED_DIR", str(tmp_path / "shared"))
    directory = tmp_path / relative
    cache = directory.parent / ".uv-cache"
    assert uv_environment(directory) == {"UV_LINK_MODE": "hardlink", "UV_CACHE_DIR": str(cache)}
    assert not cache.exists()


def test_uv_environment_normalizes_relative_paths_and_parent_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SOLO_SHARED_DIR", "shared")
    assert uv_environment(Path("shared/projects/model/../factor/project"))["UV_CACHE_DIR"] == str(tmp_path / "shared/projects/.uv-cache")
    outside = Path("shared/projects/../../outside/project")
    assert uv_environment(outside)["UV_CACHE_DIR"] == str(tmp_path / "outside/.uv-cache")


@pytest.fixture
def inherited_environment(monkeypatch, tmp_path):
    inherited = {
        "UV_LINK_MODE": "copy", "UV_CACHE_DIR": str(tmp_path / "wrong-volume-cache"),
        "GIT_TERMINAL_PROMPT": "1", "UV_INDEX_URL": "https://private.example/simple",
        "UV_EXTRA_INDEX_URL": "https://extra.example/simple", "UV_DEFAULT_INDEX": "https://default.example/simple",
        "UV_INDEX": "private=https://named.example/simple", "UV_OFFLINE": "1",
        "VIRTUAL_ENV": "inherited-venv", "PYTHONPATH": "inherited-pythonpath",
        "PYTHONHOME": "inherited-pythonhome", "UV_PROJECT_ENVIRONMENT": "inherited-project-environment",
        "UV_WORKING_DIR": "inherited-working-directory", "UV_PROJECT": "inherited-project",
    }
    for name, value in inherited.items():
        monkeypatch.setenv(name, value)
    return inherited


@pytest.mark.parametrize("relative, cache_relative", [
    ("projects/model/current", "projects/.uv-cache"),
    ("projects/model/.solo-install-test/stage", "projects/.uv-cache"),
    ("runs/frozen-build", "runs/.uv-cache"),
    ("runs/frozen-build/environment", "runs/.uv-cache"),
    ("artifacts/release/environment", "artifacts/.uv-cache"),
    ("outside/project", "outside/.uv-cache"),
])
def test_versions_command_passes_routed_hardlink_environment_to_subprocess(
    tmp_path, monkeypatch, inherited_environment, relative, cache_relative,
):
    monkeypatch.setenv("SOLO_SHARED_DIR", str(tmp_path))
    directory = tmp_path / relative
    execute = Mock(return_value=SimpleNamespace(returncode=0, stdout="output", stderr=""))
    monkeypatch.setattr(versions.subprocess, "run", execute)
    payload = {"parameters": {"count": 2}}
    arguments = ["uv", "lock", "--project", str(directory)]
    assert versions.command(arguments, directory, payload) == "output"
    kwargs = execute.call_args.kwargs
    environment = kwargs.pop("env")
    assert execute.call_args.args == (arguments,)
    assert kwargs == {"cwd": directory, "input": json.dumps(payload), "text": True,
                      "capture_output": True, "timeout": 600, "check": False}
    assert environment["UV_LINK_MODE"] == "hardlink"
    assert environment["UV_CACHE_DIR"] == str(tmp_path / cache_relative)
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_PROJECT_ENVIRONMENT"):
        assert name not in environment
    for name in ("UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX", "UV_OFFLINE"):
        assert environment[name] == inherited_environment[name]
    assert all(os.environ[name] == value for name, value in inherited_environment.items())


@pytest.mark.parametrize("mode", ["source", "artifact"])
@pytest.mark.parametrize("shared", [True, False], ids=["shared-mount", "outside-shared"])
@pytest.mark.parametrize("accept_failure", [False, True], ids=["commit", "rollback"])
def test_install_common_subprocesses_share_own_directory_cache(
    tmp_path, monkeypatch, inherited_environment, mode, shared, accept_failure,
):
    root = tmp_path / "workspace"
    monkeypatch.setenv("SOLO_SHARED_DIR", str(root if shared else tmp_path / "other-shared"))
    current = {"project_id": str(uuid4()), "path": "projects/model/current", "scheme_version": "1.2.0",
               "name": "current", "kind": "model"}
    directory = root / current["path"]
    directory.mkdir(parents=True)
    (directory / ".solo").write_text(json.dumps(current), encoding="utf-8")
    config = {"project": {"name": "current-package", "version": "1.2.0", "dependencies": ["scheme>=1.2.0,<1.3.0"]},
              "tool": {"uv": {"index": [{"name": "private", "url": "https://private.example/simple", "default": True}]}}}
    scheme_source = {"git": "https://git.example/scheme.git#" + "a" * 40}
    lock = {"version": 1, "package": [{"name": "scheme", "version": "1.2.0", "source": scheme_source}]}
    (directory / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    (directory / "uv.lock").write_text(tomlkit.dumps(lock), encoding="utf-8")
    original = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    interpreter = dependencies._python(directory)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"original environment")
    # The selected source is on a different logical volume; cache ownership is the consumer's.
    source = root / "runs/upstream"
    source.mkdir(parents=True)
    source_config = {"project": {"name": "source-package", "version": "1.2.7", "dependencies": ["scheme>=1.2.0,<1.3.0"]}}
    (source / "pyproject.toml").write_text(tomlkit.dumps(source_config), encoding="utf-8")
    wheel = source / "source_package-1.2.7-py3-none-any.whl"
    wheel.write_bytes(b"test wheel")
    installation = SimpleNamespace(install_id=str(uuid4()), protect=AsyncMock(), start=Mock(), stop=AsyncMock(),
                                   failure=None, failure_cancelled_owner=False)
    monkeypatch.setattr(dependencies, "_ArtifactInstallation", lambda _: installation)
    operation = "abort" if accept_failure else "complete"
    finalize = AsyncMock(return_value={"install_id": installation.install_id,
                                     "aborted" if accept_failure else "completed": True})
    monkeypatch.setattr(dependencies, "_artifact_api", finalize)
    monkeypatch.setattr(dependencies, "_installed_artifacts", AsyncMock(return_value=[]))
    accept = AsyncMock(side_effect=RuntimeError("accept rejected") if accept_failure else None)
    monkeypatch.setattr(dependencies, "_accept_dependencies", accept)
    metadata = {"python": sys.executable, "markers": {"python_version": "3.12", "python_full_version": "3.12.12"},
                "packages": [{"name": "scheme", "version": "1.2.0", "requires": []}]}
    calls = []

    async def subprocess_exec(*arguments, cwd, env, stdout, stderr):
        calls.append((arguments, cwd, dict(env)))
        if arguments[:2] == ("uv", "sync"):
            target = dependencies._python(cwd)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"new environment")
        output = json.dumps(metadata).encode() if "-c" in arguments else b""
        return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(output, None)), kill=Mock())

    monkeypatch.setattr(dependencies.asyncio, "create_subprocess_exec", subprocess_exec)

    async def freeze(*arguments):
        await dependencies._run(["uv", "build", "--wheel"], arguments[3], arguments[5])
        return wheel, {}, {}

    monkeypatch.setattr(dependencies, "_freeze_runtime", freeze)
    monkeypatch.setattr(dependencies, "_stage_artifact_graph", AsyncMock(return_value=({"source-package": wheel}, {})))
    options = {"source_directory": source} if mode == "source" else {"artifact": {"package": "source-package"}}
    if accept_failure:
        with pytest.raises(RuntimeError, match="accept rejected"):
            asyncio.run(dependencies._install_common(root, current, "source-package", **options))
        assert {name: (directory / name).read_bytes() for name in original} == original
        assert interpreter.read_bytes() == b"original environment"
    else:
        assert asyncio.run(dependencies._install_common(root, current, "source-package", **options)) == "source-package"
        assert interpreter.read_bytes() == b"new environment"
    assert tomllib.loads((directory / "uv.lock").read_text(encoding="utf-8"))["package"][0]["source"] == scheme_source
    assert tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["uv"]["index"] == config["tool"]["uv"]["index"]
    expected_cache = root / "projects/.uv-cache" if shared else directory.parent / ".uv-cache"
    assert calls
    for arguments, cwd, environment in calls:
        assert environment["UV_LINK_MODE"] == "hardlink"
        assert environment["UV_CACHE_DIR"] == str(expected_cache)
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert cwd == directory or cwd.is_relative_to(directory.parent)
        for name in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "UV_WORKING_DIR", "UV_PROJECT"):
            assert name not in environment
        for name in ("UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX", "UV_OFFLINE"):
            assert environment[name] == inherited_environment[name]
    sync_calls = [(cwd, environment) for arguments, cwd, environment in calls if arguments[:2] == ("uv", "sync")]
    assert len(sync_calls) == 2
    stage, stage_environment = sync_calls[0]
    assert stage.name == "stage" and stage.parent.parent == directory.parent
    assert stage_environment["UV_PROJECT_ENVIRONMENT"] == str(stage / ".venv")
    assert sync_calls[1][0] == directory
    assert sync_calls[1][1]["UV_PROJECT_ENVIRONMENT"] == str(directory / ".venv")
    assert stage_environment["UV_PYTHON"] == sys.executable
    installation.protect.assert_awaited_once_with()
    accept.assert_awaited_once_with(current, [], installation)
    finalize.assert_awaited_once_with(
        f'projects/{current["project_id"]}/dependencies/installations/{installation.install_id}/{operation}', {})
    assert not (directory / dependencies._INSTALLATION_INTENT).exists()
    assert not list(directory.parent.glob(".solo-install-*"))
    assert not dependencies._installing
    assert all(os.environ[name] == value for name, value in inherited_environment.items())
