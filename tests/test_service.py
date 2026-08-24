import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modelscope_manager.service import (
    ModelScopeService,
    RemoteEntry,
    Repository,
    normalize_remote_path,
    repository_directories,
    oversized_upload_files,
    parse_modelscope_repository_url,
    upload_paths,
)


class PathTests(unittest.TestCase):
    def test_parse_public_dataset_url(self):
        repo = parse_modelscope_repository_url("https://www.modelscope.cn/datasets/moonshotai/PerceptionBench/files")
        self.assertEqual(repo, Repository("moonshotai/PerceptionBench", "dataset", "public"))

    def test_parse_rejects_non_modelscope_host(self):
        with self.assertRaises(ValueError):
            parse_modelscope_repository_url("https://example.com/datasets/a/b")

    def test_normalize_remote_path(self):
        self.assertEqual(normalize_remote_path("/models\\v1/", "weights.bin"), "models/v1/weights.bin")
        self.assertEqual(normalize_remote_path("", "folder"), "folder")

    def test_reject_parent_traversal(self):
        with self.assertRaises(ValueError):
            normalize_remote_path("../secret")

    def test_repository_directories_infers_selectable_folders(self):
        paths = ["magia record", "magia record/a.txt", "other/nested/b.bin"]
        self.assertEqual(
            repository_directories(paths),
            {"magia record", "other", "other/nested"},
        )

    def test_oversized_upload_files_scans_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            small = root / "small.bin"
            large = root / "nested" / "large.bin"
            large.parent.mkdir()
            small.write_bytes(b"12")
            large.write_bytes(b"1234")
            self.assertEqual(oversized_upload_files([root], limit=3), [large.resolve()])


class UploadTests(unittest.TestCase):
    def test_upload_paths_routes_files_and_folders(self):
        service = MagicMock()
        repo = Repository("alice/demo", "dataset")
        reports = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "a.txt"
            file_path.write_text("a", encoding="utf-8")
            folder = root / "folder"
            folder.mkdir()
            ok, failed = upload_paths(service, repo, [file_path, folder], "target", True, lambda *args: reports.append(args))
        self.assertEqual((ok, failed), (2, 0))
        service.upload_file.assert_called_once_with(repo, file_path, "target")
        service.upload_folder.assert_called_once_with(repo, folder, "target", True)
        self.assertEqual(len(reports), 2)


class RepositoryTests(unittest.TestCase):
    def test_repository_page_size_does_not_exceed_api_limit(self):
        service = ModelScopeService.__new__(ModelScopeService)
        service.user = type("User", (), {"username": "alice"})()
        service.api = MagicMock()
        service.api.list_repos.return_value = type(
            "Page", (), {"items": [], "total_count": 0}
        )()

        service.list_repositories(("model",))

        service.api.list_repos.assert_called_once_with(
            "model", owner="alice", page_number=1, page_size=50
        )

    def test_dataset_file_listing_uses_paginated_sdk_method(self):
        service = ModelScopeService.__new__(ModelScopeService)
        service.api = MagicMock()
        service.api.legacy.list_dataset_files_paginated.return_value = [
            {"Path": "b/file.txt"},
            {"Path": "a.txt"},
            {"Path": "a.txt"},
        ]
        repo = Repository("alice/data", "dataset")

        paths = service.list_files(repo)

        self.assertEqual(paths, ["a.txt", "b/file.txt"])
        service.api.legacy.list_dataset_files_paginated.assert_called_once_with(
            "alice/data", page_size=100
        )

    def test_dataset_file_listing_keeps_all_paginated_results(self):
        service = ModelScopeService.__new__(ModelScopeService)
        service.api = MagicMock()
        service.api.legacy.list_dataset_files_paginated.return_value = [
            {"Path": ".gitattributes"},
            {"Path": "001.bin"},
            {"Path": "images/first.png"},
            {"Path": "videos/last.mp4"},
        ]

        paths = service.list_files(Repository("alice/data", "dataset"))

        self.assertEqual(
            paths,
            [".gitattributes", "001.bin", "images/first.png", "videos/last.mp4"],
        )

    def test_list_entries_preserves_size_hash_and_type(self):
        service = ModelScopeService.__new__(ModelScopeService)
        service.api = MagicMock()
        service.api.legacy.list_dataset_files_paginated.return_value = [
            {"Path": "folder", "Type": "tree", "Size": 0},
            {"Path": "folder/a.bin", "Type": "blob", "Size": 7, "Sha256": "abc"},
        ]
        entries = service.list_entries(Repository("alice/data", "dataset"))
        self.assertEqual(
            entries,
            [RemoteEntry("folder", 0, "", True), RemoteEntry("folder/a.bin", 7, "abc", False)],
        )


if __name__ == "__main__":
    unittest.main()
