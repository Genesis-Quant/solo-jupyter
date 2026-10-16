import asyncio
import json
import runpy
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from packaging.version import Version
from solo_jupyter import dependencies, handlers, versions
from tornado import web
from tornado.httpclient import HTTPClientError


_CHECK_PROJECT_RELEASE = dependencies.check_project_release
_ARTIFACT_API = dependencies._artifact_api


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    snapshots, contents, accepted = {}, {}, {}

    async def api(path, body=None, *, binary=False):
        if path == "artifacts/register":
            assert set(body) == {"project_id", "wheel"}
            wheel = Path(body["wheel"])
            metadata = next(json.loads(marker.read_text()) for marker in tmp_path.rglob(".solo")
                            if json.loads(marker.read_text()).get("project_id") == body["project_id"])
            assert wheel.is_absolute() and ".solo-wheels" in wheel.parts
            record = artifact_fixture(wheel, kind=metadata["kind"], origin=body["project_id"], published=False)
            snapshots[record["id"]] = record
            contents[record["id"]] = wheel.read_bytes()
            return record
        if path.startswith("projects/") and path.endswith("/dependencies"):
            assert set(body) == {"artifacts"}
            accepted[path.split("/")[1]] = [snapshots[identifier] for identifier in body["artifacts"]]
            return {"artifacts": [accepted_snapshot(item) for item in accepted[path.split("/")[1]]]}
        if path.startswith("projects/"):
            assert body is None
            identifier = path.split("/")[1]
            return {"id": identifier, "artifactDependencies": {
                item["package"]: accepted_snapshot(item) for item in accepted.get(identifier, [])}}
        if path == "artifacts" or path.startswith("artifacts?"):
            return [item for item in snapshots.values() if "published=true" not in path or item["publishedAt"]]
        if path.startswith("artifacts/"):
            identifier = path.split("/")[1]
            if identifier not in snapshots:
                raise web.HTTPError(404, reason="Artifact does not exist")
            return contents[identifier] if binary else snapshots[identifier]
        raise AssertionError(f"Unexpected registry request: {path}")

    mock = AsyncMock(side_effect=api)
    monkeypatch.setattr(dependencies, "_artifact_api", mock)
    return SimpleNamespace(api=mock, snapshots=snapshots, contents=contents, accepted=accepted)


@pytest.fixture(autouse=True)
def admission(monkeypatch):
    check = AsyncMock()
    monkeypatch.setattr(dependencies, "check_project_release", check)
    return check


@pytest.mark.parametrize("body, status", [(b'{"allowed": true}', None), (b'{"allowed": false}', 422),
    (b'{"allowed": 1}', 502), (b'{}', 502), (b'[]', 502), (b'null', 502), (b'not-json', 502)])
def test_central_admission_response_is_fail_closed(monkeypatch, body, status):
    identifier = str(uuid4())
    client = SimpleNamespace(fetch=AsyncMock(return_value=SimpleNamespace(body=body)))
    monkeypatch.setattr(dependencies, "AsyncHTTPClient", lambda: client)
    monkeypatch.setenv("SOLO_BACKEND_URL", "http://backend.invalid/")
    if status:
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(_CHECK_PROJECT_RELEASE(identifier))
        assert error.value.status_code == status
    else:
        asyncio.run(_CHECK_PROJECT_RELEASE(identifier))
    client.fetch.assert_awaited_once_with(f"http://backend.invalid/api/v1/version-policy/projects/{identifier}/check",
        method="POST", body="{}", headers={"Content-Type": "application/json"}, request_timeout=30)


@pytest.mark.parametrize("detail", ["retired test-password", {"reason": "retired test-password", "versions": ["irrelevant"]}])
def test_central_admission_rejection_extracts_reason_and_redacts_secrets(monkeypatch, detail):
    client = SimpleNamespace(fetch=AsyncMock(side_effect=HTTPClientError(422,
        response=SimpleNamespace(body=json.dumps({"detail": detail}).encode()))))
    monkeypatch.setattr(dependencies, "AsyncHTTPClient", lambda: client)
    monkeypatch.setenv("ADMISSION_TEST_PASSWORD", "test-password")
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(_CHECK_PROJECT_RELEASE(str(uuid4())))
    assert error.value.status_code == 422
    assert error.value.reason == "retired [REDACTED]"


@pytest.fixture
def projects():
    return ({"project_id": str(uuid4()), "path": "projects/model/current", "name": "current", "kind": "model", "scheme_version": "1.2.0"},
            {"id": str(uuid4()), "directory": "projects/factor/source", "kind": "factor", "schemeVersion": "1.2.7", "archived": False})


@pytest.fixture
def project_files(tmp_path, projects):
    current, candidate = projects
    for path, metadata, package in (
        (current["path"], current, "current-package"),
        (candidate["directory"], {"project_id": candidate["id"], "name": "source", "kind": candidate["kind"], "scheme_version": candidate["schemeVersion"]}, "source-package"),
    ):
        directory = tmp_path / path
        (directory / "src").mkdir(parents=True)
        (directory / "src/probe.py").write_text("VALUE = 1\n")
        (directory / ".solo").write_text(json.dumps(metadata))
        (directory / "pyproject.toml").write_text(
            f'[project]\nname = "{package}"\nversion = "1.2.0"\ndependencies = ["scheme>=1.2.0,<1.3.0"]\n'
        )
        (directory / "uv.lock").write_text('version = 1\n[[package]]\nname = "scheme"\nversion = "1.2.0"\n')
        interpreter = dependencies._python(directory)
        interpreter.parent.mkdir(parents=True)
        interpreter.write_text("temporary environment sentinel\n")
    return tmp_path, current, candidate


def handler_for(root, current, candidate, *, dev=True):
    return SimpleNamespace(current_user=object(), settings={"server_root_dir": str(root)},
        current_project=Mock(return_value=current), get_query_argument=Mock(return_value=current["path"]),
        get_json_body=Mock(return_value={"path": current["path"], "live_project_id": candidate["id"], "dev": dev}), finish=Mock())


def file_snapshot(root):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def forbid_install_side_effects(monkeypatch, *, block_source_read=True):
    operations = [(dependencies, "project_package", Mock), (dependencies.asyncio, "create_subprocess_exec", AsyncMock),
        *((Path, operation, Mock) for operation in ("read_text", "read_bytes", "write_text", "write_bytes", "unlink"))]
    if block_source_read:
        operations.append((dependencies, "read_project", Mock))
    guards = []
    for owner, attribute, mock_type in operations:
        guard = mock_type(side_effect=AssertionError(f"Rejected installation called {attribute}"))
        monkeypatch.setattr(owner, attribute, guard)
        guards.append(guard)
    return guards


@pytest.mark.parametrize("current_version", ["1.2.0", "1.2.7", "v1.2.0"])
@pytest.mark.parametrize("candidate_version, expected", [("1.2.0", True), ("1.2.7", True), ("1.2.999", True), ("v1.2.7", True),
    ("1.1.0", False), ("1.1.99", False), ("1.3.0", False), ("1.3.99", False), ("2.2.0", False)])
def test_compatible_requires_same_major_and_minor_but_any_patch(projects, current_version, candidate_version, expected):
    current, candidate = projects
    current["scheme_version"], candidate["schemeVersion"] = current_version, candidate_version
    assert dependencies.compatible(current, candidate) is expected


@pytest.mark.parametrize("side", ["current", "candidate"])
@pytest.mark.parametrize("value", [None, "", "not-a-version", "1.2.x", "1..2", 1, [], {}])
def test_compatible_safely_rejects_invalid_versions(projects, side, value):
    current, candidate = projects
    project, field = (current, "scheme_version") if side == "current" else (candidate, "schemeVersion")
    project[field] = value
    assert not dependencies.compatible(current, candidate)


@pytest.mark.parametrize("side", ["current", "candidate"])
def test_compatible_safely_rejects_missing_versions(projects, side):
    current, candidate = projects
    project, field = (current, "scheme_version") if side == "current" else (candidate, "schemeVersion")
    del project[field]
    assert not dependencies.compatible(current, candidate)


@pytest.mark.parametrize("current_kind", dependencies.PROJECT_KINDS)
@pytest.mark.parametrize("candidate_kind", dependencies.PROJECT_KINDS)
def test_compatible_preserves_same_kind_and_upstream_only(projects, current_kind, candidate_kind):
    current, candidate = projects
    current["kind"], candidate["kind"] = current_kind, candidate_kind
    assert dependencies.compatible(current, candidate) is (dependencies.PROJECT_KINDS.index(candidate_kind) <= dependencies.PROJECT_KINDS.index(current_kind))


@pytest.mark.parametrize("current_version", ["1.2.0", "1.2.7", "v1.2.0", "invalid", None])
def test_get_candidates_filters_archived_retired_and_incompatible(projects, tmp_path, monkeypatch, current_version):
    current, candidate = projects
    current["scheme_version"] = current_version
    catalog = [{**candidate, "id": "patch-zero", "schemeVersion": "1.2.0"}, {**candidate, "id": "patch-seven", "schemeVersion": "1.2.7"},
        {**candidate, "id": "previous-minor", "schemeVersion": "1.1.9"}, {**candidate, "id": "next-minor", "schemeVersion": "1.3.0"},
        {**candidate, "id": "invalid", "schemeVersion": "not-a-version"}, {**candidate, "id": "archived", "archived": True},
        {**candidate, "id": "retired", "retired": True}, {**candidate, "id": current["project_id"]}, {**candidate, "id": "downstream", "kind": "execution"}]
    monkeypatch.setattr(dependencies, "project_catalog", AsyncMock(return_value=catalog))
    handler = handler_for(tmp_path, current, candidate)
    with monkeypatch.context() as blocked:
        guards = forbid_install_side_effects(blocked)
        asyncio.run(dependencies.DependenciesHandler.get(handler))
        for guard in guards:
            guard.assert_not_called()
    handler.finish.assert_called_once_with({"projects": catalog[:2] if current_version in ("1.2.0", "1.2.7", "v1.2.0") else [], "artifacts": []})


@pytest.mark.parametrize("current_updates, candidate_updates", [({}, {"schemeVersion": "1.1.9"}), ({}, {"schemeVersion": "1.3.0"}),
    ({}, {"schemeVersion": "2.2.0"}), ({}, {"schemeVersion": "invalid"}), ({}, {"schemeVersion": None}),
    ({"scheme_version": {}}, {}), ({}, {"archived": True}), ({}, {"retired": True}), ({"retired": True}, {}),
    ({}, {"kind": "execution"}), ({}, {"id": "self"})])
def test_post_rejects_before_reading_source_or_changing_files_or_running_uv(project_files, monkeypatch, current_updates, candidate_updates, admission):
    root, current, candidate = project_files
    current.update(current_updates)
    candidate.update(candidate_updates)
    if candidate["id"] == "self":
        candidate["id"] = current["project_id"]
    monkeypatch.setattr(dependencies, "project_catalog", AsyncMock(return_value=[candidate]))
    handler = handler_for(root, current, candidate)
    before = file_snapshot(root)
    with monkeypatch.context() as blocked:
        guards = forbid_install_side_effects(blocked)
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.DependenciesHandler.post(handler))
        assert error.value.status_code == 422
        for guard in guards:
            guard.assert_not_called()
    admission.assert_not_awaited()
    assert file_snapshot(root) == before


@pytest.mark.parametrize("which", ["current", "candidate"])
def test_authoritative_admission_rejection_has_no_side_effects(project_files, monkeypatch, admission, which):
    root, current, candidate = project_files
    async def check(identifier):
        if identifier == (current["project_id"] if which == "current" else candidate["id"]):
            raise web.HTTPError(422, reason="project tag retired")
    admission.side_effect = check
    before = file_snapshot(root)
    with monkeypatch.context() as blocked:
        guards = forbid_install_side_effects(blocked)
        with pytest.raises(web.HTTPError, match="project tag retired"):
            asyncio.run(dependencies.install_project(root, current, candidate))
        for guard in guards:
            guard.assert_not_called()
    assert file_snapshot(root) == before


@pytest.mark.parametrize("source_version", ["1.1.9", "1.3.0", "2.2.0", "invalid", None, "missing", []])
def test_actual_source_manifest_series_is_rechecked_before_package_or_uv(project_files, monkeypatch, source_version):
    root, current, candidate = project_files
    source = {"project_id": candidate["id"], "path": candidate["directory"], "scheme_version": source_version}
    if source_version == "missing":
        del source["scheme_version"]
    monkeypatch.setattr(dependencies, "read_project", Mock(return_value=source))
    before = file_snapshot(root)
    with monkeypatch.context() as blocked:
        guards = forbid_install_side_effects(blocked, block_source_read=False)
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.install_project(root, current, candidate))
        assert error.value.status_code == 422
        for guard in guards:
            guard.assert_not_called()
    assert file_snapshot(root) == before


