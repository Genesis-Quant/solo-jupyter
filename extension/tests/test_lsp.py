import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pylsp import uris
from pylsp.config.config import Config
from pylsp.python_lsp import PythonLSPServer
from pylsp.workspace import Workspace
from solo_jupyter import lsp


@pytest.fixture
def roots(monkeypatch, tmp_path):
    shared = tmp_path / "shared"
    virtual = tmp_path / "virtual"
    shared.mkdir()
    virtual.mkdir()
    monkeypatch.setenv("SOLO_SHARED_DIR", str(shared))
    monkeypatch.setenv("JP_LSP_VIRTUAL_DIR", str(virtual))
    return shared, virtual


def make_project(shared, kind="model", name="model_a"):
    root = shared / "projects" / kind / name
    (root / "src").mkdir(parents=True)
    (root / ".venv/bin").mkdir(parents=True)
    (root / ".solo").write_text(json.dumps({"project_id": str(uuid4()), "name": name, "kind": kind}))
    (root / "pyproject.toml").write_text('[project]\nname = "probe"\n')
    (root / ".venv/pyvenv.cfg").write_text("home = /usr/bin\n")
    python = root / ".venv/bin/python"
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    return root


def config_for(shared):
    config = Config(uris.from_fs_path(str(shared)), {}, None, {})
    lsp.pylsp_settings(config)
    config.update({"configurationSources": ["pycodestyle"], "plugins": {
        "jedi": {"environment": None, "extra_paths": []},
        "jedi_definition": {"enabled": True, "follow_imports": True},
    }})
    lsp.pylsp_workspace_configuration_changed(config, SimpleNamespace(_config=config))
    return config


def test_python_file_binds_its_own_project(roots):
    shared, _ = roots
    project = make_project(shared)
    source = lsp.SoloProjectConfig(str(shared))
    result = source.project_config(str(project / "src/algo.py"))
    assert result == {"plugins": {"jedi": {
        "environment": str(project / ".venv/bin/python"),
        "extra_paths": [str(project / "src")],
    }}}


def test_project_source_is_enabled_at_plugin_startup(roots):
    shared, _ = roots
    project = make_project(shared)
    config = Config(uris.from_fs_path(str(shared)), {}, None, {})
    lsp.pylsp_settings(config)
    result = config.plugin_settings("jedi", document_path=str(project / "src/algo.py"))
    assert result["environment"] == str(project / ".venv/bin/python")
    assert config._settings["configurationSources"] == ["solo", "pycodestyle"]


@pytest.mark.parametrize("settings", [
    {}, {"configurationSources": []}, {"configurationSources": ["pycodestyle"]},
    {"configurationSources": ["flake8", "pycodestyle"]},
])
def test_configuration_update_keeps_project_source_without_changing_client_values(roots, settings):
    shared, _ = roots
    project = make_project(shared)
    config = config_for(shared)
    client_settings = {**settings, "plugins": {"jedi": {"environment": None, "extra_paths": []}}}
    config.update(client_settings)
    lsp.pylsp_workspace_configuration_changed(config, SimpleNamespace(_config=config))
    expected = ["solo", *settings.get("configurationSources", ["pycodestyle"])]
    assert config._settings["configurationSources"] == expected
    assert "solo" not in client_settings.get("configurationSources", [])
    result = config.plugin_settings("jedi", document_path=str(project / "src/algo.py"))
    assert result["environment"] == str(project / ".venv/bin/python")
    assert client_settings["plugins"]["jedi"]["environment"] is None


def test_initialize_restores_source_after_empty_configuration(roots):
    shared, _ = roots
    project = make_project(shared)
    config = config_for(shared)
    workspace = Workspace(uris.from_fs_path(str(shared)), Mock(), config)
    config.update({})
    lsp.pylsp_initialize(config, workspace)
    assert config.plugin_settings("jedi", document_path=str(project / "src/algo.py"))["environment"] == str(project / ".venv/bin/python")


