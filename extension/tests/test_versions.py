import asyncio
import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from tornado import web

from solo_jupyter import dependencies, versions


@pytest.fixture
def project():
    return {"project_id": str(uuid4()), "path": "projects/model/example", "kind": "model"}


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


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("validate", [False, True])
def test_parameters_only_checks_form_without_factor_calculation(
    tmp_path, monkeypatch, frozen, validate,
):
    directory = tmp_path / "project"
    source = tmp_path / "frozen" if frozen else None
    values = {"form": {"window": 17}} if validate else None
    payload = {"entry": "sample:Factor", "factor": {"window": 17}}
    execute = Mock(return_value=SimpleNamespace(
        returncode=0, stdout="user factor output\n" + json.dumps(payload) + "\n", stderr="",
    ))
    monkeypatch.setattr(versions.subprocess, "run", execute)
    assert versions.parameters(directory, values, source) == payload
    arguments = [str(directory / ".venv/bin/scheme"), "parameters", "--project", str(source or directory)]
    if validate:
        arguments.append("--validate")
    assert execute.call_args.args == (arguments,)
    assert execute.call_args.kwargs["cwd"] == directory
    assert execute.call_args.kwargs["input"] == (json.dumps(values) if validate else None)
    assert execute.call_args.kwargs["timeout"] == 600
    versions.command(["uv", "build", "--wheel"], directory)
    assert execute.call_args.kwargs["timeout"] == 600


def test_parameter_open_checks_admission_before_project_code(project, monkeypatch):
    admission = AsyncMock(side_effect=rejected())
    execute = Mock(side_effect=AssertionError("Retired project executed parameters"))
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "parameters", execute)
    with pytest.raises(web.HTTPError, match="版本已退役"):
        asyncio.run(versions.VersionsHandler.get(handler(project, parameters=True)))
    admission.assert_awaited_once_with(project["project_id"])
    execute.assert_not_called()


def test_factor_parameter_get_remains_schema_only(project, monkeypatch):
    project = {**project, "kind": "factor"}
    admission = AsyncMock()
    parameters = Mock(return_value={"protocol": 2})
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "parameters", parameters)
    request = handler(project, parameters=True)
    asyncio.run(versions.VersionsHandler.get(request))
    parameters.assert_called_once_with(Path("/shared") / project["path"])
    request.finish.assert_called_once_with({"protocol": 2})
    assert not dependencies._project_busy


def test_factor_validate_only_remains_schema_only_without_build_or_record(project, monkeypatch):
    project = {**project, "kind": "factor"}
    parameters = Mock(return_value={})
    build = Mock(side_effect=AssertionError("validate_only built a version"))
    backend = AsyncMock(side_effect=AssertionError("validate_only created a version"))
    monkeypatch.setattr(versions, "check_project_release", AsyncMock())
    monkeypatch.setattr(versions, "parameters", parameters)
    monkeypatch.setattr(versions, "build_version", build)
    monkeypatch.setattr(versions, "backend", backend)
    values = {"form": {"window": 17}}
    request = handler(project, body={
        "path": project["path"], "parameters": values, "validate_only": True,
    })
    asyncio.run(versions.VersionsHandler.post(request))
    parameters.assert_called_once_with(Path("/shared") / project["path"], values)
    build.assert_not_called()
    backend.assert_not_called()
    request.finish.assert_called_once_with({"valid": True})
    assert not dependencies._project_busy
    assert not versions._tasks


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


@pytest.mark.parametrize("validate_only", [False, True])
def test_invalid_form_is_rejected_before_version_creation_or_build(project, monkeypatch, validate_only):
    project = {**project, "kind": "factor"}
    parameters = Mock(side_effect=ValueError("invalid factor form"))
    build = Mock()
    backend = AsyncMock()
    monkeypatch.setattr(versions, "check_project_release", AsyncMock())
    monkeypatch.setattr(versions, "parameters", parameters)
    monkeypatch.setattr(versions, "build_version", build)
    monkeypatch.setattr(versions, "backend", backend)
    values = {"form": {"window": 0}}
    request = handler(project, body={
        "path": project["path"], "parameters": values, "validate_only": validate_only,
    })
    with pytest.raises(web.HTTPError, match="invalid factor form") as error:
        asyncio.run(versions.VersionsHandler.post(request))
    assert error.value.status_code == 422
    parameters.assert_called_once_with(Path("/shared") / project["path"], values)
    build.assert_not_called()
    backend.assert_not_called()
    assert not dependencies._project_busy
    assert not versions._tasks


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


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution"])
def test_background_save_builds_every_kind_without_factor_validation(
    project, monkeypatch, tmp_path, kind,
):
    project = {**project, "kind": kind}
    admission = AsyncMock()
    build = Mock()
    backend = AsyncMock()
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "build_version", build)
    monkeypatch.setattr(versions, "backend", backend)
    version = {"id": str(uuid4())}
    values = {"form": {"window": 17}}
    asyncio.run(versions.save(tmp_path, project, version, values))
    admission.assert_awaited_once_with(project["project_id"])
    build.assert_called_once_with(tmp_path, version, values)
    backend.assert_awaited_once_with(f'{project["project_id"]}/versions/{version["id"]}/submit', {})
    assert not dependencies._project_busy