def test_installer_busy_gate_uses_normalized_project_uuid_before_any_io(project_files, monkeypatch, admission):
    root, current, candidate = project_files
    before = file_snapshot(root)
    with dependencies.project_operation(current["project_id"].upper()):
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.install_project(root, current, candidate))
        assert error.value.status_code == 409
    admission.assert_not_awaited()
    assert file_snapshot(root) == before
    assert not dependencies._project_busy


def test_operations_handler_reports_busy_until_project_operation_exits():
    identifier = str(uuid4())
    handler = SimpleNamespace(current_user=object(), get_query_argument=Mock(return_value=identifier.upper()), finish=Mock())
    asyncio.run(dependencies.ProjectOperationsHandler.get(handler))
    handler.finish.assert_called_with({"project_id": identifier, "busy": False})
    with pytest.raises(RuntimeError):
        with dependencies.project_operation(identifier):
            asyncio.run(dependencies.ProjectOperationsHandler.get(handler))
            handler.finish.assert_called_with({"project_id": identifier, "busy": True})
            raise RuntimeError("operation failed")
    asyncio.run(dependencies.ProjectOperationsHandler.get(handler))
    handler.finish.assert_called_with({"project_id": identifier, "busy": False})


@pytest.mark.parametrize("identifier", ["", "bad", "../project"])
def test_operations_handler_rejects_invalid_uuid(identifier):
    handler = SimpleNamespace(current_user=object(), get_query_argument=Mock(return_value=identifier), finish=Mock())
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(dependencies.ProjectOperationsHandler.get(handler))
    assert error.value.status_code == 422
    handler.finish.assert_not_called()