def test_secondary_workspace_request_restores_its_own_config(roots):
    shared, _ = roots
    first = make_project(shared)
    second = make_project(shared, "factor", "factor_b")
    config = config_for(shared)
    other_config = config_for(second)
    workspace = Workspace(uris.from_fs_path(str(second)), Mock(), other_config)
    uri = uris.from_fs_path(str(second / "src/algo.py"))
    workspace.put_document(uri, "")
    other_config.update({})
    request = lsp.pylsp_definitions(config, workspace, workspace.get_document(uri), {"line": 0, "character": 0})
    next(request)
    result = other_config.plugin_settings("jedi", document_path=str(second / "src/algo.py"))
    assert result["environment"] == str(second / ".venv/bin/python")
    assert config.plugin_settings("jedi", document_path=str(first / "src/algo.py"))["environment"] == str(first / ".venv/bin/python")
    with pytest.raises(StopIteration):
        next(request)


def test_client_configuration_sources_follow_installed_schema():
    from pathlib import Path

    from jsonschema import Draft7Validator
    from jupyter_lsp.specs.config import load_config_schema

    source = Path(__file__).parents[2] / "config/lab/user-settings/@jupyter-lsp/jupyterlab-lsp/plugin.jupyterlab-settings"
    if not source.is_file():
        pytest.skip("当前测试目录未包含 Jupyter 用户设置")
    settings = json.loads(source.read_text())["language_servers"]["pylsp"]["serverSettings"]
    schema = load_config_schema("pylsp")["properties"]["pylsp.configurationSources"]
    Draft7Validator(schema).validate(settings["pylsp.configurationSources"])


def test_multiple_projects_do_not_change_global_settings(roots):
    shared, _ = roots
    first = make_project(shared)
    second = make_project(shared, "factor", "factor_b")
    config = config_for(shared)
    for project in (first, second, first, second):
        result = config.plugin_settings("jedi", document_path=str(project / "src/algo.py"))
        assert result["environment"] == str(project / ".venv/bin/python")
        assert result["extra_paths"] == [str(project / "src")]
    assert config._settings["plugins"]["jedi"] == {"environment": None, "extra_paths": []}


@pytest.mark.parametrize("suffix", ["research.ipynb", "research.ipynb.python.py", "research.ipynb.python-1(python).py"])
def test_notebook_virtual_documents_bind_original_project(roots, suffix):
    shared, virtual = roots
    project = make_project(shared)
    source = lsp.SoloProjectConfig(str(shared))
    path = virtual / project.relative_to(shared) / suffix
    assert source.project_config(str(path)) == source.project_config(str(project / "research.ipynb"))


def test_relative_virtual_directory_is_under_shared_root(roots):
    shared, _ = roots
    project = make_project(shared)
    source = lsp.SoloProjectConfig(str(shared), virtual_dir=".virtual_documents")
    path = shared / ".virtual_documents" / project.relative_to(shared) / "research.ipynb"
    assert source.project_path(path) == project


def test_file_uri_decodes_spaces_and_unicode(roots):
    shared, _ = roots
    project = make_project(shared, name="中文 项目")
    source = lsp.SoloProjectConfig(str(shared))
    assert source.project_path((project / "src/algo.py").as_uri()) == project


@pytest.mark.parametrize("path", [
    "vscode-notebook-cell:/shared/projects/model/model_a/research.ipynb",
    "https://example.test/shared/projects/model/model_a/algo.py",
    "file://remote/shared/projects/model/model_a/algo.py",
    "file:///shared/projects/model/model_a/algo.py?environment=/tmp/python",
    "relative/algo.py", "", None,
])
def test_nonlocal_or_unsupported_paths_do_not_bind(roots, path):
    shared, _ = roots
    source = lsp.SoloProjectConfig(str(shared))
    assert source.project_config(path) == {}


def test_outside_workspace_and_traversal_do_not_bind(roots):
    shared, virtual = roots
    project = make_project(shared)
    source = lsp.SoloProjectConfig(str(shared))
    for path in (
        shared.parent / "outside.py",
        shared / "runs/algo.py",
        shared / "projects-other/model/model_a/algo.py",
        project / "src/../algo.py",
        virtual / "../shared/projects/model/model_a/algo.py",
        (project / "src").as_uri() + "/%2e%2e/algo.py",
    ):
        assert source.project_config(str(path)) == {}


