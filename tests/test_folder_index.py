import tempfile
import unittest
from pathlib import Path

from modelscope_manager.folder_index import FolderSizeIndex, calculate_folder_sizes
from modelscope_manager.service import RemoteEntry, Repository


class FolderSizeIndexTests(unittest.TestCase):
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