@pytest.mark.parametrize("side", ["current", "source"])
@pytest.mark.parametrize("requirement", ["scheme==1.2.7", "scheme>=1.2.7,<1.3", "scheme @ https://example.test/scheme.whl", None,
])
def test_declared_scheme_must_allow_complete_series_before_uv(project_files, monkeypatch, side, requirement):
    root, current, candidate = project_files
    path = root / (current["path"] if side == "current" else candidate["directory"]) / "pyproject.toml"
    requirements = requirement if isinstance(requirement, list) else [requirement] if requirement else []
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', json.dumps(requirements)))
    before = file_snapshot(root)
    uv = AsyncMock(side_effect=AssertionError("Invalid requirement must not run uv"))
    monkeypatch.setattr(dependencies.asyncio, "create_subprocess_exec", uv)
    with pytest.raises(web.HTTPError, match="全部 patch"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    uv.assert_not_called()
    assert file_snapshot(root) == before


@pytest.mark.parametrize("side", ["current", "source"])
@pytest.mark.parametrize("version", ["0.1.0", "1.1.7", "1.3.0", "2.2.0"])
def test_actual_project_package_version_must_match_scheme_series_before_uv(project_files, monkeypatch, side, version):
    root, current, candidate = project_files
    path = root / (current["path"] if side == "current" else candidate["directory"]) / "pyproject.toml"
    path.write_text(path.read_text().replace('version = "1.2.0"', f'version = "{version}"'))
    before = file_snapshot(root)
    uv = AsyncMock(side_effect=AssertionError("Wrong series must not run uv"))
    monkeypatch.setattr(dependencies.asyncio, "create_subprocess_exec", uv)
    with pytest.raises(web.HTTPError, match="项目包版本"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    uv.assert_not_called()
    assert file_snapshot(root) == before


@pytest.mark.parametrize("lock_versions", [None, [], ["invalid"], ["1.3.0"], ["1.2.0", "1.2.7"], ["1.2.0", "1.2.0"]])
def test_scheme_lock_missing_invalid_or_ambiguous_is_rejected(project_files, monkeypatch, lock_versions):
    root, current, candidate = project_files
    lock = root / current["path"] / "uv.lock"
    if lock_versions is None:
        lock.unlink()
    else:
        lock.write_text("version = 1\n" + "".join(f'[[package]]\nname = "scheme"\nversion = "{version}"\n' for version in lock_versions))
    before = file_snapshot(root)
    uv = AsyncMock(side_effect=AssertionError("Invalid Scheme lock must not run uv"))
    monkeypatch.setattr(dependencies.asyncio, "create_subprocess_exec", uv)
    with pytest.raises(FileNotFoundError if lock_versions is None else web.HTTPError):
        asyncio.run(dependencies.install_project(root, current, candidate))
    uv.assert_not_called()
    assert file_snapshot(root) == before


@pytest.mark.parametrize("mismatch", ["id", "directory", "missing-metadata", "invalid-metadata"])
def test_source_identity_and_invalid_manifest_rejected_without_mutation(project_files, monkeypatch, mismatch):
    root, current, candidate = project_files
    marker = root / candidate["directory"] / ".solo"
    if mismatch == "id":
        metadata = json.loads(marker.read_text())
        metadata["project_id"] = str(uuid4())
        marker.write_text(json.dumps(metadata))
    elif mismatch == "directory":
        candidate["directory"] += "/src"
    elif mismatch == "missing-metadata":
        marker.unlink()
    else:
        marker.write_text('{"project_id": "invalid", "secret": "not-used"}')
    before = file_snapshot(root)
    uv = AsyncMock(side_effect=AssertionError("Invalid manifest must not run uv"))
    monkeypatch.setattr(dependencies.asyncio, "create_subprocess_exec", uv)
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert error.value.status_code == (422 if mismatch == "invalid-metadata" else 409)
    uv.assert_not_called()
    assert file_snapshot(root) == before


_OFFLINE_BUILD_BACKEND = '''from pathlib import Path
import tomllib
from zipfile import ZipFile, ZipInfo

def get_requires_for_build_wheel(config_settings=None):
    return []

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    name = project["name"].replace("-", "_")
    version = project["version"]
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.1\\nName: {project['name']}\\nVersion: {version}\\n"
    metadata += "Requires-Python: >=3.12\\n"
    metadata += "".join(f"Requires-Dist: {item}\\n" for item in project.get("dependencies", []))
    for extra, requirements in project.get("optional-dependencies", {}).items():
        metadata += f"Provides-Extra: {extra}\\n"
        metadata += "".join(f"Requires-Dist: {item}; extra == '{extra}'\\n" for item in requirements)
    code = Path("src/payload.py").read_text() if Path("src/payload.py").exists() else "VALUE = 1\\n"
    files = {f"{name}/__init__.py": (f"__version__ = {version!r}\\n" + code).encode(),
        f"{dist_info}/METADATA": (metadata + "\\n").encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\\nGenerator: solo-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}
    records = []
    for path, content in files.items():
        records.append(f"{path},,{len(content)}\\n")
    record = f"{dist_info}/RECORD"
    files[record] = ("".join(records) + f"{record},,\\n").encode()
    filename = f"{name}-{version}-py3-none-any.whl"
    with ZipFile(Path(wheel_directory) / filename, "w") as wheel:
        for path, content in files.items():
            wheel.writestr(ZipInfo(path, (2020, 1, 1, 0, 0, 0)), content)
    return filename

get_requires_for_build_editable = get_requires_for_build_wheel
build_editable = build_wheel
'''


def write_offline_package(directory, name, version, requirements=()):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pyproject.toml").write_text(
        '[build-system]\nrequires = []\nbuild-backend = "offline_backend"\nbackend-path = ["src"]\n'
        f'\n[project]\nname = "{name}"\nversion = "{version}"\nrequires-python = ">=3.12"\n'
        f'dependencies = {json.dumps(list(requirements))}\n')
    (directory / "src").mkdir(exist_ok=True)
    (directory / "src/offline_backend.py").write_text(_OFFLINE_BUILD_BACKEND)
    (directory / "src/payload.py").write_text("VALUE = 1\n")


def build_fixture_wheel(directory, destination, monkeypatch):
    destination.mkdir(parents=True, exist_ok=True)
    backend = runpy.run_path(str(directory / "src/offline_backend.py"))
    with monkeypatch.context() as building:
        building.chdir(directory)
        return destination / backend["build_wheel"](str(destination))


def set_sources(directory, sources, extra=""):
    import tomlkit
    path = directory / "pyproject.toml"
    config = tomlkit.parse(path.read_text())
    config.setdefault("tool", {}).setdefault("uv", {})["sources"] = sources
    path.write_text(tomlkit.dumps(config) + extra)


def run_uv(arguments, directory):
    result = subprocess.run(["uv", *arguments], cwd=directory, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def installed(directory):
    result = subprocess.run([str(dependencies._python(directory)), "-I", "-B", "-c", dependencies._METADATA], cwd=directory, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    return {item["name"]: item for item in json.loads(result.stdout)["packages"]}


@pytest.fixture
def uv_projects(tmp_path, monkeypatch, projects):
    if sys.platform != "linux" or shutil.which("uv") is None:
        pytest.skip("Requires the Jupyter Linux uv runtime")
    current, candidate = projects
    root = tmp_path / "workspace"
    root.mkdir()
    wheels = root / "wheels"
    for version in ("1.2.0", "1.2.7", "1.3.0"):
        scheme = root / "scheme" / version
        write_offline_package(scheme, "scheme", version)
        build_fixture_wheel(scheme, wheels, monkeypatch)
    for directory, name, version, project_id, kind in (
        (root / current["path"], "current-package", "1.2.0", current["project_id"], current["kind"]),
        (root / candidate["directory"], "source-package", "1.2.7", candidate["id"], candidate["kind"]),
    ):
        write_offline_package(directory, name, version, ["scheme>=1.2.0,<1.3.0"])
        set_sources(directory, {"scheme": {"path": str(wheels / f"scheme-{version}-py3-none-any.whl")}})
        (directory / ".solo").write_text(json.dumps({"project_id": project_id, "name": name, "kind": kind, "scheme_version": version}))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(tmp_path / "managed-python"))
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS", "never")
    monkeypatch.setenv("UV_PYTHON", sys.executable)
    monkeypatch.setenv("UV_LINK_MODE", "copy")
    for variable in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH"):
        monkeypatch.delenv(variable, raising=False)
    return root, current, candidate


def sync_fixture(root, current, version=None):
    directory = root / current["path"]
    if version:
        import tomlkit
        current["scheme_version"] = version
        path = directory / "pyproject.toml"
        config = tomlkit.parse(path.read_text())
        config["project"]["version"] = version
        config["tool"]["uv"]["sources"]["scheme"]["path"] = str(root / "wheels" / f"scheme-{version}-py3-none-any.whl")
        path.write_text(tomlkit.dumps(config))
    run_uv(["sync", "--project", str(directory), "--no-editable"], root)


@pytest.mark.parametrize("links", [False, True], ids=["large-file", "large-file-and-symlinks"])
def test_freeze_runtime_builds_in_place_and_renames_final_wheel(project_files, tmp_path, monkeypatch, registry, links):
    root, current, candidate = project_files
    directory, source = root / current["path"], root / candidate["directory"]
    write_offline_package(source, "source-package", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    data = source / "research-data/panel.parquet"
    data.parent.mkdir()
    with data.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024)
    if links:
        try:
            (source / "linked-data").symlink_to(data.parent, target_is_directory=True)
            (source / "missing-data").symlink_to(root / "missing", target_is_directory=True)
        except OSError:
            pytest.skip("Creating symlinks requires permission on this platform")
    temporary = tmp_path / "build"
    temporary.mkdir()
    created, outputs = [], []
    for name in ("copytree", "copy2", "copyfile"):
        monkeypatch.setattr(dependencies.shutil, name, Mock(side_effect=AssertionError("Source or built wheel was copied")))
    store = Mock(side_effect=AssertionError("Built wheel was copied to storage"))
    monkeypatch.setattr(dependencies, "_store_wheel", store)

    async def run(arguments, cwd, environment):
        output = Path(arguments[arguments.index("--out-dir") + 1])
        assert arguments == ["uv", "build", "--wheel", "--no-sources", "--out-dir", str(output), str(source)]
        assert output.parent == directory / ".solo-wheels/source-package"
        UUID(output.name)
        assert output in created
        assert cwd == temporary
        wheel = build_fixture_wheel(source, output, monkeypatch)
        outputs.append((wheel, wheel.stat().st_ino))
        return ""

    monkeypatch.setattr(dependencies, "_run", run)
    wheel, frozen, snapshots = asyncio.run(dependencies._freeze_runtime(
        root, directory, source, temporary, Version("1.2.0"), {}, {}, created, current, frozenset(),
    ))
    built, inode = outputs[0]
    assert wheel == directory / ".solo-wheels/source-package" / snapshots["source-package"]["id"] / built.name
    assert wheel.is_file() and wheel.stat().st_ino == inode
    assert not built.parent.exists()
    assert created == [wheel.parent]
    assert not frozen
    assert list(temporary.iterdir()) == []
    assert data.stat().st_size == 64 * 1024 * 1024
    if links:
        assert (source / "linked-data").is_symlink()
        assert (source / "missing-data").is_symlink()
    registered = next(call.args[1]["wheel"] for call in registry.api.await_args_list if call.args[0] == "artifacts/register")
    assert Path(registered) == built
    store.assert_not_called()


def test_failed_direct_build_removes_output_before_configuration_changes(project_files, monkeypatch):
    root, current, candidate = project_files
    directory = root / current["path"]
    before = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    metadata = {"packages": [{"name": "scheme", "version": "1.2.0"}], "markers": {}}
    monkeypatch.setattr(dependencies, "_metadata", AsyncMock(return_value=metadata))
    outputs = []

    async def run(arguments, cwd, environment):
        assert arguments[:2] == ["uv", "build"]
        output = Path(arguments[arguments.index("--out-dir") + 1])
        assert output.parent == directory / ".solo-wheels/source-package"
        (output / "incomplete.whl").write_bytes(b"partial build")
        outputs.append(output)
        raise web.HTTPError(422, reason="build rejected")

    monkeypatch.setattr(dependencies, "_run", run)
    with pytest.raises(web.HTTPError, match="build rejected"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert outputs and all(not output.exists() for output in outputs)
    assert {name: (directory / name).read_bytes() for name in before} == before
    assert not dependencies._project_busy


@pytest.mark.parametrize("current_version, source_version", [("1.2.0", "1.2.7"), ("1.2.7", "1.2.0")])
@pytest.mark.parametrize("dev", [True, False])
def test_real_uv_both_patch_directions_repeat_and_group_move(uv_projects, monkeypatch, current_version, source_version, dev):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    candidate["schemeVersion"] = source_version
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('version = "1.2.7"', f'version = "{source_version}"').replace("scheme-1.2.7-", f"scheme-{source_version}-"))
    metadata = json.loads((source / ".solo").read_text())
    metadata["scheme_version"] = source_version
    (source / ".solo").write_text(json.dumps(metadata))
    sync_fixture(root, current, current_version)
    original_source = tomllib.loads((directory / "pyproject.toml").read_text())["tool"]["uv"]["sources"]["scheme"]
    with (directory / "pyproject.toml").open("a") as config:
        config.write('\n[[tool.uv.index]]\nname = "private"\nurl = "https://packages.invalid/simple"\nexplicit = true\n')
    source_before = file_snapshot(source)
    calls = []
    real_run = dependencies._run
    async def record(arguments, cwd, environment):
        calls.append((arguments, cwd))
        return await real_run(arguments, cwd, environment)
    monkeypatch.setattr(dependencies, "_run", record)
    for group in (dev, dev, not dev, dev):
        assert asyncio.run(dependencies.install_project(root, current, candidate, dev=group)) == "source-package"
        config = tomllib.loads((directory / "pyproject.toml").read_text())
        assert config["tool"]["uv"]["sources"]["scheme"] == original_source
        assert config["tool"]["uv"]["index"] == [{"name": "private", "url": "https://packages.invalid/simple", "explicit": True}]
        assert "override-dependencies" not in config["tool"]["uv"]
        assert config["tool"]["uv"]["constraint-dependencies"].count(f"scheme=={current_version}") == 1
        assert any("source-package" in item for item in (config["dependency-groups"]["dev"] if group else config["project"]["dependencies"]))
        assert not any("source-package" in item for item in (config["project"]["dependencies"] if group else config.get("dependency-groups", {}).get("dev", [])))
        actual = installed(directory)
        assert actual["scheme"]["version"] == current_version
        assert actual["source-package"]["version"] == source_version
        lock = tomllib.loads((directory / "uv.lock").read_text())
        selected = next(item for item in lock["package"] if item["name"] == "source-package")
        assert "path" in selected["source"] and ".solo-wheels/" in selected["source"]["path"]
    assert file_snapshot(source) == source_before
    assert not (source / ".venv").exists()
    assert (directory / ".solo").is_file()
    assert len([args for args, _ in calls if args[:2] == ["uv", "lock"]]) == 4
    assert all(cwd == directory for args, cwd in calls if args[:2] in (["uv", "lock"], ["uv", "sync"]))


@pytest.mark.parametrize("requirement", ["scheme>=1.3.0,<1.4.0", "scheme==1.2.7"])
def test_real_uv_rejects_transitive_business_requirements_without_env_drift(uv_projects, monkeypatch, requirement):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    dependency = root / "libraries/c"
    write_offline_package(dependency, "transitive-c", "0.1.0", [requirement])
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', '["scheme>=1.2.0,<1.3.0", "transitive-c>=0.1"]'))
    set_sources(source, {"scheme": {"path": str(root / "wheels/scheme-1.2.7-py3-none-any.whl")}, "transitive-c": {"path": str(dependency)}})
    sync_fixture(root, current)
    before, source_before = file_snapshot(directory), file_snapshot(source)
    actual_before = installed(directory)
    with pytest.raises(web.HTTPError, match="uv 安装失败"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert all((directory / name).read_bytes() == before[Path(name)] for name in ("pyproject.toml", "uv.lock"))
    assert file_snapshot(source) == source_before
    assert installed(directory) == actual_before
    assert not list(directory.glob(".solo-wheels/**/*.whl"))


def test_real_uv_reinstall_gets_new_uuid_without_overwriting_old_wheel(uv_projects):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    sync_fixture(root, current)
    asyncio.run(dependencies.install_project(root, current, candidate))
    old_wheel = next(directory.glob(".solo-wheels/source-package/*/*.whl"))
    old_content = old_wheel.read_bytes()
    (source / "src/payload.py").write_text("VALUE = 2\n")
    asyncio.run(dependencies.install_project(root, current, candidate))
    wheels = list(directory.glob(".solo-wheels/source-package/*/*.whl"))
    assert len(wheels) == 2
    assert old_wheel.read_bytes() == old_content
    for wheel in wheels:
        UUID(wheel.parent.name)
    selected = tomllib.loads((directory / "pyproject.toml").read_text())["tool"]["uv"]["sources"]["source-package"]["path"]
    assert (directory / selected) != old_wheel
    result = subprocess.run([str(dependencies._python(directory)), "-I", "-c", "import source_package; print(source_package.VALUE)"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == "2"


def test_real_uv_local_runtime_recursion_ignores_dev_graph_and_source_scheme(uv_projects, admission):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    middle, leaf = root / "projects/factor/middle", root / "libraries/leaf"
    middle_id = str(uuid4())
    write_offline_package(middle, "middle-project", "1.2.7", ["scheme>=1.2.0,<1.3.0", "runtime-leaf==0.1.0"])
    (middle / ".solo").write_text(json.dumps({"project_id": middle_id, "name": "middle", "kind": "factor", "scheme_version": "1.2.7"}))
    write_offline_package(leaf, "runtime-leaf", "0.1.0", ["scheme>=1.2.0,<1.3.0"])
    source_config = source / "pyproject.toml"
    source_config.write_text(source_config.read_text().replace('["scheme>=1.2.0,<1.3.0"]', '["scheme>=1.2.0,<1.3.0", "middle-project>=1.2"]'))
    set_sources(source, {"scheme": {"path": "/does-not-exist/outside/scheme.whl"}, "middle-project": {"path": "../middle"},
        "unused-dev": {"path": "/does-not-exist/outside/dev"}}, '\n[dependency-groups]\ndev = ["unused-dev", "source-package"]\n')
    set_sources(middle, {"scheme": {"path": str(root / "wheels/scheme-1.2.7-py3-none-any.whl")}, "runtime-leaf": {"path": str(leaf)}})
    set_sources(leaf, {"scheme": {"path": "/outside/scheme-1.3.0.whl"}})
    sync_fixture(root, current)
    before = {path: file_snapshot(path) for path in (source, middle, leaf)}
    assert asyncio.run(dependencies.install_project(root, current, candidate)) == "source-package"
    actual = installed(directory)
    assert actual["scheme"]["version"] == "1.2.0"
    assert {"middle-project", "runtime-leaf"} <= actual.keys()
    assert "unused-dev" not in actual
    assert any(call.args == (middle_id,) for call in admission.await_args_list)
    for path, files in before.items():
        assert file_snapshot(path) == files


@pytest.mark.parametrize("requested_extra, source_extra", [
    ("gpu-fast", "gpu_fast"), ("gpu_fast", "gpu-fast"),
    ("GPU.Fast", "gpu_fast"), ("gpu_fast", "GPU.Fast"),
])
def test_real_uv_normalized_extras_freeze_and_admit_local_runtime_sources(
    uv_projects, admission, requested_extra, source_extra,
):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    middle, leaf = root / "libraries/middle", root / "projects/factor/extra-leaf"
    leaf_id = str(uuid4())
    write_offline_package(middle, "middle-package", "0.1.0")
    middle_config = middle / "pyproject.toml"
    middle_config.write_text(middle_config.read_text() + f'\n[project.optional-dependencies]\n{json.dumps(source_extra)} = ["normalized-leaf>=1.2"]\n')
    write_offline_package(leaf, "normalized-leaf", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    (leaf / ".solo").write_text(json.dumps({
        "project_id": leaf_id, "name": "extra-leaf", "kind": "factor", "scheme_version": "1.2.7",
    }))
    path = source / "pyproject.toml"
    config = tomllib.loads(path.read_text())
    requirements = ["scheme>=1.2.0,<1.3.0", f"middle-package[{requested_extra}]>=0.1"]
    path.write_text(path.read_text().replace(json.dumps(config["project"]["dependencies"]), json.dumps(requirements)))
    set_sources(source, {"middle-package": {"path": str(middle)}})
    set_sources(middle, {"normalized-leaf": {"path": str(leaf), "extra": source_extra}})
    sync_fixture(root, current)
    before = {path: file_snapshot(path) for path in (source, middle, leaf)}
    assert asyncio.run(dependencies.install_project(root, current, candidate, dev=False)) == "source-package"
    admission.assert_any_await(leaf_id)
    actual = installed(directory)
    assert actual["normalized-leaf"]["version"] == "1.2.7"
    assert actual["scheme"]["version"] == "1.2.0"
    lock = tomllib.loads((directory / "uv.lock").read_text())
    selected = next(item for item in lock["package"] if item["name"] == "normalized-leaf")
    assert ".solo-wheels/normalized-leaf/" in selected["source"]["path"]
    for path, snapshot in before.items():
        assert file_snapshot(path) == snapshot


@pytest.mark.parametrize("requested_extra, source_extra", [("gpu-fast", "gpu_fast"), ("gpu_fast", "gpu-fast")])
def test_normalized_extra_retired_leaf_is_denied_before_asset_publication(
    uv_projects, admission, requested_extra, source_extra,
):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    middle, leaf = root / "libraries/middle", root / "projects/factor/retired-extra-leaf"
    leaf_id = str(uuid4())
    write_offline_package(middle, "middle-package", "0.1.0")
    middle_config = middle / "pyproject.toml"
    middle_config.write_text(middle_config.read_text() + f'\n[project.optional-dependencies]\n{json.dumps(source_extra)} = ["normalized-leaf>=1.2"]\n')
    write_offline_package(leaf, "normalized-leaf", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    (leaf / ".solo").write_text(json.dumps({
        "project_id": leaf_id, "name": "retired-extra-leaf", "kind": "factor", "scheme_version": "1.2.7",
    }))
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', json.dumps([
        "scheme>=1.2.0,<1.3.0", f"middle-package[{requested_extra}]>=0.1",
    ])))
    set_sources(source, {"middle-package": {"path": str(middle)}})
    set_sources(middle, {"normalized-leaf": {"path": str(leaf), "extra": source_extra}})
    sync_fixture(root, current)
    before = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    async def deny(identifier):
        if identifier == leaf_id:
            assert {name: (directory / name).read_bytes() for name in before} == before
            assert not (directory / ".solo-wheels/normalized-leaf").exists()
            raise web.HTTPError(422, reason="normalized extra source retired")
    admission.side_effect = deny
    with pytest.raises(web.HTTPError, match="normalized extra source retired"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    admission.assert_any_await(leaf_id)
    assert {name: (directory / name).read_bytes() for name in before} == before
    assert not list(directory.glob(".solo-wheels/*/*"))


def test_retired_recursive_runtime_project_cleans_built_assets(uv_projects, admission, registry):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    child = root / "projects/factor/retired"
    child_id = str(uuid4())
    write_offline_package(child, "retired-child", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    (child / ".solo").write_text(json.dumps({"project_id": child_id, "name": "child", "kind": "factor", "scheme_version": "1.2.7"}))
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', '["scheme>=1.2.0,<1.3.0", "retired-child>=1.2"]'))
    set_sources(source, {"retired-child": {"path": str(child)}})
    sync_fixture(root, current)
    files_before = file_snapshot(directory)
    source_before, child_before = file_snapshot(source), file_snapshot(child)
    async def check(identifier):
        if identifier == child_id:
            assert all((directory / name).read_bytes() == files_before[Path(name)] for name in ("pyproject.toml", "uv.lock"))
            assert not (directory / ".solo-wheels/retired-child").exists()
            raise web.HTTPError(422, reason="recursive source retired")
    admission.side_effect = check
    with pytest.raises(web.HTTPError, match="recursive source retired"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert file_snapshot(directory) == files_before
    assert not list(directory.glob(".solo-wheels/*/*"))
    assert not any(call.args[0] == "artifacts/register" for call in registry.api.await_args_list)
    assert file_snapshot(source) == source_before and file_snapshot(child) == child_before


def test_real_uv_git_and_wheel_runtime_sources_do_not_inject_upstream_scheme(uv_projects, monkeypatch):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    git_source = root / "git/runtime"
    wheel_source = root / "libraries/wheel"
    write_offline_package(git_source, "git-runtime", "0.1.0", ["scheme>=1.2.0,<1.3.0"])
    write_offline_package(wheel_source, "wheel-runtime", "0.1.0", ["scheme>=1.2.0,<1.3.0"])
    set_sources(git_source, {"scheme": {"path": "/outside/scheme-1.3.0-py3-none-any.whl"}})
    set_sources(wheel_source, {"scheme": {"path": "/outside/wheel-scheme-1.3.0.whl"}})
    wheel = build_fixture_wheel(wheel_source, root / "wheels", monkeypatch)
    for args in (["init", str(git_source)], ["-C", str(git_source), "add", "."],
        ["-C", str(git_source), "-c", "user.name=Solo Test", "-c", "user.email=test@invalid", "commit", "-m", "fixture"]):
        result = subprocess.run(["git", *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    commit = subprocess.check_output(["git", "-C", str(git_source), "rev-parse", "HEAD"], text=True).strip()
    uri = git_source.as_uri()
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]',
        '["scheme>=1.2.0,<1.3.0", "git-runtime>=0.1", "wheel-runtime==0.1.0"]'))
    set_sources(source, {"scheme": {"path": "/outside/source-scheme-1.2.7.whl"},
        "git-runtime": {"git": uri, "rev": commit}, "wheel-runtime": {"path": str(wheel)}})
    (source / "uv.lock").write_text(f'version = 1\n[[package]]\nname = "git-runtime"\nversion = "0.1.0"\nsource = {{ git = "{uri}?rev={commit}#{commit}" }}\n')
    sync_fixture(root, current)
    source_before = file_snapshot(source)
    git_before = file_snapshot(git_source)
    # Dirty uncommitted source must not replace the exact source-lock commit.
    (git_source / "src/payload.py").write_text("VALUE = 99\n")
    git_before = file_snapshot(git_source)
    real_run, real_store = dependencies._run, dependencies._store_wheel
    outputs = {}
    async def run(arguments, cwd, environment):
        if arguments[:2] == ["uv", "build"]:
            output = Path(arguments[arguments.index("--out-dir") + 1])
            assert output.parent.parent == directory / ".solo-wheels"
            UUID(output.name)
        result = await real_run(arguments, cwd, environment)
        if arguments[:2] == ["uv", "build"]:
            built, = output.glob("*.whl")
            outputs[output.parent.name] = (built, built.stat().st_ino)
        return result
    store = Mock(wraps=real_store)
    monkeypatch.setattr(dependencies, "_run", run)
    monkeypatch.setattr(dependencies, "_store_wheel", store)
    asyncio.run(dependencies.install_project(root, current, candidate))
    store.assert_called_once()
    assert store.call_args.args[2] == wheel
    lock = tomllib.loads((directory / "uv.lock").read_text())
    actual = installed(directory)
    assert actual["scheme"]["version"] == "1.2.0"
    assert {"git-runtime", "wheel-runtime"} <= actual.keys()
    selected = {item["name"]: item for item in lock["package"]}
    assert ".solo-wheels/git-runtime/" in selected["git-runtime"]["source"]["path"]
    assert set(outputs) == {"source-package", "git-runtime"}
    for name, (built, inode) in outputs.items():
        final = directory / selected[name]["source"]["path"]
        assert final.stat().st_ino == inode
        assert not built.parent.exists()
    probe = subprocess.run([str(dependencies._python(directory)), "-I", "-B", "-c", "import git_runtime; print(git_runtime.VALUE)"], capture_output=True, text=True)
    assert probe.returncode == 0 and probe.stdout.strip() == "1"
    frozen_wheel = directory / selected["wheel-runtime"]["source"]["path"]
    assert frozen_wheel.read_bytes() == wheel.read_bytes()
    assert file_snapshot(source) == source_before
    assert file_snapshot(git_source) == git_before


def test_real_uv_preserves_current_relative_source_and_lock_patch_not_manifest_patch(uv_projects):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    scheme = root / "wheels/scheme-1.2.0-py3-none-any.whl"
    relative = str(Path(__import__("os").path.relpath(scheme, directory)))
    set_sources(directory, {"scheme": {"path": relative}})
    current["scheme_version"] = "1.2.7"
    sync_fixture(root, current)
    before_source = tomllib.loads((directory / "pyproject.toml").read_text())["tool"]["uv"]["sources"]["scheme"]
    asyncio.run(dependencies.install_project(root, current, candidate))
    config = tomllib.loads((directory / "pyproject.toml").read_text())
    assert config["tool"]["uv"]["sources"]["scheme"] == before_source
    assert installed(directory)["scheme"]["version"] == "1.2.0"
    assert "scheme==1.2.0" in config["tool"]["uv"]["constraint-dependencies"]
    run_uv(["lock", "--project", str(directory), "--check"], root)


@pytest.mark.parametrize("escape", ["outside", "cycle"])
def test_local_runtime_graph_cannot_escape_root_or_cycle(uv_projects, escape):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    dependency = root.parent / "outside" if escape == "outside" else root / "libraries/unsafe"
    write_offline_package(dependency, "unsafe-dependency", "0.1.0")
    if escape == "cycle":
        dependency = source
    path = source / "pyproject.toml"
    name = "source-package" if escape == "cycle" else "unsafe-dependency"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', json.dumps(["scheme>=1.2.0,<1.3.0", f"{name}>=0.1"])))
    set_sources(source, {name: {"path": str(dependency)}})
    sync_fixture(root, current)
    before = file_snapshot(directory)
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert error.value.status_code == (422 if escape == "cycle" else 403)
    assert file_snapshot(directory) == before


@pytest.mark.parametrize("path", ["../outside", "/outside", "projects/../../outside", "projects\\outside", "C:/outside"])
def test_original_user_paths_are_rejected_before_resolve(project_files, path):
    root, current, candidate = project_files
    candidate["directory"] = path
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert error.value.status_code == 400


def test_real_uv_freeze_build_version_keeps_shared_scheme_and_persistent_wheels(uv_projects, monkeypatch):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    with (directory / "pyproject.toml").open("a") as config:
        config.write('\n[[tool.uv.index]]\nurl = "https://pypi.tuna.tsinghua.edu.cn/simple"\ndefault = true\n')
    sync_fixture(root, current)
    asyncio.run(dependencies.install_project(root, current, candidate))
    target = root / "runs"
    target.mkdir()
    monkeypatch.setenv("SOLO_SHARED_DIR", str(root))
    monkeypatch.setattr(versions, "parameters", lambda *args, **kwargs: {
        "entry": "current_package:Model", "project_kind": "model", "backtest": {},
        "upstream": {"factor": {"package": "source-package", "version": "1.2.7", "entry": "source_package:Factor"}},
    })
    def offline_command(arguments, cwd, payload=None):
        # Test-only cache/Python isolation; run real uv build/lock and Python metadata.
        result = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        return result.stdout
    monkeypatch.setattr(versions, "command", offline_command)
    version = {"id": str(uuid4()), "packageVersion": "1.2.8"}
    versions.build_version(directory, version, {})
    run = target / version["id"]
    environment_config = tomllib.loads((run / "environment/pyproject.toml").read_text())
    assert environment_config["tool"]["uv"]["index"] == [{
        "url": "https://pypi.tuna.tsinghua.edu.cn/simple", "default": True,
    }]
    lock = tomllib.loads((run / "environment/uv.lock").read_text())
    assert next(item for item in lock["package"] if item["name"] == "scheme")["version"] == "1.2.0"
    assert next(item for item in lock["package"] if item["name"] == "source-package")["version"] == "1.2.7"
    assert not any({"editable", "directory"} & item["source"].keys() for item in lock["package"])
    assert json.loads((run / "build.json").read_text())["scheme_version"] == "1.2.0"
    source_wheel = next((directory / ".solo-wheels/source-package").glob("*/*.whl"))
    assert (run / "wheels" / source_wheel.name).read_bytes() == source_wheel.read_bytes()
    assert set(json.loads((run / "input.json").read_text())["algos"]["factor"]) == {"package", "version", "wheel", "entry"}


@pytest.mark.parametrize("package_name, expected", [(None, None), ("Factor_Ab12", "factor-ab12"), ("factor-ab12", "factor-ab12")])
def test_project_metadata_exposes_explicit_package_and_supports_legacy(project_files, package_name, expected):
    root, current, _ = project_files
    marker = root / current["path"] / ".solo"
    metadata = json.loads(marker.read_text())
    if package_name is not None:
        metadata["package_name"] = package_name
    marker.write_text(json.dumps(metadata))
    assert handlers.read_project(root, current["path"])["package_name"] == expected


@pytest.mark.parametrize("package_name", ["", "../factor", 2, {}, []])
def test_invalid_explicit_package_metadata_is_rejected(project_files, package_name):
    root, current, _ = project_files
    marker = root / current["path"] / ".solo"
    metadata = json.loads(marker.read_text())
    metadata["package_name"] = package_name
    marker.write_text(json.dumps(metadata))
    with pytest.raises(web.HTTPError) as error:
        handlers.read_project(root, current["path"])
    assert error.value.status_code == 422


def test_explicit_source_package_mismatch_is_rejected_before_any_build(project_files, monkeypatch):
    root, current, candidate = project_files
    marker = root / candidate["directory"] / ".solo"
    metadata = json.loads(marker.read_text())
    metadata["package_name"] = "factor-ab12"
    marker.write_text(json.dumps(metadata))
    before = file_snapshot(root)
    run = AsyncMock(side_effect=AssertionError("Mismatched explicit identity built code"))
    monkeypatch.setattr(dependencies, "_run", run)
    with pytest.raises(web.HTTPError, match="显式身份"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    run.assert_not_called()
    assert file_snapshot(root) == before


def lease_handler(identifier, **body):
    return SimpleNamespace(current_user=object(), get_json_body=Mock(return_value={"project_id": identifier, **body}),
                           finish=Mock(), set_status=Mock())


def test_deletion_lease_blocks_build_and_wrong_owner_cannot_unlock():
    identifier, nonce = str(uuid4()), str(uuid4())
    acquire = lease_handler(identifier.upper(), operation="delete", lease=nonce)
    asyncio.run(dependencies.ProjectOperationsHandler.post(acquire))
    lease = acquire.finish.call_args.args[0]["lease"]
    assert acquire.finish.call_args.args[0]["project_id"] == identifier
    assert lease == nonce
    try:
        for _ in range(2):
            repeat = lease_handler(identifier, operation="delete", lease=nonce)
            asyncio.run(dependencies.ProjectOperationsHandler.post(repeat))
            repeat.finish.assert_called_once_with({"project_id": identifier, "lease": nonce})
            with pytest.raises(web.HTTPError) as error:
                asyncio.run(dependencies.ProjectOperationsHandler.post(lease_handler(identifier, operation="delete", lease=str(uuid4()))))
            assert error.value.status_code == 409
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(identifier, lease=str(uuid4()))))
        assert error.value.status_code == 409
        assert dependencies._deletion_leases[identifier] == lease
        with pytest.raises(web.HTTPError) as error:
            with dependencies.project_operation(identifier):
                raise AssertionError("Deletion lease allowed source mutation")
        assert error.value.status_code == 409
    finally:
        release = lease_handler(identifier, lease=lease)
        asyncio.run(dependencies.ProjectOperationsHandler.delete(release))
        release.set_status.assert_called_once_with(204)
    for _ in range(2):
        asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(identifier, lease=lease)))
    assert identifier not in dependencies._project_busy
    assert identifier not in dependencies._deletion_leases


def test_deletion_lease_acquire_and_idempotent_release_do_not_unlock_non_delete_operation():
    identifier, nonce = str(uuid4()), str(uuid4())
    with dependencies.project_operation(identifier):
        for _ in range(2):
            with pytest.raises(web.HTTPError) as error:
                asyncio.run(dependencies.ProjectOperationsHandler.post(lease_handler(identifier, operation="delete", lease=nonce)))
            assert error.value.status_code == 409
            asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(identifier, lease=nonce)))
            assert identifier in dependencies._project_busy and identifier not in dependencies._deletion_leases
            with pytest.raises(web.HTTPError) as error:
                with dependencies.project_operation(identifier):
                    raise AssertionError("Release unlocked an ordinary source operation")
            assert error.value.status_code == 409
    assert identifier not in dependencies._project_busy