@pytest.mark.parametrize("file", [".solo", "pyproject.toml", ".venv/bin/python", ".venv/pyvenv.cfg"])
def test_missing_project_or_environment_files_fall_back(roots, file):
    shared, _ = roots
    project = make_project(shared)
    (project / file).unlink()
    assert lsp.SoloProjectConfig(str(shared)).project_config(str(project / "src/algo.py")) == {}


@pytest.mark.parametrize("metadata", ["broken", "[]", "null", "{}", '{"project_id": 42}', '{"project_id": "not-a-uuid"}'])
def test_invalid_metadata_falls_back(roots, metadata):
    shared, _ = roots
    project = make_project(shared)
    (project / ".solo").write_text(metadata)
    assert lsp.SoloProjectConfig(str(shared)).project_config(str(project / "src/algo.py")) == {}


def test_metadata_must_match_project_directory(roots):
    shared, _ = roots
    project = make_project(shared)
    marker = project / ".solo"
    data = json.loads(marker.read_text())
    data["name"] = "different_project"
    marker.write_text(json.dumps(data))
    assert lsp.SoloProjectConfig(str(shared)).project_config(str(project / "src/algo.py")) == {}


def test_oversized_metadata_does_not_bind(roots):
    shared, _ = roots
    project = make_project(shared)
    (project / ".solo").write_text(" " * 65537)
    assert lsp.SoloProjectConfig(str(shared)).project_config(str(project / "src/algo.py")) == {}


def symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("当前环境不支持符号链接")


def test_uv_managed_python_symlink_keeps_venv_path(roots, tmp_path):
    shared, _ = roots
    project = make_project(shared)
    managed = tmp_path / "managed-python"
    managed.write_text("#!/bin/sh\n")
    managed.chmod(0o755)
    python = project / ".venv/bin/python"
    python.unlink()
    symlink(python, managed)
    result = lsp.SoloProjectConfig(str(shared)).project_config(str(project / "src/algo.py"))
    assert result["plugins"]["jedi"]["environment"] == str(python)


def test_project_symlink_cannot_escape_shared_root(roots, tmp_path):
    shared, _ = roots
    outside = make_project(tmp_path / "outside")
    target = shared / "projects/model"
    target.mkdir(parents=True)
    symlink(target / "linked", outside, directory=True)
    assert lsp.SoloProjectConfig(str(shared)).project_config(str(target / "linked/src/algo.py")) == {}


def test_metadata_symlink_cannot_escape_project(roots, tmp_path):
    shared, _ = roots
    project = make_project(shared)
    marker = project / ".solo"
    outside = tmp_path / "other-marker"
    outside.write_text(marker.read_text())
    marker.unlink()
    symlink(marker, outside)
    assert lsp.SoloProjectConfig(str(shared)).project_config(str(project / "src/algo.py")) == {}


def test_missing_environment_is_rechecked_on_definition_request(roots):
    shared, _ = roots
    project = make_project(shared)
    python = project / ".venv/bin/python"
    python.unlink()
    config = config_for(shared)
    workspace = Workspace(uris.from_fs_path(str(shared)), Mock(), config)
    uri = uris.from_fs_path(str(project / "src/algo.py"))
    workspace.put_document(uri, "")
    document = workspace.get_document(uri)
    lsp.pylsp_document_did_open(config, workspace, document)
    assert config.plugin_settings("jedi", document_path=document.path)["environment"] is None
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    request = lsp.pylsp_definitions(config, workspace, document, {"line": 0, "character": 0})
    next(request)
    assert config.plugin_settings("jedi", document_path=document.path)["environment"] == str(python)
    with pytest.raises(StopIteration):
        next(request)


def test_refresh_invalidates_only_changed_project_environment(roots):
    shared, _ = roots
    first = make_project(shared)
    second = make_project(shared, "factor", "factor_b")
    config = config_for(shared)
    source = config._config_sources["solo"]
    workspace = SimpleNamespace(_environments={})
    document_path = str(first / "src/algo.py")
    source.refresh(config, workspace, document_path)
    first_python = str(first / ".venv/bin/python")
    second_python = str(second / ".venv/bin/python")
    workspace._environments = {first_python: "old first", second_python: "second"}
    source.refresh(config, workspace, document_path)
    assert workspace._environments[first_python] == "old first"
    (first / "uv.lock").write_text("version = 1\n")
    source.refresh(config, workspace, document_path)
    assert workspace._environments == {second_python: "second"}


