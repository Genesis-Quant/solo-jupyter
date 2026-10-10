"""通过 Jupyter 的认证和文件传输能力读取工作区文件。"""

from pathlib import Path, PurePosixPath

from jupyter_core.paths import is_hidden
from jupyter_server.base.handlers import AuthenticatedFileHandler
from tornado import web


class FilesHandler(AuthenticatedFileHandler):
    def parse_url_path(self, url_path: str) -> str:
        relative = PurePosixPath(url_path)
        if (
            not url_path
            or relative.is_absolute()
            or ".." in relative.parts
            or any(character in url_path for character in ("\\", ":", "\x00"))
        ):
            raise web.HTTPError(400, reason="Invalid workspace file path")
        return super().parse_url_path(url_path)

    def validate_absolute_path(self, root: str, absolute_path: str) -> str:
        workspace = Path(root).resolve()
        try:
            resolved = Path(absolute_path).resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise web.HTTPError(400, reason="Invalid workspace file path") from error
        if not resolved.is_relative_to(workspace):
            raise web.HTTPError(403, reason="Path is outside the Jupyter workspace")
        if not self.contents_manager.allow_hidden and (
            is_hidden(absolute_path, workspace) or is_hidden(resolved, workspace)
        ):
            raise web.HTTPError(404)
        return super().validate_absolute_path(root, str(resolved))
