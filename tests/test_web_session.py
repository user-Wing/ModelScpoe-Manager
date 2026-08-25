import unittest
from unittest.mock import patch

from modelscope_manager.web_session import (
    ModelScopeWebSession,
    delete_dataset_file,
    delete_repository_file,
    delete_repository_files,
    fetch_web_user_info,
    list_repository_file_paths,
    web_session_username,
)


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.body


class WebSessionTests(unittest.TestCase):
    def setUp(self):
        self.session = ModelScopeWebSession("web", "csrf-session", "csrf-token%3D")

    def test_csrf_header_is_decoded_and_cookie_value_is_preserved(self):
        self.assertEqual(self.session.headers()["X-CSRF-TOKEN"], "csrf-token=")
        self.assertEqual(self.session.cookies()["csrf_token"], "csrf-token%3D")

    @patch("modelscope_manager.web_session.requests.get")
    def test_user_info_validates_saved_web_session(self, request):
        request.return_value = FakeResponse({"Code": 200, "Success": True, "Data": {"Username": "alice"}})
        self.assertEqual(fetch_web_user_info(self.session)["Username"], "alice")
        kwargs = request.call_args.kwargs
        self.assertEqual(kwargs["cookies"]["m_session_id"], "web")
        self.assertEqual(kwargs["headers"]["Origin"], "https://www.modelscope.cn")

    @patch("modelscope_manager.web_session.requests.delete")
    def test_dataset_delete_uses_browser_endpoint_and_normalized_path(self, request):
        request.return_value = FakeResponse({"Code": 200, "Message": "success"})
        result = delete_dataset_file(self.session, "alice/demo", "/folder/file name.txt")
        self.assertEqual(result["Code"], 200)
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://www.modelscope.cn/api/v1/datasets/alice/demo/repo")
        self.assertEqual(kwargs["params"], {"FilePath": "folder/file name.txt"})
        self.assertEqual(kwargs["headers"]["X-CSRF-TOKEN"], "csrf-token=")

    @patch("modelscope_manager.web_session.requests.delete")
    def test_model_delete_uses_file_endpoint_and_revision(self, request):
        request.return_value = FakeResponse({"Code": 200})
        delete_repository_file(self.session, "alice/model", "model", "weights/a.bin")
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://www.modelscope.cn/api/v1/models/alice/model/file")
        self.assertEqual(kwargs["params"], {"FilePath": "weights/a.bin", "Revision": "master"})

    @patch("modelscope_manager.web_session.requests.post")
    def test_batch_delete_uses_one_commit_with_all_paths(self, request):
        request.return_value = FakeResponse({"Code": 200, "Success": True})
        delete_repository_files(self.session, "alice/demo", "dataset", ["folder/a.txt", "/folder/b.txt"])
        args, kwargs = request.call_args
        self.assertEqual(args[0], "https://www.modelscope.cn/api/v1/repos/datasets/alice/demo/commit/master")
        self.assertEqual(
            [action["path"] for action in kwargs["json"]["actions"]],
            ["folder/a.txt", "folder/b.txt"],
        )

    @patch("modelscope_manager.web_session.requests.get")
    def test_directory_listing_uses_root_and_pagination(self, request):
        request.side_effect = [
            FakeResponse({"Code": 200, "Data": {"Files": [{"Path": f"test/f{i}.txt"} for i in range(100)]}}),
            FakeResponse({"Code": 200, "Data": {"Files": [{"Path": "test/last.txt"}]}}),
        ]
        paths = list_repository_file_paths(self.session, "alice/demo", "dataset", "test")
        self.assertEqual(len(paths), 101)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["params"]["Root"], "test")
        self.assertEqual(request.call_args_list[1].kwargs["params"]["PageNumber"], 2)

    def test_username_supports_current_lowercase_user_info_shape(self):
        self.assertEqual(web_session_username({"user_name": "alice"}), "alice")


if __name__ == "__main__":
    unittest.main()
