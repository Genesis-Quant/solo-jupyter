"""Regression tests for the project-local, read-only DSL inspection CLI."""

import builtins
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

HELPER_PATH = (
    Path(__file__).resolve().parents[1] / "codex/skills/dsl/scripts/inspect_dsl.py"
)
spec = importlib.util.spec_from_file_location("solo_inspect_dsl", HELPER_PATH)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.fixture(autouse=True)
def no_connections(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("The DSL helper must not connect to a service")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.fixture
def runtime_models(monkeypatch):
    # Tests must not load credentials or .env files even when launched from a
    # configured research project. This does not change the helper's policy.
    for key in tuple(os.environ):
        if key.startswith(("DOLPHIN_", "OBJECT_STORAGE_")) or key == "TUSHARE_TOKEN":
            monkeypatch.delenv(key)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    if importlib.util.find_spec("runtime") is None:
        pytest.skip("Run with a project's .venv Python containing arena-runtime")
    pytest.importorskip("pydantic")
    from runtime.apps.query import Derivative, FactorQuery
    from runtime.workers import available_factors

    return FactorQuery, Derivative, available_factors


def invoke(capsys, monkeypatch, args, payload=None):
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    status = helper.main(args)
    captured = capsys.readouterr()
    if status == 0:
        assert captured.err == ""
        return status, json.loads(captured.out)
    assert captured.out == ""
    return status, json.loads(captured.err)


def node(op="binary.add", fields=None, params=None, kind="DIRECT", **extra):
    return {
        "type": kind,
        "op": op,
        "fields": fields if fields is not None else {"left": "close", "right": 1},
        "params": params if params is not None else {},
        **extra,
    }


def query(**updates):
    return {
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
        "codes": [],
        "factors": ["close"],
        **updates,
    }


def test_version_reports_the_active_project_python(runtime_models, capsys, monkeypatch):
    status, result = invoke(capsys, monkeypatch, ["version"])
    assert status == 0
    assert Path(result["python"]["executable"]).resolve() == Path(sys.executable).resolve()
    assert result["packages"]["arena-runtime"]
    assert result["packages"]["pydantic"]


def test_schema_explicitly_requires_real_model_validation(runtime_models, capsys, monkeypatch):
    factor_query, _, _ = runtime_models
    _, result = invoke(capsys, monkeypatch, ["schema"])
    assert result["schema"] == factor_query.model_json_schema()
    assert result["schema"]["additionalProperties"] is False
    assert result["validation"]["method"] == "FactorQuery.model_validate"
    assert result["validation"]["requires_model_validation"] is True
    assert "available_factors" in result["validation"]["note"]
    assert "does not enumerate" in result["validation"]["note"]


def test_factors_are_complete_untruncated_and_current(runtime_models, capsys, monkeypatch):
    _, _, available_factors = runtime_models
    _, result = invoke(capsys, monkeypatch, ["factors"])
    expected = list(available_factors())
    assert result["factors"] == expected
    assert result["count"] == len(expected)
    assert expected == sorted(set(expected))
    assert "close" in expected
    assert "adj_factor" in expected


def test_operators_match_the_complete_registration(runtime_models, capsys, monkeypatch):
    _, derivative, _ = runtime_models
    _, result = invoke(capsys, monkeypatch, ["operators"])
    assert [item["op"] for item in result["operators"]] == sorted(derivative.operators)
    assert result["count"] == result["total"] == len(derivative.operators)
    assert all(item["description"] for item in result["operators"])
    assert {item["type"] for item in result["operators"]} == {"DIRECT", "TS", "CS"}
    assert {item["output_kind"] for item in result["operators"]} <= {"NUMBER", "BOOL", "ANY"}


@pytest.mark.parametrize("kind", ["DIRECT", "TS", "CS"])
def test_operator_type_filter(runtime_models, capsys, monkeypatch, kind):
    _, result = invoke(capsys, monkeypatch, ["operators", "--type", kind])
    assert result["count"] > 0
    assert all(item["type"] == kind for item in result["operators"])
    assert result["factors"] == list(runtime_models[2]())
    assert result["factor_count"] == len(runtime_models[2]())


@pytest.mark.parametrize("search", ["PCT_CHANGE", "按股票", "no_such_operator"])
def test_search_checks_name_and_description(runtime_models, capsys, monkeypatch, search):
    _, complete = invoke(capsys, monkeypatch, ["operators"])
    _, result = invoke(capsys, monkeypatch, ["operators", "--search", search])
    expected = [
        item for item in complete["operators"]
        if search.casefold() in item["op"].casefold()
        or search.casefold() in item["description"].casefold()
    ]
    assert result["operators"] == expected
    assert result["count"] == len(expected)
    assert result["total"] == complete["total"]
    assert result["factors"] == complete["factors"] == list(runtime_models[2]())
    assert result["factor_count"] == complete["factor_count"] == len(runtime_models[2]())


def test_combined_search_and_type(runtime_models, capsys, monkeypatch):
    _, result = invoke(capsys, monkeypatch, ["operators", "--search", "pct_change", "--type", "CS"])
    assert result["count"] == 0
    assert result["operators"] == []


def test_describe_uses_concrete_fields_params_and_operator_schema(runtime_models, capsys, monkeypatch):
    _, derivative, _ = runtime_models
    _, result = invoke(capsys, monkeypatch, ["describe", "unary.pct_change"])
    model = derivative.operators["unary.pct_change"]
    assert result["schema"] == model.model_json_schema()
    assert result["fields_schema"] == model.model_fields["fields"].annotation.model_json_schema()
    assert result["params_schema"] == model.model_fields["params"].annotation.model_json_schema()
    assert result["type"] == "TS"
    assert result["supports_on"] is True
    assert result["fields_schema"]["required"] == ["col"]
    assert result["params_schema"]["properties"]["periods"]["minimum"] == 1
    assert result["params_schema"]["properties"]["periods"]["default"] == 1
    assert result["description"]
    _, direct = invoke(capsys, monkeypatch, ["describe", "binary.gt"])
    assert direct["supports_on"] is False
    assert direct["output_kind"] == "BOOL"
    assert direct["params_schema"]["properties"] == {}


def test_unknown_operator_is_structured(runtime_models, capsys, monkeypatch):
    status, result = invoke(capsys, monkeypatch, ["describe", "unary.not_registered"])
    assert status == 1
    assert result["error"]["type"] == "unknown_operator"


def test_validate_stdin_returns_normalized_query_and_dependencies(runtime_models, capsys, monkeypatch):
    payload = query(
        lookback="30D",
        derivatives={
            "ret": node("unary.pct_change", {"col": "close"}, kind="TS"),
            "allowed": node("binary.gt", {"left": "ret", "right": 0}),
        },
        filters=["allowed"],
    )
    _, result = invoke(capsys, monkeypatch, ["validate", "-"], payload)
    assert result["success"] is True
    assert result["normalized_query"]["codes"] == []
    assert result["normalized_query"]["lookback"] == "P30D"
    assert result["normalized_query"]["derivatives"]["ret"]["on"] is None
    assert result["normalized_query"]["derivatives"]["ret"]["params"] == {"periods": 1}
    assert result["source_factors"] == ["close"]


def test_validate_utf8_file_without_modifying_it(runtime_models, tmp_path, capsys, monkeypatch):
    path = tmp_path / "payload.json"
    text = json.dumps(query(derivatives={"中文名称": node()}), ensure_ascii=False)
    path.write_text("\ufeff" + text, encoding="utf-8")
    before = path.read_bytes()
    _, result = invoke(capsys, monkeypatch, ["validate", str(path)])
    assert "中文名称" in result["normalized_query"]["derivatives"]
    assert path.read_bytes() == before


def test_nested_and_named_references_expand_source_factors(runtime_models, capsys, monkeypatch):
    payload = query(derivatives={
        "value": node("binary.mul", {"left": "close", "right": "adj_factor"}),
        "rank": node("unary.rank", {"col": "value"}, kind="CS"),
        "positive": node("binary.gt", {
            "left": node("unary.rolling_mean", {"col": "rank"}, {"window": 3}, kind="TS"),
            "right": 0,
        }),
    })
    _, result = invoke(capsys, monkeypatch, ["validate", "-"], payload)
    assert result["source_factors"] == ["close", "adj_factor"]


@pytest.mark.parametrize("payload", [
    query(factors=["not_a_runtime_factor"]),
    query(derivatives={"x": node(fields={"left": "not_a_runtime_factor", "right": 1})}),
    query(derivatives={"x": node(fields={
        "left": node(fields={"left": "not_a_runtime_factor", "right": 1}), "right": 1,
    })}),
])
def test_validate_rejects_unknown_factors_including_nested_references(
    runtime_models, capsys, monkeypatch, payload,
):
    status, result = invoke(capsys, monkeypatch, ["validate", "-"], payload)
    assert status == 1
    assert result["error"]["type"] == "unknown_factors"
    assert result["error"]["details"]["unknown_factors"] == ["not_a_runtime_factor"]


@pytest.mark.parametrize("payload", [
    query(extra="not_allowed"),
    query(derivatives={"x": node(extra="not_allowed")}),
    query(derivatives={"x": node(fields={"left": "close", "right": 1, "extra": 1})}),
    query(derivatives={"x": node(params={"extra": 1})}),
    query(derivatives={"x": node(fields={"left": node(extra=1), "right": 1})}),
    query(derivatives={"x": node(fields={"left": node(params={"extra": 1}), "right": 1})}),
    query(derivatives={"x": node(fields={
        "left": node(fields={"left": "close", "right": 1, "extra": 1}), "right": 1,
    })}),
    query(derivatives={"x": node("missing.operator")}),
    query(derivatives={"x": node("unary.pct_change", {"col": "close"}, {"periods": 0}, kind="TS")}),
    query(derivatives={"x": node("unary.rolling_mean", {"col": "close"}, {"window": 3, "min_periods": 4}, kind="TS")}),
    query(derivatives={"x": node("unary.rank", {"col": "close"}, {"ascending": "true"}, kind="CS")}),
    query(derivatives={"x": node()}, filters=["x"]),
    query(derivatives={"x": node()}, filters=["missing"]),
    query(derivatives={"a": node(fields={"left": "b", "right": 1}), "b": node(fields={"left": "a", "right": 1})}),
    query(start_date="2025-1-01"),
    query(start_date="2025-02-30"),
    query(end_date="2024-12-31"),
    query(codes=["000001.SZ", "000001.SZ"]),
    query(factors=[], derivatives={}),
    query(derivatives={"x": node("unary.rank", {"col": "close"}, kind="CS", on="is_st")}),
    query(derivatives={"x": node(on=True)}),
    query(derivatives={"x": node("unary.pct_change", {"col": "close"}, kind="DIRECT")}),
    query(derivatives={"time": node()}),
    query(lookback="-1D"),
    query(dsl_source={"language": "python", "python_source": "pass"}),
])
def test_validate_uses_runtime_constraints(runtime_models, capsys, monkeypatch, payload):
    status, result = invoke(capsys, monkeypatch, ["validate", "-"], payload)
    assert status == 1
    assert result["success"] is False
    assert result["error"]["type"] == "validation_error"
    assert result["error"]["details"]
    assert all("input" not in item and "ctx" not in item for item in result["error"]["details"])


@pytest.mark.parametrize("payload", ["{", "[]", "null", '{"value": NaN}', '{"value": Infinity}'])
def test_invalid_json_is_structured(runtime_models, capsys, monkeypatch, payload):
    status, result = invoke(capsys, monkeypatch, ["validate", "-"], payload)
    assert status == 1
    assert result["error"]["type"] == "invalid_json"


def test_missing_input_file(runtime_models, tmp_path, capsys, monkeypatch):
    status, result = invoke(capsys, monkeypatch, ["validate", str(tmp_path / "missing.json")])
    assert status == 1
    assert result["error"]["type"] == "input_error"


@pytest.mark.parametrize("args", [
    [], ["unknown"], ["describe"], ["validate"], ["version", "extra"],
    ["operators", "--search"], ["operators", "--type", "INVALID"],
    ["factors", "--search", "close"], ["validate", "-", "extra"],
])
def test_cli_errors_go_only_to_json_stderr(capsys, monkeypatch, args):
    status, result = invoke(capsys, monkeypatch, args)
    assert status == 2
    assert result["error"]["type"] == "cli_error"


def test_help_is_json_stdout(capsys, monkeypatch):
    with pytest.raises(SystemExit) as error:
        helper.main(["--help"])
    assert error.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "operators" in json.loads(captured.out)["help"]


def test_missing_runtime_suggests_project_python_without_installing(capsys, monkeypatch):
    original_import = builtins.__import__

    def missing_runtime(name, *args, **kwargs):
        if name == "runtime.apps.query":
            raise ModuleNotFoundError("No module named runtime", name="runtime")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_runtime)
    status, result = invoke(capsys, monkeypatch, ["schema"])
    assert status == 1
    assert result["error"]["type"] == "dependency_error"
    assert ".venv Python" in result["error"]["message"]
    assert "does not install" in result["error"]["message"]


