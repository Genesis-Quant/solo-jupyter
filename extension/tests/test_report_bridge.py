import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest


spec = importlib.util.spec_from_file_location("solo_report_bridge_test", Path(__file__).resolve().parents[2] / "solo_report_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


FORWARD_SPECS = {
    f"P5L{lag}": {
        "kind": "simple", "periods": 5, "lag": lag,
        "price_column": "open_hfq", "turnover_period": 5,
    }
    for lag in range(1, 4)
}

LAG_SPECS = {
    "r0o": {"kind": "simple", "periods": 1, "lag": 0, "session": "overnight", "turnover_period": 1},
    "r1": {"kind": "simple", "periods": 1, "lag": 1, "session": "intraday", "turnover_period": 1},
    "r1o": {"kind": "simple", "periods": 1, "lag": 1, "session": "overnight", "turnover_period": 2},
    "r2": {"kind": "simple", "periods": 1, "lag": 2, "session": "intraday", "turnover_period": 2},
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    directory = tmp_path / "projects" / "factor" / "因子 项目"
    directory.mkdir(parents=True)
    monkeypatch.chdir(directory)
    monkeypatch.setenv("SOLO_SHARED_DIR", str(tmp_path))
    monkeypatch.setenv("SOLO_REPORT_FRONTEND_URL", "http://127.0.0.1:5174")
    monkeypatch.setattr(bridge, "version", lambda _: "1.2.3")
    return tmp_path, directory


def report(kind="factor"):
    names = ("information_coefficient", "group_returns") if kind == "factor" else (
        "daily_portfolios", "trade_details", "daily_positions", "daily_trading_statistics")
    result = SimpleNamespace(report_kind=kind, filenames={name: name + ".parquet" for name in names})
    result.parameters = SimpleNamespace(
        columns=["momentum"], period=5, price_column="open_hfq", max_lag=3, groups=5, n_select=10,
    )
    def save(output):
        paths = []
        for filename in result.filenames.values():
            path = output / filename
            path.write_bytes(b"PAR1 fixture")
            paths.append(path)
        return paths
    result.save = Mock(side_effect=save)
    return result


def iframe_url(value):
    from html import unescape
    from html.parser import HTMLParser
    class Parser(HTMLParser):
        url = None
        def handle_starttag(self, tag, attrs):
            if tag == "iframe":
                attributes = dict(attrs)
                assert "srcdoc" not in attributes
                self.url = attributes["src"]
    parser = Parser()
    parser.feed(value)
    return unescape(parser.url)


@pytest.mark.parametrize("kind", ["factor", "backtest"])
def test_iframe_saves_manifest_and_links_frontend_without_embedded_assets(project, kind):
    workspace, _ = project
    current = report(kind)
    value = bridge.report_iframe(current, height=900, theme="dark", annual_trading_days=250, risk_free_rate=0.02)
    url = urlsplit(iframe_url(value))
    assert url.netloc == "127.0.0.1:5174" and url.path == "/embed/report"
    query = parse_qs(url.query)
    assert query["source"] == ["jupyter"] and query["theme"] == ["dark"]
    manifest = workspace / query["path"][0]
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["protocol"] == 1 and payload["format"] == "solo-notebook-report"
    assert payload["schemeVersion"] == "1.2.3" and payload["kind"] == kind
    assert payload["theme"] == "dark" and payload["annualTradingDays"] == 250
    assert payload["riskFreeRate"] == 0.02
    assert payload["files"] == current.filenames
    assert all((manifest.parent / name).read_bytes() == b"PAR1 fixture" for name in current.filenames.values())
    if kind == "factor":
        assert payload["parameters"] == {
            "factor_columns": ["momentum"],
            "return_columns": ["P5L1", "P5L2", "P5L3"],
            "return_specs": FORWARD_SPECS,
            "n_groups": 5,
            "n_select": 10,
        }
    else:
        assert "parameters" not in payload
    assert len(value) < 1000
    assert not any(marker in value for marker in ("srcdoc", "base64", "token=", "duckdb", "<script"))
    current.save.assert_called_once_with(manifest.parent)


@pytest.mark.parametrize("max_lag", [1, 2])
def test_factor_max_lag_metadata_without_scheme_properties(project, max_lag):
    workspace, _ = project
    current = report()
    current.parameters = SimpleNamespace(columns=["momentum"], max_lag=max_lag, groups=5, n_select=10)
    value = bridge.report_iframe(current)
    path = parse_qs(urlsplit(iframe_url(value)).query)["path"][0]
    metadata = json.loads((workspace / path).read_text(encoding="utf-8"))["parameters"]
    expected_columns = ["r0o", "r1"] if max_lag == 1 else ["r0o", "r1", "r1o", "r2"]
    assert metadata["return_columns"] == expected_columns
    assert metadata["return_specs"] == {name: LAG_SPECS[name] for name in expected_columns}
    assert len(metadata["return_columns"]) == len(set(metadata["return_columns"])) == 2 * max_lag
    assert not hasattr(current.parameters, "return_columns")
    assert not hasattr(current.parameters, "return_specs")


@pytest.mark.parametrize("period", [1, 5])
@pytest.mark.parametrize("max_lag", [1, 3])
@pytest.mark.parametrize("price_column", ["open", "close", "open_hfq", "close_hfq"])
def test_forward_metadata_uses_holding_period_and_entry_lag(project, period, max_lag, price_column):
    workspace, _ = project
    current = report()
    current.parameters.period = period
    current.parameters.max_lag = max_lag
    current.parameters.price_column = price_column
    value = bridge.report_iframe(current)
    path = parse_qs(urlsplit(iframe_url(value)).query)["path"][0]
    metadata = json.loads((workspace / path).read_text(encoding="utf-8"))["parameters"]
    expected = {
        f"P{period}L{lag}": {
            "kind": "simple", "periods": period, "lag": lag,
            "price_column": price_column, "turnover_period": period,
        }
        for lag in range(1, max_lag + 1)
    }
    assert metadata["return_columns"] == list(expected)
    assert metadata["return_specs"] == expected
    assert not hasattr(current.parameters, "return_columns")
    assert not hasattr(current.parameters, "return_specs")


@pytest.mark.parametrize("period", [True, False, 0, -1, 1.0, 1.5, "5", None])
def test_invalid_forward_period_fails_without_legacy_fallback(project, period):
    _, directory = project
    current = report()
    current.parameters.period = period
    current.parameters.return_periods = [1]
    with pytest.raises(ValueError, match="period"):
        bridge.report_iframe(current)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


@pytest.mark.parametrize("price_column", ["adj_open", "adj_factor", "open_qfq", "", 1, True, None, []])
def test_invalid_forward_price_fails_without_legacy_fallback(project, price_column):
    _, directory = project
    current = report()
    current.parameters.price_column = price_column
    with pytest.raises(ValueError, match="price_column"):
        bridge.report_iframe(current)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


@pytest.mark.parametrize("field", ["period", "price_column", "max_lag"])
def test_incomplete_forward_parameters_are_not_reinterpreted_as_legacy(project, field):
    _, directory = project
    current = report()
    delattr(current.parameters, field)
    with pytest.raises(ValueError, match=field):
        bridge.report_iframe(current)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


def test_factor_legacy_return_periods_keep_original_metadata(project):
    workspace, _ = project
    current = report()
    current.parameters = SimpleNamespace(columns=["momentum"], return_periods=[1, 5, 20], groups=5, n_select=10)
    value = bridge.report_iframe(current)
    path = parse_qs(urlsplit(iframe_url(value)).query)["path"][0]
    metadata = json.loads((workspace / path).read_text(encoding="utf-8"))["parameters"]
    assert metadata == {
        "factor_columns": ["momentum"],
        "return_columns": ["return_1", "return_5", "return_20"],
        "return_specs": {
            "return_1": {"kind": "simple", "periods": 1},
            "return_5": {"kind": "simple", "periods": 5},
            "return_20": {"kind": "simple", "periods": 20},
        },
        "n_groups": 5,
        "n_select": 10,
    }
    assert current.parameters.return_periods == [1, 5, 20]
    assert not hasattr(current.parameters, "max_lag")


@pytest.mark.parametrize("max_lag", [True, False, 0, -1, 1.0, 1.5, "2", None])
def test_invalid_max_lag_fails_before_saving_without_legacy_fallback(project, max_lag):
    _, directory = project
    current = report()
    current.parameters.max_lag = max_lag
    current.parameters.return_periods = [1, 5, 20]
    with pytest.raises(ValueError, match="max_lag"):
        bridge.report_iframe(current)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


@pytest.mark.parametrize("periods", [[], [1, 1], [True], [0], [-1], [1.0], ["1"]])
def test_invalid_legacy_return_periods_fail_before_saving(project, periods):
    _, directory = project
    current = report()
    current.parameters = SimpleNamespace(columns=["momentum"], return_periods=periods, groups=5, n_select=10)
    with pytest.raises(ValueError, match="收益持有期|return_periods"):
        bridge.report_iframe(current)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("columns", [[], ["momentum", "momentum"], [""], [" "], ["bad\x00column"], [1]])
def test_invalid_factor_columns_fail_before_saving(project, legacy, columns):
    _, directory = project
    current = report()
    if legacy:
        current.parameters = SimpleNamespace(columns=columns, return_periods=[1], groups=5, n_select=10)
    else:
        current.parameters.columns = columns
    with pytest.raises(ValueError, match="因子列"):
        bridge.report_iframe(current)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


def test_repeated_show_creates_unique_output_without_overwriting(project):
    workspace, _ = project
    current = report()
    paths = [parse_qs(urlsplit(iframe_url(bridge.report_iframe(current))).query)["path"][0] for _ in range(2)]
    assert len(set(paths)) == 2
    assert all((workspace / path).is_file() for path in paths)


@pytest.mark.parametrize("settings", [
    {"height": 0}, {"height": -1}, {"height": True}, {"height": "200"}, {"height": float("nan")}, {"theme": "other"},
    {"annual_trading_days": 0}, {"annual_trading_days": True}, {"risk_free_rate": float("nan")},
    {"risk_free_rate": float("inf")}, {"risk_free_rate": True},
])
def test_invalid_display_settings_write_nothing(project, settings):
    _, directory = project
    current = report()
    with pytest.raises(ValueError):
        bridge.report_iframe(current, **settings)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@example.invalid", "https://example.invalid?token=secret", "https://example.invalid#fragment"])
def test_frontend_url_cannot_include_credentials_or_non_http(project, monkeypatch, url):
    _, directory = project
    monkeypatch.setenv("SOLO_REPORT_FRONTEND_URL", url)
    with pytest.raises(ValueError, match="SOLO_REPORT_FRONTEND_URL"):
        bridge.report_iframe(report())
    assert not (directory / "reports").exists()


@pytest.mark.parametrize("filename", ["../secret.parquet", "/secret.parquet", "https://host/file.parquet", ".hidden.parquet", "file\\path.parquet", "data.json"])
def test_manifest_files_cannot_escape_the_report_directory(project, filename):
    _, directory = project
    current = report()
    current.filenames["information_coefficient"] = filename
    with pytest.raises(ValueError, match="文件名"):
        bridge.report_iframe(current)
    current.save.assert_not_called()
    assert not (directory / "reports").exists()


def test_invalid_factor_parameters_fail_before_saving(project):
    current = report()
    current.parameters = None
    with pytest.raises(ValueError, match="分析参数"):
        bridge.report_iframe(current)
    current.save.assert_not_called()


def test_workspace_root_is_a_required_boundary(project, monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("SOLO_SHARED_DIR", str(tmp_path / "workspace"))
    with pytest.raises(ValueError, match="工作区"):
        bridge.report_iframe(report())
    assert not (outside / "reports").exists()


def test_reports_symlink_is_rejected(project, tmp_path):
    _, directory = project
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (directory / "reports").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires permission on this platform")
    with pytest.raises(ValueError, match="普通目录"):
        bridge.report_iframe(report())
    assert list(outside.iterdir()) == []


def test_install_only_changes_runtime_show_and_provider(monkeypatch):
    old_show = Mock()
    export = Mock()
    fake = type("Report", (), {"show": old_show, "to_html": export})
    monkeypatch.setattr(bridge.importlib, "import_module", lambda _: SimpleNamespace(Report=fake))
    shell = SimpleNamespace()
    bridge.install(shell)
    assert shell.solo_report_display is bridge.display_report
    assert fake.show is bridge._show
    assert fake.to_html is export


def test_install_without_scheme_still_registers_provider(monkeypatch):
    def missing(_):
        error = ModuleNotFoundError("No module named scheme")
        error.name = "scheme"
        raise error
    monkeypatch.setattr(bridge.importlib, "import_module", missing)
    shell = SimpleNamespace()
    bridge.install(shell)
    assert shell.solo_report_display is bridge.display_report


@pytest.mark.parametrize("height", [1, 199, 0.5, 1000.0])
def test_any_positive_numeric_height_is_accepted(project, height):
    assert f"height:{height}px" in bridge.report_iframe(report(), height=height)
