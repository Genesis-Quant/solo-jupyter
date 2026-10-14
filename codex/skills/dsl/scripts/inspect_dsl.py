#!/usr/bin/env python3
"""Inspect the active project's Arena Runtime DSL without executing a query."""

import argparse
import inspect
import json
import os
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import Any, get_args

# Set these before importing Runtime: workers import runtime.config, which otherwise
# loads .env files. Keep INDEX_CODES and other existing non-secret configuration.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
sys.dont_write_bytecode = True


class HelperError(Exception):
    def __init__(self, kind: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details


def emit(value: Any, stream: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), file=stream)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise HelperError("cli_error", message)

    def print_help(self, file: Any = None) -> None:
        emit({"help": self.format_help()}, file if file is not None else sys.stdout)


def parser() -> argparse.ArgumentParser:
    result = JsonArgumentParser(description=__doc__)
    result.add_argument(
        "--index-codes", metavar="CODES",
        help="Explicit comma-separated INDEX_CODES for factors/operators/validate; overrides the environment.",
    )
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="Show the active Python and package versions.")
    commands.add_parser("schema", help="Show FactorQuery schema and validation limitations.")
    commands.add_parser("factors", help="List the complete current Worker factor allowlist.")
    operators = commands.add_parser("operators", help="List registered operator summaries.")
    operators.add_argument("--search", default="", help="Search operator names and descriptions.")
    operators.add_argument("--type", choices=("DIRECT", "TS", "CS"), dest="operator_type")
    describe = commands.add_parser("describe", help="Show one operator's exact schema.")
    describe.add_argument("operator")
    validate = commands.add_parser("validate", help="Validate a Runtime FactorQuery JSON object.")
    validate.add_argument("request", help="UTF-8 JSON file, or - to read stdin.")
    return result


def dependency_error(error: ImportError) -> HelperError:
    missing = getattr(error, "name", None) or "arena-runtime / pydantic"
    return HelperError(
        "dependency_error",
        f"Cannot load {missing!r} with {sys.executable}. "
        "Run this helper with the current project's .venv Python containing arena-runtime; "
        "this helper does not install packages.",
    )


def query_models() -> tuple[Any, Any]:
    try:
        from runtime.apps.query import Derivative, FactorQuery
    except ImportError as error:
        raise dependency_error(error) from error
    return FactorQuery, Derivative


def factor_catalog(index_codes: str | None) -> tuple[tuple[str, ...], dict[str, Any]]:
    try:
        from runtime.config import INDEX_CODES
        from runtime.utils import index_weight_factor
        from runtime.workers import available_factors
    except ImportError as error:
        raise dependency_error(error) from error

    if index_codes is not None:
        source, value = "option", index_codes
    elif "INDEX_CODES" in os.environ:
        source, value = "environment", os.environ["INDEX_CODES"]
    else:
        source, value = "runtime-defaults", None
    selected = (
        tuple(code.strip() for code in value.split(",") if code.strip())
        if value is not None else INDEX_CODES
    )
    try:
        imported_weights = {index_weight_factor(code) for code in INDEX_CODES}
        selected_weights = {index_weight_factor(code) for code in selected}
    except ValueError as error:
        raise HelperError("invalid_index_configuration", str(error)) from error
    # Worker catalogs are initialized once per process; an explicit configuration
    # must not inherit an earlier import's index weights.
    factors = tuple(sorted((set(available_factors()) - imported_weights) | selected_weights))
    configuration: dict[str, Any] = {
        "source": source,
        "index_codes": list(selected),
        "dotenv_loaded": False,
        "requires_confirmation": source == "runtime-defaults",
    }
    if source == "runtime-defaults":
        configuration["warning"] = (
            "INDEX_CODES was not supplied. This catalog uses Runtime defaults, not the project's "
            ".env configuration. Provide --index-codes before the command or export INDEX_CODES "
            "to validate index weight factors; .env files are never loaded."
        )
    return factors, configuration


def package_versions() -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in ("arena-runtime", "scheme", "backtest", "pydantic"):
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError as error:
            if name in {"arena-runtime", "pydantic"}:
                raise dependency_error(error) from error
            packages[name] = None
    return {
        "python": {
            "executable": sys.executable,
            "version": platform.python_version(),
            "prefix": sys.prefix,
        },
        "packages": packages,
    }


def operator_summary(operation: str, model: Any) -> dict[str, Any]:
    return {
        "op": operation,
        "type": get_args(model.model_fields["type"].annotation)[0],
        "description": inspect.getdoc(model) or model.model_fields["op"].description or "",
        "output_kind": model.output_kind,
    }