@pytest.mark.parametrize("body", [None, {}, {"operation": "save"}, {"operation": "delete", "project_id": "invalid"},
    {"operation": "delete", "project_id": str(uuid4())},
    *({"operation": "delete", "project_id": str(uuid4()), "lease": nonce}
      for nonce in (None, "", "  ", 12, {}, []))])
def test_deletion_lease_rejects_invalid_requests(body):
    request = SimpleNamespace(current_user=object(), get_json_body=Mock(return_value=body), finish=Mock())
    with pytest.raises(web.HTTPError):
        asyncio.run(dependencies.ProjectOperationsHandler.post(request))
    request.finish.assert_not_called()


@pytest.mark.parametrize("restart", [False, True])
def test_deletion_lease_response_loss_after_grant_is_idempotent_with_persisted_nonce(monkeypatch, restart):
    identifier, nonce = str(uuid4()), str(uuid4())
    # Isolate only the process-local lease state; the Backend's persisted nonce survives.
    monkeypatch.setattr(dependencies, "_project_busy", set())
    monkeypatch.setattr(dependencies, "_deletion_leases", {})
    acquire = lease_handler(identifier.upper(), operation="delete", lease=nonce)
    acquire.finish.side_effect = ConnectionError("successful acquire response was lost")
    with pytest.raises(ConnectionError, match="response was lost"):
        asyncio.run(dependencies.ProjectOperationsHandler.post(acquire))
    assert dependencies._deletion_leases == {identifier: nonce}
    assert dependencies._project_busy == {identifier}
    if restart:
        dependencies._project_busy.clear()
        dependencies._deletion_leases.clear()
    for _ in range(2):
        retry = lease_handler(identifier, operation="delete", lease=nonce)
        asyncio.run(dependencies.ProjectOperationsHandler.post(retry))
        retry.finish.assert_called_once_with({"project_id": identifier, "lease": nonce})
        assert dependencies._deletion_leases == {identifier: nonce}
    asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(identifier.upper(), lease=nonce)))
    assert not dependencies._deletion_leases and not dependencies._project_busy


