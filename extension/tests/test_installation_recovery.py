"""Focused durable-finalization and explicit owner-cancellation regressions; no Notebook/API writes."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
import tomlkit
from solo_jupyter import dependencies as d, versions
from tornado import web


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    current = {"project_id": str(uuid4()), "path": "projects/model/current", "name": "current",
               "kind": "model", "scheme_version": "1.2.0"}
    directory = root / current["path"]
    directory.mkdir(parents=True)
    (directory / ".solo").write_text(json.dumps(current), encoding="utf-8")
    raw = tmp_path / "wheel-bytes"
    raw.mkdir()

    def wheel(version):
        path = raw / f"factor_bbbb-{version}-py3-none-any.whl"
        with ZipFile(path, "w") as archive:
            archive.writestr(f"factor_bbbb-{version}.dist-info/METADATA",
                f"Metadata-Version: 2.1\nName: factor-bbbb\nVersion: {version}\nRequires-Dist: scheme>=1.2.0,<1.3.0\n")
        return path, {"id": str(uuid4()), "package": "factor-bbbb", "version": version,
                      "filename": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "kind": "factor", "schemeVersion": "1.2.0", "publishedAt": "2026-01-01",
                      "dependencies": {}}

    old_file, old = wheel("1.2.0")
    new_file, new = wheel("1.2.1")
    old_target = d._store_wheel(root, directory, old_file, old["package"], [])
    config = {"project": {"name": "model-aaaa", "version": "1.2.0", "dependencies": ["scheme>=1.2.0,<1.3.0"]},
              "dependency-groups": {"dev": ["factor-bbbb"]},
              "tool": {"uv": {"sources": {"factor-bbbb": {"path": old_target.as_posix()}}}}}
    lock = {"version": 1, "package": [
        {"name": "scheme", "version": "1.2.0", "source": {"registry": "https://example.invalid/simple"}},
        {"name": old["package"], "version": old["version"], "source": {"path": old_target.as_posix()}}]}
    (directory / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    (directory / "uv.lock").write_text(tomlkit.dumps(lock), encoding="utf-8")
    python = d._python(directory)
    python.parent.mkdir(parents=True)
    python.write_bytes(b"original environment")
    before = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock")}
    accepted = {old["package"]: old}
    receipts, calls = {}, []
    records = {item["id"]: item for item in (old, new)}
    contents = {old["id"]: old_file.read_bytes(), new["id"]: new_file.read_bytes()}

    async def api(path, body=None, *, binary=False):
        calls.append((path, body))
        if path.endswith("/installations"):
            nonce = body["install_id"]
            assert not any(key != nonce and value["accepted"] is not None for key, value in receipts.items())
            receipt = receipts.setdefault(nonce, {"before": dict(accepted), "pending": {}, "accepted": None})
            for identifier in body["artifacts"]:
                receipt["pending"][identifier] = records[identifier]
            return {"install_id": nonce, "artifacts": list(receipt["pending"].values()), "expires_at": "2099-01-01T00:00:00Z"}
        if path.endswith("/dependencies"):
            nonce = body["install_id"]
            assert set(body["artifacts"]) <= set(receipts[nonce]["pending"])
            accepted.clear()
            accepted.update({records[identifier]["package"]: records[identifier] for identifier in body["artifacts"]})
            receipts[nonce]["accepted"] = dict(accepted)
            return {"artifacts": list(accepted.values())}
        if path.endswith(("/abort", "/complete")):
            nonce, operation = path.split("/")[-2:]
            receipt = receipts.get(nonce)
            if receipt is not None:
                if operation == "abort":
                    assert python.read_bytes() == b"original environment"
                    assert all((directory / name).read_bytes() == value for name, value in before.items())
                    accepted.clear()
                    accepted.update(receipt["before"])
                else:
                    assert receipt["accepted"] == accepted
                del receipts[nonce]
            return {"install_id": nonce, "aborted" if operation == "abort" else "completed": True}
        if path.startswith("artifacts/"):
            identifier = path.split("/")[1]
            return contents[identifier] if binary else records[identifier]
        raise AssertionError(f"Unexpected mocked API request: {path}")

    async def run(arguments, cwd, environment):
        target = directory / ".solo-wheels" / new["package"] / new["sha256"] / new["filename"]
        if arguments[:2] == ["uv", "add"]:
            config = tomlkit.parse((cwd / "pyproject.toml").read_text(encoding="utf-8"))
            config["tool"]["uv"]["sources"][new["package"]] = {"path": target.as_posix()}
            (cwd / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
        if arguments[:2] == ["uv", "lock"]:
            staged = {"version": 1, "package": [lock["package"][0],
                {"name": new["package"], "version": new["version"], "source": {"path": target.as_posix()}}]}
            (cwd / "uv.lock").write_text(tomlkit.dumps(staged), encoding="utf-8")
        if arguments[:2] == ["uv", "sync"]:
            interpreter = d._python(cwd)
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.write_bytes(b"new environment")
        return ""

    metadata = {"python": "fixture-python", "markers": {"python_version": "3.12", "python_full_version": "3.12.12"},
                "packages": [{"name": "scheme", "version": "1.2.0", "requires": []}]}
    monkeypatch.setattr(d, "_artifact_api", api)
    monkeypatch.setattr(d, "_run", run)
    monkeypatch.setattr(d, "_metadata", AsyncMock(return_value=metadata))
    monkeypatch.setattr(d, "_verify_environment", AsyncMock())
    monkeypatch.setattr(d, "check_project_release", AsyncMock())
    monkeypatch.setattr(d, "_recovery_blocked", set())
    assert not d._project_busy and not d._installing
    yield SimpleNamespace(root=root, current=current, directory=directory, python=python, old=old, new=new,
                          before=before, accepted=accepted, receipts=receipts, calls=calls, api=api, run=run,
                          records=records, contents=contents)
    assert not d._project_busy and not d._installing


def marker(f):
    return f.directory / d._INSTALLATION_INTENT


def assert_old(f):
    assert f.python.read_bytes() == b"original environment"
    assert all((f.directory / name).read_bytes() == content for name, content in f.before.items())


@pytest.mark.parametrize("committed_reply", [False, True])
def test_failed_complete_replays_original_nonce_without_rollback(installation, monkeypatch, committed_reply):
    f = installation
    async def fail(path, body=None, **kwargs):
        if path.endswith("/complete"):
            assert json.loads(marker(f).read_text())["operation"] == "complete"
            if committed_reply:
                await f.api(path, body, **kwargs)
            raise web.HTTPError(502, reason="complete reply lost")
        return await f.api(path, body, **kwargs)
    monkeypatch.setattr(d, "_artifact_api", fail)
    assert asyncio.run(d.install_artifact(f.root, f.current, f.new)) == f.new["package"]
    intent = json.loads(marker(f).read_text())
    assert f.python.read_bytes() == b"new environment"
    assert f.accepted == {f.new["package"]: f.new}
    assert not any(path.endswith("/abort") for path, _ in f.calls)
    assert not list(f.directory.parent.glob(".solo-install-*"))
    files = {name: (f.directory / name).read_bytes() for name in f.before}
    monkeypatch.setattr(d, "_artifact_api", f.api)
    with d.project_operation(f.current["project_id"]):
        asyncio.run(d.recover_installation(f.root, f.current))
    assert f.calls[-1][0].endswith(f'/{intent["install_id"]}/complete')
    assert not marker(f).exists() and not f.receipts
    assert all((f.directory / name).read_bytes() == content for name, content in files.items())


@pytest.mark.parametrize("abort_committed", [False, True])
def test_lost_accept_failed_abort_keeps_old_fs_and_durable_exact_nonce(installation, monkeypatch, abort_committed):
    f = installation
    async def fail(path, body=None, **kwargs):
        if path.endswith("/abort"):
            assert_old(f)
            assert json.loads(marker(f).read_text())["operation"] == "abort"
            if abort_committed:
                await f.api(path, body, **kwargs)
            raise web.HTTPError(502, reason="abort reply lost")
        result = await f.api(path, body, **kwargs)
        if path.endswith("/dependencies"):
            raise web.HTTPError(502, reason="accept committed; reply lost")
        return result
    monkeypatch.setattr(d, "_artifact_api", fail)
    with pytest.raises(web.HTTPError, match="accept committed; reply lost"):
        asyncio.run(d.install_artifact(f.root, f.current, f.new))
    assert_old(f)
    intent = json.loads(marker(f).read_text())
    assert intent["install_id"] == f.calls[0][1]["install_id"] and intent["operation"] == "abort"
    assert not list(f.directory.parent.glob(".solo-install-*"))
    if not abort_committed:
        assert f.accepted == {f.new["package"]: f.new}
        assert f.receipts[intent["install_id"]]["before"] == {f.old["package"]: f.old}
    monkeypatch.setattr(d, "_artifact_api", f.api)
    with d.project_operation(f.current["project_id"]):
        asyncio.run(d.recover_installation(f.root, f.current))
    assert f.calls[-1][0].endswith(f'/{intent["install_id"]}/abort')
    assert f.accepted == {f.old["package"]: f.old} and not f.receipts and not marker(f).exists()
    assert_old(f)


@pytest.mark.parametrize("invalid", ["json", "duplicate", "format", "project", "install", "operation", "hash", "stale", "missing", "extra"])
def test_invalid_marker_fails_closed_without_rpc_or_source_execution(installation, monkeypatch, invalid):
    f = installation
    d._write_installation_intent(f.root, f.current, str(uuid4()), "abort")
    intent = json.loads(marker(f).read_text())
    if invalid == "json":
        marker(f).write_text("not-json")
    elif invalid == "duplicate":
        marker(f).write_text('{"format":1,' + marker(f).read_text()[1:])
    elif invalid == "stale":
        (f.directory / "uv.lock").write_bytes(b"changed after final state")
    elif invalid == "missing":
        (f.directory / "pyproject.toml").unlink()
    else:
        if invalid == "format": intent["format"] = True
        if invalid == "project": intent["project_id"] = str(uuid4())
        if invalid == "install": intent["install_id"] = "not-a-uuid"
        if invalid == "operation": intent["operation"] = "accept"
        if invalid == "hash": intent["hashes"]["uv.lock"] = "0" * 64
        if invalid == "extra": intent["path"] = "../other"
        marker(f).write_text(json.dumps(intent))
    before = marker(f).read_bytes()
    api = AsyncMock(side_effect=AssertionError("Invalid intent reached Backend"))
    monkeypatch.setattr(d, "_artifact_api", api)
    with pytest.raises(web.HTTPError) as error:
        asyncio.run(d.recover_installation(f.root, f.current))
    assert error.value.status_code == 409
    api.assert_not_awaited()
    assert marker(f).read_bytes() == before


@pytest.mark.parametrize("target", ["marker", "solo", "lock", "directory"])
def test_recovery_rejects_symlink_paths(installation, monkeypatch, target):
    f = installation
    d._write_installation_intent(f.root, f.current, str(uuid4()), "abort")
    path = {"marker": marker(f), "solo": f.directory / ".solo", "lock": f.directory / "uv.lock", "directory": f.directory}[target]
    moved = path.with_name(path.name + "-original")
    path.rename(moved)
    try:
        path.symlink_to(moved, target_is_directory=target == "directory")
    except OSError:
        moved.rename(path)
        pytest.skip("Creating symlinks requires permission")
    api = AsyncMock(side_effect=AssertionError("Linked intent reached Backend"))
    monkeypatch.setattr(d, "_artifact_api", api)
    with pytest.raises(web.HTTPError):
        asyncio.run(d.recover_installation(f.root, f.current))
    api.assert_not_awaited()


def test_marker_project_ownership_and_hardlinks_are_guarded(installation, monkeypatch):
    f = installation
    d._write_installation_intent(f.root, f.current, str(uuid4()), "abort")
    os.link(marker(f), f.directory / "linked-marker")
    api = AsyncMock(side_effect=AssertionError("Unowned intent reached Backend"))
    monkeypatch.setattr(d, "_artifact_api", api)
    with pytest.raises(web.HTTPError):
        asyncio.run(d.recover_installation(f.root, f.current))
    (f.directory / "linked-marker").unlink()
    owner = {**f.current, "project_id": str(uuid4())}
    (f.directory / ".solo").write_text(json.dumps(owner))
    with pytest.raises(web.HTTPError):
        asyncio.run(d.recover_installation(f.root, f.current))
    api.assert_not_awaited()


@pytest.mark.parametrize("response", [None, {}, {"aborted": 1}, {"completed": True}, {"aborted": False}, {"aborted": True, "completed": True}])
def test_finish_reply_requires_matching_nonce_and_exact_operation_flag(installation, monkeypatch, response):
    f = installation
    nonce = str(uuid4())
    d._write_installation_intent(f.root, f.current, nonce, "abort")
    before = marker(f).read_bytes()
    value = {"install_id": nonce, **response} if isinstance(response, dict) else response
    monkeypatch.setattr(d, "_artifact_api", AsyncMock(return_value=value))
    with pytest.raises(web.HTTPError):
        asyncio.run(d.recover_installation(f.root, f.current))
    assert marker(f).read_bytes() == before


def test_matching_success_cannot_remove_marker_if_hashes_change_during_rpc(installation, monkeypatch):
    f = installation
    nonce = str(uuid4())
    d._write_installation_intent(f.root, f.current, nonce, "abort")
    async def changed(*args):
        (f.directory / "uv.lock").write_bytes(b"concurrent user edit")
        return {"install_id": nonce, "aborted": True}
    monkeypatch.setattr(d, "_artifact_api", changed)
    with pytest.raises(web.HTTPError):
        asyncio.run(d.recover_installation(f.root, f.current))
    assert marker(f).exists()


def test_new_install_replays_before_admission_or_new_nonce(installation, monkeypatch):
    f = installation
    nonce = str(uuid4())
    d._write_installation_intent(f.root, f.current, nonce, "abort")
    async def admit(*args):
        assert not marker(f).exists()
        assert f.calls[-1][0].endswith(f"/{nonce}/abort")
        raise web.HTTPError(422, reason="now retired")
    monkeypatch.setattr(d, "check_project_release", admit)
    with pytest.raises(web.HTTPError, match="now retired"):
        asyncio.run(d.install_artifact(f.root, f.current, f.new))
    assert not any(path.endswith("/installations") for path, _ in f.calls)


@pytest.mark.parametrize("action", ["install", "parameters", "save", "background", "read"])
def test_unsettled_marker_blocks_source_paths_before_admission(installation, monkeypatch, action):
    f = installation
    d._write_installation_intent(f.root, f.current, str(uuid4()), "abort")
    api = AsyncMock(side_effect=web.HTTPError(502, reason="offline"))
    monkeypatch.setattr(d, "_artifact_api", api)
    admission = AsyncMock(side_effect=AssertionError("Recovery must precede release admission"))
    monkeypatch.setattr(d, "check_project_release", admission)
    monkeypatch.setattr(versions, "check_project_release", admission)
    execute = Mock(side_effect=AssertionError("Unsettled installation executed source"))
    monkeypatch.setattr(versions, "parameters", execute)
    monkeypatch.setattr(versions, "build_version", execute)
    request = SimpleNamespace(current_user=object(), settings={"server_root_dir": str(f.root)},
        current_project=lambda _: f.current, get_query_argument=lambda key, default="": "1" if key == "parameters" else f.current["path"],
        get_json_body=lambda: {"path": f.current["path"], "parameters": {}}, finish=Mock())
    with pytest.raises(web.HTTPError):
        if action == "install": asyncio.run(d.install_artifact(f.root, f.current, f.new))
        if action == "parameters": asyncio.run(versions.VersionsHandler.get(request))
        if action == "save": asyncio.run(versions.VersionsHandler.post(request))
        if action == "background": asyncio.run(versions.save(f.directory, f.current, {"id": str(uuid4())}, {}))
        if action == "read": asyncio.run(d.RecoveringProjectHandler.get(request))
    admission.assert_not_awaited()
    execute.assert_not_called()
    assert marker(f).exists()


@pytest.mark.parametrize("kind", d.PROJECT_KINDS)
def test_background_recovery_scans_registered_owned_roots_and_skips_busy(installation, monkeypatch, kind):
    f = installation
    destination = f.root / "projects" / kind / "current"
    if destination != f.directory:
        destination.parent.mkdir(parents=True, exist_ok=True)
        f.directory.rename(destination)
        f.directory = destination
        f.current.update(path=destination.relative_to(f.root).as_posix(), kind=kind)
        (destination / ".solo").write_text(json.dumps(f.current))
    nonce = str(uuid4())
    d._write_installation_intent(f.root, f.current, nonce, "abort")
    record = {"id": f.current["project_id"], "kind": kind, "name": "current", "directory": f.current["path"]}
    monkeypatch.setattr(d, "project_catalog", AsyncMock(return_value=[{**record, "id": str(uuid4())}, record]))
    with d.project_operation(f.current["project_id"]):
        asyncio.run(d.recover_installations(f.root))
    assert marker(f).exists() and not f.calls
    asyncio.run(d.recover_installations(f.root))
    assert not marker(f).exists() and f.calls[-1][0].endswith(f"/{nonce}/abort")


def test_background_recovery_cannot_replay_unregistered_preserved_project(installation, monkeypatch):
    f = installation
    d._write_installation_intent(f.root, f.current, str(uuid4()), "abort")
    monkeypatch.setattr(d, "project_catalog", AsyncMock(return_value=[]))
    asyncio.run(d.recover_installations(f.root))
    assert marker(f).exists() and not f.calls
    monkeypatch.setattr(d, "project_catalog", AsyncMock(side_effect=web.HTTPError(502, reason="offline")))
    asyncio.run(d.recover_installations(f.root))
    assert marker(f).exists() and not f.calls


def test_source_copies_omit_intent_and_atomic_marker_temporary_files(installation, tmp_path):
    f = installation
    d._write_installation_intent(f.root, f.current, str(uuid4()), "abort")
    (f.directory / (d._INSTALLATION_INTENT + ".test.tmp")).write_text("incomplete intent")
    for copy, target in ((lambda target: d._copy_source(f.root, f.directory, target), tmp_path / "dependency-source"),
                         (lambda target: versions.copy_source(f.directory, target), tmp_path / "version-source")):
        copy(target)
        assert not list(target.glob(".solo-installation.json*"))
        assert (target / ".solo").exists()


def test_marker_write_failure_preserves_accepted_new_fs_unique_backup_and_protection(installation, monkeypatch):
    f = installation
    replace = d.os.replace
    def fail(source, destination):
        if Path(destination).name == d._INSTALLATION_INTENT:
            raise OSError("disk refused intent")
        return replace(source, destination)
    monkeypatch.setattr(d.os, "replace", fail)
    with pytest.raises(web.HTTPError, match="无法持久记录"):
        asyncio.run(d.install_artifact(f.root, f.current, f.new))
    assert f.python.read_bytes() == b"new environment" and f.receipts
    backups = list(f.directory.parent.glob(".solo-install-*/original-venv"))
    assert len(backups) == 1
    assert (backups[0] / f.python.relative_to(f.directory / ".venv")).read_bytes() == b"original environment"
    assert not any(path.endswith(("/complete", "/abort")) for path, _ in f.calls)
    with pytest.raises(web.HTTPError):
        asyncio.run(d.recover_installation(f.root, f.current))


def test_stop_is_idempotent_and_owner_cancel_does_not_cancel_shared_renewal(installation, monkeypatch):
    f = installation
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def api(path, body=None, **kwargs):
            if path.endswith("/installations") and body["renew"]:
                entered.set()
                await release.wait()
            return await f.api(path, body, **kwargs)
        monkeypatch.setattr(d, "_artifact_api", api)
        monkeypatch.setattr(d, "_INSTALL_RENEW_SECONDS", 0.001)
        transaction = d._ArtifactInstallation(f.current)
        await transaction.protect()
        transaction.start()
        await asyncio.wait_for(entered.wait(), 2)
        owner = asyncio.create_task(transaction.stop())
        await asyncio.sleep(0)
        owner.cancel("explicit owner cancellation")
        await asyncio.sleep(0)
        assert not owner.done() and not transaction._heartbeat.cancelled()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="explicit owner cancellation"):
            await owner
        await transaction.stop()
        await transaction.stop()
        assert transaction._heartbeat.done() and not transaction._heartbeat.cancelled()
    asyncio.run(scenario())


def test_single_owner_cancel_during_inflight_renewal_joins_then_rolls_back_and_aborts(installation, monkeypatch):
    f = installation
    async def scenario():
        renew_entered, release, stop_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        heartbeat = []
        original_stop = d._ArtifactInstallation.stop
        async def api(path, body=None, **kwargs):
            if path.endswith("/installations") and body["renew"] and not body["artifacts"]:
                renew_entered.set()
                await release.wait()
            return await f.api(path, body, **kwargs)
        async def run(arguments, cwd, environment):
            result = await f.run(arguments, cwd, environment)
            if arguments[:2] == ["uv", "sync"] and cwd == f.directory:
                await renew_entered.wait()
            return result
        async def stop(transaction):
            heartbeat.append(transaction._heartbeat)
            stop_entered.set()
            await original_stop(transaction)
        monkeypatch.setattr(d, "_artifact_api", api)
        monkeypatch.setattr(d, "_run", run)
        monkeypatch.setattr(d, "_INSTALL_RENEW_SECONDS", 0.001)
        monkeypatch.setattr(d._ArtifactInstallation, "stop", stop)
        owner = asyncio.create_task(d.install_artifact(f.root, f.current, f.new))
        await asyncio.wait_for(stop_entered.wait(), 2)
        owner.cancel("single explicit cancellation")
        await asyncio.sleep(0)
        assert not owner.done() and not heartbeat[0].cancelled()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="single explicit cancellation"):
            await asyncio.wait_for(owner, 3)
        assert_old(f)
        assert f.calls[-1][0].endswith("/abort")
        assert not any(path.endswith("/dependencies") for path, _ in f.calls)
        assert not f.receipts and not marker(f).exists()
        assert all(task.done() and not task.cancelled() for task in heartbeat)
        assert not list(f.directory.parent.glob(".solo-install-*"))
    asyncio.run(scenario())


def test_cancelled_shared_heartbeat_does_not_poison_later_stop(installation, monkeypatch):
    f = installation
    async def scenario():
        transaction = d._ArtifactInstallation(f.current)
        await transaction.protect()
        transaction.start()
        transaction._heartbeat.cancel()
        await asyncio.gather(transaction._heartbeat, return_exceptions=True)
        await transaction.stop()
        await transaction.stop()
        assert transaction.failure is not None and transaction._heartbeat.done()
    asyncio.run(scenario())


def test_published_root_uses_sealed_flat_closure_not_descendant_dev_graph(installation, monkeypatch, tmp_path):
    f = installation
    unused = {**f.old, "id": str(uuid4()), "package": "unused-retired-dev", "retired": True}
    child = {**f.old, "publishedAt": None, "dependencies": {"unused-retired-dev": unused}}
    f.records[child["id"]] = child
    f.new["dependencies"] = {child["package"]: child}
    # Different versions of the same package remain a conflict; choose distinct child package bytes.
    path = tmp_path / "leaf-0.1.0-py3-none-any.whl"
    with ZipFile(path, "w") as archive:
        archive.writestr("leaf-0.1.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: leaf\nVersion: 0.1.0\n")
    child.update(package="leaf", version="0.1.0", filename=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest(), kind="dependency")
    f.contents[child["id"]] = path.read_bytes()
    f.new["dependencies"] = {"leaf": child}
    async def scenario():
        transaction = d._ArtifactInstallation(f.current)
        await transaction.protect()
        staged, snapshots = await d._stage_artifact_graph(f.root, f.directory, [f.new], tmp_path,
                                                        __import__("packaging.version", fromlist=["Version"]).Version("1.2.0"), [], transaction)
        assert set(staged) == set(snapshots) == {f.new["package"], "leaf"}
        assert unused["id"] not in transaction.protected
        assert not any(unused["id"] in path for path, _ in f.calls)
    asyncio.run(scenario())