# The child process exercises the real entry point and imports with no write or
# connect operations allowed. Socket creation/bind (urllib3 IPv6 probing) is not
# treated as connecting to a service.
READ_ONLY_RUNNER = r'''
import builtins, io, os, runpy, socket, sys

def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected write or connection in DSL helper")

original_open = builtins.open
original_io_open = io.open
original_os_open = os.open

def guarded_open(original):
    def guarded(file, mode="r", *args, **kwargs):
        if any(char in mode for char in "wax+"):
            forbidden()
        if isinstance(file, (str, bytes, os.PathLike)) and os.path.basename(os.fsdecode(file)).startswith(".env"):
            raise AssertionError("Unexpected .env read in DSL helper")
        return original(file, mode, *args, **kwargs)
    return guarded

def guarded_os_open(file, flags, *args, **kwargs):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
        forbidden()
    return original_os_open(file, flags, *args, **kwargs)

builtins.open = guarded_open(original_open)
io.open = guarded_open(original_io_open)
os.open = guarded_os_open
for name in ("mkdir", "remove", "unlink", "rename", "replace", "rmdir"):
    setattr(os, name, forbidden)
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
'''


def run_cli(tmp_path, args, payload=None, extra_env=None):
    env = {
        key: os.environ[key]
        for key in ("SystemRoot", "WINDIR", "PATH", "TEMP", "TMP", "INDEX_CODES")
        if key in os.environ
    }
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="0")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-B", "-c", READ_ONLY_RUNNER, str(HELPER_PATH), *args],
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=tmp_path,
        env=env,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize("args", [
    ["version"], ["schema"], ["factors"], ["operators", "--search", "pct_change"],
    ["describe", "unary.pct_change"], ["validate", "-"],
])
def test_real_cli_has_no_writes_or_connections(runtime_models, tmp_path, args):
    (tmp_path / ".env").write_text("INDEX_CODES=999999.SH\n", encoding="utf-8")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    result = run_cli(tmp_path, args, query())
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert isinstance(json.loads(result.stdout), dict)
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()} == before
    assert not (tmp_path / "__pycache__").exists()


