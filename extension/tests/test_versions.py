import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from solo_jupyter import dependencies, versions
from tornado import web


@pytest.fixture
def project():
    return {"project_id": str(uuid4()), "path": "projects/model/example"}


def handler(project, *, parameters=False, body=None):
    def query(name, default=""):
        return "1" if name == "parameters" and parameters else project["path"] if name == "path" else default

    return SimpleNamespace(
        current_user=object(), settings={"server_root_dir": "/shared"},
        current_project=Mock(return_value=project), get_query_argument=query,
        get_json_body=Mock(return_value=body), finish=Mock(), set_status=Mock(),
    )


def rejected():
    return web.HTTPError(422, reason="版本已退役；仅允许读取历史")


def test_parameter_open_checks_admission_before_project_code(project, monkeypatch):
    admission = AsyncMock(side_effect=rejected())
    execute = Mock(side_effect=AssertionError("Retired project executed parameters"))
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "parameters", execute)
    with pytest.raises(web.HTTPError, match="版本已退役"):
        asyncio.run(versions.VersionsHandler.get(handler(project, parameters=True)))
    admission.assert_awaited_once_with(project["project_id"])
    execute.assert_not_called()


@pytest.mark.parametrize("validate_only", [False, True])
def test_save_checks_admission_before_validation_or_record_creation(project, monkeypatch, validate_only):
    admission = AsyncMock(side_effect=rejected())
    execute = Mock(side_effect=AssertionError("Retired project executed parameters"))
    backend = AsyncMock(side_effect=AssertionError("Retired project created a record"))
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "parameters", execute)
    monkeypatch.setattr(versions, "backend", backend)
    body = {"path": project["path"], "parameters": {}, "validate_only": validate_only}
    with pytest.raises(web.HTTPError, match="版本已退役"):
        asyncio.run(versions.VersionsHandler.post(handler(project, body=body)))
    admission.assert_awaited_once_with(project["project_id"])
    execute.assert_not_called()
    backend.assert_not_called()


def test_history_list_does_not_require_active_version(project, monkeypatch):
    admission = AsyncMock(side_effect=rejected())
    backend = AsyncMock(return_value=[{"id": str(uuid4()), "retired": True}])
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "backend", backend)
    request = handler(project)
    asyncio.run(versions.VersionsHandler.get(request))
    admission.assert_not_called()
    backend.assert_awaited_once_with(f'{project["project_id"]}/versions')
    request.finish.assert_called_once_with({"versions": backend.return_value})


def test_background_save_rechecks_before_creating_snapshot(project, monkeypatch, tmp_path):
    admission = AsyncMock(side_effect=rejected())
    build = Mock(side_effect=AssertionError("Retired project created a snapshot"))
    backend = AsyncMock()
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "build_version", build)
    monkeypatch.setattr(versions, "backend", backend)
    version = {"id": str(uuid4())}
    asyncio.run(versions.save(tmp_path, project, version, {}))
    admission.assert_awaited_once_with(project["project_id"])
    build.assert_not_called()
    backend.assert_awaited_once_with(
        f'{project["project_id"]}/versions/{version["id"]}/submit',
        {"error": "版本已退役；仅允许读取历史"},
    )


def test_active_save_checks_before_parameters_and_creates_version(project, monkeypatch):
    calls = []

    async def admit(identifier):
        calls.append("admission")

    def parameters(*args):
        calls.append("parameters")
        return {}

    async def backend(path, body=None):
        calls.append("record")
        return {"id": str(uuid4())}

    save = AsyncMock()
    monkeypatch.setattr(versions, "check_project_release", admit)
    monkeypatch.setattr(versions, "parameters", parameters)
    monkeypatch.setattr(versions, "backend", backend)
    monkeypatch.setattr(versions, "_save", save)
    request = handler(project, body={"path": project["path"], "parameters": {}})

    async def run():
        await versions.VersionsHandler.post(request)
        await asyncio.gather(*versions._tasks)

    asyncio.run(run())
    assert calls == ["admission", "parameters", "record"]
    request.set_status.assert_called_once_with(202)
    save.assert_awaited_once()


