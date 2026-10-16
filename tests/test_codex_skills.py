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


def test_factor_skill_examples_use_max_lag_and_document_segment_boundaries():
    directory = SKILLS / "factor-analysis"
    for path in [directory / "SKILL.md", directory / "references/factor-contract.md", directory / "references/evaluation.md"]:
        text = path.read_text(encoding="utf-8")
        assert "max_lag" in text
        assert "r0o" in text and "r1o" in text and "r2" in text
        assert "simple" in text and "隔夜" in text and "日内" in text
        assert "[start,end]" in text and "NULL" in text
        assert "close-to-close累计收益" in text
        assert "历史报告" in text or "篡改历史" in text
        for source in re.findall(r"^```python\s*\n(.*?)^```", text, re.M | re.S):
            tree = ast.parse(source)
            assert not any(isinstance(node, ast.keyword) and node.arg == "return_periods" for node in ast.walk(tree))
    contract = (directory / "references/factor-contract.md").read_text(encoding="utf-8")
    assert "max_lag=2" in contract
    assert "end + timedelta(days=1)" in contract
    assert "factor.validate(start,end,n_sample=5)" in contract
    assert "compute(d,d)" in contract
    assert "params.return_columns" in contract and "params.return_specs" in contract
    for field in ("kind", "periods", "lag", "session", "turnover_period"):
        assert field in contract


def test_current_factor_example_builds_without_execution():
    pytest.importorskip("scheme")
    path = SKILLS / "factor-analysis/references/factor-contract.md"
    blocks = re.findall(r"^```python\s*\n(.*?)^```", path.read_text(encoding="utf-8"), re.M | re.S)
    namespace = {"__name__": "skill_factor_example"}
    exec(compile(blocks[0], str(path), "exec"), namespace)
    from scheme import StockPool
    from scheme.execute.strategy.assembly import parameter_type

    example = ast.parse(blocks[1], filename=str(path))
    assert ast.unparse(example.body[-1]) == "report = params.run(Factor)"
    example.body.pop()  # 仅构建示例参数，不执行研究或连接数据库。
    exec(compile(example, str(path), "exec"), namespace)
    params = namespace["params"]
    assert params.periods == 20
    assert params.universe.pool == StockPool.SSE50
    assert params.max_lag == 2
    assert not hasattr(params, "return_periods")
    assert params.return_columns == ["r0o", "r1", "r1o", "r2"]
    assert params.return_specs == {
        "r0o": {"kind": "simple", "periods": 1, "lag": 0, "session": "overnight", "turnover_period": 1},
        "r1": {"kind": "simple", "periods": 1, "lag": 1, "session": "intraday", "turnover_period": 1},
        "r1o": {"kind": "simple", "periods": 1, "lag": 1, "session": "overnight", "turnover_period": 2},
        "r2": {"kind": "simple", "periods": 1, "lag": 2, "session": "intraday", "turnover_period": 2},
    }
    for settings in ({}, {"max_lag": 1}):
        single_lag = namespace["FactorReportForm"](
            start="2024-06-03", end="2024-06-08", pool=StockPool.SSE50,
            periods=20, **settings,
        ).build()
        assert single_lag.max_lag == 1
        assert single_lag.return_columns == ["r0o", "r1"]
        assert single_lag.return_specs == {name: params.return_specs[name] for name in ("r0o", "r1")}
    assert parameter_type(namespace["Factor"]) is namespace["FactorParams"]


def test_dsl_defaults_validate_existing_index_weights_and_never_load_dotenv(monkeypatch, tmp_path):
    from types import ModuleType, SimpleNamespace
    import sys

    spec = importlib.util.spec_from_file_location("skill_inspect_dsl", SKILLS / "dsl/scripts/inspect_dsl.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.delenv("INDEX_CODES", raising=False)
    modules = {name: ModuleType(name) for name in ("runtime", "runtime.config", "runtime.utils", "runtime.workers", "pydantic")}
    modules["pydantic"].ValidationError = ValueError
    modules["runtime.config"].INDEX_CODES = ("000300.SH",)
    modules["runtime.utils"].index_weight_factor = lambda code: "weight_" + code.replace(".", "")
    modules["runtime.workers"].available_factors = lambda: ("close", "weight_000300SH")
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    request = tmp_path / "request.json"
    request.write_text('{}', encoding="utf-8")
    query = SimpleNamespace(source_factors=lambda: ["weight_000300SH"], model_dump=lambda **_: {"factors": ["weight_000300SH"]})
    result = helper.validate_request(str(request), SimpleNamespace(model_validate=lambda _: query), None)
    assert result["success"] is True
    assert result["configuration"]["source"] == "runtime-defaults"
    assert result["configuration"]["dotenv_loaded"] is False
    assert "Runtime defaults" in result["configuration"]["warning"]
    assert ".env files are never loaded" in result["configuration"]["warning"]
    assert helper.os.environ["PYTHON_DOTENV_DISABLED"] == "1"
    assert "Runtime 默认" in (SKILLS / "dsl/SKILL.md").read_text(encoding="utf-8")