def test_real_cli_keeps_index_configuration_without_reading_dotenv(runtime_models, tmp_path):
    (tmp_path / ".env").write_text("INDEX_CODES=999999.SH\n", encoding="utf-8")
    result = run_cli(tmp_path, ["factors"], extra_env={"INDEX_CODES": "123456.SH,654321.SZ"})
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    data = json.loads(result.stdout)
    factors = data["factors"]
    assert sorted(value for value in factors if value.startswith("weight_")) == [
        "weight_123456SH", "weight_654321SZ",
    ]
    assert data["configuration"] == {
        "source": "environment", "index_codes": ["123456.SH", "654321.SZ"],
        "dotenv_loaded": False,
    }


def test_real_cli_validates_file_without_writes(runtime_models, tmp_path):
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(query()), encoding="utf-8")
    result = run_cli(tmp_path, ["validate", str(path)])
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert json.loads(result.stdout)["source_factors"] == ["close"]


@pytest.mark.parametrize("args", [["factors"], ["operators", "--search", "pct_change"]])
def test_dotenv_only_configuration_is_not_presented_as_project_catalog(
    runtime_models, tmp_path, monkeypatch, args,
):
    monkeypatch.delenv("INDEX_CODES", raising=False)
    (tmp_path / ".env").write_text("INDEX_CODES=123456.SH\n", encoding="utf-8")
    result = run_cli(tmp_path, args)
    assert result.returncode == 0, result.stderr
    configuration = json.loads(result.stdout)["configuration"]
    assert configuration["source"] == "runtime-defaults"
    assert configuration["dotenv_loaded"] is False
    assert "--index-codes" in configuration["warning"]
    assert ".env" in configuration["warning"]


