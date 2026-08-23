import tempfile
import unittest
from pathlib import Path

from modelscope_manager.app import ImageUploadThread
from modelscope_manager.database import initialize_database
from modelscope_manager.image_bed import ImageStore
from modelscope_manager.service import Repository


class FakeImageService:
    def __init__(self):
        self.uploads = []

    def upload_file_as(self, repo, local_path, remote_path):
        self.uploads.append((repo, Path(local_path), remote_path))

    def get_download_url(self, repo, remote_path):
        return f"https://example.test/{repo.repo_id}/{remote_path}"


class ImageBedTests(unittest.TestCase):
    def test_upload_creates_record_direct_link_and_local_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "manager.sqlite3"
            initialize_database(database)
            source = root / "picture.png"
            source.write_bytes(b"fake-png")
            store = ImageStore(database, root / "cache")
            service = FakeImageService()
            repo = Repository("alice/images", "dataset")
            thread = ImageUploadThread(store, "account-a", service, repo, [source], "images")
            thread.run()

            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0].direct_url.startswith("https://example.test/"))
            self.assertTrue(Path(records[0].cache_path).is_file())
            self.assertRegex(records[0].remote_path, r"^images/\d{4}/\d{2}/[0-9a-f]{8}_picture\.png$")
            store.remove(records[0].image_id)
            self.assertEqual(store.list_records(), [])
            self.assertFalse(Path(records[0].cache_path).exists())
