import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from modelscope_manager.media_proxy import AuthenticatedMediaProxy


class MediaProxyTests(unittest.TestCase):
    def test_private_stream_forwards_authentication_and_range_without_cache(self):
        payload = b"0123456789"
        observed = {}

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                observed["authorization"] = self.headers.get("Authorization")
                observed["cookie"] = self.headers.get("Cookie")
                observed["range"] = self.headers.get("Range")
                start = 3 if observed["range"] == "bytes=3-" else 0
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(payload) - start))
                self.send_header("Accept-Ranges", "bytes")
                if start:
                    self.send_header("Content-Range", f"bytes {start}-{len(payload)-1}/{len(payload)}")
                self.end_headers()
                self.wfile.write(payload[start:])

            def log_message(self, _format, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        proxy = AuthenticatedMediaProxy()
        try:
            source = f"http://127.0.0.1:{upstream.server_port}/video.mp4"
            with patch(
                "modelscope_manager.media_proxy.modelscope_token_headers",
                return_value={"Authorization": "Bearer secret-token", "Cookie": "m_session_id=secret-token"},
            ):
                request = urllib.request.Request(proxy.stream_url(source, "secret-token"), headers={"Range": "bytes=3-"})
                with urllib.request.urlopen(request, timeout=3) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), payload[3:])
            self.assertEqual(observed, {
                "authorization": "Bearer secret-token",
                "cookie": "m_session_id=secret-token",
                "range": "bytes=3-",
            })
        finally:
            proxy.stop()
            upstream.shutdown()
            upstream.server_close()

    def test_token_is_removed_before_cross_host_signed_redirect(self):
        observed = {}

        class Storage(BaseHTTPRequestHandler):
            def do_GET(self):
                observed["authorization"] = self.headers.get("Authorization")
                observed["cookie"] = self.headers.get("Cookie")
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, _format, *_args):
                pass

        storage = ThreadingHTTPServer(("127.0.0.1", 0), Storage)

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{storage.server_port}/signed")
                self.end_headers()

            def log_message(self, _format, *_args):
                pass

        redirect = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        for server in (storage, redirect):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        proxy = AuthenticatedMediaProxy()
        try:
            with patch(
                "modelscope_manager.media_proxy.modelscope_token_headers",
                return_value={"Authorization": "Bearer private", "Cookie": "m_session_id=private"},
            ):
                url = proxy.stream_url(f"http://127.0.0.1:{redirect.server_port}/file", "private")
                with urllib.request.urlopen(url, timeout=3) as response:
                    self.assertEqual(response.read(), b"ok")
            self.assertEqual(observed, {"authorization": None, "cookie": None})
        finally:
            proxy.stop()
            for server in (storage, redirect):
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
