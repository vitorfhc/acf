"""File explorer previews, downloads, and uploads in an isolated filesystem."""

import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import threading
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote, urlencode


ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "src/acf/runtime/files.py"
spec = importlib.util.spec_from_file_location("file_explorer_test_server", SERVICE)
explorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(explorer)


class FileExplorerDefaults(unittest.TestCase):
    def test_acf_service_starts_on_port_8081_without_credentials(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(explorer.sys, "argv", ["files.py"]), \
                mock.patch.object(explorer.os, "nice"), \
                mock.patch.object(explorer, "FileExplorerServer") as server, \
                contextlib.redirect_stdout(io.StringIO()):
            server.return_value.server_address = ("0.0.0.0", 8081)
            explorer.main()
        server.assert_called_once_with(("0.0.0.0", 8081), root="/")
        server.return_value.serve_forever.assert_called_once_with()
        server.return_value.server_close.assert_called_once_with()


class FileExplorerHTTP(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "filesystem"
        self.root.mkdir()
        self.html = self.base / "explorer.html"
        self.html.write_text("<!doctype html><title>File explorer fixture</title>")
        self.token = "fixture-access-token-0123456789abcdefABCDEF"
        self.token_path = self.base / "access-token"
        self.token_path.write_text(self.token + "\n")
        self.logs = io.StringIO()
        capture = contextlib.redirect_stderr(self.logs)
        capture.__enter__()
        self.addCleanup(capture.__exit__, None, None, None)
        self.server = explorer.FileExplorerServer(
            ("127.0.0.1", 0), root=self.root, html_path=self.html, token_path=self.token_path)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, path, method="GET", body=None, headers=None, token=True):
        # Every route except the public bundled assets lives below the token.
        if token and not path.startswith("/assets/"):
            path = "/" + self.token + path
        request_headers = dict(headers or {})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def api(self, name, path="/", **options):
        status, headers, body = self.request("/api/" + name + "?" + urlencode(dict(path=path, **options)))
        self.assertEqual(status, 200, body)
        self.assertIn("application/json", headers["content-type"])
        return json.loads(body)

    def upload(self, name, data=b"uploaded bytes", directory="/", token=True, **headers):
        return self.request("/api/upload?" + urlencode({"path": directory, "name": name}),
                            method="POST", body=data, token=token,
                            headers={"Content-Type": "application/octet-stream", "X-ACF-Upload": "1", **headers})

    def test_upload_preserves_bytes_in_selected_folder_and_encodes_filename(self):
        folder = self.root / "reports & notes"
        folder.mkdir()
        name = "résumé #100% + ?.bin"
        data = bytes(range(256)) * 1024
        status, headers, body = self.upload(name, data, directory="/reports & notes")
        self.assertEqual(status, 201, body)
        self.assertEqual(json.loads(body), {"path": "/reports & notes/" + name, "size": len(data)})
        self.assertEqual((folder / name).read_bytes(), data)
        self.assertEqual((folder / name).stat().st_mode & 0o777, 0o644)
        self.assertEqual([entry.name for entry in folder.iterdir()], [name])
        self.assertNotIn("set-cookie", headers)
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(self.request("/download" + quote("/reports & notes/" + name, safe="/"))[2], data)

    def test_empty_upload_and_same_origin_browser_headers(self):
        host, port = self.server.server_address
        status, _, body = self.upload("empty.txt", b"", **{
            "Origin": f"http://{host}:{port}", "Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 201, body)
        self.assertEqual(json.loads(body)["size"], 0)
        self.assertEqual((self.root / "empty.txt").read_bytes(), b"")

    def test_downloads_configuration_creates_home_folder_and_accepts_upload(self):
        self.assertFalse((self.root / "root/Downloads").exists())
        configuration = self.api("config")
        self.assertEqual(configuration, {"home": "/root", "downloads": "/root/Downloads"})
        self.assertTrue((self.root / "root/Downloads").is_dir())
        self.assertEqual(self.api("config"), configuration)
        status, _, body = self.upload("received.txt", directory=configuration["downloads"])
        self.assertEqual(status, 201, body)
        self.assertEqual((self.root / "root/Downloads/received.txt").read_bytes(), b"uploaded bytes")

    def test_downloads_cannot_be_created_through_an_outside_symlink(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "root").symlink_to(outside)
        status, _, _ = self.request("/api/config")
        self.assertEqual(status, 403)
        self.assertFalse((outside / "Downloads").exists())

    def test_upload_refuses_to_overwrite_files_folders_or_symlinks(self):
        (self.root / "existing.txt").write_bytes(b"original")
        (self.root / "folder").mkdir()
        (self.root / "linked").symlink_to(self.root / "existing.txt")
        for name in ("existing.txt", "folder", "linked"):
            with self.subTest(name=name):
                status, _, body = self.upload(name)
                self.assertEqual(status, 409, body)
                self.assertIn("already exists", json.loads(body)["error"])
        self.assertEqual((self.root / "existing.txt").read_bytes(), b"original")
        self.assertTrue((self.root / "linked").is_symlink())
        self.assertEqual(list(self.root.glob(".acf-upload-*")), [])

    def test_upload_requires_explicit_request_headers_and_rejects_cross_origin(self):
        for headers, expected in (({"Content-Type": "text/plain"}, 415),
                                  ({"X-ACF-Upload": "0"}, 415),
                                  ({"Origin": "https://foreign.example"}, 403),
                                  ({"Origin": "null"}, 403),
                                  ({"Sec-Fetch-Site": "cross-site"}, 403),
                                  ({"Sec-Fetch-Site": "same-site"}, 403),
                                  ({"Transfer-Encoding": "chunked"}, 400)):
            with self.subTest(headers=headers):
                status, _, body = self.upload("blocked.txt", **headers)
                self.assertEqual(status, expected, body)
                self.assertFalse((self.root / "blocked.txt").exists())
        status, _, _ = self.request("/api/upload?path=/&name=blocked.txt", method="POST", body=b"data")
        self.assertEqual(status, 415)
        status, headers, _ = self.request("/api/upload", method="OPTIONS", headers={
            "Origin": "https://foreign.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-acf-upload,content-type"})
        self.assertEqual(status, 405)
        self.assertNotIn("access-control-allow-origin", headers)

    def test_upload_rejects_path_in_filename_and_missing_destination(self):
        for name in ("", ".", "..", "../escape", "/absolute", "sub/name", "sub\\name", "null\0.txt",
                     "line\n.txt", "x" * 256):
            with self.subTest(name=name):
                self.assertEqual(self.upload(name)[0], 400)
        self.assertEqual(self.upload("file.txt", directory="relative")[0], 400)
        self.assertEqual(self.upload("file.txt", directory="/missing")[0], 404)
        (self.root / "regular.txt").write_text("regular")
        self.assertEqual(self.upload("file.txt", directory="/regular.txt")[0], 400)
        self.assertFalse((self.base / "escape").exists())
        self.assertEqual(list(self.root.glob(".acf-upload-*")), [])

    def test_upload_rejects_outside_and_virtual_destination(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.root / "escape").symlink_to(outside)
        self.assertEqual(self.upload("outside.txt", directory="/escape")[0], 403)
        self.assertEqual(list(outside.iterdir()), [])
        blocked = self.root / "virtual"
        blocked.mkdir()
        with mock.patch.object(explorer, "VIRTUAL_ROOTS", (blocked,)):
            self.assertEqual(self.upload("blocked.txt", directory="/virtual")[0], 403)
        self.assertEqual(list(blocked.iterdir()), [])

    def test_upload_rejects_invalid_content_lengths(self):
        for length in ("-1", "no", "+1", "1, 1"):
            with self.subTest(length=length):
                self.assertEqual(self.upload("bad.txt", b"x", **{"Content-Length": length})[0], 400)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_upload_streams_past_previous_size_limit_without_large_allocations(self):
        length = 256 * 1024 * 1024 + 65537
        handler = object.__new__(explorer.FileExplorerHandler)
        handler.headers = http.client.parse_headers(io.BytesIO(
            ("Host: localhost\r\nContent-Type: application/octet-stream\r\n"
             "X-ACF-Upload: 1\r\nContent-Length: %d\r\n\r\n" % length).encode()))
        self.assertEqual(handler.upload_request_length(), length)
        reads = []
        class GeneratedBody:
            remaining = length
            def read(stream, size):
                reads.append(size)
                self.assertLessEqual(size, 65536)
                count = min(size, stream.remaining)
                stream.remaining -= count
                return b"x" * count
        # The real upload loop and final publication run, but a sparse output
        # avoids writing 256 MiB to disk solely to test the removed size cap.
        original_fdopen = os.fdopen
        class SparseOutput:
            def __init__(output, fd, mode):
                output.file = original_fdopen(fd, mode)
            def __enter__(output):
                return output
            def __exit__(output, *args):
                output.file.close()
            def write(output, data):
                output.file.seek(len(data), os.SEEK_CUR)
            def flush(output):
                output.file.truncate()
                output.file.flush()
            def fileno(output):
                return output.file.fileno()
        stream = GeneratedBody()
        with mock.patch.object(explorer.os, "fdopen", SparseOutput):
            result = self.server.upload("/", "large.bin", stream, length)
        self.assertEqual(result, {"path": "/large.bin", "size": length})
        self.assertEqual((self.root / "large.bin").stat().st_size, length)
        self.assertEqual(stream.remaining, 0)
        self.assertGreater(len(reads), 1)
        self.assertEqual(list(self.root.glob(".acf-upload-*")), [])

    def raw_upload(self, headers, data=b""):
        connection = socket.create_connection(self.server.server_address, timeout=3)
        self.addCleanup(connection.close)
        head = (f"POST /{self.token}/api/upload?path=/&name=partial.txt HTTP/1.1\r\n"
                "Host: localhost\r\nContent-Type: application/octet-stream\r\n"
                "X-ACF-Upload: 1\r\n" + headers + "\r\n")
        connection.sendall(head.encode("ascii") + data)
        connection.shutdown(socket.SHUT_WR)
        response = http.client.HTTPResponse(connection)
        response.begin()
        status, body = response.status, response.read()
        response.close()
        return status, body

    def test_upload_requires_one_content_length_and_cleans_truncated_file(self):
        for headers, data, expected in (("", b"", 411),
                                         ("Content-Length: 1\r\nContent-Length: 1\r\n", b"x", 400),
                                         ("Content-Length: 10\r\n", b"partial", 400)):
            with self.subTest(headers=headers):
                status, body = self.raw_upload(headers, data)
                self.assertEqual(status, expected, body)
                self.assertEqual(list(self.root.iterdir()), [])

    def test_upload_does_not_replace_file_created_during_upload(self):
        target = self.root / "racing.txt"
        class RacingBody(io.BytesIO):
            def read(stream, size):
                target.write_bytes(b"created elsewhere")
                return super().read(size)
        with self.assertRaises(explorer.FileError) as error:
            self.server.upload("/", "racing.txt", RacingBody(b"uploaded"), 8)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(target.read_bytes(), b"created elsewhere")
        self.assertEqual(list(self.root.glob(".acf-upload-*")), [])

    ENDPOINTS = ("/browse/", "/browse/notes.txt", "/api/config", "/api/list?path=/",
                 "/api/stat?path=/notes.txt", "/api/preview?path=/notes.txt",
                 "/api/text?path=/notes.txt", "/download/notes.txt")

    def test_explorer_and_file_endpoints_work_below_the_access_token_without_cookies(self):
        (self.root / "notes.txt").write_text("fixture contents")
        for path in self.ENDPOINTS:
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 200)
                self.assertNotIn("set-cookie", headers)
                self.assertIn("no-store", headers.get("cache-control", ""))
                self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        for path in ("/", ""):
            with self.subTest(path=path):
                status, headers, _ = self.request(path)
                self.assertEqual(status, 303)
                self.assertEqual(headers["location"], "/" + self.token + "/browse/")
        self.assertEqual(self.request("/download/notes.txt")[2], b"fixture contents")

    def test_missing_or_wrong_token_is_rejected_for_every_endpoint(self):
        (self.root / "notes.txt").write_text("fixture contents")
        wrong = "x" * len(self.token)
        for prefix in ("", "/" + wrong, "/" + self.token[:-1], "/" + self.token + "x"):
            for path in ("/", *self.ENDPOINTS):
                with self.subTest(prefix=prefix, path=path):
                    status, headers, body = self.request(prefix + path, token=False)
                    self.assertEqual(status, 403, body)
                    self.assertNotIn(b"fixture contents", body)
                    self.assertNotIn("set-cookie", headers)
        status, _, _ = self.upload("blocked.txt", token=False)
        self.assertEqual(status, 403)
        self.assertFalse((self.root / "blocked.txt").exists())

    def test_query_tokens_and_cookies_do_not_grant_access(self):
        # Cookies reach every port on a host, so a token there would leak to other projects.
        status, headers, _ = self.request("/browse/?token=" + self.token, token=False,
                                          headers={"Cookie": "sandbox_files=" + self.token})
        self.assertEqual(status, 403)
        self.assertNotIn("set-cookie", headers)
        self.assertNotIn(self.token, self.logs.getvalue())

    def test_explorer_fails_closed_until_a_token_is_delivered_and_reads_replacements(self):
        self.token_path.unlink()
        status, _, body = self.request("/api/list?path=/")
        self.assertEqual(status, 503, body)
        self.token_path.write_text("too-short\n")
        self.assertEqual(self.request("/api/list?path=/")[0], 503)
        replacement = "replacement-access-token-0123456789abcdefAB"
        self.token_path.write_text(replacement + "\n")
        self.assertEqual(self.request("/api/list?path=/")[0], 403)
        self.assertEqual(self.request("/" + replacement + "/api/list?path=/", token=False)[0], 200)
        # Bundled libraries stay public so the page can load before the token arrives.
        self.token_path.unlink()
        self.assertEqual(self.request("/assets/markdown-it.min.js")[0], 200)

    def test_directory_urls_reload_and_encoded_names_round_trip(self):
        folder = self.root / "notes & sources #1"
        folder.mkdir()
        filename = "évidence 100% + ?.txt"
        (folder / filename).write_text("source evidence\n")
        virtual_folder = "/" + folder.name
        virtual_file = virtual_folder + "/" + filename
        for path in ("/browse/", "/browse" + quote(virtual_folder, safe="/") + "/",
                     "/browse" + quote(virtual_file, safe="/")):
            with self.subTest(path=path):
                status, headers, body = self.request(path)
                self.assertEqual(status, 200)
                self.assertIn("text/html", headers["content-type"])
                self.assertIn(b"File explorer fixture", body)
        listing = self.api("list", virtual_folder)
        self.assertEqual(listing["path"], virtual_folder)
        self.assertEqual(listing["parent"], "/")
        entry, = listing["entries"]
        self.assertEqual((entry["name"], entry["path"], entry["kind"]),
                         (filename, virtual_file, "file"))
        self.assertEqual(self.api("preview", virtual_file)["text"], "source evidence\n")

    def test_bundled_markdown_parser_is_available_and_uses_safe_headers(self):
        route = "/assets/markdown-it.min.js"
        status, headers, body = self.request(route)
        self.assertEqual(status, 200)
        self.assertEqual(body, (SERVICE.parent / "vendor/markdown-it.min.js").read_bytes())
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["content-security-policy"], self.server.content_security_policy)
        status, headers, head_body = self.request(route, method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(int(headers["content-length"]), len(body))
        self.assertEqual(head_body, b"")

    def test_asset_route_does_not_serve_unlisted_files_or_allow_traversal(self):
        assets = self.root / "assets"
        assets.mkdir()
        (assets / "custom.js").write_text("untrusted script fixture")
        for route in ("/assets/custom.js", "/assets/markdown-it.LICENSE",
                      "/assets/../file-explorer.py", "/assets/%2e%2e/file-explorer.py",
                      "/assets/..%2ffile-explorer.py", "/assets/markdown-it.min.js/extra",
                      "/assets/%6darkdown-it.min.js"):
            with self.subTest(route=route):
                status, headers, body = self.request(route)
                self.assertEqual(status, 404)
                self.assertIn("application/json", headers["content-type"])
                self.assertNotIn(b"untrusted script fixture", body)

    def test_directory_pagination_and_lazy_tree_filter(self):
        for name in ("alpha", "beta", "gamma"):
            (self.root / name).mkdir()
        for name in ("one.txt", "two.txt", "three.txt"):
            (self.root / name).write_text(name)
        first = self.api("list", limit=2)
        self.assertEqual(len(first["entries"]), 2)
        self.assertFalse(first["truncated"])
        self.assertEqual(first["next_offset"], 2)
        entries = list(first["entries"])
        page = first
        while page["next_offset"] is not None:
            page = self.api("list", offset=page["next_offset"], limit=2)
            entries.extend(page["entries"])
        self.assertEqual({entry["name"] for entry in entries},
                         {"alpha", "beta", "gamma", "one.txt", "two.txt", "three.txt"})
        self.assertEqual(len(entries), 6)
        directories = self.api("list", directories=1)
        self.assertEqual({entry["name"] for entry in directories["entries"]},
                         {"alpha", "beta", "gamma"})
        self.assertTrue(all(entry["kind"] == "directory" for entry in directories["entries"]))
        with mock.patch.object(explorer, "DIRECTORY_LIMIT", 3):
            bounded = self.api("list")
        self.assertEqual(len(bounded["entries"]), 3)
        self.assertTrue(bounded["truncated"])
        self.assertIsNone(bounded["next_offset"])

    def test_text_preview_is_bounded_without_cutting_utf8_into_binary(self):
        contents = "A" * (explorer.PREVIEW_LIMIT - 1) + "é" * 10
        (self.root / "unicode.txt").write_text(contents)
        result = self.api("preview", "/unicode.txt")
        self.assertTrue(result["truncated"])
        self.assertFalse(result["binary"])
        self.assertLessEqual(len(result["text"].encode()), explorer.PREVIEW_LIMIT)
        self.assertTrue(contents.startswith(result["text"]))
        self.assertTrue(result["copyable"])
        self.assertEqual(self.api("text", "/unicode.txt")["text"], contents)

    def test_full_text_copy_has_a_size_limit_and_binary_is_not_text(self):
        (self.root / "large.txt").write_bytes(b"x" * (explorer.TEXT_LIMIT + 1))
        result = self.api("preview", "/large.txt")
        self.assertTrue(result["truncated"])
        self.assertFalse(result["copyable"])
        status, _, _ = self.request("/api/text?path=/large.txt")
        self.assertEqual(status, 413)
        binary = bytes(range(256))
        (self.root / "payload.bin").write_bytes(binary)
        preview = self.api("preview", "/payload.bin")
        self.assertTrue(preview["binary"])
        self.assertFalse(preview["copyable"])
        status, _, _ = self.request("/api/text?path=/payload.bin")
        self.assertEqual(status, 415)
        status, headers, body = self.request("/download/payload.bin")
        self.assertEqual(status, 200)
        self.assertEqual(body, binary)
        self.assertIn("attachment", headers["content-disposition"])

    def test_download_preserves_bytes_and_cannot_inject_headers_or_render_html(self):
        filename = 'résumé "quoted"\r\nX-Injection: yes.html'
        payload = b"<script>alert('fixture')</script>\n" + bytes(range(256)) * 4096
        (self.root / filename).write_bytes(payload)
        url = "/download/" + quote(filename, safe="")
        status, headers, body = self.request(url)
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)
        self.assertEqual(int(headers["content-length"]), len(payload))
        self.assertTrue(headers["content-disposition"].lower().startswith("attachment;"))
        self.assertIn("filename*=utf-8''", headers["content-disposition"].lower())
        self.assertNotIn("\r", headers["content-disposition"])
        self.assertNotIn("\n", headers["content-disposition"])
        self.assertNotIn("x-injection", headers)
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertIn("no-store", headers.get("cache-control", ""))
        head_status, head_headers, head_body = self.request(url, method="HEAD")
        self.assertEqual(head_status, 200)
        self.assertEqual(int(head_headers["content-length"]), len(payload))
        self.assertEqual(head_body, b"")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires Unix named pipes")
    def test_special_files_are_listed_but_not_opened_or_downloaded(self):
        os.mkfifo(self.root / "waiting-pipe")
        self.assertEqual(self.api("stat", "/waiting-pipe")["kind"], "other")
        entry, = self.api("list")["entries"]
        self.assertEqual(entry["kind"], "other")
        for path in ("/api/preview?path=/waiting-pipe", "/api/text?path=/waiting-pipe",
                     "/download/waiting-pipe"):
            with self.subTest(path=path):
                status, _, _ = self.request(path)
                self.assertIn(status, (403, 415))

    def test_symlinks_cannot_escape_the_configured_root(self):
        outside = self.base / "outside.txt"
        outside.write_text("outside root fixture secret")
        (self.root / "escape").symlink_to(outside)
        (self.root / "inside.txt").write_text("inside root")
        (self.root / "shortcut").symlink_to(self.root / "inside.txt")
        self.assertEqual(self.api("preview", "/shortcut")["text"], "inside root")
        for path in ("/api/preview?path=/escape", "/api/text?path=/escape", "/download/escape",
                     "/api/preview?path=/../outside.txt", "/download/../outside.txt"):
            with self.subTest(path=path):
                status, _, body = self.request(path)
                self.assertIn(status, (400, 403, 404))
                self.assertNotIn(outside.read_bytes(), body)

    def test_non_upload_modifying_methods_are_rejected_and_missing_files_are_404(self):
        target = self.root / "kept.txt"
        target.write_text("original")
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                status, _, _ = self.request("/download/kept.txt", method=method, body=b"changed")
                self.assertEqual(status, 405)
                self.assertEqual(target.read_text(), "original")
        for path in ("/api/stat?path=/missing", "/api/list?path=/missing",
                     "/api/preview?path=/missing", "/download/missing"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)


if __name__ == "__main__":
    unittest.main()