def test_deletion_lease_release_response_loss_retry_never_unlocks_new_source_operation(monkeypatch):
    identifier, nonce = str(uuid4()), str(uuid4())
    monkeypatch.setattr(dependencies, "_project_busy", set())
    monkeypatch.setattr(dependencies, "_deletion_leases", {})
    asyncio.run(dependencies.ProjectOperationsHandler.post(lease_handler(identifier, operation="delete", lease=nonce)))
    release = lease_handler(identifier, lease=nonce)
    release.finish.side_effect = ConnectionError("successful release response was lost")
    with pytest.raises(ConnectionError, match="response was lost"):
        asyncio.run(dependencies.ProjectOperationsHandler.delete(release))
    assert not dependencies._project_busy and not dependencies._deletion_leases
    with dependencies.project_operation(identifier):
        asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(identifier, lease=nonce)))
        assert identifier in dependencies._project_busy
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.ProjectOperationsHandler.post(lease_handler(identifier, operation="delete", lease=nonce)))
        assert error.value.status_code == 409
    assert not dependencies._project_busy


def test_deletion_lease_isolated_by_project_uuid_even_with_same_nonce(monkeypatch):
    original, replacement, nonce = str(uuid4()), str(uuid4()), str(uuid4())
    monkeypatch.setattr(dependencies, "_project_busy", set())
    monkeypatch.setattr(dependencies, "_deletion_leases", {})
    asyncio.run(dependencies.ProjectOperationsHandler.post(lease_handler(original, operation="delete", lease=nonce)))
    with dependencies.project_operation(replacement):
        asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(original, lease=nonce)))
        assert replacement in dependencies._project_busy and original not in dependencies._project_busy
        asyncio.run(dependencies.ProjectOperationsHandler.post(lease_handler(original, operation="delete", lease=nonce)))
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.ProjectOperationsHandler.post(lease_handler(replacement, operation="delete", lease=nonce)))
        assert error.value.status_code == 409
        asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(original, lease=nonce)))
        assert replacement in dependencies._project_busy
    assert not dependencies._project_busy and not dependencies._deletion_leases


@pytest.mark.parametrize("nonce", [None, "", "  ", 12, {}, []])
def test_deletion_lease_invalid_release_cannot_change_owner(monkeypatch, nonce):
    identifier, owned = str(uuid4()), str(uuid4())
    monkeypatch.setattr(dependencies, "_project_busy", {identifier})
    monkeypatch.setattr(dependencies, "_deletion_leases", {identifier: owned})
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(dependencies.ProjectOperationsHandler.delete(lease_handler(identifier, lease=nonce)))
    assert error.value.status_code == 400
    assert dependencies._project_busy == {identifier} and dependencies._deletion_leases == {identifier: owned}


def test_artifact_lookup_has_no_implicit_published_filter(monkeypatch):
    fetch = AsyncMock(return_value=SimpleNamespace(body=b"[]"))
    monkeypatch.setattr(dependencies, "_artifact_api", _ARTIFACT_API)
    monkeypatch.setattr(dependencies, "AsyncHTTPClient", lambda: SimpleNamespace(fetch=fetch))
    monkeypatch.setenv("SOLO_BACKEND_URL", "http://registry.invalid/")
    assert asyncio.run(dependencies.artifact_catalog()) == []
    assert fetch.await_args.args[0] == "http://registry.invalid/api/v1/artifacts"


@pytest.mark.parametrize("detail", ["registry test-password", {"reason": "registry test-password"}])
def test_artifact_api_failure_redacts_backend_details(monkeypatch, detail):
    fetch = AsyncMock(side_effect=HTTPClientError(422, response=SimpleNamespace(body=json.dumps({"detail": detail}).encode())))
    monkeypatch.setattr(dependencies, "_artifact_api", _ARTIFACT_API)
    monkeypatch.setattr(dependencies, "AsyncHTTPClient", lambda: SimpleNamespace(fetch=fetch))
    monkeypatch.setenv("REGISTRY_TEST_PASSWORD", "test-password")
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(dependencies._artifact_api("artifacts/register", {}))
    assert error.value.status_code == 422
    assert error.value.reason == "registry [REDACTED]"


def artifact_fixture(wheel, *, kind="factor", origin=None, dependencies_=None, published=True):
    name, version, _ = dependencies._wheel_metadata(wheel)
    identifier = str(uuid4())
    return {"id": identifier, "storageDirectory": identifier, "kind": kind, "package": name, "version": version, "filename": wheel.name,
            "entry": f'{name.replace("-", "_")}:Algo',
            "schemeVersion": "1.2.7", "sources": {"scheme": {"version": "1.2.7"}},
            "sourceProjectId": origin or str(uuid4()), "sourceProjectName": "deleted source",
            "sourceVersionId": None, "retired": False, "retiredReason": None,
            "sizeBytes": wheel.stat().st_size, "bundlePath": None,
            "dependencies": dependencies_ or {}, "publishedAt": "2026-10-08T00:00:00Z" if published else None}


def accepted_snapshot(item):
    return {"id": item["id"], "artifact_id": item["id"], "kind": item["kind"], "package": item["package"],
            "version": item["version"], "filename": item["filename"], "size_bytes": item["sizeBytes"],
            "entry": item["entry"], "scheme_version": item["schemeVersion"], "sources": item["sources"],
            "origin_project_id": item["sourceProjectId"], "origin_project_name": item["sourceProjectName"],
            "origin_version_id": item["sourceVersionId"], "dependencies": item["dependencies"]}


def managed_wheel_fixture(root, directory, monkeypatch, registry, *, legacy=True):
    source = root / "libraries/kept-helper"
    write_offline_package(source, "kept-helper", "0.1.0")
    storage = "a1" * 32 if legacy else str(uuid4())  # Artificial legacy name, not a digest.
    wheel = build_fixture_wheel(source, directory / ".solo-wheels/kept-helper" / storage, monkeypatch)
    record = artifact_fixture(wheel, kind="dependency", published=False)
    record["storageDirectory"] = storage
    if not legacy:
        record["id"] = storage
    registry.snapshots[record["id"]] = record
    registry.contents[record["id"]] = wheel.read_bytes()
    return wheel, record


def mock_install_commands(root, current, monkeypatch, *, failure=None):
    import tomlkit

    directory = root / current["path"]
    original_lock = tomllib.loads((directory / "uv.lock").read_text())
    scheme = next(item["version"] for item in original_lock["package"] if item["name"] == "scheme")
    state = SimpleNamespace(calls=[], installed={item["name"]: item["version"] for item in original_lock["package"]}, failed=False)
    monkeypatch.setattr(dependencies, "_metadata", AsyncMock(return_value={
        "packages": [{"name": "scheme", "version": scheme}], "markers": {},
    }))

    async def run(arguments, cwd, environment):
        step = arguments[1]
        state.calls.append(step)
        if step == "build":
            build_fixture_wheel(Path(arguments[-1]), Path(arguments[arguments.index("--out-dir") + 1]), monkeypatch)
        elif step == "lock":
            uv = tomllib.loads((directory / "pyproject.toml").read_text())["tool"]["uv"]
            sources = {name: (directory / location["path"]).resolve() for name, location in uv["sources"].items()
                       if "path" in location and location["path"].endswith(".whl") and name != "scheme"}
            constraints = uv.get("constraint-dependencies", [])
            for wheel in directory.glob(".solo-wheels/*/*/*.whl"):
                name, _, _ = dependencies._wheel_metadata(wheel)
                if f"{name} @ {wheel.as_uri()}" in constraints:
                    sources[name] = wheel
            packages = [{"name": "scheme", "version": scheme}]
            for name, wheel in sources.items():
                _, version, _ = dependencies._wheel_metadata(wheel)
                packages.append({"name": name, "version": version, "source": {"path": str(wheel)}})
            (directory / "uv.lock").write_text(tomlkit.dumps({"version": 1, "package": packages}))
        elif step == "sync":
            state.installed = {item["name"]: item["version"] for item in tomllib.loads((directory / "uv.lock").read_text())["package"]}
        else:
            raise AssertionError(f"Unexpected command: {arguments}")
        if step == failure and not state.failed:
            state.failed = True
            raise web.HTTPError(422, reason=f"injected {step} failure")
        return ""

    monkeypatch.setattr(dependencies, "_run", run)
    return state


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-storage", "uuid-storage"])
def test_installed_artifact_identity_is_exact_and_cached(project_files, monkeypatch, registry, legacy):
    root, current, _ = project_files
    directory = root / current["path"]
    wheel, selected = managed_wheel_fixture(root, directory, monkeypatch, registry, legacy=legacy)
    other = {**selected, "id": str(uuid4()), "storageDirectory": "b2" * 32}
    registry.snapshots[other["id"]] = other
    registry.accepted[current["project_id"]] = [selected]
    lock = {"package": [{"name": "Kept_Helper", "version": selected["version"], "source": {"path": str(wheel)}}]}
    known, projects = {}, {}
    for _ in range(2):
        assert asyncio.run(dependencies._installed_artifacts(root, directory, lock, known,
            project_id=current["project_id"], projects=projects)) == [selected]
    paths = [call.args[0] for call in registry.api.await_args_list]
    assert paths.count(f'projects/{current["project_id"]}') == int(legacy)
    assert paths.count(f'artifacts/{selected["id"]}') == 1
    assert f'artifacts/{other["id"]}' not in paths
    assert not any(path == "artifacts" or path.startswith("artifacts?") for path in paths)
    assert known == {wheel: selected}