@pytest.mark.parametrize("action", ["parameters", "validate", "save", "background"])
def test_install_gate_blocks_all_parameter_and_snapshot_paths_without_io(project, monkeypatch, tmp_path, action):
    admission = AsyncMock(side_effect=AssertionError("Busy project called admission"))
    execute = Mock(side_effect=AssertionError("Busy project executed source code"))
    build = Mock(side_effect=AssertionError("Busy project created snapshot"))
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "parameters", execute)
    monkeypatch.setattr(versions, "build_version", build)
    with dependencies.project_operation(project["project_id"].upper()):
        with pytest.raises(web.HTTPError) as error:
            if action == "parameters":
                asyncio.run(versions.VersionsHandler.get(handler(project, parameters=True)))
            elif action == "background":
                asyncio.run(versions.save(tmp_path, project, {"id": str(uuid4())}, {}))
            else:
                body = {"path": project["path"], "parameters": {}, "validate_only": action == "validate"}
                asyncio.run(versions.VersionsHandler.post(handler(project, body=body)))
        assert error.value.status_code == 409
    admission.assert_not_called()
    execute.assert_not_called()
    build.assert_not_called()
    assert not dependencies._project_busy


def test_save_owns_gate_from_record_creation_through_background_submit(project, monkeypatch, tmp_path):
    current = {**project, "kind": "model", "scheme_version": "1.2.0"}
    candidate = {"id": str(uuid4()), "kind": "factor", "schemeVersion": "1.2.7"}
    async def rejected_install():
        with pytest.raises(web.HTTPError) as error:
            await dependencies.install_project(tmp_path, current, candidate)
        assert error.value.status_code == 409
    monkeypatch.setattr(versions, "check_project_release", AsyncMock())
    monkeypatch.setattr(versions, "parameters", Mock(return_value={}))
    version = {"id": str(uuid4())}
    steps = []
    async def backend(path, body=None):
        await rejected_install()
        steps.append("submit" if path.endswith("submit") else "record")
        return version
    def build(*args):
        assert project["project_id"] in dependencies._project_busy
        steps.append("build")
    monkeypatch.setattr(versions, "backend", backend)
    monkeypatch.setattr(versions, "build_version", build)
    request = handler(project, body={"path": project["path"], "parameters": {}})
    async def run():
        await versions.VersionsHandler.post(request)
        await rejected_install()
        await asyncio.gather(*versions._tasks)
    asyncio.run(run())
    assert steps == ["record", "build", "submit"]
    assert not dependencies._project_busy
    assert not versions._tasks


@pytest.mark.parametrize("action, endpoint", [("cancel", "cancel"), ("retry", "submit")])
def test_recovery_uses_backend_record_decision(project, monkeypatch, action, endpoint):
    admission = AsyncMock(side_effect=rejected())
    backend = AsyncMock(return_value={})
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "backend", backend)
    identifier = str(uuid4())
    request = handler(project, body={"path": project["path"], "action": action, "version_id": identifier})
    asyncio.run(versions.VersionsHandler.post(request))
    admission.assert_not_called()
    backend.assert_awaited_once_with(f'{project["project_id"]}/versions/{identifier}/{endpoint}', {})


