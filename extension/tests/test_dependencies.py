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
from uuid import UUID, uuid4

import pytest
from solo_jupyter import dependencies, handlers, versions
from tornado import web
from tornado.httpclient import HTTPClientError


_CHECK_PROJECT_RELEASE = dependencies.check_project_release
_ARTIFACT_API = dependencies._artifact_api


def install_receipts(records):
    """Model only the durable exact-ID receipt contract, never expand artifact closure."""
    pending, accepted = {}, {}

    def installation(path, body):
        parts = path.split("/")
        if len(parts) < 4 or parts[:1] != ["projects"] or parts[2:4] != ["dependencies", "installations"]:
            return None
        consumer = parts[1]
        if len(parts) == 4:
            identifier = str(UUID(body["install_id"]))
            if body.get("renew"):
                assert identifier in pending, "A renewing nonce must never recreate an expired/deleted receipt"
            if any(key != identifier and receipt["consumer"] == consumer and receipt["accepted"] is not None
                   for key, receipt in pending.items()):
                raise web.HTTPError(409, reason="Another accepted installation awaits explicit finalization")
            receipt = pending.setdefault(identifier, {"consumer": consumer, "before": accepted.get(consumer, {}).copy(),
                                                      "pending": {}, "accepted": None})
            assert receipt["consumer"] == consumer
            for artifact in body["artifacts"]:
                receipt["pending"][artifact] = records[artifact]
            return {"install_id": identifier, "artifacts": list(receipt["pending"].values()),
                    "expires_at": "2099-01-01T00:00:00+00:00"}
        identifier, operation = parts[4:]
        assert operation in {"complete", "abort"}
        receipt = pending.get(identifier)
        if receipt is not None:
            assert receipt["consumer"] == consumer
            if operation == "abort":
                assert accepted.get(consumer, {}) in (receipt["before"], receipt["accepted"])
                accepted[consumer] = receipt["before"]
            else:
                assert receipt["accepted"] is not None
            del pending[identifier]
        return {"install_id": identifier, "completed" if operation == "complete" else "aborted": True}

    def accept(path, body):
        receipt = pending[body["install_id"]]
        consumer = path.split("/")[1]
        assert receipt["consumer"] == consumer
        assert set(body["artifacts"]) <= set(receipt["pending"])
        result = {records[identifier]["package"]: records[identifier] for identifier in body["artifacts"]}
        accepted[consumer] = result
        receipt["accepted"] = result
        return {"artifacts": list(result.values())}

    return SimpleNamespace(pending=pending, accepted=accepted, installation=installation, accept=accept)


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    snapshots = {}
    receipts = install_receipts(snapshots)

    async def api(path, body=None, *, binary=False):
        result = receipts.installation(path, body)
        if result is not None:
            return result
        if path == "artifacts/register":
            wheel = Path(body["wheel"])
            name, version, _ = dependencies._wheel_metadata(wheel)
            matches = [json.loads(marker.read_text()) for marker in tmp_path.rglob(".solo")
                       if json.loads(marker.read_text()).get("project_id") == body["project_id"]]
            assert matches, "Registration must use the actual built project marker"
            metadata = matches[0]
            assert wheel.is_absolute() and ".solo-wheels" in wheel.parts
            assert body["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
            previous = next((item for item in snapshots.values() if item["sha256"] == body["sha256"]), None)
            kind = "dependency" if body["project_id"] == body["consumer_project_id"] else metadata["kind"]
            snapshot = previous or {"id": str(uuid4()), "package": name, "version": version,
                "filename": wheel.name, "sha256": body["sha256"], "kind": kind,
                "entry": f'{name.replace("-", "_")}:Algo', "schemeVersion": metadata["scheme_version"],
                "sourceProjectId": metadata["project_id"], "sourceProjectName": metadata["name"],
                "sources": {"scheme": {"version": metadata["scheme_version"]}}, "dependencies": {}, "publishedAt": None}
            receipt = receipts.pending[body["install_id"]]
            assert receipt["consumer"] == body["consumer_project_id"]
            snapshots[snapshot["id"]] = snapshot
            receipt["pending"][snapshot["id"]] = snapshot
            return snapshot
        if path.startswith("projects/") and path.endswith("/dependencies"):
            return receipts.accept(path, body)
        if path.startswith("artifacts?"):
            from urllib.parse import parse_qs
            query = parse_qs(path.split("?", 1)[1])
            return [item for item in snapshots.values()
                    if ("sha256" not in query or item["sha256"] == query["sha256"][0])
                    and ("published" not in query or bool(item["publishedAt"]) == (query["published"][0] == "true"))]
        if path.startswith("artifacts/"):
            identifier = path.split("/")[1]
            return snapshots[identifier]
        raise AssertionError(f"Unexpected registry request: {path}")

    mock = AsyncMock(side_effect=api)
    monkeypatch.setattr(dependencies, "_artifact_api", mock)
    return SimpleNamespace(api=mock, snapshots=snapshots, receipts=receipts)


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
        get_json_body=Mock(return_value={"path": current["path"], "project_id": candidate["id"], "dev": dev}), finish=Mock())