def test_background_save_does_not_need_kind_to_decide_factor_validation(
    project, monkeypatch, tmp_path,
):
    project = {key: value for key, value in project.items() if key != "kind"}
    build = Mock()
    backend = AsyncMock()
    monkeypatch.setattr(versions, "check_project_release", AsyncMock())
    monkeypatch.setattr(versions, "build_version", build)
    monkeypatch.setattr(versions, "backend", backend)
    version = {"id": str(uuid4())}
    asyncio.run(versions.save(tmp_path, project, version, {}))
    build.assert_called_once_with(tmp_path, version, {})
    backend.assert_awaited_once_with(
        f'{project["project_id"]}/versions/{version["id"]}/submit', {},
    )
    assert not dependencies._project_busy


@pytest.mark.parametrize("kind", ["factor", "model"])
def test_active_save_checks_before_parameters_and_creates_version(project, monkeypatch, kind):
    project = {**project, "kind": kind}
    calls = []

    async def admit(identifier):
        calls.append("admission")

    def parameters(directory, values):
        assert directory == Path("/shared") / project["path"]
        assert values == {}
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


def test_frozen_parameter_failure_after_record_creation_uses_only_error_callback(
    build_project, tmp_path, monkeypatch, project,
):
    directory = build_project
    project = {**project, "kind": "factor", "path": directory.name}
    version = {"id": str(uuid4()), "packageVersion": "1.2.1"}
    target = tmp_path / "runs" / version["id"]
    values = {"form": {"window": 17}}
    events = []
    submissions = []
    command = Mock(side_effect=AssertionError("failed validation continued a build command"))

    def parameters(selected_directory, submitted, source=None):
        assert project["project_id"] in dependencies._project_busy
        assert selected_directory == directory
        assert submitted == values
        if source is None:
            events.append("schema")
            return {}
        assert source == target / "source"
        events.append("frozen parameters")
        raise ValueError("frozen source has invalid form")

    async def backend(path, body=None):
        assert project["project_id"] in dependencies._project_busy
        if path.endswith("/submit"):
            events.append("error callback")
            submissions.append((path, body))
            return {}
        assert path == f'{project["project_id"]}/versions'
        events.append("record")
        return version

    monkeypatch.setattr(versions, "check_project_release", AsyncMock())
    monkeypatch.setattr(versions, "parameters", parameters)
    monkeypatch.setattr(versions, "command", command)
    monkeypatch.setattr(versions, "backend", backend)
    request = handler(project, body={"path": project["path"], "parameters": values})
    request.settings["server_root_dir"] = str(directory.parent)

    async def run():
        await versions.VersionsHandler.post(request)
        request.set_status.assert_called_once_with(202)
        request.finish.assert_called_once_with(version)
        await asyncio.gather(*versions._tasks)

    asyncio.run(run())
    assert events == ["schema", "record", "frozen parameters", "error callback"]
    assert submissions == [(
        f'{project["project_id"]}/versions/{version["id"]}/submit',
        {"error": "frozen source has invalid form"},
    )]
    command.assert_not_called()
    assert not (target / "wheels").exists()
    assert not (target / "input.json").exists()
    assert not dependencies._project_busy
    assert not versions._tasks


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
    project = {**project, "kind": "factor"}
    admission = AsyncMock(side_effect=rejected())
    backend = AsyncMock(return_value={})
    parameters = Mock(side_effect=AssertionError("retry must not validate parameters"))
    build = Mock(side_effect=AssertionError("retry must not build a version"))
    monkeypatch.setattr(versions, "check_project_release", admission)
    monkeypatch.setattr(versions, "backend", backend)
    monkeypatch.setattr(versions, "parameters", parameters)
    monkeypatch.setattr(versions, "build_version", build)
    identifier = str(uuid4())
    request = handler(project, body={"path": project["path"], "action": action, "version_id": identifier})
    asyncio.run(versions.VersionsHandler.post(request))
    admission.assert_not_called()
    backend.assert_awaited_once_with(f'{project["project_id"]}/versions/{identifier}/{endpoint}', {})
    parameters.assert_not_called()
    build.assert_not_called()
    assert not versions._tasks


@pytest.fixture
def build_project(tmp_path, monkeypatch):
    directory = tmp_path / "project"
    (directory / "src/sample").mkdir(parents=True)
    (directory / "src/sample/__init__.py").write_text("VALUE = 1\n")
    (directory / "pyproject.toml").write_text(
        '[project]\nname = "sample"\nversion = "1.2.0"\nrequires-python = ">=3.12"\n',
    )
    (directory / "uv.lock").write_text("version = 1\npackage = []\n")
    (tmp_path / "runs").mkdir()
    monkeypatch.setenv("SOLO_SHARED_DIR", str(tmp_path))
    return directory


