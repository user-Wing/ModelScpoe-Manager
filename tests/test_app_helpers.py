import unittest

from modelscope_manager.app import MainWindow
from modelscope_manager.service import Repository


class AppHelperTests(unittest.TestCase):
    def test_folder_share_url_preserves_nested_path(self):
        repo = Repository("alice/example", "dataset")
        url = MainWindow._repository_web_url(repo, "magia record/视频")
        self.assertEqual(
            url,
            "https://www.modelscope.cn/datasets/alice/example/files?path=magia%20record/%E8%A7%86%E9%A2%91",
        )
