import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest
from jinja2 import DictLoader, Environment
from jupyter_server.auth import Authorizer, IdentityProvider
from jupyter_server.services.contents.filemanager import FileContentsManager
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

import solo_jupyter
from solo_jupyter.files import FilesHandler


class ReadAuthorizer(Authorizer):
    def is_authorized(self, handler, user, action, resource):
        return action == "read" and resource == "contents" and handler.get_query_argument("deny", "") != "1"


class TestFilesHandler(AsyncHTTPTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="solo-files-test-")
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.report = self.workspace / "projects" / "因子 项目" / "reports"
        self.report.mkdir(parents=True)
        self.data = bytes(range(256)) * 1024
        (self.report / "报告.parquet").write_bytes(self.data)
        (self.report / "report.json").write_text('{"kind":"factor"}', encoding="utf-8")
        (self.workspace / ".private").write_text("hidden", encoding="utf-8")
        (self.workspace / "page.html").write_text("<script>window.probe = true</script>", encoding="utf-8")
        (self.workspace / "research.ipynb").write_text('{"cells": []}', encoding="utf-8")
        self.outside = Path(self.temporary.name) / "outside.txt"
        self.outside.write_text("outside sentinel", encoding="utf-8")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        self.temporary.cleanup()

    def get_app(self):
        self.identity = IdentityProvider(token="test-file-service-token")
        self.contents = FileContentsManager(root_dir=str(self.workspace))
        settings = {
            "base_url": "/prefix/",
            "default_url": "/prefix/lab",
            "login_url": "/prefix/login",
            "cookie_secret": b"test-cookie-secret",
            "identity_provider": self.identity,
            "authorizer": ReadAuthorizer(identity_provider=self.identity),
            "contents_manager": self.contents,
            "server_root_dir": str(self.workspace),
            "allow_unauthenticated_access": False,
            "allow_remote_access": False,
            "xsrf_cookies": True,
            "jinja2_env": Environment(loader=DictLoader({"error.html": "{{ status_code }} {{ status_message }}"})),
            "contents_js_source": "",
            "version_hash": "test",
            "jinja_template_vars": {},
        }
        return Application([
            (r"/prefix/solo/files/(.*)", FilesHandler, {"path": str(self.workspace)}),
        ], **settings)

    def read(self, path, *, method="GET", headers=None, authenticated=True):
        request_headers = {"Authorization": "token test-file-service-token"} if authenticated else {}
        request_headers.update(headers or {})
        return self.fetch("/prefix/solo/files/" + path, method=method,
                          headers=request_headers, follow_redirects=False)

    def report_path(self, filename="报告.parquet"):
        return quote("projects/因子 项目/reports/" + filename)

    def test_authenticated_read_streams_original_bytes(self):
        response = self.read(self.report_path())
        assert response.code == 200
        assert response.body == self.data
        assert response.headers["Content-Length"] == str(len(self.data))
        assert response.headers["Accept-Ranges"] == "bytes"
        assert "Content-Disposition" not in response.headers

    def test_file_responses_do_not_compute_content_versions(self):
        from unittest.mock import patch

        with patch.object(FilesHandler, "get_content_version", side_effect=AssertionError("File content version requested")):
            response = self.read(self.report_path())
        assert response.code == 200
        assert "Etag" not in response.headers

    def test_head_has_headers_without_file_body(self):
        response = self.read(self.report_path(), method="HEAD")
        assert response.code == 200
        assert response.body == b""
        assert response.headers["Content-Length"] == str(len(self.data))

    def test_json_can_be_read_inline(self):
        response = self.read(self.report_path("report.json"))
        assert response.code == 200
        assert response.headers["Content-Type"] == "application/json"
        assert json.loads(response.body) == {"kind": "factor"}

    def test_download_uses_encoded_unicode_filename(self):
        response = self.read(self.report_path() + "?download=1")
        assert response.code == 200
        assert response.body == self.data
        disposition = response.headers["Content-Disposition"]
        assert disposition.startswith("attachment;")
        assert "filename*=utf-8''" + quote("报告.parquet") in disposition

    def test_notebook_is_downloaded_as_attachment(self):
        response = self.read("research.ipynb")
        assert response.code == 200
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert response.headers["Content-Type"] == "application/x-ipynb+json"

    def test_ranges_use_original_tornado_behavior(self):
        response = self.read(self.report_path(), headers={"Range": "bytes=2-9"})
        assert response.code == 206
        assert response.body == self.data[2:10]
        assert response.headers["Content-Range"] == f"bytes 2-9/{len(self.data)}"
        suffix = self.read(self.report_path(), headers={"Range": "bytes=-4"})
        assert suffix.code == 206
        assert suffix.body == self.data[-4:]
        invalid = self.read(self.report_path(), headers={"Range": f"bytes={len(self.data) + 1}-"})
        assert invalid.code == 416
        assert invalid.headers["Content-Range"] == f"bytes */{len(self.data)}"

    def test_authentication_is_required_for_read_and_download(self):
        for path in (self.report_path(), self.report_path() + "?download=1"):
            for method in ("GET", "HEAD"):
                response = self.read(path, method=method, authenticated=False)
                assert response.code == 302
                assert response.headers["Location"].startswith("/prefix/login?")
                assert self.data not in response.body
        invalid = self.read(self.report_path(), headers={"Authorization": "token invalid"})
        assert invalid.code == 302

    def test_jupyter_authorizer_is_not_bypassed(self):
        for method in ("GET", "HEAD"):
            response = self.read(self.report_path() + "?deny=1", method=method)
            assert response.code == 403
            assert self.data not in response.body

    def test_hidden_files_follow_contents_manager_policy(self):
        response = self.read(".private")
        assert response.code == 404
        self.contents.allow_hidden = True
        response = self.read(".private")
        assert response.code == 200
        assert response.body == b"hidden"

    def test_missing_files_and_directories_are_not_served(self):
        for path in ("missing.bin", "projects", "projects/"):
            response = self.read(path)
            assert response.code in {403, 404}
            assert response.body != self.data

    def test_invalid_and_escaping_paths_are_rejected(self):
        for path in ("", "%2Foutside.txt", "../outside.txt", "%2e%2e/outside.txt",
                     "projects/%2e%2e/%2e%2e/outside.txt", "C%3A/outside.txt",
                     "projects%5Coutside.txt", "%00outside.txt"):
            response = self.read(path)
            assert response.code == 400, (path, response.code)
            assert b"outside sentinel" not in response.body

    def test_inline_html_keeps_jupyter_sandbox_and_nosniff(self):
        response = self.read("page.html")
        assert response.code == 200
        policy = response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'self'" in policy
        assert "sandbox allow-scripts" in policy
        assert response.headers["X-Content-Type-Options"] == "nosniff"

    def test_only_read_methods_are_supported(self):
        response = self.read(self.report_path(), method="DELETE")
        assert response.code == 405
        assert (self.report / "报告.parquet").read_bytes() == self.data

    def test_symlinks_stay_in_workspace_and_keep_hidden_policy(self):
        try:
            (self.workspace / "external.txt").symlink_to(self.outside)
            (self.workspace / "internal.parquet").symlink_to(self.report / "报告.parquet")
            (self.workspace / "hidden-alias.txt").symlink_to(self.workspace / ".private")
            (self.workspace / ".hidden-directory").symlink_to(self.report, target_is_directory=True)
        except OSError:
            pytest.skip("Creating symlinks requires permission on this platform")
        outside = self.read("external.txt")
        assert outside.code == 403
        assert b"outside sentinel" not in outside.body
        internal = self.read("internal.parquet")
        assert internal.code == 200
        assert internal.body == self.data
        hidden = self.read("hidden-alias.txt")
        assert hidden.code == 404
        hidden_directory = self.read(quote(".hidden-directory/报告.parquet"))
        assert hidden_directory.code == 404
        self.contents.allow_hidden = True
        assert self.read("hidden-alias.txt").body == b"hidden"
        assert self.read(quote(".hidden-directory/报告.parquet")).body == self.data


@pytest.mark.parametrize("base_url", ["/", "/users/research/"])
def test_server_startup_registers_file_handler_under_base_url(tmp_path, monkeypatch, base_url):
    web_app = SimpleNamespace(settings={"base_url": base_url}, add_handlers=Mock())
    server = SimpleNamespace(web_app=web_app, root_dir=str(tmp_path))
    solo_jupyter._load_jupyter_server_extension(server)
    web_app.add_handlers.assert_called_once()
    host, routes = web_app.add_handlers.call_args.args
    assert host == ".*$"
    files = next(route for route in routes if route[1] is FilesHandler)
    assert files[0] == base_url + "solo/files/(.*)"
    assert files[2] == {"path": str(tmp_path.resolve())}
    assert solo_jupyter._jupyter_server_extension_points() == [{"module": "solo_jupyter"}]