@pytest.mark.parametrize("replacement", ["outside", "project-symlink", "other-project"])
def test_document_binding_is_rechecked_when_its_path_changes(roots, tmp_path, replacement):
    shared, _ = roots
    first = make_project(shared)
    second = make_project(shared, "factor", "factor_b")
    nested = first / "src/nested"
    nested.mkdir()
    target = first / "src/target.py"
    target.write_text("")
    alias = nested / "alias.py"
    symlink(alias, target)
    config = config_for(shared)
    workspace = Workspace(uris.from_fs_path(str(shared)), Mock(), config)
    workspace.put_document(alias.as_uri(), "")
    document = workspace.get_document(alias.as_uri())
    lsp.pylsp_document_did_open(config, workspace, document)
    first_python = str(first / ".venv/bin/python")
    second_python = str(second / ".venv/bin/python")
    second_environment = object()
    workspace._environments.update({first_python: object(), second_python: second_environment})
    assert config.plugin_settings("jedi", document_path=document.path)["environment"] == first_python
    source = config._config_sources["solo"]
    destination = second / "src/target.py" if replacement == "other-project" else tmp_path / "outside.py"
    destination.write_text("")
    if replacement == "other-project":
        source.refresh(config, workspace, str(destination))
        workspace._environments[second_python] = second_environment
        config.plugin_settings("jedi", document_path=document.path)
    if replacement == "project-symlink":
        moved = tmp_path / "moved-project"
        first.rename(moved)
        symlink(first, moved, directory=True)
    else:
        alias.unlink()
        symlink(alias, destination)

    request = lsp.pylsp_definitions(config, workspace, document, {"line": 0, "character": 0})
    next(request)
    result = config.plugin_settings("jedi", document_path=document.path)
    assert result["environment"] == (second_python if replacement == "other-project" else None)
    assert first_python not in workspace._environments
    assert workspace._environments[second_python] is second_environment
    with pytest.raises(StopIteration):
        next(request)

    # Returning to the original path must bind again without reopening the document.
    if replacement == "project-symlink":
        first.unlink()
        moved.rename(first)
    else:
        alias.unlink()
        symlink(alias, target)
    request = lsp.pylsp_definitions(config, workspace, document, {"line": 0, "character": 0})
    next(request)
    assert config.plugin_settings("jedi", document_path=document.path)["environment"] == first_python
    with pytest.raises(StopIteration):
        next(request)


def test_language_server_empty_and_builtin_updates_keep_all_workspaces_bound(roots):
    shared, _ = roots
    first = make_project(shared)
    second = make_project(shared, "factor", "factor_b")
    server = PythonLSPServer(None, None, consumer=Mock())
    server.lint = Mock()
    server.m_initialize(rootUri=uris.from_fs_path(str(shared)), workspaceFolders=[{
        "uri": uris.from_fs_path(str(second)), "name": "factor_b",
    }])
    for workspace in server.workspaces.values():
        config = workspace._config
        if config.plugin_manager.get_plugin("solo_project_environment") is None:
            config.plugin_manager.register(lsp, "solo_project_environment")
            lsp.pylsp_settings(config)
    lsp.pylsp_initialize(server.config, server.workspace)
    for settings in ({}, {"pylsp": {}}, {"pylsp": {"configurationSources": ["pycodestyle"]}}, {}):
        server.m_workspace__did_change_configuration(settings=settings)
        for project in (first, second):
            uri = uris.from_fs_path(str(project / "src/algo.py"))
            server.m_text_document__did_open(textDocument={"uri": uri, "text": "", "version": 1})
            document = server._match_uri_to_workspace(uri).get_document(uri)
            result = document._config.plugin_settings("jedi", document_path=document.path)
            assert result["environment"] == str(project / ".venv/bin/python")
            assert result["extra_paths"] == [str(project / "src")]
    server._endpoint.shutdown()


