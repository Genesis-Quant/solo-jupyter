import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_final_image_inherits_complete_current_lts_node():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM ${NOTEBOOK_IMAGE} AS notebook-node" in dockerfile
    assert re.search(r"^FROM notebook-node$", dockerfile, re.M)
    node_stage = dockerfile.split("\nFROM notebook-node AS solo-extension\n", 1)[0]
    assert "https://deb.nodesource.com/setup_lts.x" in node_stage
    assert "apt-get install -y --no-install-recommends nodejs" in node_stage
    assert "node --version && npm --version && npx --version" in node_stage
    assert node_stage.index("rm -f /usr/local/bin/node /usr/local/bin/npm /usr/local/bin/npx") < node_stage.index("setup_lts.x")
    assert not re.search(r"setup_\d|node-v\d|sha256|node_sha", node_stage)
    assert "COPY --from=codex-runtime" not in dockerfile


def test_codex_and_acp_are_installed_globally_from_latest():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    final = dockerfile.split("\nFROM notebook-node\n", 1)[1]
    assert "npm install --global --no-audit --no-fund" in final
    assert final.index("rm -f /usr/local/bin/codex /usr/local/bin/codex-acp") < final.index("npm install --global")
    assert not (ROOT / "codex/package.json").exists()
    assert not (ROOT / "codex/package-lock.json").exists()
    assert not (ROOT / "codex/.npmrc").exists()
    assert "@openai/codex@latest" in final
    assert "@agentclientprotocol/codex-acp@latest" in final
    assert not re.search(r"@(openai/codex|agentclientprotocol/codex-acp)@\d", final)
    assert "npm_config_prefix=/usr/local" in dockerfile
    assert "CODEX_PATH=/usr/local/bin/codex" in dockerfile
    assert "/opt/jupyter-codex/node_modules/.bin" not in dockerfile
    assert "codex --version && codex-acp --version" in final


def test_python_installs_do_not_pin_every_preinstalled_package():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "requirements-existing.txt" not in dockerfile
    assert "m.distributions()" not in dockerfile
    for name in ("requirements-ai.txt", "requirements-dolphindb.txt"):
        install = dockerfile.split(f"COPY {name}", 1)[1].split("\n\n", 1)[0]
        assert "--requirement /tmp/" + name in install
        assert "python -m pip check" in install