def describe_operator(operation: str, operators: dict[str, Any]) -> dict[str, Any]:
    if operation not in operators:
        raise HelperError(
            "unknown_operator", f"Unknown operator {operation!r}; use operators to list this Runtime."
        )
    model = operators[operation]
    return {
        **operator_summary(operation, model),
        "supports_on": "on" in model.model_fields,
        "schema": model.model_json_schema(),
        "fields_schema": model.model_fields["fields"].annotation.model_json_schema(),
        "params_schema": model.model_fields["params"].annotation.model_json_schema(),
    }


def read_request(path: str) -> dict[str, Any]:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise HelperError("input_error", str(error)) from error

    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON constant {value!r} is not allowed")

    try:
        value = json.loads(text, parse_constant=reject_constant)
    except ValueError as error:
        raise HelperError("invalid_json", str(error)) from error
    if not isinstance(value, dict):
        raise HelperError("invalid_json", "The request must be a Runtime FactorQuery JSON object.")
    return value


def validate_request(path: str, factor_query: Any, index_codes: str | None) -> dict[str, Any]:
    from pydantic import ValidationError

    value = read_request(path)
    try:
        query = factor_query.model_validate(value)
    except ValidationError as error:
        raise HelperError(
            "validation_error",
            "FactorQuery.model_validate rejected the request.",
            error.errors(include_url=False, include_context=False, include_input=False),
        ) from error
    source_factors = query.source_factors()
    factors, configuration = factor_catalog(index_codes)
    if configuration["requires_confirmation"] and any(
        factor.startswith("weight_") for factor in source_factors
    ):
        raise HelperError(
            "index_configuration_required",
            "Index weight validation needs the project's effective INDEX_CODES. "
            "Provide --index-codes before the command or export INDEX_CODES; .env files are not loaded.",
            {"configuration": configuration},
        )
    unknown = sorted(set(source_factors) - set(factors))
    if unknown:
        raise HelperError(
            "unknown_factors",
            "The query references factors not declared by the selected Runtime configuration.",
            {"unknown_factors": unknown, "configuration": configuration},
        )
    return {
        "success": True,
        "normalized_query": query.model_dump(mode="json"),
        "source_factors": source_factors,
        "configuration": configuration,
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "version":
        return package_versions()
    if args.command == "factors":
        factors, configuration = factor_catalog(args.index_codes)
        return {"factors": list(factors), "count": len(factors), "configuration": configuration}
    factor_query, derivative = query_models()
    if args.command == "schema":
        return {
            "model": "runtime.apps.query.FactorQuery",
            "schema": factor_query.model_json_schema(),
            "validation": {
                "method": "FactorQuery.model_validate",
                "requires_model_validation": True,
                "note": (
                    "This JSON Schema does not enumerate concrete operator dispatch constraints. "
                    "Use FactorQuery.model_validate for fields/params, BOOL rules and dependencies; "
                    "use describe <operator> for concrete schemas. The validate command also checks "
                    "query.source_factors() against runtime.workers.available_factors() with explicit "
                    "--index-codes or exported INDEX_CODES; index weights require configuration "
                    "confirmation when only Runtime defaults are available. "
                    "Validation neither executes DolphinScript nor checks database contents."
                ),
            },
        }
    if args.command == "operators":
        search = args.search.casefold()
        operators = [
            operator_summary(name, model)
            for name, model in sorted(derivative.operators.items())
        ]
        operators = [
            item for item in operators
            if (not args.operator_type or item["type"] == args.operator_type)
            and (search in item["op"].casefold() or search in item["description"].casefold())
        ]
        factors, configuration = factor_catalog(args.index_codes)
        return {
            "operators": operators,
            "count": len(operators),
            "total": len(derivative.operators),
            "factors": list(factors),
            "factor_count": len(factors),
            "configuration": configuration,
        }
    if args.command == "describe":
        return describe_operator(args.operator, derivative.operators)
    return validate_request(args.request, factor_query, args.index_codes)


def main(argv: list[str] | None = None) -> int:
    try:
        result = execute(parser().parse_args(argv))
        emit(result, sys.stdout)
        return 0
    except HelperError as error:
        failure: dict[str, Any] = {"type": error.kind, "message": str(error)}
        if error.details is not None:
            failure["details"] = error.details
        emit({"success": False, "error": failure}, sys.stderr)
        return 2 if error.kind == "cli_error" else 1
    except Exception as error:  # noqa: BLE001 -- CLI failures must remain structured JSON.
        emit(
            {"success": False, "error": {"type": "runtime_error", "message": str(error)}},
            sys.stderr,
        )
        return 1


if __name__ == "__main__":
    # Windows redirected streams otherwise use the locale code page, while JSON
    # consumers and UTF-8 stdin must preserve the Runtime's Chinese descriptions.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