def file_snapshot(root, *, ignore_intent=False):
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and (not ignore_intent or path.name != dependencies._INSTALLATION_INTENT)}


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
            lock.write(f'\n[[package]]\nname = "legacy-package"\nversion = "1.2.0"\nsource = {{ {location.removeprefix("locked-")} = "{source.as_posix()}" }}\n')
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


def test_installer_holds_source_project_guard(project_files, monkeypatch):
    root, current, candidate = project_files
    async def install(*args, **kwargs):
        assert current["project_id"] in dependencies._project_busy
        assert candidate["id"] in dependencies._project_busy
        raise RuntimeError("installer failed")
    monkeypatch.setattr(dependencies, "_install_project", install)
    with pytest.raises(RuntimeError, match="installer failed"):
        asyncio.run(dependencies.install_project(root, current, candidate))
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
    with (directory / "pyproject.toml").open("a") as config:
        config.write('\n[[tool.uv.index]]\nurl = "https://pypi.tuna.tsinghua.edu.cn/simple"\ndefault = true\n')
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
    assert json.loads((run / "input.json").read_text())["algos"]["factor"]["sha256"] == hashlib.sha256(source_wheel.read_bytes()).hexdigest()


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
      for nonce in (None, "", "  ", 12, {}, [], "x" * 129))])
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


@pytest.mark.parametrize("nonce", [None, "", "  ", 12, {}, [], "x" * 129])
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
    digest = "a" * 64
    assert asyncio.run(dependencies.artifact_catalog(sha256=digest)) == []
    assert fetch.await_args.args[0] == f"http://registry.invalid/api/v1/artifacts?sha256={digest}"


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
    return {"id": str(uuid4()), "kind": kind, "package": name, "version": version, "filename": wheel.name,
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "entry": f'{name.replace("-", "_")}:Algo',
            "schemeVersion": "1.2.7", "sources": {"scheme": {"version": "1.2.7"}},
            "sourceProjectId": origin or str(uuid4()), "sourceProjectName": "deleted source",
            "dependencies": dependencies_ or {}, "publishedAt": "2026-10-08T00:00:00Z" if published else None}


