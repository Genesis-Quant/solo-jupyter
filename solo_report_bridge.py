"""在项目 Kernel 中将 Scheme 报告保存为文件，并通过 Solo URL iframe 展示。"""

import html
import importlib
import json
import math
import os
import re
import sys
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

from IPython.display import display

REQUIRED_FILES = {
    "factor": {"information_coefficient", "group_returns"},
    "backtest": {
        "daily_portfolios",
        "trade_details",
        "daily_positions",
        "daily_trading_statistics",
    },
}


def positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} 必须为正整数")
    return value


def validate_settings(height, theme, annual_trading_days, risk_free_rate):
    if (
        isinstance(height, bool)
        or not isinstance(height, (int, float))
        or not height > 0
    ):
        raise ValueError("height 必须为正数")
    if theme not in {"light", "dark"}:
        raise ValueError("theme 必须为 light 或 dark")
    positive_integer(annual_trading_days, "annual_trading_days")
    if (
        isinstance(risk_free_rate, bool)
        or not isinstance(risk_free_rate, (int, float))
        or not math.isfinite(risk_free_rate)
    ):
        raise ValueError("risk_free_rate 必须为有限数值")


def frontend_url():
    value = os.environ.get("SOLO_REPORT_FRONTEND_URL", "http://127.0.0.1:5174").rstrip(
        "/"
    )
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "SOLO_REPORT_FRONTEND_URL 必须是无凭据、查询参数或片段的 HTTP(S) 前端地址"
        )
    return value


def report_manifest(report, *, theme, annual_trading_days, risk_free_rate):
    kind = report.report_kind
    if kind not in REQUIRED_FILES:
        raise ValueError(f"不支持的报告类型：{kind}")
    filenames = dict(report.filenames)
    if not REQUIRED_FILES[kind].issubset(filenames):
        raise ValueError("报告缺少必要数据文件")
    if any(
        not isinstance(name, str)
        or not name
        or not isinstance(filename, str)
        or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\.parquet", filename) is None
        or ".." in filename
        for name, filename in filenames.items()
    ):
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
        if (
            not columns
            or any(
                not isinstance(column, str) or not column.strip() or "\x00" in column
                for column in columns
            )
            or len(set(columns)) != len(columns)
        ):
            raise ValueError("因子报告缺少有效且不重复的因子列")
        positive_integer(parameters.period, "period")
        positive_integer(parameters.max_lag, "max_lag")
        if parameters.price_column not in {"open", "close", "open_hfq", "close_hfq"}:
            raise ValueError("price_column 必须为 open、close、open_hfq 或 close_hfq")
        return_specs = parameters.return_specs
        data["parameters"] = {
            "factor_columns": columns,
            "return_columns": list(return_specs),
            "return_specs": return_specs,
            "n_groups": positive_integer(parameters.groups, "groups"),
            "n_select": positive_integer(parameters.n_select, "n_select"),
        }
    return data


def report_directory():
    workspace = Path(os.environ.get("SOLO_SHARED_DIR", "/home/jovyan")).resolve()
    current = Path.cwd().resolve()
    if not current.is_relative_to(workspace) or any(
        part.startswith(".") for part in current.relative_to(workspace).parts
    ):
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


def report_iframe(
    report, *, height=1000, theme="light", annual_trading_days=252, risk_free_rate=0.0
):
    """保存已计算的结果；不查询数据或重新运行算法。"""
    validate_settings(height, theme, annual_trading_days, risk_free_rate)
    frontend = frontend_url()
    data = report_manifest(
        report,
        theme=theme,
        annual_trading_days=annual_trading_days,
        risk_free_rate=risk_free_rate,
    )
    workspace, output = report_directory()
    report.save(output)
    if any(
        not (output / filename).is_file()
        or (output / filename).resolve().parent != output
        for filename in data["files"].values()
    ):
        raise ValueError("报告保存未生成清单声明的文件")
    manifest = output / "report.json"
    temporary = output / "report.json.tmp"
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(manifest)
    query = urlencode(
        {
            "source": "jupyter",
            "path": manifest.relative_to(workspace).as_posix(),
            "theme": theme,
        }
    )
    url = html.escape(frontend + "/embed/report?" + query, quote=True)
    return (
        f'<iframe title="Scheme 研究报告" style="width:100%;height:{height}px;border:0" '
        f'sandbox="allow-scripts allow-same-origin allow-downloads" src="{url}"></iframe>'
    )


def display_report(
    report, *, height=1000, theme="light", annual_trading_days=252, risk_free_rate=0.0
):
    display(
        {
            "text/html": report_iframe(
                report,
                height=height,
                theme=theme,
                annual_trading_days=annual_trading_days,
                risk_free_rate=risk_free_rate,
            ),
            "text/plain": "Scheme 交互研究报告",
        },
        raw=True,
    )


def install(shell):
    """在 Kernel 启动时设置报告桥接和日志，不修改研究包。"""
    shell.solo_report_display = display_report
    try:
        scheme = importlib.import_module("scheme")
    except ModuleNotFoundError as error:
        if error.name == "scheme":
            return
        raise
    logger = scheme.logger
    logger.remove()
    logger.add(
        sys.stdout,
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<7} | {message}",
        backtrace=False,
        diagnose=False,
    )