@pytest.mark.parametrize("mismatch", ["unaccepted", "unregistered", "directory", "filename", "version", "package", "ambiguous"])
def test_unmapped_legacy_consumer_is_rejected_before_install_mutation(project_files, monkeypatch, registry, mismatch):
    root, current, candidate = project_files
    directory = root / current["path"]
    wheel, selected = managed_wheel_fixture(root, directory, monkeypatch, registry)
    registry.accepted[current["project_id"]] = [selected]
    lock = directory / "uv.lock"
    lock.write_text(lock.read_text() + f'[[package]]\nname = "kept-helper"\nversion = "0.1.0"\nsource = {{ path = {json.dumps(str(wheel))} }}\n')
    if mismatch == "unaccepted":
        registry.accepted.clear()
    elif mismatch == "unregistered":
        registry.snapshots.pop(selected["id"])
    elif mismatch == "directory":
        selected["storageDirectory"] = "b2" * 32
    elif mismatch == "filename":
        selected["filename"] = "different-0.1.0-py3-none-any.whl"
    elif mismatch == "version":
        selected["version"] = "0.2.0"
    elif mismatch == "package":
        registry.snapshots[selected["id"]] = {**selected, "package": "different"}
    elif mismatch == "ambiguous":
        registry.accepted[current["project_id"]].append({**selected, "id": str(uuid4())})
    if mismatch != "ambiguous":
        # A matching package/version elsewhere is never an identity fallback.
        other = {**selected, "id": str(uuid4()), "storageDirectory": wheel.parent.name}
        registry.snapshots[other["id"]] = other
    before = file_snapshot(root)
    metadata = {"packages": [{"name": "scheme", "version": "1.2.0"}], "markers": {}}
    monkeypatch.setattr(dependencies, "_metadata", AsyncMock(return_value=metadata))
    run, freeze = AsyncMock(), AsyncMock()
    monkeypatch.setattr(dependencies, "_run", run)
    monkeypatch.setattr(dependencies, "_freeze_runtime", freeze)
    with pytest.raises(web.HTTPError, match="无法确认精确身份") as error:
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert error.value.status_code == 422
    run.assert_not_awaited()
    freeze.assert_not_awaited()
    assert not any(call.args[1] is not None for call in registry.api.await_args_list if len(call.args) > 1)
    assert file_snapshot(root) == before
    assert not dependencies._project_busy


@pytest.mark.parametrize("scheme", ["1.2.0", "1.6.2"])
@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-storage", "uuid-storage"])
def test_install_unrelated_dependency_retains_exact_managed_artifact(project_files, monkeypatch, registry, scheme, legacy):
    import tomlkit

    root, current, candidate = project_files
    directory, source = root / current["path"], root / candidate["directory"]
    series = ".".join(scheme.split(".")[:2])
    minor = int(scheme.split(".")[1])
    requirement = f"scheme>={series}.0,<1.{minor + 1}.0"
    current["scheme_version"], candidate["schemeVersion"] = scheme, scheme
    for path in (directory, source):
        marker = json.loads((path / ".solo").read_text())
        marker["scheme_version"] = scheme
        (path / ".solo").write_text(json.dumps(marker))
    write_offline_package(source, "source-package", scheme, [requirement])
    configpath = directory / "pyproject.toml"
    config = tomlkit.parse(configpath.read_text())
    config["project"].update(version=scheme, dependencies=[requirement, "kept-helper==0.1.0"])
    wheel, selected = managed_wheel_fixture(root, directory, monkeypatch, registry, legacy=legacy)
    config.setdefault("tool", {}).setdefault("uv", {})["sources"] = {"kept-helper": {"path": str(wheel)}}
    configpath.write_text(tomlkit.dumps(config))
    lock = directory / "uv.lock"
    lock.write_text(tomlkit.dumps({"version": 1, "package": [
        {"name": "scheme", "version": scheme},
        {"name": "kept-helper", "version": "0.1.0", "source": {"path": str(wheel)}}]}))
    registry.accepted[current["project_id"]] = [selected]
    other = {**selected, "id": str(uuid4()), "storageDirectory": "b2" * 32}
    registry.snapshots[other["id"]] = other
    state = mock_install_commands(root, current, monkeypatch)
    inode, original = wheel.stat().st_ino, wheel.read_bytes()
    assert asyncio.run(dependencies.install_project(root, current, candidate)) == "source-package"
    assert state.installed == {"scheme": scheme, "source-package": scheme, "kept-helper": "0.1.0"}
    assert {item["id"] for item in registry.accepted[current["project_id"]]} == {
        selected["id"], next(item["id"] for item in registry.snapshots.values() if item["package"] == "source-package")}
    assert wheel.stat().st_ino == inode and wheel.read_bytes() == original
    assert list(wheel.parent.parent.glob("*/*.whl")) == [wheel]
    assert tomllib.loads(configpath.read_text())["tool"]["uv"]["sources"]["kept-helper"] == {"path": str(wheel)}
    paths = [call.args[0] for call in registry.api.await_args_list]
    assert paths.count(f'projects/{current["project_id"]}') == int(legacy)
    assert paths.count(f'artifacts/{selected["id"]}') == 1
    assert f'artifacts/{other["id"]}' not in paths


@pytest.mark.parametrize("registered", [True, False])
@pytest.mark.parametrize("recursive", [True, False])
def test_candidate_legacy_runtime_uses_its_accepted_uuid_or_rejects_cleanly(project_files, monkeypatch, registry, registered, recursive):
    root, current, candidate = project_files
    directory, source = root / current["path"], root / candidate["directory"]
    owner, owner_id = source, candidate["id"]
    if recursive:
        owner, owner_id = root / "projects/factor/middle", str(uuid4())
        write_offline_package(source, "source-package", "1.2.7", ["scheme>=1.2.0,<1.3.0", "middle-package>=1.2"])
        write_offline_package(owner, "middle-package", "1.2.7", ["scheme>=1.2.0,<1.3.0", "kept-helper==0.1.0"])
        (owner / ".solo").write_text(json.dumps({"project_id": owner_id, "name": "middle", "kind": "factor", "scheme_version": "1.2.7"}))
        set_sources(source, {"middle-package": {"path": str(owner)}})
    else:
        write_offline_package(source, "source-package", "1.2.7", ["scheme>=1.2.0,<1.3.0", "kept-helper==0.1.0"])
    wheel, selected = managed_wheel_fixture(root, owner, monkeypatch, registry)
    set_sources(owner, {"kept-helper": {"path": str(wheel)}})
    registry.accepted[owner_id] = [selected]
    other = {**selected, "id": str(uuid4()), "storageDirectory": "b2" * 32}
    registry.snapshots[other["id"]] = other
    if not registered:
        registry.snapshots.pop(selected["id"])
    # Reuse an already staged UUID wheel instead of copying the legacy file.
    staged = build_fixture_wheel(root / "libraries/kept-helper", directory / ".solo-wheels/kept-helper" / selected["id"], monkeypatch)
    before, source_before, owner_before = file_snapshot(directory), file_snapshot(source), file_snapshot(owner)
    inode = staged.stat().st_ino
    state = mock_install_commands(root, current, monkeypatch)
    store = Mock(side_effect=AssertionError("Existing managed wheel was recopied"))
    monkeypatch.setattr(dependencies, "_store_wheel", store)
    if registered:
        assert asyncio.run(dependencies.install_project(root, current, candidate)) == "source-package"
        assert state.installed["kept-helper"] == "0.1.0"
        assert selected["id"] in {item["id"] for item in registry.accepted[current["project_id"]]}
        assert other["id"] not in {item["id"] for item in registry.accepted[current["project_id"]]}
    else:
        with pytest.raises(web.HTTPError, match="无法确认精确身份"):
            asyncio.run(dependencies.install_project(root, current, candidate))
        assert file_snapshot(directory) == before
        assert state.calls == ["build"] * (2 if recursive else 1)
        assert current["project_id"] not in registry.accepted
        assert not any(call.args[0] == "artifacts/register" for call in registry.api.await_args_list)
    assert staged.stat().st_ino == inode
    assert file_snapshot(source) == source_before
    assert file_snapshot(owner) == owner_before
    paths = [call.args[0] for call in registry.api.await_args_list]
    assert paths.count(f'projects/{owner_id}') == 1
    if recursive:
        assert f'projects/{candidate["id"]}' not in paths
    assert paths.count(f'artifacts/{selected["id"]}') == 1
    assert f'artifacts/{other["id"]}' not in paths
    assert not any(call.kwargs.get("binary") for call in registry.api.await_args_list)
    store.assert_not_called()
    assert not dependencies._project_busy


