import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("solo_patch_mcp_uuid", Path(__file__).resolve().parents[1] / "scripts/patch_mcp_uuid.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


@pytest.fixture
def toolkit(tmp_path, monkeypatch):
    root = tmp_path / "toolkit"
    static = root / "static"
    static.mkdir(parents=True)
    package = {"name": "jupyterlab-commands-toolkit", "version": "0.2.0",
               "jupyterlab": {"_build": {"load": "static/" + patch.REMOTE_NAME, "extension": "./extension"}}}
    (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
    chunk = "var a=o(262),s=o(902);" + patch.OLD_UUID
    remote = patch.LUMINO_MODULE + '509:"d3237d7e45401cb7bbcc";v="d3237d7e45401cb7bbcc"'
    (static / patch.CHUNK_NAME).write_text(chunk, encoding="utf-8")
    (static / patch.REMOTE_NAME).write_text(remote, encoding="utf-8")
    monkeypatch.setattr(patch, "CHUNK_SHA256", patch.hashlib.sha256(chunk.encode()).hexdigest())
    monkeypatch.setattr(patch, "REMOTE_SHA256", patch.hashlib.sha256(remote.encode()).hexdigest())
    return root


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_guarded_patch_uses_imported_lumino_uuid_and_versions_every_cache_reference(toolkit):
    before = snapshot(toolkit)
    result = patch.patch_toolkit(toolkit)
    assert result["uuid_generator"] == "@lumino/coreutils.UUID.uuid4"
    chunk = toolkit / "static" / result["chunk"]
    remote = toolkit / "static" / result["remote"]
    assert patch.NEW_UUID in chunk.read_text()
    assert "crypto.randomUUID" not in chunk.read_text()
    assert chunk.name.split(".")[1] == patch.hashlib.sha256(chunk.read_bytes()).hexdigest()[:20]
    assert remote.name.split(".")[1] == patch.hashlib.sha256(remote.read_bytes()).hexdigest()[:20]
    assert remote.read_text().count(chunk.name.split(".")[1]) == 2
    assert "d3237d7e45401cb7bbcc" not in remote.read_text()
    assert json.loads((toolkit / "package.json").read_text())["jupyterlab"]["_build"]["load"] == "static/" + remote.name
    for name in ("static/" + patch.CHUNK_NAME, "static/" + patch.REMOTE_NAME):
        assert (toolkit / name).read_bytes() == before[name]


def test_patch_is_idempotent_and_does_not_change_original_assets(toolkit):
    first = patch.patch_toolkit(toolkit)
    before = snapshot(toolkit)
    assert patch.patch_toolkit(toolkit) == first
    assert snapshot(toolkit) == before


@pytest.mark.parametrize("asset", ["chunk", "remote"])
def test_upstream_asset_changes_fail_before_any_write(toolkit, asset):
    name = patch.CHUNK_NAME if asset == "chunk" else patch.REMOTE_NAME
    (toolkit / "static" / name).write_text("unrecognized asset")
    before = snapshot(toolkit)
    with pytest.raises(RuntimeError, match="Unexpected toolkit asset"):
        patch.patch_toolkit(toolkit)
    assert snapshot(toolkit) == before


def test_unknown_package_version_is_not_silently_patched(toolkit):
    path = toolkit / "package.json"
    package = json.loads(path.read_text())
    package["version"] = "0.3.0"
    path.write_text(json.dumps(package))
    before = snapshot(toolkit)
    with pytest.raises(RuntimeError, match="0.2.0"):
        patch.patch_toolkit(toolkit)
    assert snapshot(toolkit) == before


def test_other_entrypoint_modifications_are_preserved(toolkit):
    path = toolkit / "package.json"
    package = json.loads(path.read_text())
    package["jupyterlab"]["_build"]["load"] = "static/custom.js"
    path.write_text(json.dumps(package))
    before = snapshot(toolkit)
    with pytest.raises(RuntimeError, match="another patch"):
        patch.patch_toolkit(toolkit)
    assert snapshot(toolkit) == before