def published_graph(root, candidate, monkeypatch, *, marked=False):
    leaf = root / "libraries/published-leaf"
    write_offline_package(leaf, "published-leaf", "0.1.0", ["scheme>=1.2.0,<1.3.0"])
    leaf_wheel = build_fixture_wheel(leaf, root / "registry-bytes", monkeypatch)
    leaf_snapshot = artifact_fixture(leaf_wheel, kind="dependency", published=False)
    child = root / "projects/factor/published-child"
    write_offline_package(child, "published-child", "1.2.7", ["scheme>=1.2.0,<1.3.0", "published-leaf==0.1.0"])
    child_wheel = build_fixture_wheel(child, root / "registry-bytes", monkeypatch)
    child_snapshot = artifact_fixture(child_wheel, dependencies_=[leaf_snapshot], published=False)
    source = root / candidate["directory"]
    raw = 'published-child>=1.2; python_version < "0"' if marked else "published-child>=1.2"
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', json.dumps(["scheme>=1.2.0,<1.3.0", raw])))
    wheel = build_fixture_wheel(source, root / "registry-bytes", monkeypatch)
    snapshot = artifact_fixture(wheel, origin=candidate["id"],
                                dependencies_={"published-child": child_snapshot, "published-leaf": leaf_snapshot})
    records = {item["id"]: item for item in (snapshot, child_snapshot, leaf_snapshot)}
    contents = {item["id"]: asset.read_bytes() for item, asset in ((snapshot, wheel), (child_snapshot, child_wheel), (leaf_snapshot, leaf_wheel))}
    for directory in (source, child, leaf):
        shutil.rmtree(directory)
    return snapshot, records, contents


def published_registry(monkeypatch, records, contents):
    receipts = install_receipts(records)

    async def api(path, body=None, *, binary=False):
        result = receipts.installation(path, body)
        if result is not None:
            return result
        if path.startswith("artifacts/"):
            identifier = path.split("/")[1]
            if binary:
                assert set(records) <= {identifier for receipt in receipts.pending.values() for identifier in receipt["pending"]}
            return contents[identifier] if binary else records[identifier]
        if path.startswith("projects/") and path.endswith("/dependencies"):
            return receipts.accept(path, body)
        raise AssertionError(f"Published installation attempted origin lookup: {path}")
    mock = AsyncMock(side_effect=api)
    mock.receipts = receipts
    monkeypatch.setattr(dependencies, "_artifact_api", mock)
    return mock


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
        staged = directory / ".solo-wheels" / record["package"] / record["sha256"] / record["filename"]
        assert staged.read_bytes() == contents[record["id"]]
    accepted = next(call for call in api.await_args_list if call.args[0].endswith("/dependencies"))
    expected = {snapshot["id"]} if marked else set(records)
    assert set(accepted.args[1]["artifacts"]) == expected
    assert all("path" in item["source"] and ".solo-wheels/" in item["source"]["path"]
               for item in lock["package"] if item["name"] in {"source-package", "published-child", "published-leaf"})
    downloaded = [call for call in api.await_args_list if call.kwargs.get("binary")]
    assert len(downloaded) == 3
    assert not dependencies._project_busy
    assert not list(directory.parent.glob(".solo-install-*"))


@pytest.mark.parametrize("failure", ["hash", "metadata", "missing-id", "retired", "conflict"])
def test_real_uv_published_bad_closure_rolls_back_before_live_commit(uv_projects, monkeypatch, failure):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    snapshot, records, contents = published_graph(root, candidate, monkeypatch)
    child = next(record for record in records.values() if record["package"] == "published-child")
    if failure == "hash":
        contents[child["id"]] += b"tampered"
    elif failure == "metadata":
        child["version"] = "1.2.8"
    elif failure == "missing-id":
        del child["id"]
    elif failure == "retired":
        child["retired"] = True
    else:
        duplicate = {**child, "id": str(uuid4()), "sha256": "a" * 64}
        records[duplicate["id"]] = duplicate
        snapshot["dependencies"]["duplicate"] = duplicate
    api = published_registry(monkeypatch, records, contents)
    before, actual = file_snapshot(directory), installed(directory)
    with pytest.raises(web.HTTPError):
        asyncio.run(dependencies.install_artifact(root, current, snapshot))
    assert file_snapshot(directory) == before
    assert installed(directory) == actual
    assert not any(call.args[0].endswith("/dependencies") for call in api.await_args_list)
    assert api.await_args_list[-1].args[0].endswith("/abort")
    assert not api.receipts.pending
    assert not list(directory.parent.glob(".solo-install-*"))
    assert not dependencies._project_busy


