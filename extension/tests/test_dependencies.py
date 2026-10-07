import asyncio
import hashlib
import json
import runpy
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from solo_jupyter import dependencies, versions
from tornado import web
from tornado.httpclient import HTTPClientError


_CHECK_PROJECT_RELEASE = dependencies.check_project_release


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
        (directory / ".venv/bin").mkdir(parents=True)
        (directory / ".venv/bin/python").write_text("temporary environment sentinel\n")
    return tmp_path, current, candidate


def handler_for(root, current, candidate, *, dev=True):
    return SimpleNamespace(current_user=object(), settings={"server_root_dir": str(root)},
        current_project=Mock(return_value=current), get_query_argument=Mock(return_value=current["path"]),
        get_json_body=Mock(return_value={"path": current["path"], "project_id": candidate["id"], "dev": dev}), finish=Mock())


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
    handler.finish.assert_called_once_with({"projects": catalog[:2] if current_version in ("1.2.0", "1.2.7", "v1.2.0") else []})


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
    assert not dependencies._installing


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
    assert not dependencies._installing


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


@pytest.mark.parametrize("location", ["source-directory", "scheme-directory", "locked-editable", "locked-directory"])
def test_existing_live_directory_sources_are_rejected_before_temp_or_process(project_files, monkeypatch, location):
    root, current, candidate = project_files
    directory, source = root / current["path"], root / candidate["directory"]
    if location.endswith("directory") and not location.startswith("locked"):
        set_sources(directory, {"scheme" if location.startswith("scheme") else "legacy-package": {"path": str(source)}})
    else:
        with (directory / "uv.lock").open("a") as lock:
            lock.write(f'\n[[package]]\nname = "legacy-package"\nversion = "1.2.0"\nsource = {{ {location.removeprefix("locked-")} = "{source}" }}\n')
    before = file_snapshot(root)
    run = AsyncMock(side_effect=AssertionError("Unsafe preview ran subprocess"))
    temporary = Mock(side_effect=AssertionError("Unsafe preview created temporary directory"))
    monkeypatch.setattr(dependencies, "_run", run)
    monkeypatch.setattr(dependencies.tempfile, "mkdtemp", temporary)
    with pytest.raises(web.HTTPError, match="冻结为 wheel"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    run.assert_not_called()
    temporary.assert_not_called()
    assert file_snapshot(root) == before


@pytest.mark.parametrize("dev", [True, False])
@pytest.mark.parametrize("raw", ["source-package[feature]>=1.2", 'source-package>=1.2; python_version >= "3.12"'])
def test_existing_upstream_extras_and_markers_are_not_silently_lost(project_files, monkeypatch, dev, raw):
    root, current, candidate = project_files
    directory = root / current["path"]
    path = directory / "pyproject.toml"
    if dev:
        path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', json.dumps(["scheme>=1.2.0,<1.3.0", raw])))
    else:
        path.write_text(path.read_text() + f'\n[dependency-groups]\ndev = {json.dumps([raw])}\n')
    before = file_snapshot(root)
    run = AsyncMock(side_effect=AssertionError("Lossy grouping ran subprocess"))
    temporary = Mock(side_effect=AssertionError("Lossy grouping created temporary directory"))
    monkeypatch.setattr(dependencies, "_run", run)
    monkeypatch.setattr(dependencies.tempfile, "mkdtemp", temporary)
    with pytest.raises(web.HTTPError, match="extras 或 marker"):
        asyncio.run(dependencies.install_project(root, current, candidate, dev=dev))
    run.assert_not_called()
    temporary.assert_not_called()
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


@pytest.mark.parametrize("side", ["current", "source"])
@pytest.mark.parametrize("requirement", ["scheme==1.2.7", "scheme>=1.2.7,<1.3", "scheme>=1.2,<2", "scheme @ https://example.test/scheme.whl", None,
    "scheme[extra]>=1.2,<1.3", "scheme>=1.2,<1.3,>=1.2", "scheme>=1.2.0rc1,<1.3", "scheme>=0!1.2,<1.3", "scheme>=1.2.0.0,<1.3",
    ["scheme>=1.2,<1.3", "scheme>=1.2,<1.3"]])
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
@pytest.mark.parametrize("version", ["0.1.0", "1.1.7", "1.3.0", "2.2.0", "1.2.0rc1", "0!1.2.0", "1.2.0.0"])
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


@pytest.mark.parametrize("lock_versions", [None, [], ["invalid"], ["1.3.0"], ["1.2.0", "1.2.7"], ["1.2.0", "1.2.0"], ["1.2.0rc1"], ["0!1.2.0"], ["1.2.0.0"]])
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
    with pytest.raises(web.HTTPError, match="锁定记录"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    uv.assert_not_called()
    assert file_snapshot(root) == before


@pytest.mark.parametrize("override", ["scheme==1.2.0", "scheme>=1.2.0,<1.3.0", "scheme==1.2.7"])
def test_all_scheme_overrides_are_rejected_not_rewritten(project_files, monkeypatch, override):
    root, current, candidate = project_files
    path = root / current["path"] / "pyproject.toml"
    path.write_text(path.read_text() + f'\n[tool.uv]\noverride-dependencies = {json.dumps([override])}\n')
    before = file_snapshot(root)
    uv = AsyncMock(side_effect=AssertionError("Scheme override must not run uv"))
    monkeypatch.setattr(dependencies.asyncio, "create_subprocess_exec", uv)
    with pytest.raises(web.HTTPError, match="移除 Scheme override"):
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


_OFFLINE_BUILD_BACKEND = '''import base64
import hashlib
from pathlib import Path
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
    code = Path("payload.py").read_text() if Path("payload.py").exists() else "VALUE = 1\\n"
    files = {f"{name}/__init__.py": (f"__version__ = {version!r}\\n" + code).encode(),
        f"{dist_info}/METADATA": (metadata + "\\n").encode(),
        f"{dist_info}/WHEEL": b"Wheel-Version: 1.0\\nGenerator: solo-test\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}
    records = []
    for path, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        records.append(f"{path},sha256={digest},{len(content)}\\n")
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
        '[build-system]\nrequires = []\nbuild-backend = "offline_backend"\nbackend-path = ["."]\n'
        f'\n[project]\nname = "{name}"\nversion = "{version}"\nrequires-python = ">=3.12"\n'
        f'dependencies = {json.dumps(list(requirements))}\n')
    (directory / "offline_backend.py").write_text(_OFFLINE_BUILD_BACKEND)
    (directory / "payload.py").write_text("VALUE = 1\n")


def build_fixture_wheel(directory, destination, monkeypatch):
    destination.mkdir(parents=True, exist_ok=True)
    backend = runpy.run_path(str(directory / "offline_backend.py"))
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
        assert not any(str(path).startswith(".solo-install-") for path in directory.parent.iterdir())
    assert file_snapshot(source) == source_before
    assert not (source / ".venv").exists()
    assert (directory / ".solo").is_file()
    add_commands = [arguments for arguments, _ in calls if arguments[:2] == ["uv", "add"]]
    assert len(add_commands) == 4 and all("--no-sync" in arguments and arguments[-1].endswith(".whl") for arguments in add_commands)
    assert all(cwd != directory for arguments, cwd in calls if arguments[:2] in (["uv", "add"], ["uv", "lock"]))


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
    assert file_snapshot(directory) == before
    assert file_snapshot(source) == source_before
    assert installed(directory) == actual_before
    assert not list(directory.glob(".solo-wheels/**/*.whl"))
    assert not list(directory.parent.glob(".solo-install-*"))


def test_real_uv_preview_failure_never_mutates_live_environment(uv_projects, monkeypatch):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    before, actual_before = file_snapshot(directory), installed(directory)
    real_run = dependencies._run
    async def fail_preview(arguments, cwd, environment):
        if arguments[:2] == ["uv", "sync"] and cwd != directory:
            await real_run(arguments, cwd, environment)
            raise web.HTTPError(422, reason="preview validation failure")
        return await real_run(arguments, cwd, environment)
    monkeypatch.setattr(dependencies, "_run", fail_preview)
    with pytest.raises(web.HTTPError, match="preview validation failure"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert file_snapshot(directory) == before
    assert installed(directory) == actual_before
    assert not list(directory.parent.glob(".solo-install-*"))


@pytest.mark.parametrize("failure", ["sync", "metadata"])
def test_real_uv_live_failure_restores_original_environment_and_lock(uv_projects, monkeypatch, failure):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    sentinel = directory / ".venv/bin/original-script"
    sentinel.write_text(f"#!{dependencies._python(directory)}\noriginal env\n")
    old_assets = directory / ".solo-wheels/existing/oldhash"
    old_assets.mkdir(parents=True)
    (old_assets / "kept.whl").write_bytes(b"pre-existing asset")
    before, actual_before = file_snapshot(directory), installed(directory)
    original_inode = (directory / ".venv").stat().st_ino
    real_run, real_metadata = dependencies._run, dependencies._metadata
    async def fail_sync(arguments, cwd, environment):
        result = await real_run(arguments, cwd, environment)
        if failure == "sync" and arguments[:2] == ["uv", "sync"] and cwd == directory:
            assert (directory / ".venv").stat().st_ino != original_inode
            assert installed(directory)["source-package"]["version"] == "1.2.7"
            raise web.HTTPError(422, reason="live sync failure")
        return result
    live_reads = 0
    async def fail_metadata(cwd, environment):
        nonlocal live_reads
        data = await real_metadata(cwd, environment)
        if cwd == directory:
            live_reads += 1
            if failure == "metadata" and live_reads > 1:
                next(item for item in data["packages"] if item["name"] == "scheme")["version"] = "1.2.7"
        return data
    monkeypatch.setattr(dependencies, "_run", fail_sync)
    monkeypatch.setattr(dependencies, "_metadata", fail_metadata)
    with pytest.raises(web.HTTPError):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert (directory / ".venv").stat().st_ino == original_inode
    assert file_snapshot(directory) == before
    assert installed(directory) == actual_before
    assert not list(directory.parent.glob(".solo-install-*"))


def test_real_uv_changed_content_gets_new_hash_without_overwriting_old_wheel(uv_projects):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    sync_fixture(root, current)
    asyncio.run(dependencies.install_project(root, current, candidate))
    old_wheel = next(directory.glob(".solo-wheels/source-package/*/*.whl"))
    old_content = old_wheel.read_bytes()
    (source / "payload.py").write_text("VALUE = 2\n")
    asyncio.run(dependencies.install_project(root, current, candidate))
    wheels = list(directory.glob(".solo-wheels/source-package/*/*.whl"))
    assert len(wheels) == 2
    assert old_wheel.read_bytes() == old_content
    for wheel in wheels:
        assert wheel.parent.name == hashlib.sha256(wheel.read_bytes()).hexdigest()
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
    before = file_snapshot(directory)
    directories = {path.relative_to(directory) for path in directory.rglob("*") if path.is_dir()}
    async def deny(identifier):
        if identifier == leaf_id:
            assert file_snapshot(directory) == before
            assert not (directory / ".solo-wheels").exists()
            raise web.HTTPError(422, reason="normalized extra source retired")
    admission.side_effect = deny
    with pytest.raises(web.HTTPError, match="normalized extra source retired"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    admission.assert_any_await(leaf_id)
    assert file_snapshot(directory) == before
    assert {path.relative_to(directory) for path in directory.rglob("*") if path.is_dir()} == directories


def test_retired_recursive_runtime_project_leaves_no_real_assets_or_directories(uv_projects, admission):
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
    dirs_before = {path.relative_to(directory) for path in directory.rglob("*") if path.is_dir()}
    source_before, child_before = file_snapshot(source), file_snapshot(child)
    async def check(identifier):
        if identifier == child_id:
            assert file_snapshot(directory) == files_before
            assert not (directory / ".solo-wheels").exists()
            raise web.HTTPError(422, reason="recursive source retired")
    admission.side_effect = check
    with pytest.raises(web.HTTPError, match="recursive source retired"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert file_snapshot(directory) == files_before
    assert {path.relative_to(directory) for path in directory.rglob("*") if path.is_dir()} == dirs_before
    assert file_snapshot(source) == source_before and file_snapshot(child) == child_before
    assert not list(directory.parent.glob(".solo-install-*"))


@pytest.mark.parametrize("via", ["candidate", "local", "wheel"])
def test_direct_local_directory_requires_dist_is_rejected_before_real_asset_publication(uv_projects, monkeypatch, via):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    live = root / "projects/factor/live"
    write_offline_package(live, "live-child", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    requirement = f"live-child @ {live.as_uri()}"
    if via == "candidate":
        path = source / "pyproject.toml"
        path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', json.dumps(["scheme>=1.2.0,<1.3.0", requirement])))
    else:
        middle = root / "libraries/middle"
        write_offline_package(middle, "unsafe-middle", "0.1.0", [requirement])
        location = middle if via == "local" else build_fixture_wheel(middle, root / "wheels", monkeypatch)
        path = source / "pyproject.toml"
        path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', '["scheme>=1.2.0,<1.3.0", "unsafe-middle>=0.1"]'))
        set_sources(source, {"unsafe-middle": {"path": str(location)}})
    sync_fixture(root, current)
    before, live_before = file_snapshot(directory), file_snapshot(live)
    with pytest.raises(web.HTTPError, match="非 wheel file URL"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert file_snapshot(directory) == before and file_snapshot(live) == live_before
    assert not (directory / ".solo-wheels").exists()


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
    (git_source / "payload.py").write_text("VALUE = 99\n")
    git_before = file_snapshot(git_source)
    asyncio.run(dependencies.install_project(root, current, candidate))
    lock = tomllib.loads((directory / "uv.lock").read_text())
    actual = installed(directory)
    assert actual["scheme"]["version"] == "1.2.0"
    assert {"git-runtime", "wheel-runtime"} <= actual.keys()
    selected = {item["name"]: item for item in lock["package"]}
    assert ".solo-wheels/git-runtime/" in selected["git-runtime"]["source"]["path"]
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


@pytest.mark.parametrize("escape", ["outside", "symlink", "cycle"])
def test_local_runtime_graph_cannot_escape_root_follow_links_or_cycle(uv_projects, escape):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    dependency = root.parent / "outside" if escape == "outside" else root / "libraries/unsafe"
    write_offline_package(dependency, "unsafe-dependency", "0.1.0")
    if escape == "symlink":
        link = root / "libraries/link"
        link.symlink_to(dependency, target_is_directory=True)
        dependency = link
    elif escape == "cycle":
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
    sync_fixture(root, current)
    asyncio.run(dependencies.install_project(root, current, candidate))
    target = root / "runs"
    target.mkdir()
    monkeypatch.setenv("SOLO_SHARED_DIR", str(root))
    monkeypatch.setattr(versions, "parameters", lambda *args: {
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
    lock = tomllib.loads((run / "environment/uv.lock").read_text())
    assert next(item for item in lock["package"] if item["name"] == "scheme")["version"] == "1.2.0"
    assert next(item for item in lock["package"] if item["name"] == "source-package")["version"] == "1.2.7"
    assert not any({"editable", "directory"} & item["source"].keys() for item in lock["package"])
    assert json.loads((run / "build.json").read_text())["scheme_version"] == "1.2.0"
    source_wheel = next((directory / ".solo-wheels/source-package").glob("*/*.whl"))
    assert (run / "wheels" / source_wheel.name).read_bytes() == source_wheel.read_bytes()
    assert json.loads((run / "input.json").read_text())["algos"]["factor"]["sha256"] == hashlib.sha256(source_wheel.read_bytes()).hexdigest()
