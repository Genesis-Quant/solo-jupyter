import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("patch_codex_skills", ROOT / "scripts/patch_codex_skills.py")
patcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patcher)

SOURCE = '''import path8 from "node:path";
function arraysEqual(left, right) {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}
export class Session {
  constructor(client) {
    this.codexClient = client;
    this.skillExtraRoots = [];
  }
  async refreshSkills(cwd, additionalRoots) {
    if (!cwd) {
      return;
    }
    const skillExtraRoots = additionalRoots.map((root) => path8.join(root, ".agents", "skills"));
    if (!arraysEqual(this.skillExtraRoots, skillExtraRoots)) {
      await this.codexClient.skillsExtraRootsSet({ extraRoots: skillExtraRoots });
      this.skillExtraRoots = skillExtraRoots;
    }
  }
}
'''


def test_patch_changes_only_existing_root_selection_and_is_idempotent(tmp_path):
    entry = tmp_path / "adapter.mjs"
    config = tmp_path / "skills.json"
    entry.write_text(SOURCE, encoding="utf-8")
    assert patcher.patch(entry, config)
    changed = entry.read_text(encoding="utf-8")
    assert changed.count(patcher.MARKER) == 1
    assert json.dumps(str(config)) in changed
    assert 'this.codexClient.skillsExtraRootsSet({ extraRoots: skillExtraRoots })' in changed
    assert "writable_roots" not in changed
    assert not patcher.patch(entry, config)
    assert entry.read_text(encoding="utf-8") == changed


@pytest.mark.parametrize("source", ["unrecognized adapter", SOURCE + SOURCE])
def test_unknown_or_ambiguous_adapter_is_not_modified(tmp_path, source):
    entry = tmp_path / "adapter.mjs"
    entry.write_text(source, encoding="utf-8")
    with pytest.raises(RuntimeError, match="refresh hook changed"):
        patcher.patch(entry)
    assert entry.read_text(encoding="utf-8") == source


def test_refresh_registers_configured_roots_preserves_workspace_roots_and_reloads_changes(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is unavailable")
    config = tmp_path / "skills.json"
    config.write_text(json.dumps({"extraRoots": ["/shared/skills"]}), encoding="utf-8")
    entry = tmp_path / "adapter.mjs"
    entry.write_text(SOURCE, encoding="utf-8")
    patcher.patch(entry, config)
    test = tmp_path / "check.mjs"
    test.write_text('''import assert from "node:assert/strict";
import { writeFileSync, unlinkSync } from "node:fs";
import path from "node:path";
import { Session } from "./adapter.mjs";
const config = process.argv[2];
const calls = [];
const session = new Session({ async skillsExtraRootsSet(request) { calls.push(request); } });
await session.refreshSkills("", []);
assert.equal(calls.length, 0);
await session.refreshSkills("/workspace", []);
assert.deepEqual(calls, [{ extraRoots: ["/shared/skills"] }]);
await session.refreshSkills("/workspace", []);
assert.equal(calls.length, 1);
await session.refreshSkills("/workspace", ["/other"]);
assert.deepEqual(calls.at(-1), { extraRoots: [path.join("/other", ".agents", "skills"), "/shared/skills"] });
writeFileSync(config, JSON.stringify({ extraRoots: ["/new/skills", "/new/skills"] }));
await session.refreshSkills("/workspace", ["/other"]);
assert.deepEqual(calls.at(-1), { extraRoots: [path.join("/other", ".agents", "skills"), "/new/skills"] });
writeFileSync(config, JSON.stringify({ extraRoots: [] }));
await session.refreshSkills("/workspace", []);
assert.deepEqual(calls.at(-1), { extraRoots: [] });
writeFileSync(config, "not JSON");
await assert.rejects(session.refreshSkills("/workspace", []));
assert.equal(calls.length, 4);
unlinkSync(config);
await assert.rejects(session.refreshSkills("/workspace", []));
assert.equal(calls.length, 4);
console.log("configured skill roots registered and refreshed without permission changes");
''', encoding="utf-8")
    result = subprocess.run([node, str(test), str(config)], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