@pytest.mark.parametrize("client_settings", [None, {}, {"configurationSources": ["pycodestyle"]}])
def test_real_jedi_definitions_use_each_projects_site_packages(roots, client_settings):
    import sys
    import venv

    shared, virtual = roots
    projects = [make_project(shared), make_project(shared, "factor", "factor_b")]
    config = Config(uris.from_fs_path(str(shared)), {}, None, {})
    lsp.pylsp_settings(config)
    workspace = Workspace(uris.from_fs_path(str(shared)), Mock(), config)
    if client_settings is not None:
        config.update(client_settings)
        lsp.pylsp_workspace_configuration_changed(config, workspace)
    for index, project in enumerate(projects):
        python = project / ".venv/bin/python"
        python.unlink()
        venv.EnvBuilder(with_pip=False).create(project / ".venv")
        package = project / f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages/solo_binding_probe"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("\n" * index + "def project_symbol():\n    pass\n")
    for project in (projects[0], projects[1], projects[0]):
        for path in (project / "src/algo.py", virtual / project.relative_to(shared) / "research.ipynb"):
            uri = uris.from_fs_path(str(path))
            workspace.put_document(uri, "from solo_binding_probe import project_symbol\n")
            document = workspace.get_document(uri)
            lsp.pylsp_document_did_open(config, workspace, document)
            definitions = document.jedi_script().goto(1, 35, follow_imports=True)
            assert definitions
            assert definitions[0].module_path.is_relative_to(project / ".venv")


@pytest.mark.parametrize("hook_name, arguments", [
    ("pylsp_definitions", {"position": {"line": 0, "character": 0}}),
    ("pylsp_type_definition", {"position": {"line": 0, "character": 0}}),
    ("pylsp_completions", {"position": {"line": 0, "character": 0}, "ignored_names": set()}),
    ("pylsp_hover", {"position": {"line": 0, "character": 0}}),
    ("pylsp_references", {"position": {"line": 0, "character": 0}, "exclude_declaration": False}),
    ("pylsp_signature_help", {"position": {"line": 0, "character": 0}}),
    ("pylsp_lint", {"is_saved": False}),
    ("pylsp_document_symbols", {}),
    ("pylsp_document_highlight", {"position": {"line": 0, "character": 0}}),
    ("pylsp_rename", {"position": {"line": 0, "character": 0}, "new_name": "renamed"}),
])
@pytest.mark.parametrize("migrated", [False, True], ids=["original-owner", "migrated-document"])
def test_jedi_consumer_hooks_refresh_actual_owner_before_running(roots, hook_name, arguments, migrated):
    import inspect

    from pluggy import PluginManager
    from pylsp import hookimpl, hookspecs

    shared, _ = roots
    project = make_project(shared)
    other_project = make_project(shared, "factor", "factor_b")
    owner_config = config_for(shared)
    owner_workspace = Workspace(uris.from_fs_path(str(shared)), Mock(), owner_config)
    uri = uris.from_fs_path(str(project / "src/algo.py"))
    owner_workspace.put_document(uri, "")
    document = owner_workspace.get_document(uri)
    lsp.pylsp_document_did_open(owner_config, owner_workspace, document)
    python = str(project / ".venv/bin/python")
    other_python = str(other_project / ".venv/bin/python")
    stale_environment, other_environment, reloaded_environment = object(), object(), object()
    owner_workspace._environments.update({python: stale_environment, other_python: other_environment})
    if migrated:
        request_config = config_for(project)
        request_workspace = Workspace(uris.from_fs_path(str(project)), Mock(), request_config)
        request_workspace._docs[uri] = owner_workspace._docs.pop(uri)
        request_workspace._environments[python] = object()
    else:
        request_config, request_workspace = owner_config, owner_workspace
    routed_environments = request_workspace._environments.copy()

    # Register against the installed hookspecs, including type_definition's lack of workspace.
    manager = PluginManager("pylsp")
    manager.add_hookspecs(hookspecs)
    manager.register(lsp, "solo_project_environment")
    implementation = getattr(lsp, hook_name)
    specification = getattr(hookspecs, hook_name)
    assert list(inspect.signature(implementation).parameters) == list(inspect.signature(specification).parameters)
    hook = getattr(manager.hook, hook_name)
    wrapper = next(impl for impl in hook.get_hookimpls() if impl.plugin is lsp)
    assert wrapper.hookwrapper and wrapper.tryfirst
    observations = []
    result_marker = object()

    class Consumer:
        @hookimpl(specname=hook_name, tryfirst=True)
        def pylsp_cache_probe(self, document):
            assert document._workspace is owner_workspace
            assert document._config is owner_config
            observations.append((owner_workspace._environments.get(python), owner_config.settings.cache_info().currsize))
            return result_marker

    manager.register(Consumer())
    manager.check_pending()
    owner_config.settings(document_path=document.path)
    assert owner_config.settings.cache_info().currsize > 0
    (project / "uv.lock").write_text("version = 1\n")
    call_arguments = {"config": request_config, "document": document, **arguments}
    if "workspace" in inspect.signature(specification).parameters:
        call_arguments["workspace"] = request_workspace
    result = hook(**call_arguments)
    assert observations == [(None, 0)]
    assert owner_workspace._environments == {other_python: other_environment}
    if migrated:
        assert request_workspace._environments == routed_environments
    if hook.spec.opts["firstresult"]:
        assert result is result_marker
    else:
        assert result == [result_marker]

    # An unchanged request must keep the recreated environment and its settings cache.
    owner_workspace._environments[python] = reloaded_environment
    owner_config.settings(document_path=document.path)
    cached_settings = owner_config.settings.cache_info().currsize
    hook(**call_arguments)
    assert observations[-1] == (reloaded_environment, cached_settings)
    assert owner_workspace._environments[other_python] is other_environment
    if migrated:
        assert request_workspace._environments == routed_environments


