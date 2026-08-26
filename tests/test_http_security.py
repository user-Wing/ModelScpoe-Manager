import unittest
from urllib.request import Request

from modelscope_manager.http_security import (
    SafeRedirectHandler,
    is_modelscope_url,
    modelscope_token_headers,
)


class HttpSecurityTests(unittest.TestCase):
    def test_credentials_are_only_created_for_official_modelscope_hosts(self):
        self.assertTrue(is_modelscope_url("https://www.modelscope.cn/api/v1/users/info"))
        self.assertTrue(is_modelscope_url("https://modelscope.ai/models/alice/demo"))
        self.assertFalse(is_modelscope_url("https://modelscope.cn.example.test/file"))
        self.assertEqual(
            modelscope_token_headers("https://modelscope.cn/file", "secret", include_session_cookie=True),
            {"Authorization": "Bearer secret", "Cookie": "m_session_id=secret"},
        )
        self.assertEqual(modelscope_token_headers("https://storage.example.test/file", "secret"), {})

    def test_cross_origin_redirect_removes_credentials_but_keeps_range(self):
        request = Request(
            "https://modelscope.cn/file",
            headers={
                "Authorization": "Bearer secret",
                "Cookie": "m_session_id=secret",
                "Proxy-Authorization": "Basic secret",
                "Range": "bytes=10-",
            },
        )
        redirected = SafeRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://storage.example.test/signed"
        )
        self.assertIsNotNone(redirected)
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertIsNone(redirected.get_header("Cookie"))
        self.assertIsNone(redirected.get_header("Proxy-Authorization"))
        self.assertEqual(redirected.get_header("Range"), "bytes=10-")


if __name__ == "__main__":
    unittest.main()