@pytest.mark.parametrize("policy", [
    {"index": [{"url": "https://pypi.tuna.tsinghua.edu.cn/simple", "default": True}]},
    {"index": [{"name": "private", "url": "https://packages.example/simple", "default": True}],
     "index-strategy": "first-index"},
    {"index": [{"name": "private", "url": "https://packages.example/simple", "explicit": True}],
     "sources": {"registry-dependency": {"index": "private"}}},
    {"index-url": "https://packages.example/simple", "extra-index-url": ["https://extra.example/simple"]},
    {},
])
def test_build_version_preserves_index_policy_before_lock(tmp_path, monkeypatch, policy):
    import tomlkit

    directory = tmp_path / "project"
    (directory / "src").mkdir(parents=True)
    config = {
        "project": {"name": "factor-test", "version": "1.2.0", "requires-python": ">=3.12"},
        "tool": {"uv": {**policy, "override-dependencies": ["do-not-copy==1"]}},
    }
    (directory / "pyproject.toml").write_text(tomlkit.dumps(config))
    (directory / "uv.lock").write_text(tomlkit.dumps({"version": 1, "package": [
        {"name": "factor-test", "version": "1.2.0", "source": {"editable": "."}},
        {"name": "registry-dependency", "version": "2.0.0", "source": {"registry": "https://packages.example/simple"}},
    ]}))
    original = {name: (directory / name).read_bytes() for name in ["pyproject.toml", "uv.lock"]}
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("SOLO_SHARED_DIR", str(tmp_path))
    monkeypatch.setattr(versions, "parameters", lambda *args: {
        "entry": "factor_test:Factor", "factor": {}, "analysis": {},
    })
    locked = []

    def command(arguments, cwd, payload=None):
        if arguments[:2] == ["uv", "build"]:
            wheels = Path(arguments[arguments.index("--out-dir") + 1])
            (wheels / "factor_test-1.2.1-py3-none-any.whl").write_bytes(b"candidate")
            return ""
        if arguments[:2] == ["uv", "lock"]:
            environment = Path(arguments[arguments.index("--project") + 1])
            locked.append(tomllib.loads((environment / "pyproject.toml").read_text()))
            (environment / "uv.lock").write_text('version = 1\npackage = []\n')
            return ""
        return json.dumps({"version": "1.2.0", "direct": {}})

    monkeypatch.setattr(versions, "command", command)
    versions.build_version(directory, {"id": str(uuid4()), "packageVersion": "1.2.1"}, {})
    uv = locked[0]["tool"]["uv"]
    expected_indexes = [dict(index) for index in policy.get("index", [])]
    if "index-url" not in policy and not any(index.get("default") for index in expected_indexes):
        expected_indexes.append({"url": "https://pypi.tuna.tsinghua.edu.cn/simple", "default": True})
    assert uv.get("index", []) == expected_indexes
    for name in ("index-url", "extra-index-url", "index-strategy"):
        assert uv.get(name) == policy.get(name)
    assert uv["sources"].get("registry-dependency") == policy.get("sources", {}).get("registry-dependency")
    assert "override-dependencies" not in uv
    assert uv["constraint-dependencies"] == ["registry-dependency==2.0.0"]
    assert {name: (directory / name).read_bytes() for name in original} == original


