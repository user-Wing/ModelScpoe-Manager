from __future__ import annotations

import unittest
from types import SimpleNamespace

from modelscope_manager.service import DATASET_FILE_PAGE_SIZE, ModelScopeService, Repository


class _LegacyApi:
    def __init__(self) -> None:
        self.page_size = None

    def list_dataset_files_paginated(self, _repo_id: str, page_size: int):
        self.page_size = page_size
        return [
            {"Path": "folder", "Type": "tree"},
            {"Path": "folder/video.mkv", "Type": "blob", "Size": 123},
        ]


class LargeDatasetListingTest(unittest.TestCase):
    def test_dataset_listing_uses_modelscope_maximum_page_size(self) -> None:
        legacy = _LegacyApi()
        service = object.__new__(ModelScopeService)
        service.api = SimpleNamespace(legacy=legacy)

        entries = service.list_entries(Repository("owner/large-dataset", "dataset"))

        self.assertEqual(legacy.page_size, DATASET_FILE_PAGE_SIZE)
        self.assertEqual(DATASET_FILE_PAGE_SIZE, 3000)
        self.assertEqual([entry.path for entry in entries], ["folder", "folder/video.mkv"])


if __name__ == "__main__":
    unittest.main()
