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
    result.parameters = SimpleNamespace(columns=["momentum"], return_periods=[1, 5, 20], groups=5, n_select=10)
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
        assert payload["parameters"]["factor_columns"] == ["momentum"]
        assert payload["parameters"]["return_specs"]["return_5"] == {"kind": "simple", "periods": 5}
    else:
        assert "parameters" not in payload
    assert len(value) < 1000
    assert not any(marker in value for marker in ("srcdoc", "base64", "token=", "duckdb", "<script"))
    current.save.assert_called_once_with(manifest.parent)


def test_repeated_show_creates_unique_output_without_overwriting(project):
    workspace, _ = project
    current = report()
    paths = [parse_qs(urlsplit(iframe_url(bridge.report_iframe(current))).query)["path"][0] for _ in range(2)]
    assert len(set(paths)) == 2
    assert all((workspace / path).is_file() for path in paths)


@pytest.mark.parametrize("settings", [
    {"height": 199}, {"height": True}, {"height": 1000.0}, {"theme": "other"},
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