@pytest.fixture
def editable_dependency(roots):
    import sys
    import venv

    shared, _ = roots
    project = make_project(shared)
    (project / ".venv/bin/python").unlink()
    venv.EnvBuilder(with_pip=False).create(project / ".venv")
    old_root, new_root = shared.parent / "old", shared.parent / "new"
    for root in (old_root, new_root):
        package = root / "solo_refresh_probe"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("class Probe:\n    pass\n")
    site = project / f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    pth = site / "_solo_editable_refresh.pth"
    pth.write_text(str(old_root) + "\n")
    return SimpleNamespace(
        project=project, pth=pth, old_root=old_root, new_root=new_root,
        old_uri=(old_root / "solo_refresh_probe/__init__.py").as_uri(),
        new_uri=(new_root / "solo_refresh_probe/__init__.py").as_uri(),
        uri=uris.from_fs_path(str(project / "src/check.py")),
        source="from solo_refresh_probe import Probe\nprobe = Probe()\nprobe\n",
    )


@pytest.fixture
def private_language_server(roots):
    shared, _ = roots
    server = PythonLSPServer(None, None, consumer=Mock())
    server.lint = Mock()
    try:
        server.m_initialize(rootUri=uris.from_fs_path(str(shared)), capabilities={
            "textDocument": {"completion": {"completionItem": {"snippetSupport": True}}},
        })
        config = server.config
        if config.plugin_manager.get_plugin("solo_project_environment") is None:
            config.plugin_manager.register(lsp, "solo_project_environment")
            lsp.pylsp_settings(config)
        server.m_workspace__did_change_configuration(settings={"pylsp": {
            "configurationSources": ["pycodestyle"],
            "plugins": {
                "ruff": {"enabled": False}, "jedi": {"auto_import_modules": []},
                "jedi_completion": {"include_params": True},
            },
        }})
        yield server
    finally:
        server._endpoint.shutdown()


