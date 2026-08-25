import tempfile
import unittest
from pathlib import Path

from modelscope_manager.folder_index import FolderSizeIndex, calculate_folder_sizes
from modelscope_manager.service import RemoteEntry, Repository


class FolderSizeIndexTests(unittest.TestCase):
    def test_missing_cache_is_distinct_from_a_zero_byte_folder(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = FolderSizeIndex(Path(temporary) / "folder_sizes.sqlite3")
            repo = Repository("alice/demo", "dataset")
            self.assertIsNone(index.cached_folder_size(repo))
            index.update_repository(repo, [RemoteEntry("empty", is_dir=True)])
            self.assertEqual(index.cached_folder_size(repo, "empty"), 0)

    def test_recursive_sizes_include_all_descendants_and_empty_folders(self):
        sizes = calculate_folder_sizes([
            RemoteEntry("root.bin", 2),
            RemoteEntry("folder/file.bin", 3),
            RemoteEntry("folder/deep/file.bin", 5),
            RemoteEntry("empty", is_dir=True),
        ])
        self.assertEqual(sizes[""], 10)
        self.assertEqual(sizes["folder"], 8)
        self.assertEqual(sizes["folder/deep"], 5)
        self.assertEqual(sizes["empty"], 0)

    def test_sqlite_index_persists_repository_sizes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "folder_sizes.sqlite3"
            repo = Repository("alice/demo", "dataset")
            FolderSizeIndex(path).update_repository(repo, [
                RemoteEntry("folder/a.bin", 4),
                RemoteEntry("folder/nested/b.bin", 6),
            ])
            reopened = FolderSizeIndex(path)
            self.assertEqual(reopened.folder_size(repo), 10)
            self.assertEqual(reopened.folder_size(repo, "folder"), 10)
            self.assertEqual(reopened.folder_size(repo, "folder/nested"), 6)
            self.assertEqual(reopened.repositories_size([repo]), 10)

    def test_update_folder_only_writes_the_selected_folder_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            index = FolderSizeIndex(Path(temporary) / "folder_sizes.sqlite3")
            repo = Repository("alice/demo", "dataset")
            entries = [RemoteEntry("folder/a.bin", 4), RemoteEntry("folder/nested/b.bin", 6)]
            self.assertEqual(index.update_folder(repo, "folder", entries), 10)
            self.assertEqual(index.cached_folder_size(repo, "folder"), 10)
            self.assertIsNone(index.cached_folder_size(repo, ""))

    def test_remove_entries_decrements_ancestors_and_removes_subtree_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "folder_sizes.sqlite3"
            repo = Repository("alice/demo", "dataset")
            index = FolderSizeIndex(path)
            entries = [
                RemoteEntry("folder/a.bin", 4),
                RemoteEntry("folder/nested/b.bin", 6),
                RemoteEntry("other.bin", 3),
            ]
            index.update_repository(repo, entries)

            index.remove_entries(repo, entries[:2], ["folder"])

            self.assertEqual(index.cached_folder_size(repo), 3)
            self.assertIsNone(index.cached_folder_size(repo, "folder"))
            self.assertIsNone(index.cached_folder_size(repo, "folder/nested"))
            self.assertEqual(FolderSizeIndex(path).cached_folder_size(repo), 3)
