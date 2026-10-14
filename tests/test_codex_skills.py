import ast
import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "codex/skills"
NAMES = {"dsl", "dolphindb", "data-query", "factor-analysis", "backtest"}
spec = importlib.util.spec_from_file_location("check_codex_skills", ROOT / "scripts/check_codex_skills.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def test_all_skills_have_valid_metadata_and_small_entrypoints():
    assert {p.name for p in SKILLS.iterdir() if p.is_dir()} == NAMES
    for name in NAMES:
        path = SKILLS / name / "SKILL.md"
        source = path.read_text(encoding="utf-8")
        front = re.match(r"\A---\n(.*?)\n---\n", source, re.S)
        assert front, path
        metadata = dict(line.split(": ", 1) for line in front[1].splitlines())
        assert metadata["name"] == name
        assert 20 <= len(metadata["description"]) <= 1024
        assert len(source.splitlines()) < 150
        assert (path.parent / "references").is_dir()


def test_markdown_links_resolve_and_stay_inside_bundle():
    for path in SKILLS.rglob("*.md"):
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if target.startswith(("https://", "http://", "#")):
                continue
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            assert resolved.is_relative_to(SKILLS.resolve()), (path, target)
            assert resolved.is_file(), (path, target)


def test_python_and_json_examples_parse():
    for path in SKILLS.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for language, source in re.findall(r"^```(python|json)\s*\n(.*?)^```", text, re.M | re.S):
            if language == "python":
                tree = ast.parse(source, filename=str(path))
                assert not any(getattr(node, "type_params", ()) for node in ast.walk(tree))
            else:
                json.loads(source)


def test_image_includes_skills_outside_persistent_codex_home():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "COPY codex/skills/ /etc/codex/skills/" in dockerfile
    assert "!codex/skills/" in ignore.splitlines()
    assert "!codex/skills/**" in ignore.splitlines()
    assert "COPY scripts/check_codex_skills.py /opt/jupyter-codex/check_skills.py" in dockerfile
    assert "RUN python -B /opt/jupyter-codex/check_skills.py --cwd /tmp" in dockerfile
    assert not re.search(r"COPY[^\n]*skills[^\n]*/\.codex/", dockerfile)
    assert "**/__pycache__/" in ignore


def discovery_response(root):
    return {"data": [{"cwd": "/shared/projects", "errors": [], "skills": [
        {"name": name, "path": str(root / name / "SKILL.md"), "scope": "admin", "enabled": True}
        for name in sorted(NAMES)
    ]}]}


def test_discovery_accepts_all_five_enabled_system_skills(tmp_path):
    result = check.verify_response(discovery_response(tmp_path), tmp_path)
    assert {s["name"] for s in result} == NAMES


@pytest.mark.parametrize("fault", ["missing", "disabled", "parse_error", "shadowed", "scope", "duplicate", "workspaces"])
def test_discovery_rejects_partial_or_shadowed_installation(tmp_path, fault):
    response = discovery_response(tmp_path)
    entry = response["data"][0]
    if fault == "missing":
        entry["skills"].pop()
    elif fault == "disabled":
        entry["skills"][0]["enabled"] = False
    elif fault == "parse_error":
        entry["errors"] = [{"path": "bad/SKILL.md", "message": "invalid metadata"}]
    elif fault == "shadowed":
        entry["skills"][0]["path"] = str(tmp_path / "user/SKILL.md")
    elif fault == "scope":
        entry["skills"][0]["scope"] = "user"
    elif fault == "duplicate":
        entry["skills"].append(entry["skills"][0].copy())
    else:
        response["data"].append(entry.copy())
    with pytest.raises(ValueError):
        check.verify_response(response, tmp_path)


def test_discovery_does_not_start_model_turns():
    source = (ROOT / "scripts/check_codex_skills.py").read_text(encoding="utf-8")
    assert '"skills/list"' in source
    assert '"initialize"' in source
    assert '"turn/start"' not in source
    assert '"thread/start"' not in source
    assert "TemporaryDirectory" in source
    assert 'CODEX_HOME=str(codex_home)' in source


def test_current_factor_example_builds_without_execution():
    pytest.importorskip("scheme")
    path = SKILLS / "factor-analysis/references/factor-contract.md"
    blocks = re.findall(r"^```python\s*\n(.*?)^```", path.read_text(encoding="utf-8"), re.M | re.S)
    namespace = {"__name__": "skill_factor_example"}
    exec(compile(blocks[0], str(path), "exec"), namespace)
    from scheme import StockPool
    from scheme.execute.strategy.assembly import parameter_type

    form = namespace["FactorReportForm"](
        start="2024-06-03", end="2024-06-08", pool=StockPool.SSE50,
        periods=20, return_periods=[1, 2],
    )
    params = form.build()
    assert params.periods == 20
    assert params.universe.pool == StockPool.SSE50
    assert parameter_type(namespace["Factor"]) is namespace["FactorParams"]
