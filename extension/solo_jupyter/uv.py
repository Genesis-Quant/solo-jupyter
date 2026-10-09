"""Keep uv's hardlink cache on the target environment or build volume."""

import os
from pathlib import Path


def uv_environment(directory: Path, shared_root: Path | None = None) -> dict[str, str]:
    directory = Path(os.path.abspath(directory))
    root = Path(os.path.abspath(shared_root or os.environ.get("SOLO_SHARED_DIR", "/shared")))
    cache = directory.parent / ".uv-cache"
    for volume in ("projects", "runs", "artifacts"):
        mount = root / volume
        if directory.is_relative_to(mount):
            cache = mount / ".uv-cache"
            break
    return {"UV_LINK_MODE": "hardlink", "UV_CACHE_DIR": str(cache)}