@pytest.mark.parametrize("step", ["lock", "sync", "accept"])
def test_legacy_dependency_keeps_ordinary_failure_rollback(project_files, monkeypatch, registry, step):
    root, current, candidate = project_files
    directory, source = root / current["path"], root / candidate["directory"]
    write_offline_package(source, "source-package", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    wheel, selected = managed_wheel_fixture(root, directory, monkeypatch, registry)
    set_sources(directory, {"kept-helper": {"path": str(wheel)}})
    lock = directory / "uv.lock"
    lock.write_text(lock.read_text() + f'[[package]]\nname = "kept-helper"\nversion = "0.1.0"\nsource = {{ path = {json.dumps(str(wheel))} }}\n')
    registry.accepted[current["project_id"]] = [selected]
    before = file_snapshot(directory)
    state = mock_install_commands(root, current, monkeypatch, failure=step)
    installed_before = state.installed.copy()
    api = registry.api.side_effect

    async def reject(path, body=None, *, binary=False):
        if step == "accept" and path.endswith("/dependencies") and body is not None:
            raise web.HTTPError(502, reason="injected acceptance failure")
        return await api(path, body, binary=binary)

    registry.api.side_effect = reject
    with pytest.raises(web.HTTPError, match="injected"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert file_snapshot(directory) == before
    assert state.installed == installed_before
    assert state.calls.count("sync") == (1 if step == "lock" else 2)
    assert registry.accepted[current["project_id"]] == [selected]
    assert list(directory.glob(".solo-wheels/*/*/*.whl")) == [wheel]
    assert not dependencies._project_busy


@pytest.mark.parametrize("failure", [None, "missing", "lock", "sync", "accept"])
def test_real_uv_legacy_consumer_retains_exact_identity_and_rolls_back(uv_projects, monkeypatch, registry, failure):
    import tomlkit

    root, current, candidate = uv_projects
    directory = root / current["path"]
    wheel, selected = managed_wheel_fixture(root, directory, monkeypatch, registry)
    configpath = directory / "pyproject.toml"
    config = tomlkit.parse(configpath.read_text())
    config["project"]["dependencies"].append("kept-helper==0.1.0")
    config["tool"]["uv"]["sources"]["kept-helper"] = {"path": str(wheel)}
    configpath.write_text(tomlkit.dumps(config))
    registry.accepted[current["project_id"]] = [selected]
    other = {**selected, "id": str(uuid4()), "storageDirectory": "b2" * 32}
    registry.snapshots[other["id"]] = other
    sync_fixture(root, current)
    before = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    actual_before, inode = installed(directory), wheel.stat().st_ino
    wheel_directories = set(directory.glob(".solo-wheels/*/*"))
    if failure == "missing":
        registry.snapshots.pop(selected["id"])
    real_run, api = dependencies._run, registry.api.side_effect
    calls, failed = [], False

    async def run(arguments, cwd, environment):
        nonlocal failed
        calls.append(arguments[1])
        result = await real_run(arguments, cwd, environment)
        if arguments[1] == failure and not failed:
            failed = True
            raise web.HTTPError(422, reason="injected ordinary failure")
        return result

    async def reject(path, body=None, *, binary=False):
        if failure == "accept" and path.endswith("/dependencies") and body is not None:
            raise web.HTTPError(502, reason="injected ordinary failure")
        return await api(path, body, binary=binary)

    monkeypatch.setattr(dependencies, "_run", run)
    registry.api.side_effect = reject
    if failure:
        with pytest.raises(web.HTTPError, match="无法确认精确身份" if failure == "missing" else "injected"):
            asyncio.run(dependencies.install_project(root, current, candidate))
        assert {name: (directory / name).read_bytes() for name in before} == before
        assert installed(directory) == actual_before
        assert set(directory.glob(".solo-wheels/*/*")) == wheel_directories
        assert registry.accepted[current["project_id"]] == [selected]
        assert calls.count("sync") == (0 if failure == "missing" else 1 if failure == "lock" else 2)
    else:
        assert asyncio.run(dependencies.install_project(root, current, candidate)) == "source-package"
        assert installed(directory)["kept-helper"] == actual_before["kept-helper"]
        assert selected["id"] in {item["id"] for item in registry.accepted[current["project_id"]]}
        assert len(registry.accepted[current["project_id"]]) == 2
    assert wheel.stat().st_ino == inode
    assert list(wheel.parent.parent.glob("*/*.whl")) == [wheel]
    assert not any(call.kwargs.get("binary") for call in registry.api.await_args_list)
    assert not dependencies._project_busy


@pytest.mark.parametrize("legacy", [True, False], ids=["legacy-storage", "uuid-storage"])
def test_real_uv_candidate_managed_runtime_uses_exact_artifact(uv_projects, monkeypatch, registry, legacy):
    import tomlkit

    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    wheel, selected = managed_wheel_fixture(root, source, monkeypatch, registry, legacy=legacy)
    registry.accepted[candidate["id"]] = [selected]
    other = {**selected, "id": str(uuid4()), "storageDirectory": "b2" * 32}
    registry.snapshots[other["id"]] = other
    configpath = source / "pyproject.toml"
    config = tomlkit.parse(configpath.read_text())
    config["project"]["dependencies"].append("kept-helper==0.1.0")
    config["tool"]["uv"]["sources"]["kept-helper"] = {"path": str(wheel)}
    configpath.write_text(tomlkit.dumps(config))
    target = build_fixture_wheel(root / "libraries/kept-helper", directory / ".solo-wheels/kept-helper" / selected["id"], monkeypatch)
    sync_fixture(root, current)
    before, inode = file_snapshot(source), target.stat().st_ino
    assert asyncio.run(dependencies.install_project(root, current, candidate)) == "source-package"
    assert installed(directory)["kept-helper"]["version"] == "0.1.0"
    assert selected["id"] in {item["id"] for item in registry.accepted[current["project_id"]]}
    assert other["id"] not in {item["id"] for item in registry.accepted[current["project_id"]]}
    assert file_snapshot(source) == before
    assert target.stat().st_ino == inode
    assert not any(call.kwargs.get("binary") for call in registry.api.await_args_list)
    assert not dependencies._project_busy


def published_graph(root, candidate, monkeypatch, *, marked=False):
    leaf = root / "libraries/published-leaf"
    write_offline_package(leaf, "published-leaf", "0.1.0", ["scheme>=1.2.0,<1.3.0"])
    leaf_wheel = build_fixture_wheel(leaf, root / "registry-bytes", monkeypatch)
    leaf_snapshot = artifact_fixture(leaf_wheel, kind="dependency", published=False)
    child = root / "projects/factor/published-child"
    write_offline_package(child, "published-child", "1.2.7", ["scheme>=1.2.0,<1.3.0", "published-leaf==0.1.0"])
    child_wheel = build_fixture_wheel(child, root / "registry-bytes", monkeypatch)
    child_snapshot = artifact_fixture(child_wheel, dependencies_={"published-leaf": accepted_snapshot(leaf_snapshot)}, published=False)
    source = root / candidate["directory"]
    raw = 'published-child>=1.2; python_version < "0"' if marked else "published-child>=1.2"
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', json.dumps(["scheme>=1.2.0,<1.3.0", raw])))
    wheel = build_fixture_wheel(source, root / "registry-bytes", monkeypatch)
    snapshot = artifact_fixture(wheel, origin=candidate["id"],
                                dependencies_={"published-child": accepted_snapshot(child_snapshot), "published-leaf": accepted_snapshot(leaf_snapshot)})
    records = {item["id"]: item for item in (snapshot, child_snapshot, leaf_snapshot)}
    contents = {item["id"]: asset.read_bytes() for item, asset in ((snapshot, wheel), (child_snapshot, child_wheel), (leaf_snapshot, leaf_wheel))}
    for directory in (source, child, leaf):
        shutil.rmtree(directory)
    return snapshot, records, contents


def published_registry(monkeypatch, records, contents):
    async def api(path, body=None, *, binary=False):
        if path.startswith("artifacts/"):
            identifier = path.split("/")[1]
            return contents[identifier] if binary else records[identifier]
        if path.startswith("projects/") and path.endswith("/dependencies"):
            assert set(body) == {"artifacts"}
            return {"artifacts": [accepted_snapshot(records[identifier]) for identifier in body["artifacts"]]}
        raise AssertionError(f"Published installation attempted origin lookup: {path}")
    mock = AsyncMock(side_effect=api)
    monkeypatch.setattr(dependencies, "_artifact_api", mock)
    return mock


@pytest.mark.parametrize("existing", [False, True])
def test_artifact_download_writes_only_final_wheel_and_reuses_existing_file(project_files, monkeypatch, existing):
    root, current, candidate = project_files
    directory = root / current["path"]
    snapshot = {"id": str(uuid4()), "kind": "factor", "package": "source-package", "schemeVersion": "1.2.7",
                "filename": "source_package-1.2.7-py3-none-any.whl", "publishedAt": "2026-10-08T00:00:00Z"}
    target = directory / ".solo-wheels/source-package" / snapshot["id"] / snapshot["filename"]
    if existing:
        target.parent.mkdir(parents=True)
        target.write_bytes(b"existing wheel")
    original_open, writes, created = Path.open, [], []

    def open_(path, mode="r", *args, **kwargs):
        assert mode == "xb" and path == target, "Artifact used an intermediate file"
        assert target.parent in created
        writes.append(path)
        return original_open(path, mode, *args, **kwargs)

    async def api(path, body=None, *, binary=False):
        if path == f'artifacts/{snapshot["id"]}':
            return snapshot
        assert path == f'artifacts/{snapshot["id"]}/wheel' and binary
        assert writes == [target]
        return b"downloaded wheel"

    download = AsyncMock(side_effect=api)
    store = Mock(side_effect=AssertionError("Downloaded wheel was copied"))
    monkeypatch.setattr(dependencies, "_artifact_api", download)
    monkeypatch.setattr(dependencies, "_store_wheel", store)
    with monkeypatch.context() as writing:
        writing.setattr(Path, "open", open_)
        staged, snapshots = asyncio.run(dependencies._stage_artifact_graph(root, directory, [snapshot], Version("1.2.0"), created))
    assert staged == {"source-package": target}
    assert snapshots == {"source-package": snapshot}
    assert target.read_bytes() == (b"existing wheel" if existing else b"downloaded wheel")
    assert writes == ([] if existing else [target])
    assert created == ([] if existing else [target.parent])
    assert len([call for call in download.await_args_list if call.kwargs.get("binary")]) == (0 if existing else 1)
    store.assert_not_called()


def test_failed_artifact_download_removes_final_directory(project_files, monkeypatch):
    root, current, candidate = project_files
    directory = root / current["path"]
    before = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    snapshot = {"id": str(uuid4()), "kind": "factor", "package": "source-package", "schemeVersion": "1.2.7",
                "sourceProjectId": candidate["id"], "filename": "source_package-1.2.7-py3-none-any.whl",
                "publishedAt": "2026-10-08T00:00:00Z"}
    parent = directory / ".solo-wheels/source-package" / snapshot["id"]
    monkeypatch.setattr(dependencies, "_metadata", AsyncMock(return_value={
        "packages": [{"name": "scheme", "version": "1.2.0"}], "markers": {},
    }))

    async def api(path, body=None, *, binary=False):
        if binary:
            assert (parent / snapshot["filename"]).is_file()
            raise web.HTTPError(502, reason="download rejected")
        assert path == f'artifacts/{snapshot["id"]}'
        return snapshot

    monkeypatch.setattr(dependencies, "_artifact_api", api)
    with pytest.raises(web.HTTPError, match="download rejected"):
        asyncio.run(dependencies.install_artifact(root, current, snapshot))
    assert not parent.exists()
    assert {name: (directory / name).read_bytes() for name in before} == before
    assert not dependencies._project_busy


@pytest.mark.parametrize("dev", [True, False])
@pytest.mark.parametrize("marked", [True, False])
def test_real_uv_published_install_survives_deleted_sources_and_preserves_markers(uv_projects, monkeypatch, dev, marked):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    original_scheme = tomllib.loads((directory / "pyproject.toml").read_text())["tool"]["uv"]["sources"]["scheme"]
    snapshot, records, contents = published_graph(root, candidate, monkeypatch, marked=marked)
    api = published_registry(monkeypatch, records, contents)
    source_reads = Mock(side_effect=AssertionError("Published installer read an origin workspace"))
    monkeypatch.setattr(dependencies, "read_project", source_reads)
    monkeypatch.setattr(dependencies, "project_catalog", AsyncMock(side_effect=AssertionError("Published installer listed source projects")))
    assert asyncio.run(dependencies.install_artifact(root, current, snapshot, dev=dev)) == "source-package"
    source_reads.assert_not_called()
    actual = installed(directory)
    assert actual["scheme"]["version"] == "1.2.0"
    assert actual["source-package"]["version"] == "1.2.7"
    assert ("published-child" in actual) is (not marked)
    assert ("published-leaf" in actual) is (not marked)
    config = tomllib.loads((directory / "pyproject.toml").read_text())
    assert config["tool"]["uv"]["sources"]["scheme"] == original_scheme
    assert "override-dependencies" not in config["tool"]["uv"]
    assert any("source-package" in raw for raw in (config["dependency-groups"]["dev"] if dev else config["project"]["dependencies"]))
    lock = tomllib.loads((directory / "uv.lock").read_text())
    for record in records.values():
        staged = directory / ".solo-wheels" / record["package"] / record["id"] / record["filename"]
        assert staged.read_bytes() == contents[record["id"]]
    accepted = next(call for call in api.await_args_list if call.args[0].endswith("/dependencies"))
    expected = {snapshot["id"]} if marked else set(records)
    assert set(accepted.args[1]["artifacts"]) == expected
    assert all("path" in item["source"] and ".solo-wheels/" in item["source"]["path"]
               for item in lock["package"] if item["name"] in {"source-package", "published-child", "published-leaf"})
    downloaded = [call for call in api.await_args_list if call.kwargs.get("binary")]
    assert len(downloaded) == 3
    assert not dependencies._project_busy


def test_real_uv_registration_failure_removes_new_assets_before_config_changes(uv_projects, monkeypatch, registry):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    before = file_snapshot(directory)
    base = registry.api
    async def api(path, body=None, *, binary=False):
        if path == "artifacts/register":
            assert Path(body["wheel"]).is_file()
            raise web.HTTPError(422, reason="register rejected")
        return await base(path, body, binary=binary)
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    real = dependencies._run
    async def run(arguments, *args):
        assert arguments[:2] not in (["uv", "add"], ["uv", "lock"], ["uv", "sync"])
        return await real(arguments, *args)
    monkeypatch.setattr(dependencies, "_run", run)
    with pytest.raises(web.HTTPError, match="register rejected"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert file_snapshot(directory) == before


def test_real_uv_busy_recursive_source_rejected_before_its_build_or_asset_registration(uv_projects, monkeypatch, registry):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    child = root / "projects/factor/busy-child"
    child_id = str(uuid4())
    write_offline_package(child, "busy-child", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    (child / ".solo").write_text(json.dumps({"project_id": child_id, "name": "busy-child", "kind": "factor", "scheme_version": "1.2.7"}))
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', '["scheme>=1.2.0,<1.3.0", "busy-child>=1.2"]'))
    set_sources(source, {"busy-child": {"path": str(child)}})
    sync_fixture(root, current)
    before = file_snapshot(directory)
    real_run = dependencies._run
    async def run(arguments, cwd, environment):
        if arguments[:2] == ["uv", "build"]:
            assert Path(arguments[-1]) != child, "Busy recursive source was built"
        return await real_run(arguments, cwd, environment)
    monkeypatch.setattr(dependencies, "_run", run)
    with dependencies.project_operation(child_id):
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.install_project(root, current, candidate))
        assert error.value.status_code == 409
    assert file_snapshot(directory) == before
    assert not any(call.args[0] == "artifacts/register" for call in registry.api.await_args_list)
    assert not dependencies._project_busy


def test_real_uv_source_graph_reuses_copied_artifact_after_its_origin_is_deleted(uv_projects, monkeypatch, registry):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    sync_fixture(root, current)
    deleted = root / "projects/factor/deleted"
    write_offline_package(deleted, "frozen-child", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    wheel = build_fixture_wheel(deleted, root / "registry-bytes", monkeypatch)
    frozen = artifact_fixture(wheel, published=False)
    target = source / ".solo-wheels/frozen-child" / frozen["id"]
    target.mkdir(parents=True)
    wheel = Path(shutil.copy2(wheel, target))
    content = wheel.read_bytes()
    registry.snapshots[frozen["id"]] = frozen
    shutil.rmtree(deleted)
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', '["scheme>=1.2.0,<1.3.0", "frozen-child>=1.2"]'))
    set_sources(source, {"frozen-child": {"path": str(wheel)}})
    base = registry.api
    async def api(path, body=None, *, binary=False):
        if path == f'artifacts/{frozen["id"]}/wheel':
            return content
        return await base(path, body, binary=binary)
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    asyncio.run(dependencies.install_project(root, current, candidate))
    assert installed(directory)["frozen-child"]["version"] == "1.2.7"
    assert not deleted.exists()
    register = [call for call in registry.api.await_args_list if call.args[0] == "artifacts/register"]
    assert len(register) == 1 and register[0].args[1]["project_id"] == candidate["id"]


@pytest.mark.parametrize("dev", [True, False])
def test_real_uv_dependency_acceptance_retains_previous_selected_roots(uv_projects, registry, dev):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    asyncio.run(dependencies.install_project(root, current, candidate))
    previous = next(item for item in registry.snapshots.values() if item["package"] == "source-package")
    another = {**candidate, "id": str(uuid4()), "directory": "projects/factor/second"}
    source = root / another["directory"]
    write_offline_package(source, "second-package", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    (source / ".solo").write_text(json.dumps({"project_id": another["id"], "name": "second", "kind": "factor", "scheme_version": "1.2.7"}))
    asyncio.run(dependencies.install_project(root, current, another, dev=dev))
    latest = [call for call in registry.api.await_args_list if call.args[0].endswith("/dependencies")][-1]
    assert len(latest.args[1]["artifacts"]) == 2
    assert previous["id"] in latest.args[1]["artifacts"]
    assert {"source-package", "second-package"} <= installed(directory).keys()


def test_real_uv_save_snapshot_after_published_origin_deleted_is_self_contained(uv_projects, monkeypatch):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    snapshot, records, contents = published_graph(root, candidate, monkeypatch)
    published_registry(monkeypatch, records, contents)
    asyncio.run(dependencies.install_artifact(root, current, snapshot))
    (root / "runs").mkdir()
    monkeypatch.setenv("SOLO_SHARED_DIR", str(root))
    monkeypatch.setattr(versions, "parameters", lambda *args, **kwargs: {
        "entry": "current_package:Model", "project_kind": "model", "backtest": {},
        "upstream": {"factor": {"package": "source-package", "version": "1.2.7", "entry": "source_package:Factor"}},
    })
    def command(arguments, cwd, payload=None):
        result = subprocess.run(arguments, cwd=cwd, env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                text=True, capture_output=True, check=False)
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        return result.stdout
    monkeypatch.setattr(versions, "command", command)
    old_input = root / "old-input.json"
    old_input.write_bytes(b'{"immutable": "old accepted input"}\n')
    old_bytes = old_input.read_bytes()
    original = file_snapshot(directory)
    version = {"id": str(uuid4()), "packageVersion": "1.2.8"}
    versions.build_version(directory, version, {})
    run = root / "runs" / version["id"]
    data = json.loads((run / "input.json").read_text())
    assert set(data["algos"]["factor"]) == {"package", "version", "wheel", "entry"}
    for record in records.values():
        wheel = run / "wheels" / record["filename"]
        assert wheel.read_bytes() == contents[record["id"]]
    lock = tomllib.loads((run / "environment/uv.lock").read_text())
    assert {item["name"] for item in lock["package"]} >= {"source-package", "published-child", "published-leaf"}
    assert all(not {"directory", "editable"} & item["source"].keys() for item in lock["package"])
    assert next(item for item in lock["package"] if item["name"] == "scheme")["version"] == "1.2.0"
    assert file_snapshot(directory) == original
    assert old_input.read_bytes() == old_bytes




@pytest.mark.parametrize("version", ["1.2.0rc1", "v1.2.7", "1.2.0.0", "0!1.2.1"])
@pytest.mark.parametrize("requirement", ["scheme>=1.2,<2", "scheme~=1.2.0", "scheme==1.2.*",
    "scheme[extra]>=1.2.0rc1,<1.3", "scheme>=1.2,<1.3,>=1.2", "scheme>=0!1.2,<1.3"])
def test_scheme_contract_is_semantic_not_string_shape(version, requirement):
    from packaging.version import Version
    dependencies._validate_project(version, [requirement], Version("1.2.0"))


@pytest.mark.parametrize("requirement", ["scheme<1.2.99", "scheme<=1.2.0", "scheme!=1.2.42",
    "scheme>=1.2.1", "scheme==1.2.0", "scheme!=1.2.*", "scheme<1.2.0.1"])
def test_scheme_range_must_cover_entire_patch_series(requirement):
    from packaging.version import Version
    with pytest.raises(web.HTTPError, match="全部 patch"):
        dependencies._validate_project("1.2.0", [requirement], Version("1.2.0"))


@pytest.mark.parametrize("dev", [True, False])
def test_dependency_update_preserves_extras_and_marker_when_moving_groups(tmp_path, dev):
    import tomlkit
    from packaging.requirements import Requirement

    config = tomlkit.parse("""[project]
name = "consumer"
dependencies = ['source-package[GPU_fast]>=1.2; python_version >= "3.12"']
[dependency-groups]
dev = ['source-package[other]>=1.2; sys_platform == "linux"']
""")
    wheel = tmp_path / "source_package-1.2.7-py3-none-any.whl"
    dependencies._update_dependency(config, "source-package", "1.2.7", wheel, tmp_path, dev)
    target = config["dependency-groups"]["dev"] if dev else config["project"]["dependencies"]
    other = config["project"]["dependencies"] if dev else config["dependency-groups"]["dev"]
    assert not other
    parsed = [Requirement(raw) for raw in target]
    assert {frozenset(item.extras) for item in parsed} == {frozenset({"GPU_fast"}), frozenset({"other"})}
    assert {str(item.marker) for item in parsed} == {'python_version >= "3.12"', 'sys_platform == "linux"'}
    assert all(str(item.specifier) == "==1.2.7" for item in parsed)


@pytest.mark.parametrize("external", [False, True])
def test_dependency_paths_follow_links_but_stay_in_workspace(tmp_path, external):
    root = tmp_path / "workspace"
    root.mkdir()
    target = tmp_path / "outside" if external else root / "actual"
    target.mkdir()
    link = root / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires permission on this platform")
    if external:
        with pytest.raises(web.HTTPError) as error:
            dependencies._safe_path(root, link)
        assert error.value.status_code == 403
    else:
        assert dependencies._safe_path(root, link) == target


@pytest.mark.parametrize("step", ["lock", "sync", "scheme", "accept"])
def test_real_uv_failure_restores_memory_snapshot_and_resyncs(uv_projects, monkeypatch, registry, step):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    original = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    before = installed(directory)
    real_run, real_api = dependencies._run, registry.api
    failed = False
    syncs = []

    async def run(arguments, cwd, environment):
        nonlocal failed
        result = await real_run(arguments, cwd, environment)
        if arguments[:2] == ["uv", "sync"]:
            syncs.append(arguments)
            if step == "scheme" and not failed:
                failed = True
                path = directory / "uv.lock"
                path.write_text(path.read_text().replace('version = "1.2.0"', 'version = "1.2.7"'))
        target = {"lock": ["uv", "lock"], "sync": ["uv", "sync"]}.get(step)
        if not failed and arguments[:2] == target:
            failed = True
            raise web.HTTPError(422, reason="injected installation failure")
        return result

    async def api(path, body=None, *, binary=False):
        if step == "accept" and path.endswith("/dependencies"):
            raise web.HTTPError(502, reason="injected acceptance failure")
        return await real_api(path, body, binary=binary)

    monkeypatch.setattr(dependencies, "_run", run)
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    with pytest.raises(web.HTTPError):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert {name: (directory / name).read_bytes() for name in original} == original
    assert installed(directory) == before
    assert len(syncs) == (1 if step == "lock" else 2)
    assert not list(directory.glob(".solo-wheels/*/*/*.whl"))
    assert not dependencies._project_busy


@pytest.mark.parametrize("kind", ["editable", "override", "workspace", "requires-dist"])
def test_real_uv_accepts_existing_configuration_without_admission_layers(uv_projects, monkeypatch, kind):
    import tomlkit

    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    config_path = directory / "pyproject.toml"
    config = tomlkit.parse(config_path.read_text())
    if kind == "editable":
        local = root / "libraries/existing"
        write_offline_package(local, "existing-local", "0.1.0")
        config["project"]["dependencies"].append("existing-local==0.1.0")
        config["tool"]["uv"]["sources"]["existing-local"] = {"path": str(local), "editable": True}
    elif kind == "override":
        config["tool"]["uv"]["override-dependencies"] = ["scheme==1.2.0"]
    elif kind == "workspace":
        config["tool"]["uv"]["workspace"] = {"members": ["members/*"]}
    else:
        local = root / "libraries/url"
        write_offline_package(local, "local-url", "0.1.0")
        path = source / "pyproject.toml"
        upstream = tomlkit.parse(path.read_text())
        upstream["project"]["dependencies"].append(f"local-url @ {local.as_uri()}")
        path.write_text(tomlkit.dumps(upstream))
    config_path.write_text(tomlkit.dumps(config))
    sync_fixture(root, current)
    assert asyncio.run(dependencies.install_project(root, current, candidate)) == "source-package"
    assert installed(directory)["scheme"]["version"] == "1.2.0"
    after = tomllib.loads(config_path.read_text())["tool"]["uv"]
    if kind in ("override", "workspace"):
        key = "override-dependencies" if kind == "override" else "workspace"
        assert after[key] == config["tool"]["uv"][key]
    if kind == "editable":
        assert installed(directory)["existing-local"]["version"] == "0.1.0"
    if kind == "requires-dist":
        assert installed(directory)["local-url"]["version"] == "0.1.0"


@pytest.mark.parametrize("dev", [True, False])
def test_real_uv_updating_selected_extras_freezes_their_runtime_sources(uv_projects, monkeypatch, dev):
    import tomlkit
    from packaging.requirements import Requirement

    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    sync_fixture(root, current)
    asyncio.run(dependencies.install_project(root, current, candidate))
    leaf = root / "libraries/selected-extra"
    write_offline_package(leaf, "selected-extra", "0.1.0")
    path = source / "pyproject.toml"
    upstream = tomlkit.parse(path.read_text())
    upstream["project"]["optional-dependencies"] = {"feature": ["selected-extra==0.1.0"]}
    upstream["tool"]["uv"]["sources"]["selected-extra"] = {"path": str(leaf), "extra": "feature"}
    path.write_text(tomlkit.dumps(upstream))
    path = directory / "pyproject.toml"
    config = tomlkit.parse(path.read_text())
    config["dependency-groups"]["dev"] = ['source-package[feature]==1.2.7; python_version >= "3.12"']
    path.write_text(tomlkit.dumps(config))
    asyncio.run(dependencies.install_project(root, current, candidate, dev=dev))
    assert installed(directory)["selected-extra"]["version"] == "0.1.0"
    config = tomllib.loads(path.read_text())
    group = config["dependency-groups"]["dev"] if dev else config["project"]["dependencies"]
    requirement = next(Requirement(raw) for raw in group if Requirement(raw).name == "source-package")
    assert requirement.extras == {"feature"}
    assert str(requirement.marker) == 'python_version >= "3.12"'