def test_default_weight_validation_uses_catalog_and_warns(runtime_models, tmp_path, monkeypatch):
    monkeypatch.delenv("INDEX_CODES", raising=False)
    catalog = json.loads(run_cli(tmp_path, ["factors"]).stdout)
    factor = next(value for value in catalog["factors"] if value.startswith("weight_"))
    result = run_cli(tmp_path, ["validate", "-"], query(factors=[factor]))
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert data["configuration"]["source"] == "runtime-defaults"
    assert "warning" in data["configuration"]
    result = run_cli(tmp_path, ["validate", "-"], query(factors=["weight_123456SH"]))
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["type"] == "unknown_factors"


@pytest.mark.parametrize("extra_env", [{}, {"INDEX_CODES": "654321.SZ"}])
def test_explicit_indices_validate_dotenv_config_without_reading_it(
    runtime_models, tmp_path, monkeypatch, extra_env,
):
    monkeypatch.delenv("INDEX_CODES", raising=False)
    (tmp_path / ".env").write_text(
        "INDEX_CODES=123456.SH\nDOLPHIN_RUNTIME_PASSWORD=synthetic-secret-not-for-output\n",
        encoding="utf-8",
    )
    payload = query(factors=[], derivatives={
        "member": node("binary.gt", {"left": "weight_123456SH", "right": 0}),
    })
    result = run_cli(tmp_path, ["--index-codes", "123456.SH", "validate", "-"], payload, extra_env)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert data["source_factors"] == ["weight_123456SH"]
    assert data["configuration"] == {
        "source": "option", "index_codes": ["123456.SH"],
        "dotenv_loaded": False,
    }
    assert "synthetic-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("args", [["factors"], ["operators", "--type", "TS"]])