@pytest.mark.parametrize("kind", ["factor", "model"])
def test_build_version_checks_frozen_parameters_once_without_factor_calculation(
    build_project, tmp_path, monkeypatch, kind,
):
    directory = build_project
    version = {"id": str(uuid4()), "packageVersion": "1.2.1"}
    target = tmp_path / "runs" / version["id"]
    values = {"form": {"window": 17, "start": "2026-06-01", "end": "2026-06-01"}}
    factor_params = {"universe": {"pool": "中证 500"}, "window": 17}
    analysis = {"start": "2026-06-01", "end": "2026-06-01", "columns": ["momentum"]}
    result = {"entry": "sample:Factor", "factor": factor_params, "analysis": analysis} if kind == "factor" else {
        "entry": "sample:ModelAlgo", "project_kind": "model", "upstream": {}, "backtest": analysis,
    }
    calls = []

    def parameters(directory_arg, submitted, source):
        calls.append("parameters")
        assert directory_arg == directory
        assert submitted == values
        assert source == target / "source"
        assert tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"] == "1.2.1"
        assert (source / "src/sample/__init__.py").read_text() == "VALUE = 1\n"
        assert not (target / "wheels").exists()
        return result

    parameters_mock = Mock(side_effect=parameters)
    monkeypatch.setattr(versions, "parameters", parameters_mock)

    def command(arguments, cwd, payload=None):
        calls.append(arguments[:2])
        if arguments[:2] == ["uv", "build"]:
            wheels = Path(arguments[arguments.index("--out-dir") + 1])
            (wheels / "sample-1.2.1-py3-none-any.whl").write_bytes(b"candidate")
            return ""
        if arguments[:2] == ["uv", "lock"]:
            environment = Path(arguments[arguments.index("--project") + 1])
            (environment / "uv.lock").write_text("version = 1\npackage = []\n")
            return ""
        return json.dumps({"version": "1.6.3", "direct": {}})

    monkeypatch.setattr(versions, "command", command)
    versions.build_version(directory, version, values)
    parameters_mock.assert_called_once_with(directory, values, target / "source")
    assert calls[0] == "parameters"
    assert calls[1] == ["uv", "build"]
    data = json.loads((target / "input.json").read_text())
    assert data["kind"] == kind
    if kind == "factor":
        assert data["factor"]["params"] == factor_params
        assert not {"start", "end"} & data["factor"]["params"].keys()
        assert data["analysis"] == analysis
    else:
        assert data["backtest"] == analysis
    assert tomllib.loads((directory / "pyproject.toml").read_text())["project"]["version"] == "1.2.0"
    assert (target / "build.json").is_file()


@pytest.mark.parametrize("error", [
    ValueError("invalid factor form"),
    subprocess.CalledProcessError(1, ["scheme", "parameters"]),
])
def test_background_parameter_failure_only_submits_error_and_stops_build(
    build_project, tmp_path, monkeypatch, project, error,
):
    project = {**project, "kind": "factor"}
    directory = build_project
    version = {"id": str(uuid4()), "packageVersion": "1.2.1"}
    target = tmp_path / "runs" / version["id"]
    values = {"form": {"window": 17}}
    parameters = Mock(side_effect=error)
    command = Mock(side_effect=AssertionError("failed validation continued wheel construction"))
    backend = AsyncMock()
    monkeypatch.setattr(versions, "check_project_release", AsyncMock())
    monkeypatch.setattr(versions, "parameters", parameters)
    monkeypatch.setattr(versions, "command", command)
    monkeypatch.setattr(versions, "backend", backend)
    asyncio.run(versions.save(directory, project, version, values))
    parameters.assert_called_once_with(directory, values, target / "source")
    command.assert_not_called()
    backend.assert_awaited_once_with(
        f'{project["project_id"]}/versions/{version["id"]}/submit', {"error": str(error)},
    )
    assert (target / "source/pyproject.toml").is_file()
    assert not (target / "wheels").exists()
    assert not (target / "environment").exists()
    assert not (target / "input.json").exists()
    assert not (target / "build.json").exists()
    assert not dependencies._project_busy


@pytest.mark.parametrize("message", [
    "invalid factor form",
    "scheme: error: missing project parameters",
])
def test_cli_parameter_failure_with_success_shaped_stdout_is_not_accepted(
    build_project, tmp_path, monkeypatch, message,
):
    directory = build_project
    version = {"id": str(uuid4()), "packageVersion": "1.2.1"}
    target = tmp_path / "runs" / version["id"]
    execute = Mock(return_value=SimpleNamespace(
        returncode=1, stdout=json.dumps({"entry": "sample:Factor", "factor": {}}),
        stderr=message,
    ))
    monkeypatch.setattr(versions.subprocess, "run", execute)
    with pytest.raises(ValueError, match=message):
        versions.build_version(directory, version, {"form": {}})
    execute.assert_called_once()
    assert execute.call_args.args[0][-1] == "--validate"
    assert execute.call_args.kwargs["timeout"] == 600
    assert not (target / "wheels").exists()
    assert not (target / "input.json").exists()


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
    monkeypatch.setattr(versions, "parameters", lambda *args, **kwargs: {
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
    monkeypatch.setattr(versions, "parameters", lambda *args, **kwargs: {"entry": "factor_test:Factor", "factor": {}, "analysis": {}})
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
