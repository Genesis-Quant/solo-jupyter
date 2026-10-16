import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location("solo_patch_mcp_uuid", Path(__file__).resolve().parents[1] / "scripts/patch_mcp_uuid.py")
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


@pytest.fixture
def toolkit(tmp_path):
    root = tmp_path / "toolkit"
    static = root / "static"
    static.mkdir(parents=True)
    package = {"name": "jupyterlab-commands-toolkit", "version": "0.2.0",
               "jupyterlab": {"_build": {"load": "static/remoteEntry.original.js", "extension": "./extension"}}}
    (root / "package.json").write_text(json.dumps(package), encoding="utf-8")
    (static / "509.original.js").write_text("var a=o(262),s=o(902);" + patch.OLD_UUID, encoding="utf-8")
    (static / "remoteEntry.original.js").write_text(patch.LUMINO_MODULE + '509:"original";v="original"', encoding="utf-8")
    return root


def snapshot(root):
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def test_feature_patch_uses_lumino_and_deterministic_cache_references(toolkit):
    result = patch.patch_toolkit(toolkit)
    assert result["uuid_generator"] == "@lumino/coreutils.UUID.uuid4"
    assert result["chunk"] == "509.solo-uuid.js"
    assert result["remote"] == "remoteEntry.solo-uuid.js"
    assert patch.NEW_UUID in (toolkit / "static" / result["chunk"]).read_text()
    assert (toolkit / "static" / result["remote"]).read_text().count("solo-uuid") == 2
    assert patch.OLD_UUID in (toolkit / "static/509.original.js").read_text()
    assert json.loads((toolkit / "package.json").read_text())["jupyterlab"]["_build"]["load"] == "static/remoteEntry.solo-uuid.js"


def test_patch_is_idempotent(toolkit):
    first = patch.patch_toolkit(toolkit)
    before = snapshot(toolkit)
    assert patch.patch_toolkit(toolkit) == first
    assert snapshot(toolkit) == before


def test_unrelated_changes_do_not_block_feature_patch(toolkit):
    for asset in ("509.original.js", "remoteEntry.original.js"):
        with (toolkit / "static" / asset).open("a") as stream:
            stream.write("/* unrelated upstream change */")
    patch.patch_toolkit(toolkit)
    assert "unrelated upstream change" in (toolkit / "static/509.solo-uuid.js").read_text()


@pytest.mark.parametrize("asset", ["509.original.js", "remoteEntry.original.js"])
def test_missing_patch_feature_fails_before_writes(toolkit, asset):
    (toolkit / "static" / asset).write_text("unrecognized asset")
    before = snapshot(toolkit)
    with pytest.raises((RuntimeError, ValueError)):
        patch.patch_toolkit(toolkit)
    assert snapshot(toolkit) == before


def test_unknown_version_is_not_patched(toolkit):
    path = toolkit / "package.json"
    package = json.loads(path.read_text())
    package["version"] = "0.3.0"
    path.write_text(json.dumps(package))
    before = snapshot(toolkit)
    with pytest.raises(RuntimeError, match="0.2.0"):
        patch.patch_toolkit(toolkit)
    assert snapshot(toolkit) == before


def test_existing_target_is_not_overwritten(toolkit):
    (toolkit / "static/509.solo-uuid.js").write_text("user asset")
    before = snapshot(toolkit)
    with pytest.raises(FileExistsError):
        patch.patch_toolkit(toolkit)
    assert snapshot(toolkit) == before


def test_other_entrypoint_is_preserved(toolkit):
    path = toolkit / "package.json"
    package = json.loads(path.read_text())
    package["jupyterlab"]["_build"]["load"] = "static/custom.js"
    path.write_text(json.dumps(package))
    before = snapshot(toolkit)
    with pytest.raises(RuntimeError, match="another patch"):
        patch.patch_toolkit(toolkit)
    assert snapshot(toolkit) == before