@pytest.mark.parametrize("location", ["directory", "editable"])
def test_build_version_builds_local_dependencies_without_copying_them(tmp_path, monkeypatch, location):
    import tomlkit

    directory, dependency = tmp_path / "project", tmp_path / "dependency"
    (directory / "src").mkdir(parents=True)
    dependency.mkdir()
    (directory / "pyproject.toml").write_text(tomlkit.dumps({"project": {
        "name": "factor-test", "version": "1.2.0", "requires-python": ">=3.12", "dependencies": ["local-dependency==0.1.0"],
    }}))
    (directory / "uv.lock").write_text(tomlkit.dumps({"version": 1, "package": [
        {"name": "factor-test", "version": "1.2.0", "source": {"editable": "."}},
        {"name": "local-dependency", "version": "0.1.0", "source": {location: "../dependency"}},
    ]}))
    (dependency / "pyproject.toml").write_text('[project]\nname="local-dependency"\nversion="0.1.0"\n')
    with (dependency / "panel.parquet").open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    original = {path: path.read_bytes() for path in (directory / "pyproject.toml", directory / "uv.lock", dependency / "pyproject.toml")}
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("SOLO_SHARED_DIR", str(tmp_path))
    monkeypatch.setattr(versions, "parameters", lambda *args: {"entry": "factor_test:Factor", "factor": {}, "analysis": {}})
    copy = Mock(wraps=versions.copy_source)
    monkeypatch.setattr(versions, "copy_source", copy)
    version = {"id": str(uuid4()), "packageVersion": "1.2.1"}
    target = tmp_path / "runs" / version["id"]
    builds, locked = [], []

    def command(arguments, cwd, payload=None):
        if arguments[:2] == ["uv", "build"]:
            source = Path(arguments[-1])
            builds.append(source)
            config = tomllib.loads((source / "pyproject.toml").read_text())["project"]
            assert source in (target / "source", dependency)
            assert config["version"] == ("1.2.1" if source == target / "source" else "0.1.0")
            destination = Path(arguments[arguments.index("--out-dir") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f'{config["name"].replace("-", "_")}-{config["version"]}-py3-none-any.whl').write_bytes(b"candidate")
            return ""
        if arguments[:2] == ["uv", "lock"]:
            environment = Path(arguments[arguments.index("--project") + 1])
            locked.append(tomllib.loads((environment / "pyproject.toml").read_text()))
            (environment / "uv.lock").write_text('version = 1\npackage = []\n')
            return ""
        return json.dumps({"version": "1.2.0", "direct": {}})

    monkeypatch.setattr(versions, "command", command)
    versions.build_version(directory, version, {})
    copy.assert_called_once_with(directory, target / "source")
    assert builds == [target / "source", dependency]
    assert not (target / "dependencies").exists()
    wheel = target / "wheels/local-dependency/local_dependency-0.1.0-py3-none-any.whl"
    assert locked[0]["tool"]["uv"]["sources"]["local-dependency"] == {"path": str(wheel)}
    assert wheel.is_file()
    assert all(path.read_bytes() == content for path, content in original.items())
    assert (dependency / "panel.parquet").stat().st_size == 64 * 1024 * 1024


@pytest.mark.parametrize("license_value", ["MIT", {"file": "licenses/LICENSE"}])
def test_copy_source_only_build_inputs_and_parameters(tmp_path, license_value):
    import tomlkit

    source, target = tmp_path / "project", tmp_path / "snapshot"
    (source / "src/package").mkdir(parents=True)
    (source / "docs").mkdir()
    (source / "licenses").mkdir()
    (source / "src/package/__init__.py").write_text("VALUE = 1\n")
    (source / "docs/README.md").write_text("build readme")
    (source / "licenses/LICENSE").write_text("build license")
    (source / "pyproject.toml").write_text(tomlkit.dumps({"project": {
        "name": "sample", "version": "1.2.0", "readme": {"file": "docs/README.md", "content-type": "text/markdown"},
        "license": license_value, "license-files": ["licenses/*"],
    }}))
    (source / "uv.lock").write_text("version = 1\n")
    (source / "research.ipynb").write_text("not a build input")
    (source / "research-data").mkdir()
    (source / "research-data/panel.parquet").write_bytes(b"research panel")
    versions.copy_source(source, target)
    assert {path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()} == {
        "pyproject.toml", "uv.lock", "src/package/__init__.py", "docs/README.md", "licenses/LICENSE",
    }


def test_copy_source_follows_links_and_ignores_unrelated_broken_link(tmp_path):
    source, target = tmp_path / "project", tmp_path / "snapshot"
    (source / "src").mkdir(parents=True)
    (source / "pyproject.toml").write_text('[project]\nname="sample"\nversion="1.2.0"\n')
    outside = tmp_path / "module.py"
    outside.write_text("VALUE = 1\n")
    try:
        (source / "src/module.py").symlink_to(outside)
        (source / "research-data").symlink_to(tmp_path / "missing", target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires permission on this platform")
    versions.copy_source(source, target)
    assert (target / "src/module.py").read_text() == "VALUE = 1\n"
    assert not (target / "src/module.py").is_symlink()
    assert not (target / "research-data").exists()