def test_explicit_catalog_overrides_previous_worker_import_without_mutating_environment(
    runtime_models, capsys, monkeypatch, args,
):
    monkeypatch.setenv("INDEX_CODES", "654321.SZ")
    _, result = invoke(capsys, monkeypatch, ["--index-codes", "123456.SH, 999999.SZ", *args])
    assert sorted(f for f in result["factors"] if f.startswith("weight_")) == [
        "weight_123456SH", "weight_999999SZ",
    ]
    assert result["configuration"]["source"] == "option"
    assert result["configuration"]["index_codes"] == ["123456.SH", "999999.SZ"]
    assert os.environ["INDEX_CODES"] == "654321.SZ"


def test_confirmed_configuration_rejects_unknown_weight(runtime_models, tmp_path):
    result = run_cli(
        tmp_path, ["--index-codes", "654321.SZ", "validate", "-"],
        query(factors=["weight_123456SH"]),
    )
    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert error["type"] == "unknown_factors"
    assert error["details"]["unknown_factors"] == ["weight_123456SH"]
    assert error["details"]["configuration"]["index_codes"] == ["654321.SZ"]


@pytest.mark.parametrize("codes,expected", [
    ("", []), (" 123456.SH, ,654321.SZ ", ["123456.SH", "654321.SZ"]),
])
def test_explicit_indices_follow_runtime_empty_and_whitespace_semantics(
    runtime_models, capsys, monkeypatch, codes, expected,
):
    _, result = invoke(capsys, monkeypatch, ["--index-codes", codes, "factors"])
    assert result["configuration"]["index_codes"] == expected
    weights = [f for f in result["factors"] if f.startswith("weight_")]
    assert len(weights) == len(expected)


def test_invalid_explicit_index_is_structured(runtime_models, capsys, monkeypatch):
    status, result = invoke(capsys, monkeypatch, ["--index-codes", "invalid-code", "factors"])
    assert status == 1
    assert result["error"]["type"] == "invalid_index_configuration"


def test_default_configuration_does_not_block_static_factors(runtime_models, tmp_path, monkeypatch):
    monkeypatch.delenv("INDEX_CODES", raising=False)
    (tmp_path / ".env").write_text("INDEX_CODES=123456.SH\n", encoding="utf-8")
    result = run_cli(tmp_path, ["validate", "-"], query())
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["success"] is True
    assert data["source_factors"] == ["close"]
    assert data["configuration"]["source"] == "runtime-defaults"
