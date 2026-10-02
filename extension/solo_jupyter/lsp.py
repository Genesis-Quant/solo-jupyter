"""按文档所属 Solo 项目提供 Jedi 环境，不切换全局解释器。"""

import json
import os
from pathlib import Path
from threading import RLock
from urllib.parse import unquote, urlsplit
from uuid import UUID

from pylsp import hookimpl
from pylsp.config.config import DEFAULT_CONFIG_SOURCES
from pylsp.config.source import ConfigSource
from pylsp.plugins._resolvers import LABEL_RESOLVER, SNIPPET_RESOLVER

from .handlers import KINDS


class SoloProjectConfig(ConfigSource):
    def __init__(self, root_path, *, shared_dir=None, virtual_dir=None):
        super().__init__(root_path)
        self.shared_dir = Path(shared_dir or os.getenv("SOLO_SHARED_DIR", "/shared")).resolve()
        virtual = Path(virtual_dir or os.getenv("JP_LSP_VIRTUAL_DIR", ".virtual_documents"))
        self.virtual_dir = (virtual if virtual.is_absolute() else self.shared_dir / virtual).resolve()
        self._signatures = {}
        self._completion_paths = {}
        self._lock = RLock()

    def user_config(self):
        return {}

    def project_path(self, document_path):
        if not document_path:
            return None
        value = str(document_path)
        uri = urlsplit(value)
        if uri.scheme:
            if uri.scheme != "file" or uri.netloc not in {"", "localhost"} or uri.query or uri.fragment:
                return None
            value = unquote(uri.path)
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            return None
        try:
            path = path.resolve()
            if path.is_relative_to(self.virtual_dir):
                # JupyterLab preserves the original notebook path under its virtual root.
                path = (self.shared_dir / path.relative_to(self.virtual_dir)).resolve()
            relative = path.relative_to(self.shared_dir / "projects")
            if len(relative.parts) < 3 or relative.parts[0] not in KINDS:
                return None
            project = self.shared_dir / "projects" / relative.parts[0] / relative.parts[1]
            return project if project.resolve() == project and path.is_relative_to(project) else None
        except (OSError, ValueError, RuntimeError):
            return None

    def project_config(self, document_path):
        project = self.project_path(document_path)
        if project is None:
            return {}
        marker = project / ".solo"
        manifest = project / "pyproject.toml"
        source = project / "src"
        venv = project / ".venv"
        python = venv / "bin/python"
        try:
            files = (marker, manifest, venv / "pyvenv.cfg")
            if not all(path.is_file() and path.resolve().is_relative_to(project) for path in files):
                return {}
            if not source.is_dir() or not source.resolve().is_relative_to(project) or venv.resolve() != venv:
                return {}
            if marker.stat().st_size > 65536 or not python.is_file() or not os.access(python, os.X_OK):
                return {}
            metadata = json.loads(marker.read_text(encoding="utf-8"))
            UUID(metadata["project_id"])
            if metadata["name"] != project.name or metadata["kind"] != project.parent.name:
                return {}
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            return {}
        # uv links bin/python to its managed installation; keep the venv path for Jedi.
        return {"plugins": {"jedi": {
            "environment": str(python),
            "extra_paths": [str(source)],
        }}}

    def refresh(self, config, workspace, document_path):
        project = self.project_path(document_path)
        if project is None:
            return
        paths = [project / name for name in (".solo", "pyproject.toml", "uv.lock", "src", ".venv", ".venv/pyvenv.cfg", ".venv/bin/python")]
        try:
            sites = sorted((project / ".venv/lib").glob("python*/site-packages"))
            paths.extend(sites)
            for site in sites:
                paths.extend(sorted(site.glob("*.pth")))
        except OSError:
            pass
        signature = tuple((str(path), self._stamp(path)) for path in paths)
        with self._lock:
            if self._signatures.get(project) != signature:
                self._signatures[project] = signature
                config.settings.cache_clear()
                workspace._environments.pop(str(project / ".venv/bin/python"), None)
                self._invalidate_completion_resolvers(project)
        return project

    def _invalidate_completion_resolvers(self, project):
        external_paths = self._completion_paths.pop(project, set())
        # pylsp 1.15 keys include module_path, but not the Jedi environment.
        for resolver in (LABEL_RESOLVER, SNIPPET_RESOLVER):
            keys = {
                key for key in tuple(resolver._cache)
                if key[1] is not None
                and (key[1].is_relative_to(project) or key[1] in external_paths)
            }
            for key in keys:
                resolver._cache.pop(key, None)
            # clear_outdated() deletes cache entries indexed by these TTL sets.
            for timestamp, bucket in tuple(resolver._cache_ttl.items()):
                bucket.difference_update(keys)
                if not bucket:
                    resolver._cache_ttl.pop(timestamp, None)

    def remember_completion_paths(self, document):
        project = document.shared_data.get("solo_project")
        if project is None:
            return
        with self._lock:
            cached_paths = {
                key[1] for resolver in (LABEL_RESOLVER, SNIPPET_RESOLVER)
                for key in tuple(resolver._cache) if key[1] is not None
            }
            # External editable modules have no project prefix. Track only exact
            # cached origins used here, and forget entries that pylsp has expired.
            for owner, paths in tuple(self._completion_paths.items()):
                paths.intersection_update(cached_paths)
                if not paths:
                    del self._completion_paths[owner]
            for _, completion in document.shared_data.get("LAST_JEDI_COMPLETIONS", {}).values():
                path = completion.module_path
                if path in cached_paths and not path.is_relative_to(project):
                    self._completion_paths.setdefault(project, set()).add(path)

    @staticmethod
    def _stamp(path):
        try:
            stat = path.stat()
            link = path.lstat()
            return (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, link.st_ino, link.st_mtime_ns)
        except OSError:
            return None


