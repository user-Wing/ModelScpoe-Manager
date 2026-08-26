import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modelscope_manager.local_paths import UnsafeLocalPathError, iter_contained_files


class LocalPathTests(unittest.TestCase):
    def test_nested_regular_files_stay_under_selected_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "folder" / "file.txt"
            nested.parent.mkdir()
            nested.write_text("content", encoding="utf-8")
            self.assertEqual(list(iter_contained_files(root)), [nested])

    def test_link_like_entry_is_rejected_before_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            link = root / "link.txt"
            link.write_text("placeholder", encoding="utf-8")
            with patch(
                "modelscope_manager.local_paths.is_link_like",
                side_effect=lambda path: Path(path) == link,
            ):
                with self.assertRaises(UnsafeLocalPathError):
                    list(iter_contained_files(root))


if __name__ == "__main__":
    unittest.main()