@pytest.mark.parametrize("module_name", ["pandas", "numpy", "tensorflow", "matplotlib"])
@pytest.mark.parametrize("editable", [False, True], ids=["site-packages", "external-editable"])
def test_real_completion_refreshes_project_resolvers_only(roots, private_language_server, monkeypatch, module_name, editable):
    import sys
    import venv

    from pylsp.plugins import _resolvers

    # Keep the same resolver time bucket so expiry cannot hide stale signatures.
    monkeypatch.setattr(_resolvers, "time", lambda: 3600)
    resolvers = (_resolvers.LABEL_RESOLVER, _resolvers.SNIPPET_RESOLVER)
    assert all(module_name in resolver.cached_modules for resolver in resolvers)
    shared, _ = roots
    server = private_language_server
    # A shared path prefix must not make the other project part of the eviction.
    projects = [make_project(shared), make_project(shared, name="model_a_other")]
    modules, uris_to_complete = [], []
    for project in projects:
        (project / ".venv/bin/python").unlink()
        venv.EnvBuilder(with_pip=False).create(project / ".venv")
        site = project / f".venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
        dependency_root = shared.parent / "editable" / project.name if editable else site
        package = dependency_root / module_name
        package.mkdir(parents=True)
        if editable:
            (site / "_solo_completion.pth").write_text(str(dependency_root) + "\n")
        module = package / "__init__.py"
        module.write_text("def probe(old_argument, another_old):\n    pass\n\ndef probe_retired():\n    pass\n")
        modules.append(module)
        (project / "uv.lock").write_text("version = 1\n")
        uri = (project / "src/check.py").as_uri()
        uris_to_complete.append(uri)
        server.m_text_document__did_open(textDocument={
            "uri": uri, "text": f"import {module_name}\n{module_name}.pro", "version": 1,
        })

    def complete(index):
        response = server.m_text_document__completion(
            textDocument={"uri": uris_to_complete[index]},
            position={"line": 1, "character": len(module_name) + 4},
        )
        return next(item for item in response["items"] if item["label"].startswith("probe("))

    old_label = "probe(old_argument, another_old)"
    old_snippet = "probe(${1:old_argument}, ${2:another_old})$0"
    for index in (0, 1):
        item = complete(index)
        assert item["label"] == old_label
        assert item["insertText"] == old_snippet
        assert item["insertTextFormat"] == 2
    workspace = server.workspace
    pythons = [str(project / ".venv/bin/python") for project in projects]
    environments = [workspace._environments[python] for python in pythons]
    snapshots = [
        {key: value for key, value in resolver._cache.items() if key[1] in modules}
        for resolver in resolvers
    ]
    assert all(len(snapshot) == 4 for snapshot in snapshots)

    # Equal response text alone would not detect unnecessary eviction/recreation.
    for index in (0, 1, 0):
        assert complete(index)["label"] == old_label
    for resolver, snapshot in zip(resolvers, snapshots):
        assert all(resolver._cache[key] is value for key, value in snapshot.items())
    assert all(workspace._environments[python] is environment for python, environment in zip(pythons, environments))

    # Replace a dependency in place: probe retains its full name, path and line.
    modules[0].write_text("def probe(new_argument, second_new, third_new):\n    pass\n")
    (projects[0] / "uv.lock").write_text("version = 2\n")
    # Only the actual completion request may refresh caches after this change.
    item = complete(0)
    assert item["label"] == "probe(new_argument, second_new, third_new)"
    assert item["insertText"] == "probe(${1:new_argument}, ${2:second_new}, ${3:third_new})$0"
    assert item["insertTextFormat"] == 2
    refreshed_environment = workspace._environments[pythons[0]]
    assert refreshed_environment is not environments[0]
    assert workspace._environments[pythons[1]] is environments[1]

    for resolver, snapshot in zip(resolvers, snapshots):
        for key, value in snapshot.items():
            if key[1] == modules[1]:
                assert resolver._cache[key] is value
                assert key in resolver._cache_ttl[key[-1]]
            elif key[0] == f"{module_name}.probe":
                assert resolver._cache[key] is not value
                assert key in resolver._cache_ttl[key[-1]]
            else:
                assert key not in resolver._cache
                assert all(key not in bucket for bucket in resolver._cache_ttl.values())

    refreshed = [dict(resolver._cache) for resolver in resolvers]
    assert complete(0) == item
    other_item = complete(1)
    assert other_item["label"] == old_label
    assert other_item["insertText"] == old_snippet
    for resolver, snapshot in zip(resolvers, refreshed):
        assert resolver._cache.keys() == snapshot.keys()
        assert all(resolver._cache[key] is value for key, value in snapshot.items())
    assert workspace._environments[pythons[0]] is refreshed_environment
    assert workspace._environments[pythons[1]] is environments[1]

    # Let pylsp expire entries normally; retired keys must not cause a KeyError.
    monkeypatch.setattr(_resolvers, "time", lambda: 7200)
    assert complete(0) == item