def _enable_project_config(config):
    if "solo" not in config._config_sources:
        return
    sources = config._settings.get("configurationSources", DEFAULT_CONFIG_SOURCES)
    sources = ["solo", *[source for source in sources if source != "solo"]]
    if config._settings.get("configurationSources") != sources:
        # The client schema only accepts built-in sources; add Solo on the server.
        config._settings = {**config._settings, "configurationSources": sources}
        config.settings.cache_clear()


@hookimpl
def pylsp_settings(config):
    # pylsp 1.15 has no public registration API for document-scoped config sources.
    config._config_sources["solo"] = SoloProjectConfig(config._root_path)
    _enable_project_config(config)
    return {"plugins": {"solo_project_environment": {"enabled": True}}}


@hookimpl
def pylsp_initialize(config, workspace):
    _enable_project_config(config)
    _enable_project_config(workspace._config)


@hookimpl
def pylsp_workspace_configuration_changed(config, workspace):
    _enable_project_config(config)
    _enable_project_config(workspace._config)


def _refresh(config, workspace, document):
    # pylsp 1.15 moves open documents between workspaces without rebinding their owners.
    actual_workspace = getattr(document, "_workspace", None) or workspace
    actual_config = getattr(document, "_config", None) or config
    _enable_project_config(actual_config)
    source = actual_config._config_sources.get("solo")
    if source is not None:
        project = source.refresh(actual_config, actual_workspace, document.path)
        previous = document.shared_data.get("solo_project")
        if previous != project:
            # A retargeted document can lose its project without changing that
            # project's signature. Drop its cached binding as well.
            actual_config.settings.cache_clear()
            if previous is not None:
                actual_workspace._environments.pop(str(previous / ".venv/bin/python"), None)
            document.shared_data["solo_project"] = project


@hookimpl
def pylsp_document_did_open(config, workspace, document):
    _refresh(config, workspace, document)


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_definitions(config, workspace, document, position):
    _refresh(config, workspace, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_completions(config, workspace, document, position, ignored_names):
    _refresh(config, workspace, document)
    yield
    actual_config = getattr(document, "_config", None) or config
    source = actual_config._config_sources.get("solo")
    if source is not None:
        source.remember_completion_paths(document)


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_hover(config, workspace, document, position):
    _refresh(config, workspace, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_references(config, workspace, document, position, exclude_declaration):
    _refresh(config, workspace, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_signature_help(config, workspace, document, position):
    _refresh(config, workspace, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_lint(config, workspace, document, is_saved):
    _refresh(config, workspace, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_type_definition(config, document, position):
    # Unlike the other document hooks, this hookspec has no workspace argument.
    _refresh(config, None, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_document_symbols(config, workspace, document):
    _refresh(config, workspace, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_document_highlight(config, workspace, document, position):
    _refresh(config, workspace, document)
    yield


@hookimpl(hookwrapper=True, tryfirst=True)
def pylsp_rename(config, workspace, document, position, new_name):
    _refresh(config, workspace, document)
    yield
