"""Use each service's existing uv cache configuration without project overrides."""

import os
from pathlib import Path


def uv_environment(directory: Path, shared_root: Path | None = None) -> dict[str, str]:
    environment = {"UV_LINK_MODE": os.environ.get("UV_LINK_MODE") or "hardlink"}
    if "UV_CACHE_DIR" in os.environ:
        environment["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    return environment
