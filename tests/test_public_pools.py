import tempfile
import unittest
from pathlib import Path

from modelscope_manager.public_pools import PublicPoolStore
from modelscope_manager.service import Repository


class PublicPoolTests(unittest.TestCase):
    def test_successful_searches_are_persisted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = PublicPoolStore(Path(temporary) / "public_pools.json")
            repo = Repository("moonshotai/PerceptionBench", "dataset", "public")
            store.add("https://www.modelscope.cn/datasets/moonshotai/PerceptionBench", repo)
            store.add("https://modelscope.cn/datasets/moonshotai/PerceptionBench/files", repo)
            loaded = store.load()
            repositories = store.repositories()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["url"], "https://modelscope.cn/datasets/moonshotai/PerceptionBench/files")
        self.assertEqual(repositories, [repo])

    def test_remove_and_clear_update_persisted_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = PublicPoolStore(Path(temporary) / "public_pools.json")
            first = Repository("alice/one", "dataset", "public")
            second = Repository("alice/two", "model", "public")
            store.add("https://modelscope.cn/datasets/alice/one", first)
            store.add("https://modelscope.cn/models/alice/two", second)
            store.remove(first)
            self.assertEqual(store.repositories(), [second])
            store.clear()
            self.assertEqual(store.load(), [])


if __name__ == "__main__":
    unittest.main()
