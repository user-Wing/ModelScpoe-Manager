from __future__ import annotations

import secrets
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .http_security import modelscope_token_headers


@dataclass
class _Route:
    url: str
    token: str
    touched: float


class AuthenticatedMediaProxy:
    """A loopback-only, no-cache HTTP range proxy for private media playback."""

    def __init__(self):
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._routes: dict[str, _Route] = {}
        self._lock = threading.Lock()

    def stream_url(self, url: str, token: str) -> str:
        self.start()
        route_id = secrets.token_urlsafe(24)
        with self._lock:
            now = time.monotonic()
            self._routes = {
                key: route for key, route in self._routes.items()
                if now - route.touched < 12 * 3600
            }
            self._routes[route_id] = _Route(url, token, now)
        return f"http://127.0.0.1:{self._server.server_port}/media/{route_id}"

    def start(self) -> None:
        if self._server:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_HEAD(self):
                self._relay(False)

            def do_GET(self):
                self._relay(True)

            def _relay(self, body: bool) -> None:
                route_id = self.path.partition("?")[0].removeprefix("/media/")
                with owner._lock:
                    route = owner._routes.get(route_id)
                    if route:
                        route.touched = time.monotonic()
                if route is None:
                    self.send_error(404)
                    return
                headers = modelscope_token_headers(route.url, route.token, include_session_cookie=True)
                headers["User-Agent"] = "ModelScope-Manager/1.0 PotPlayer"
                if self.headers.get("Range"):
                    headers["Range"] = self.headers["Range"]
                request = urllib.request.Request(
                    route.url, headers=headers, method="GET" if body else "HEAD",
                )
                try:
                    response = _SAFE_OPENER.open(request, timeout=30)
                except urllib.error.HTTPError as exc:
                    response = exc
                except Exception as exc:
                    self.send_error(502, str(exc))
                    return
                try:
                    self.send_response(response.status)
                    for name in (
                        "Content-Type", "Content-Length", "Content-Range",
                        "Accept-Ranges", "Last-Modified", "ETag",
                    ):
                        value = response.headers.get(name)
                        if value:
                            self.send_header(name, value)
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    if body and response.status < 400:
                        while True:
                            chunk = response.read(256 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    response.close()

            def log_message(self, _format: str, *_args) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, name="3fp-media-proxy", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
        self._thread = None
        with self._lock:
            self._routes.clear()


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep Range on signed-storage redirects but never leak the account token."""

    def redirect_request(self, request, fp, code, msg, headers, new_url):
        redirected = super().redirect_request(request, fp, code, msg, headers, new_url)
        if redirected is not None and urlsplit(request.full_url).netloc != urlsplit(new_url).netloc:
            redirected.remove_header("Authorization")
            redirected.remove_header("Cookie")
        return redirected


_SAFE_OPENER = urllib.request.build_opener(_SafeRedirectHandler())