@pytest.mark.parametrize("mode", ["source", "published"])
@pytest.mark.parametrize("failure", ["accept", "response", "lost-response"])
def test_real_uv_acceptance_failure_restores_environment_config_lock_and_new_assets(uv_projects, monkeypatch, registry, mode, failure):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    sentinel = directory / ".venv/bin/original-script"
    sentinel.write_text("original environment bytes\n")
    inode = (directory / ".venv").stat().st_ino
    before, actual = file_snapshot(directory), installed(directory)
    install = dependencies.install_project
    base = registry.api
    if mode == "published":
        candidate, records, contents = published_graph(root, candidate, monkeypatch)
        base = published_registry(monkeypatch, records, contents)
        install = dependencies.install_artifact
    called = []
    async def api(path, body=None, *, binary=False):
        if path.startswith("projects/") and path.endswith("/dependencies"):
            called.append(body)
            assert installed(directory)["source-package"]["version"] == "1.2.7"
            assert (directory / ".venv").stat().st_ino != inode
            if failure == "accept":
                raise web.HTTPError(422, reason="registry acceptance failed")
            await base(path, body, binary=binary)  # Acceptance committed, but its reply is malformed/lost.
            if failure == "lost-response":
                raise web.HTTPError(502, reason="acceptance reply lost")
            return {"artifacts": []}
        if path.endswith("/abort"):
            assert file_snapshot(directory, ignore_intent=True) == before
            assert json.loads((directory / dependencies._INSTALLATION_INTENT).read_text())["operation"] == "abort"
            assert (directory / ".venv").stat().st_ino == inode
        return await base(path, body, binary=binary)
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    with pytest.raises(web.HTTPError):
        asyncio.run(install(root, current, candidate))
    assert len(called) == 1
    assert file_snapshot(directory) == before
    assert installed(directory) == actual
    assert (directory / ".venv").stat().st_ino == inode
    assert not list(directory.parent.glob(".solo-install-*"))
    assert not dependencies._project_busy


def test_real_uv_registration_failure_removes_new_assets_before_any_uv_preview(uv_projects, monkeypatch, registry):
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
    assert not list(directory.parent.glob(".solo-install-*"))