def update_editable_dependency(dependency, *, touch_lockfile=True):
    dependency.pth.write_text(str(dependency.new_root) + "\n")
    if touch_lockfile:
        (dependency.project / "uv.lock").write_text("version = 2\n")


@pytest.mark.parametrize("touch_lockfile", [True, False], ids=["with-lock-change", "pth-only"])
def test_type_definition_first_request_refreshes_editable_dependency(private_language_server, editable_dependency, touch_lockfile):
    server, dependency = private_language_server, editable_dependency
    server.m_text_document__did_open(textDocument={"uri": dependency.uri, "text": dependency.source, "version": 1})
    arguments = {"textDocument": {"uri": dependency.uri}, "position": {"line": 2, "character": 2}}
    assert server.m_text_document__type_definition(**arguments)[0]["uri"] == dependency.old_uri
    workspace = server._match_uri_to_workspace(dependency.uri)
    python = str(dependency.project / ".venv/bin/python")
    old_environment = workspace._environments[python]
    assert str(dependency.old_root) in old_environment.get_sys_path()
    site_stamp = lsp.SoloProjectConfig._stamp(dependency.pth.parent)
    update_editable_dependency(dependency, touch_lockfile=touch_lockfile)
    if not touch_lockfile:
        assert not (dependency.project / "uv.lock").exists()
        assert lsp.SoloProjectConfig._stamp(dependency.pth.parent) == site_stamp
    assert str(dependency.old_root) in old_environment.get_sys_path()
    assert str(dependency.new_root) not in old_environment.get_sys_path()

    # No definition, reopen, or other wrapped request may refresh the environment first.
    assert server.m_text_document__type_definition(**arguments)[0]["uri"] == dependency.new_uri
    new_environment = workspace._environments[python]
    assert new_environment is not old_environment
    assert server.m_text_document__type_definition(**arguments)[0]["uri"] == dependency.new_uri
    assert workspace._environments[python] is new_environment


def test_workspace_folder_migration_refreshes_document_owner_without_reopen(private_language_server, editable_dependency):
    server, dependency = private_language_server, editable_dependency
    server.m_text_document__did_open(textDocument={"uri": dependency.uri, "text": dependency.source, "version": 1})
    arguments = {"textDocument": {"uri": dependency.uri}, "position": {"line": 0, "character": 32}}
    assert server.m_text_document__definition(**arguments)[0]["uri"] == dependency.old_uri
    old_workspace = server._match_uri_to_workspace(dependency.uri)
    document = old_workspace.get_document(dependency.uri)
    python = str(dependency.project / ".venv/bin/python")
    old_environment = old_workspace._environments[python]
    server.m_workspace__did_change_workspace_folders(event={
        "added": [{"uri": dependency.project.as_uri(), "name": dependency.project.name}], "removed": [],
    })
    new_workspace = server._match_uri_to_workspace(dependency.uri)
    assert new_workspace is not old_workspace
    assert new_workspace.get_document(dependency.uri) is document
    assert document._workspace is old_workspace and document._config is old_workspace._config
    assert old_workspace._environments[python] is old_environment
    update_editable_dependency(dependency)
    assert str(dependency.old_root) in old_environment.get_sys_path()
    assert str(dependency.new_root) not in old_environment.get_sys_path()

    assert server.m_text_document__definition(**arguments)[0]["uri"] == dependency.new_uri
    new_environment = old_workspace._environments[python]
    assert new_environment is not old_environment
    assert server.m_text_document__definition(**arguments)[0]["uri"] == dependency.new_uri
    assert old_workspace._environments[python] is new_environment
    assert new_workspace._environments == {}
    assert new_workspace.get_document(dependency.uri) is document
    assert document._workspace is old_workspace and document._config is old_workspace._config
