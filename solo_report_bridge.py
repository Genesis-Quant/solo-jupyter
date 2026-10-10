"""在项目 Kernel 中将 Scheme 报告保存为文件，并通过 Solo URL iframe 展示。"""

import html
import importlib
import json
import math
import os
import re
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4


_REQUIRED_FILES = {
    "factor": {"information_coefficient", "group_returns"},
    "backtest": {"daily_portfolios", "trade_details", "daily_positions", "daily_trading_statistics"},
}


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须为正整数")
    return value


def _settings(height, theme, annual_trading_days, risk_free_rate):
    if isinstance(height, bool) or not isinstance(height, int) or height < 200:
        raise ValueError("height 不能小于 200")
    if theme not in {"light", "dark"}:
        raise ValueError("theme 必须为 light 或 dark")
    _positive_integer(annual_trading_days, "annual_trading_days")
    if isinstance(risk_free_rate, bool) or not isinstance(risk_free_rate, (int, float)) or not math.isfinite(risk_free_rate):
        raise ValueError("risk_free_rate 必须为有限数值")


def _frontend_url():
    value = os.environ.get("SOLO_REPORT_FRONTEND_URL", "http://127.0.0.1:5174").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("SOLO_REPORT_FRONTEND_URL 必须是无凭据、查询参数或片段的 HTTP(S) 前端地址")
    return value


def _manifest(report, *, theme, annual_trading_days, risk_free_rate):
    kind = report.report_kind
    if kind not in _REQUIRED_FILES:
        raise ValueError(f"不支持的报告类型：{kind}")
    filenames = dict(report.filenames)
    if not _REQUIRED_FILES[kind].issubset(filenames):
        raise ValueError("报告缺少必要数据文件")
    if any(not isinstance(name, str) or not name or not isinstance(filename, str)
           or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.parquet", filename) is None or ".." in filename
           for name, filename in filenames.items()):
        raise ValueError("报告文件名无效")
    if len(set(filenames.values())) != len(filenames):
        raise ValueError("报告文件名不能重复")
    data = {
        "protocol": 1,
        "format": "solo-notebook-report",
        "schemeVersion": version("scheme"),
        "kind": kind,
        "theme": theme,
        "annualTradingDays": annual_trading_days,
        "riskFreeRate": risk_free_rate,
        "files": filenames,
    }
    if kind == "factor":
        parameters = getattr(report, "parameters", None)
        if parameters is None:
            raise ValueError("因子报告缺少分析参数；请使用 analyze_factors 返回的报告")
        columns = list(parameters.columns)
        periods = list(parameters.return_periods)
        if not columns or any(not isinstance(column, str) or not column.strip() or "\x00" in column for column in columns) or len(set(columns)) != len(columns):
            raise ValueError("因子报告缺少有效且不重复的因子列")
        if not periods or len(set(periods)) != len(periods):
            raise ValueError("因子报告缺少有效且不重复的收益持有期")
        for period in periods:
            _positive_integer(period, "return_periods")
        data["parameters"] = {
            "factor_columns": columns,
            "return_columns": [f"return_{period}" for period in periods],
            "return_specs": {f"return_{period}": {"kind": "simple", "periods": period} for period in periods},
            "n_groups": _positive_integer(parameters.groups, "groups"),
            "n_select": _positive_integer(parameters.n_select, "n_select"),
        }
    return data


def _report_directory():
    workspace = Path(os.environ.get("SOLO_SHARED_DIR", "/shared")).resolve()
    current = Path.cwd().resolve()
    if not current.is_relative_to(workspace) or any(part.startswith(".") for part in current.relative_to(workspace).parts):
        raise ValueError("请在 Jupyter 工作区内的项目目录展示报告")
    parent = current / "reports"
    if parent.exists() and (not parent.is_dir() or parent.is_symlink()):
        raise ValueError("reports 必须是当前项目内的普通目录")
    if parent.resolve() != parent or not parent.resolve().is_relative_to(workspace):
        raise ValueError("报告目录不能通过链接离开当前项目")
    parent.mkdir(exist_ok=True)
    output = parent / uuid4().hex
    output.mkdir()
    return workspace, output


def report_iframe(report, *, height=1000, theme="light", annual_trading_days=252, risk_free_rate=0.0):
    """保存已计算的结果；不查询数据或重新运行算法。"""
    _settings(height, theme, annual_trading_days, risk_free_rate)
    frontend = _frontend_url()
    data = _manifest(report, theme=theme, annual_trading_days=annual_trading_days, risk_free_rate=risk_free_rate)
    workspace, output = _report_directory()
    report.save(output)
    if any(not (output / filename).is_file() or (output / filename).resolve().parent != output
           for filename in data["files"].values()):
        raise ValueError("报告保存未生成清单声明的文件")
    manifest = output / "report.json"
    temporary = output / "report.json.tmp"
    temporary.write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    query = urlencode({"source": "jupyter", "path": manifest.relative_to(workspace).as_posix(), "theme": theme})
    url = html.escape(frontend + "/embed/report?" + query, quote=True)
    return (
        f'<iframe title="Scheme 研究报告" style="width:100%;height:{height}px;border:0" '
        f'sandbox="allow-scripts allow-same-origin allow-downloads" src="{url}"></iframe>'
    )


def display_report(report, *, height=1000, theme="light", annual_trading_days=252, risk_free_rate=0.0):
    from IPython.display import display

    display({"text/html": report_iframe(report, height=height, theme=theme,
                                       annual_trading_days=annual_trading_days, risk_free_rate=risk_free_rate),
             "text/plain": "Scheme 交互研究报告"}, raw=True)


def _show(self, *, height=1000, theme="light", annual_trading_days=252, risk_free_rate=0.0):
    return display_report(self, height=height, theme=theme,
                          annual_trading_days=annual_trading_days, risk_free_rate=risk_free_rate)


def install(shell):
    """仅替换内存中的展示方法；保留项目安装包、锁文件和独立导出。"""
    shell.solo_report_display = display_report
    try:
        module = importlib.import_module("scheme.report.base")
    except ModuleNotFoundError as error:
        if error.name == "scheme":
            return
        raise
    module.Report.show = _show