def test_real_uv_runtime_graph_registers_each_actual_marker_after_all_builds_copies(uv_projects, monkeypatch, registry):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    child = root / "projects/factor/register-child"
    child_id = str(uuid4())
    write_offline_package(child, "register-child", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    (child / ".solo").write_text(json.dumps({"project_id": child_id, "name": "actual-child", "kind": "factor",
                                           "scheme_version": "1.2.7", "package_name": "register-child"}))
    path = source / "pyproject.toml"
    path.write_text(path.read_text().replace('["scheme>=1.2.0,<1.3.0"]', '["scheme>=1.2.0,<1.3.0", "register-child>=1.2"]'))
    set_sources(source, {"register-child": {"path": str(child)}})
    sync_fixture(root, current)
    base = registry.api
    registered = []
    async def api(path, body=None, *, binary=False):
        if path == "artifacts/register":
            assert len(list(directory.glob(".solo-wheels/*/*/*.whl"))) == 2
            assert candidate["id"] in dependencies._project_busy
            assert child_id in dependencies._project_busy
            registered.append(body)
        return await base(path, body, binary=binary)
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    asyncio.run(dependencies.install_project(root, current, candidate, dev=False))
    assert [body["project_id"] for body in registered] == [child_id, candidate["id"]]
    assert all(body["consumer_project_id"] == current["project_id"] for body in registered)
    nonce = registry.api.await_args_list[0].args[1]["install_id"]
    assert all(body["install_id"] == nonce for body in registered)
    assert registry.api.await_args_list[-1].args[0].endswith(f"/{nonce}/complete")
    assert not registry.receipts.pending
    assert {item["sourceProjectId"] for item in registry.snapshots.values()} == {child_id, candidate["id"]}
    assert not dependencies._project_busy


def test_real_uv_busy_recursive_source_rejected_before_source_copy_or_asset_registration(uv_projects, monkeypatch, registry):
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
    original_copy = dependencies._copy_source
    def copy(root_, source_, destination):
        assert source_ != child, "Busy recursive source was copied"
        return original_copy(root_, source_, destination)
    monkeypatch.setattr(dependencies, "_copy_source", copy)
    with dependencies.project_operation(child_id):
        with pytest.raises(web.HTTPError) as error:
            asyncio.run(dependencies.install_project(root, current, candidate))
        assert error.value.status_code == 409
    assert file_snapshot(directory) == before
    assert not any(call.args[0] == "artifacts/register" for call in registry.api.await_args_list)
    assert registry.api.await_args_list[0].args[0].endswith("/installations")
    assert registry.api.await_args_list[-1].args[0].endswith("/abort")
    assert not dependencies._project_busy


def test_real_uv_source_graph_reuses_copied_artifact_after_its_origin_is_deleted(uv_projects, monkeypatch, registry):
    root, current, candidate = uv_projects
    directory, source = root / current["path"], root / candidate["directory"]
    sync_fixture(root, current)
    deleted = root / "projects/factor/deleted"
    write_offline_package(deleted, "frozen-child", "1.2.7", ["scheme>=1.2.0,<1.3.0"])
    wheel = build_fixture_wheel(deleted, source / ".solo-wheels/frozen-child/original", monkeypatch)
    frozen = artifact_fixture(wheel, published=False)
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
    monkeypatch.setattr(versions, "parameters", lambda *args: {
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
    assert data["algos"]["factor"]["sha256"] == snapshot["sha256"]
    for record in records.values():
        wheel = run / "wheels" / record["filename"]
        assert wheel.read_bytes() == contents[record["id"]]
    lock = tomllib.loads((run / "environment/uv.lock").read_text())
    assert {item["name"] for item in lock["package"]} >= {"source-package", "published-child", "published-leaf"}
    assert all(not {"directory", "editable"} & item["source"].keys() for item in lock["package"])
    assert next(item for item in lock["package"] if item["name"] == "scheme")["version"] == "1.2.0"
    assert file_snapshot(directory) == original
    assert old_input.read_bytes() == old_bytes


@pytest.mark.parametrize("failure", ["nonce", "ttl", "expired", "artifacts"])
def test_artifact_install_begin_is_fail_closed_before_staging(project_files, monkeypatch, registry, failure):
    root, current, candidate = project_files
    before, base = file_snapshot(root), registry.api
    async def api(path, body=None, *, binary=False):
        response = await base(path, body, binary=binary)
        if path.endswith("/installations"):
            assert body == {"install_id": body["install_id"], "artifacts": [], "renew": False}
            response[{"nonce": "install_id", "ttl": "expires_at", "expired": "expires_at", "artifacts": "artifacts"}[failure]] = {
                "nonce": str(uuid4()), "ttl": "invalid", "expired": "2000-01-01T00:00:00Z", "artifacts": None,
            }[failure]
        return response
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    temporary = Mock(side_effect=AssertionError("Unsafe begin staged files"))
    monkeypatch.setattr(dependencies.tempfile, "mkdtemp", temporary)
    with pytest.raises(web.HTTPError):
        asyncio.run(dependencies.install_project(root, current, candidate))
    temporary.assert_not_called()
    assert file_snapshot(root) == before
    assert not registry.receipts.pending and not dependencies._installing


@pytest.mark.parametrize("mode", ["source", "published"])
def test_real_uv_artifact_completion_failure_keeps_committed_environment(uv_projects, monkeypatch, registry, mode):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    base, receipts, install = registry.api, registry.receipts, dependencies.install_project
    if mode == "published":
        candidate, records, contents = published_graph(root, candidate, monkeypatch)
        base = published_registry(monkeypatch, records, contents)
        receipts, install = base.receipts, dependencies.install_artifact
    calls = []
    async def api(path, body=None, *, binary=False):
        calls.append((path, body))
        if path.endswith("/complete"):
            assert installed(directory)["source-package"]["version"] == "1.2.7"
            assert not list(directory.parent.glob(".solo-install-*/original-venv"))
            raise web.HTTPError(502, reason="complete reply lost")
        return await base(path, body, binary=binary)
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    assert asyncio.run(install(root, current, candidate)) == "source-package"
    nonce = calls[0][1]["install_id"]
    assert receipts.pending[nonce]["accepted"] == receipts.accepted[current["project_id"]]
    assert installed(directory)["source-package"]["version"] == "1.2.7"
    assert not any(path.endswith("/abort") for path, _ in calls)
    assert not list(directory.parent.glob(".solo-install-*"))
    marker = directory / dependencies._INSTALLATION_INTENT
    intent = json.loads(marker.read_text())
    assert intent["install_id"] == nonce and intent["operation"] == "complete"
    monkeypatch.setattr(dependencies, "_artifact_api", base)
    with dependencies.project_operation(current["project_id"]):
        asyncio.run(dependencies.recover_installation(root, current))
    assert not marker.exists() and not receipts.pending
    assert installed(directory)["source-package"]["version"] == "1.2.7"


def test_real_uv_artifact_failed_filesystem_rollback_retains_receipt_and_backup(uv_projects, monkeypatch, registry):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    config_before = (directory / "pyproject.toml").read_bytes()
    base, rename = registry.api, Path.rename
    def failed_restore(path, target):
        if path.name == "original-venv":
            raise OSError("filesystem rollback failed")
        return rename(path, target)
    monkeypatch.setattr(Path, "rename", failed_restore)
    async def api(path, body=None, *, binary=False):
        result = await base(path, body, binary=binary)
        if path.endswith("/dependencies"):
            raise web.HTTPError(502, reason="accepted reply lost")
        return result
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    with pytest.raises(OSError, match="filesystem rollback failed"):
        asyncio.run(dependencies.install_project(root, current, candidate))
    assert (directory / "pyproject.toml").read_bytes() == config_before
    assert list(directory.parent.glob(".solo-install-*/original-venv"))
    assert registry.receipts.pending and registry.receipts.accepted[current["project_id"]]
    assert not any(call.args[0].endswith(("/abort", "/complete")) for call in registry.api.await_args_list)
    assert not dependencies._installing and not dependencies._project_busy


@pytest.mark.parametrize("step", ["build", "preview", "live"])
def test_real_uv_artifact_renewal_failure_cancels_long_step_before_rollback(uv_projects, monkeypatch, registry, step):
    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    before = file_snapshot(directory)
    base, real_run, subprocess_exec = registry.api, dependencies._run, dependencies.asyncio.create_subprocess_exec
    blocked, stopped = False, False
    processes = []
    monkeypatch.setattr(dependencies, "_INSTALL_RENEW_SECONDS", 0.01)
    async def create(*args, **kwargs):
        nonlocal blocked
        process = await subprocess_exec(*args, **kwargs)
        if "import time; time.sleep(60)" in args:
            processes.append(process)
            blocked = True  # Trigger renewal failure only after the child actually exists.
        return process
    monkeypatch.setattr(dependencies.asyncio, "create_subprocess_exec", create)
    async def run(arguments, cwd, environment):
        nonlocal blocked, stopped
        target = ((step == "build" and arguments[:2] == ["uv", "build"])
                  or (step == "preview" and arguments[:2] == ["uv", "sync"] and cwd != directory)
                  or (step == "live" and arguments[:2] == ["uv", "sync"] and cwd == directory))
        if target:
            if step != "build":
                await real_run(arguments, cwd, environment)
            try:
                return await real_run([sys.executable, "-c", "import time; time.sleep(60)"], cwd, environment)
            finally:
                stopped = True
        return await real_run(arguments, cwd, environment)
    monkeypatch.setattr(dependencies, "_run", run)
    async def api(path, body=None, *, binary=False):
        if path.endswith("/installations") and body.get("renew") and body["artifacts"] == [] and blocked:
            raise web.HTTPError(409, reason="renewal failed closed")
        if path.endswith("/abort"):
            assert stopped
            assert processes and all(process.returncode is not None for process in processes)
            assert file_snapshot(directory, ignore_intent=True) == before
            assert json.loads((directory / dependencies._INSTALLATION_INTENT).read_text())["operation"] == "abort"
        return await base(path, body, binary=binary)
    monkeypatch.setattr(dependencies, "_artifact_api", api)
    async def install():
        await asyncio.wait_for(dependencies.install_project(root, current, candidate), timeout=10)
    with pytest.raises(web.HTTPError, match="renewal failed closed"):
        asyncio.run(install())
    assert file_snapshot(directory) == before
    assert not registry.receipts.pending
    assert not any(call.args[0].endswith("/dependencies") for call in registry.api.await_args_list)
    assert not list(directory.parent.glob(".solo-install-*"))


@pytest.mark.parametrize("marked", [True, False])
@pytest.mark.parametrize("failure", [None, "lost-accept", "lost-complete", "lost-accept-abort-failed"])
def test_real_uv_artifact_receipt_integrates_actual_backend_exact_lock(uv_projects, monkeypatch, marked, failure):
    """Real uv plus real backend begin/protect/exact acceptance/compensation, no predicate mock."""
    backend = next((parent / "backend" for parent in Path(__file__).resolve().parents
                    if (parent / "backend").is_dir()), Path("/backend"))
    if not backend.is_dir():
        pytest.skip("Requires a read-only backend source mount for endpoint integration")
    monkeypatch.syspath_prepend(str(backend))
    from config import SoloSettings
    from core.apps.artifacts import service, views
    from core.apps.artifacts.models import ArtifactInstallation, ResearchArtifact
    from core.apps.projects.models import Project
    from core.database.base import Base
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import Session
    from datetime import datetime, timedelta, timezone
    from urllib.parse import parse_qs
    from fastapi import HTTPException

    root, current, candidate = uv_projects
    directory = root / current["path"]
    sync_fixture(root, current)
    marker = directory / ".solo"
    marker.write_text(json.dumps({**json.loads(marker.read_text()), "name": "current"}))
    before = file_snapshot(directory)
    snapshot, records, contents = published_graph(root, candidate, monkeypatch, marked=marked)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", root)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    calls, protections = [], []
    recovery = False
    with Session(engine, expire_on_commit=False) as session:
        consumer = Project(id=UUID(current["project_id"]), name="current", kind="model", package_name="current-package",
            template_tag="v1.2.0", template_commit="a" * 40, template_package_version="1.2.0",
            scheme_version="v1.2.0", scheme_commit="b" * 40)
        session.add(consumer)
        for record in records.values():
            artifact = ResearchArtifact(id=UUID(record["id"]), kind=record["kind"], package_name=record["package"],
                version=record["version"], sha256=record["sha256"], filename=record["filename"],
                size_bytes=len(contents[record["id"]]), entry=record["entry"] if record["kind"] != "dependency" else "",
                scheme_version="1.2.7", sources={"scheme": {"version": "1.2.7"}, "template": {"version": "1.2.0"}},
                dependencies=record["dependencies"],
                published_at=datetime.now(timezone.utc) if record["publishedAt"] else None)
            target = service.artifact_path(artifact)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents[record["id"]])
            session.add(artifact)
        session.commit()
        snapshot = service.read_artifact(session.get(ResearchArtifact, UUID(snapshot["id"])))
        async def api(path, body=None, *, binary=False):
            calls.append((path, body))
            try:
                if path.endswith("/installations"):
                    result = views.begin_installation(consumer.id, views.InstallDependencies(**body), session)
                    protections.append({item["id"] for item in result["artifacts"]})
                    return result
                if path.endswith("/dependencies"):
                    result = views.accept_dependencies(consumer.id, views.AcceptDependencies(**body), session)
                    if failure in {"lost-accept", "lost-accept-abort-failed"} and not recovery:
                        raise web.HTTPError(502, reason="accept reply lost")
                    return result
                if path.endswith("/complete"):
                    if failure == "lost-complete" and not recovery:
                        raise web.HTTPError(502, reason="complete reply lost")
                    return views.complete_installation(consumer.id, UUID(path.split("/")[-2]), session)
                if path.endswith("/abort"):
                    assert file_snapshot(directory, ignore_intent=True) == before
                    assert json.loads((directory / dependencies._INSTALLATION_INTENT).read_text())["operation"] == "abort"
                    if failure == "lost-accept-abort-failed" and not recovery:
                        raise web.HTTPError(502, reason="abort offline")
                    return views.abort_installation(consumer.id, UUID(path.split("/")[-2]), session)
                if path.startswith("artifacts?"):
                    query = parse_qs(path.split("?", 1)[1])
                    return [service.read_artifact(item) for item in session.scalars(select(ResearchArtifact))
                            if item.sha256 == query["sha256"][0]]
                if path.startswith("artifacts/"):
                    identifier = UUID(path.split("/")[1])
                    if binary:
                        assert protections[-1] == set(records)
                        return Path(views.download_wheel(identifier, session).path).read_bytes()
                    return views.get_artifact(identifier, session)
                raise AssertionError(path)
            except HTTPException as error:
                raise web.HTTPError(error.status_code, reason=str(error.detail)) from error
        monkeypatch.setattr(dependencies, "_artifact_api", api)
        if failure in {"lost-accept", "lost-accept-abort-failed"}:
            with pytest.raises(web.HTTPError, match="accept reply lost"):
                asyncio.run(dependencies.install_artifact(root, current, snapshot))
            assert file_snapshot(directory, ignore_intent=True) == before
            if failure == "lost-accept":
                assert consumer.artifact_dependencies == {}
            else:
                assert consumer.artifact_dependencies
        else:
            assert asyncio.run(dependencies.install_artifact(root, current, snapshot)) == "source-package"
            expected = {snapshot["id"]} if marked else set(records)
            assert {item["id"] for item in consumer.artifact_dependencies.values()} == expected
            assert set(next(body["artifacts"] for path, body in calls if path.endswith("/dependencies"))) == expected
        receipts = list(session.scalars(select(ArtifactInstallation)))
        unresolved = failure in {"lost-complete", "lost-accept-abort-failed"}
        assert len(receipts) == int(unresolved)
        if unresolved:
            intent_path = directory / dependencies._INSTALLATION_INTENT
            intent = json.loads(intent_path.read_text())
            nonce = calls[0][1]["install_id"]
            assert intent["install_id"] == nonce
            assert intent["operation"] == ("complete" if failure == "lost-complete" else "abort")
            files_before_replay = file_snapshot(directory, ignore_intent=True)
            receipts[0].expires_at = datetime.now(timezone.utc) - timedelta(days=1)
            session.commit()
            service.gc_unreferenced(session)
            session.commit()
            assert session.get(ArtifactInstallation, UUID(nonce)) is not None
            assert all(session.get(ResearchArtifact, UUID(identifier)) is not None for identifier in records)
            recovery = True
            with dependencies.project_operation(current["project_id"]):
                asyncio.run(dependencies.recover_installation(root, current))
            assert calls[-1][0].endswith(f'/{nonce}/{intent["operation"]}')
            assert not intent_path.exists() and session.get(ArtifactInstallation, UUID(nonce)) is None
            assert file_snapshot(directory) == files_before_replay
            if failure == "lost-accept-abort-failed":
                assert consumer.artifact_dependencies == {} and file_snapshot(directory) == before
        assert calls[0][1]["renew"] is False
        assert all(body["renew"] is True for path, body in calls[1:] if path.endswith("/installations"))
    engine.dispose()
