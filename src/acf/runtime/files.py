#!/usr/bin/env python3
"""Rookgaard's file explorer, adapted for ACF with streamed file uploads."""

import argparse
import codecs
import contextlib
import errno
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import posixpath
import secrets
import stat
import sys
import threading
from urllib.parse import parse_qs, quote, unquote, urlsplit


PREVIEW_LIMIT = 256 * 1024
TEXT_LIMIT = 2 * 1024 * 1024
HOME_PATH = "/root"
DOWNLOADS_PATH = HOME_PATH + "/Downloads"
DIRECTORY_LIMIT = 10000
PAGE_LIMIT = 1000
MAX_HANDLERS = 8
VIRTUAL_ROOTS = (Path("/proc"), Path("/sys"), Path("/dev"))
# The host issues a new token at every boot and delivers it during Sync.
ACCESS_TOKEN_FILE = "/run/acf/access-token"
MIN_TOKEN_LENGTH = 32
APP_ASSETS = {
    "/assets/markdown-it.min.js": ("vendor/markdown-it.min.js", "text/javascript; charset=utf-8"),
}


class FileError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def clean_path(value):
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise FileError(400, "An absolute filesystem path is required")
    return posixpath.normpath("/" + value.lstrip("/"))


def kind_of(mode):
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def decode_text(data, truncated=False):
    if b"\0" in data:
        return None
    try:
        # A preview may end in the middle of a UTF-8 character.
        return codecs.getincrementaldecoder("utf-8")().decode(data, final=not truncated)
    except UnicodeDecodeError:
        return None


class FileExplorerServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, root=None, html_path=None, token_path=None):
        self.root = Path(root or os.environ.get("ACF_FILES_ROOT", "/")).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("File explorer root must be a directory")
        self.token_path = Path(token_path or os.environ.get("ACF_FILES_TOKEN_FILE", ACCESS_TOKEN_FILE))
        self.html_path = Path(html_path or Path(__file__).with_name("files.html"))
        self.html = self.html_path.read_bytes()
        # Serve only these bundled assets, never a requested filesystem path.
        self.assets = {
            route: ((Path(__file__).parent / relative).read_bytes(), content_type)
            for route, (relative, content_type) in APP_ASSETS.items()
        }
        # The local app has inline JS/CSS; user files are never served as
        # active documents under this origin.
        self.content_security_policy = (
            "default-src 'none'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        self.handler_slots = threading.BoundedSemaphore(MAX_HANDLERS)
        super().__init__(address, FileExplorerHandler)

    def process_request(self, request, client_address):
        if not self.handler_slots.acquire(blocking=False):
            # Do not create another worker (or wait for one) under load.
            try:
                request.settimeout(0.1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Connection: close\r\nContent-Length: 0\r\n"
                                b"Cache-Control: no-store\r\nRetry-After: 1\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.handler_slots.release()

    def access_token(self):
        """Read the token for every request; Sync may deliver or replace it after boot."""
        try:
            token = self.token_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return None
        return token if len(token) >= MIN_TOKEN_LENGTH else None

    def resolve(self, value):
        value = clean_path(value)
        try:
            physical = (self.root / value.lstrip("/")).resolve()
            physical.relative_to(self.root)
        except (ValueError, RuntimeError):
            raise FileError(403, "This path is outside the filesystem root")
        return value, physical

    def metadata(self, value):
        value, physical = self.resolve(value)
        info = physical.stat()
        original = self.root / value.lstrip("/")
        return {
            "path": value,
            "name": posixpath.basename(value) or "/",
            "kind": kind_of(info.st_mode),
            "size": info.st_size,
            "mtime": info.st_mtime,
            "symlink": original.is_symlink(),
        }

    def listing(self, value, offset=0, limit=250, directories=False):
        value, physical = self.resolve(value)
        if not stat.S_ISDIR(physical.stat().st_mode):
            raise FileError(400, "This path is not a directory")
        entries = []
        truncated = False
        with os.scandir(physical) as children:
            for index, child in enumerate(children):
                if index >= DIRECTORY_LIMIT:
                    truncated = True
                    break
                path = posixpath.join(value, child.name)
                try:
                    entry = self.metadata(path)
                except (OSError, FileError):
                    # Broken links, unreadable targets, and links outside a
                    # configured test root remain visible in the directory.
                    try:
                        info = child.stat(follow_symlinks=False)
                        entry = {"path": path, "name": child.name, "kind": "other",
                                 "size": info.st_size, "mtime": info.st_mtime,
                                 "symlink": child.is_symlink()}
                    except OSError:
                        continue
                if not directories or entry["kind"] == "directory":
                    entries.append(entry)
        entries.sort(key=lambda entry: (entry["kind"] != "directory",
                                        entry["name"].casefold(), entry["name"]))
        next_offset = offset + limit if offset + limit < len(entries) else None
        return {"path": value, "parent": posixpath.dirname(value) if value != "/" else None,
                "entries": entries[offset:offset + limit], "offset": offset,
                "next_offset": next_offset, "truncated": truncated}

    @staticmethod
    def check_content_path(physical):
        if any(physical == blocked or blocked in physical.parents for blocked in VIRTUAL_ROOTS):
            raise FileError(403, "Contents of virtual device and process files are not available")

    @contextlib.contextmanager
    def open_regular(self, value):
        value, physical = self.resolve(value)
        self.check_content_path(physical)
        # Check before opening, then again on the descriptor. O_NONBLOCK stops
        # a file replaced by a FIFO from hanging a worker; O_NOFOLLOW prevents
        # a final-component symlink replacement after resolve().
        if not stat.S_ISREG(physical.stat().st_mode):
            raise FileError(415, "Only regular files can be previewed or downloaded")
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(physical, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise FileError(415, "Only regular files can be previewed or downloaded")
            if sys.platform.startswith("linux"):
                actual = Path(os.readlink("/proc/self/fd/%d" % fd))
                self.check_content_path(actual)
                try:
                    actual.relative_to(self.root)
                except ValueError:
                    raise FileError(403, "This path is outside the filesystem root")
            with os.fdopen(fd, "rb") as handle:
                fd = None
                yield value, handle, info
        finally:
            if fd is not None:
                os.close(fd)

    def preview(self, value):
        with self.open_regular(value) as (value, handle, info):
            data = handle.read(PREVIEW_LIMIT + 1)
        truncated = len(data) > PREVIEW_LIMIT
        text = decode_text(data[:PREVIEW_LIMIT], truncated)
        return {"path": value, "name": posixpath.basename(value), "kind": "file",
                "size": info.st_size, "mtime": info.st_mtime, "text": text,
                "binary": text is None, "truncated": truncated,
                "copyable": text is not None and info.st_size <= TEXT_LIMIT,
                "download_url": "/download" + quote(value, safe="/", errors="surrogatepass")}

    def full_text(self, value):
        with self.open_regular(value) as (_, handle, info):
            if info.st_size > TEXT_LIMIT:
                raise FileError(413, "Copy is limited to text files up to 2 MiB; download this file instead")
            data = handle.read(TEXT_LIMIT + 1)
        if len(data) > TEXT_LIMIT:
            raise FileError(413, "Copy is limited to text files up to 2 MiB; download this file instead")
        text = decode_text(data)
        if text is None:
            raise FileError(415, "This file is binary or is not UTF-8 text; download it instead")
        return {"text": text}

    def check_directory_fd(self, fd):
        if sys.platform.startswith("linux"):
            actual = Path(os.readlink("/proc/self/fd/%d" % fd))
            self.check_content_path(actual)
            try:
                actual.relative_to(self.root)
            except ValueError:
                raise FileError(403, "This path is outside the filesystem root")

    @contextlib.contextmanager
    def open_directory(self, value, *, create=False):
        """Pin each path component so uploads cannot follow replaced symlinks."""
        value, physical = self.resolve(value)
        self.check_content_path(physical)
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.root, flags)
        try:
            for part in physical.relative_to(self.root).parts:
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                    except FileExistsError:
                        pass
                following = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = following
            self.check_directory_fd(fd)
            yield value, fd
        finally:
            os.close(fd)

    def configuration(self):
        with self.open_directory(DOWNLOADS_PATH, create=True):
            pass
        return {"home": HOME_PATH, "downloads": DOWNLOADS_PATH}

    def upload(self, directory, name, stream, length):
        if (not isinstance(name, str) or not name or name in (".", "..")
                or any(character in name for character in ("/", "\\", "\0"))
                or any(ord(character) < 32 or ord(character) == 127 for character in name)
                or len(name.encode("utf-8")) > 255):
            raise FileError(400, "Choose a filename without folders or control characters (up to 255 UTF-8 bytes)")
        with self.open_directory(directory) as (directory, fd):
            try:
                os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileError(409, "A file or folder with this name already exists; choose a different filename")
            temporary = ".acf-upload-" + secrets.token_hex(16)
            created = False
            try:
                upload_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                    | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=fd)
                created = True
                with os.fdopen(upload_fd, "wb") as output:
                    remaining = length
                    while remaining:
                        data = stream.read(min(65536, remaining))
                        if not data:
                            raise FileError(400, "Upload ended before the complete file arrived; no file was saved")
                        output.write(data)
                        remaining -= len(data)
                    output.flush()
                    os.fchmod(output.fileno(), 0o644)
                self.check_directory_fd(fd)
                # Linking publishes the complete file atomically and fails if
                # another upload or process has already created this name.
                try:
                    os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    raise FileError(409, "A file or folder with this name already exists; choose a different filename")
            finally:
                if created:
                    os.unlink(temporary, dir_fd=fd)
        return {"path": posixpath.join(directory, name), "size": length}


class FileExplorerHandler(BaseHTTPRequestHandler):
    server_version = "ACFFiles"
    sys_version = ""
    timeout = 15

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # A client may disconnect while an error response is being sent.
            self.close_connection = True

    def log_message(self, _format, *_args):
        # Keep requested file names and query strings out of service logs.
        pass

    def respond(self, status, body=b"", content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.common_headers()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def common_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", self.server.content_security_policy)

    def json_response(self, status, data):
        self.respond(status, json.dumps(data, ensure_ascii=True).encode("utf-8"))

    @staticmethod
    def argument(query, key, default=None):
        values = query.get(key, [default])
        if len(values) != 1:
            raise FileError(400, "Duplicate query parameter: " + key)
        return values[0]

    def authorized_route(self, path):
        """Strip the access token path prefix. Cookies are not used because they
        are shared by every port on a host, including other projects."""
        expected = self.server.access_token()
        if expected is None:
            raise FileError(503, "The file explorer is waiting for its access token; Sync this environment from the dashboard")
        token, separator, route = path[1:].partition("/")
        if not hmac.compare_digest(token.encode(), expected.encode()):
            raise FileError(403, "Open Files from the ACF dashboard; its link contains this environment's access token")
        return "/" + token, ("/" + route if separator else "")

    def download(self, value):
        with self.server.open_regular(value) as (value, handle, info):
            name = posixpath.basename(value)
            fallback = "".join(c if 32 <= ord(c) < 127 and c not in '\\"' else "_" for c in name)
            disposition = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
                fallback or "download", quote(name, safe="", errors="replace"))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(info.st_size))
            self.send_header("Content-Disposition", disposition)
            self.common_headers()
            self.end_headers()
            if self.command == "HEAD":
                return
            remaining = info.st_size
            while remaining:
                data = handle.read(min(65536, remaining))
                if not data:
                    self.close_connection = True
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
                raise FileError(400, "Invalid request path")
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
            if parsed.path.startswith("/assets/"):
                # Bundled libraries are public; they contain no project data.
                if parsed.path not in self.server.assets:
                    raise FileError(404, "Endpoint not found")
                body, content_type = self.server.assets[parsed.path]
                self.respond(200, body, content_type)
                return
            prefix, route = self.authorized_route(parsed.path)
            if route in ("", "/"):
                self.respond(303, headers={"Location": prefix + "/browse/"})
            elif route == "/browse" or route.startswith("/browse/"):
                # Directory and file URLs both serve the app. Files themselves
                # are only ever returned as JSON text or forced downloads.
                self.respond(200, self.server.html, "text/html; charset=utf-8")
            elif route == "/api/config":
                self.json_response(200, self.server.configuration())
            elif route == "/api/stat":
                self.json_response(200, self.server.metadata(self.argument(query, "path", "/")))
            elif route == "/api/list":
                try:
                    offset = int(self.argument(query, "offset", "0"))
                    limit = int(self.argument(query, "limit", "250"))
                except ValueError:
                    raise FileError(400, "Pagination values must be integers")
                if not 0 <= offset <= DIRECTORY_LIMIT or not 1 <= limit <= PAGE_LIMIT:
                    raise FileError(400, "Pagination values are out of range")
                result = self.server.listing(self.argument(query, "path", "/"), offset, limit,
                                             self.argument(query, "directories", "0") == "1")
                self.json_response(200, result)
            elif route == "/api/preview":
                self.json_response(200, self.server.preview(self.argument(query, "path", "/")))
            elif route == "/api/text":
                self.json_response(200, self.server.full_text(self.argument(query, "path", "/")))
            elif route.startswith("/download/"):
                self.download(unquote(route[len("/download"):], errors="surrogatepass"))
            else:
                raise FileError(404, "Endpoint not found")
        except FileError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except FileNotFoundError:
            self.json_response(404, {"error": "This path does not exist"})
        except (PermissionError, IsADirectoryError):
            self.json_response(403, {"error": "This path cannot be read"})
        except (NotADirectoryError, ValueError, UnicodeError):
            self.json_response(400, {"error": "Invalid path or query"})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except OSError:
            self.json_response(403, {"error": "This path could not be read"})

    do_HEAD = do_GET

    def upload_request_length(self):
        # A normal HTML form cannot supply this custom header or content type;
        # cross-origin script requests would require an unsupported preflight.
        if (self.headers.get_all("X-ACF-Upload") != ["1"]
                or self.headers.get_all("Content-Type") != ["application/octet-stream"]):
            raise FileError(415, "Uploads require application/octet-stream and X-ACF-Upload: 1")
        origins = self.headers.get_all("Origin", [])
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or not hosts[0]:
            raise FileError(400, "A single Host header is required")
        if origins:
            if len(origins) != 1:
                raise FileError(403, "Uploads must come from this file explorer")
            origin = urlsplit(origins[0])
            if (origin.scheme not in ("http", "https") or origin.netloc.lower() != hosts[0].lower()
                    or origin.path or origin.query or origin.fragment or origin.username):
                raise FileError(403, "Uploads must come from this file explorer")
        sites = self.headers.get_all("Sec-Fetch-Site", [])
        if sites and sites not in (["same-origin"], ["none"]):
            raise FileError(403, "Uploads must come from this file explorer")
        if self.headers.get_all("Transfer-Encoding"):
            raise FileError(400, "Uploads require a Content-Length instead of transfer encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise FileError(411, "Upload Content-Length is required")
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise FileError(400, "Upload Content-Length must be a nonnegative integer")
        return int(lengths[0])

    def do_POST(self):
        # Every upload uses a fresh connection, including rejected requests
        # whose body has not been consumed.
        self.close_connection = True
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
                raise FileError(400, "Invalid request path")
            if self.authorized_route(parsed.path)[1] != "/api/upload":
                self.method_not_allowed()
                return
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=20)
            length = self.upload_request_length()
            result = self.server.upload(self.argument(query, "path"), self.argument(query, "name"),
                                        self.rfile, length)
            self.json_response(201, result)
        except FileError as exc:
            self.json_response(exc.status, {"error": str(exc)})
        except FileNotFoundError:
            self.json_response(404, {"error": "The upload destination does not exist"})
        except (NotADirectoryError, IsADirectoryError):
            self.json_response(400, {"error": "Choose an existing folder as the upload destination"})
        except PermissionError:
            self.json_response(403, {"error": "Files cannot be uploaded to this folder"})
        except (ValueError, UnicodeError):
            self.json_response(400, {"error": "Invalid upload path, filename, or headers"})
        except TimeoutError:
            self.json_response(408, {"error": "Upload timed out; no file was saved"})
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, errno.EDQUOT):
                self.json_response(507, {"error": "There is not enough space to save this file"})
            else:
                self.json_response(403, {"error": "This file could not be uploaded"})

    def method_not_allowed(self):
        allow = "GET, HEAD, POST" if urlsplit(self.path).path.endswith("/api/upload") else "GET, HEAD"
        self.respond(405, b'{"error":"This method is not supported; use the upload endpoint to add files"}',
                     headers={"Allow": allow})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_TRACE = method_not_allowed


def main():
    try:
        os.nice(10)
    except OSError:
        pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("ACF_FILES_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ACF_FILES_PORT", "8081")))
    parser.add_argument("--root", default=os.environ.get("ACF_FILES_ROOT", "/"))
    args = parser.parse_args()
    server = FileExplorerServer((args.host, args.port), root=args.root)
    print("File explorer listening on %s:%s" % server.server_address, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
