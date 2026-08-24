import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QImage
from PySide6.QtWidgets import QApplication, QLabel

from modelscope_manager.app import DropArea, ImageUploadThread, is_supported_image_file
from modelscope_manager.database import initialize_database
from modelscope_manager.image_bed import ImageStore
from modelscope_manager.service import Repository


class FakeImageService:
    def __init__(self):
        self.uploads = []
        self.token = "test-token"

    def upload_file_as(self, repo, local_path, remote_path):
        self.uploads.append((repo, Path(local_path), remote_path))


class ImageBedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_upload_creates_record_direct_link_and_local_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "manager.sqlite3"
            initialize_database(database)
            source = root / "picture.png"
            source.write_bytes(
                b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
                b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\x89\x99"
                b"=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
            )
            store = ImageStore(database, root / "cache")
            service = FakeImageService()
            repo = Repository("alice/images", "dataset", "public")
            thread = ImageUploadThread(
                store, "account-a", service, repo, [source], "images",
                temporary_paths={source},
            )
            thread.run()

            records = store.list_records()
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0].direct_url.startswith(
                "https://modelscope.cn/datasets/alice/images/resolve/master/images/"
            ))
            self.assertTrue(Path(records[0].cache_path).is_file())
            self.assertFalse(source.exists())
            self.assertRegex(records[0].remote_path, r"^images/\d{4}/\d{2}/[0-9a-f]{8}_picture\.png$")
            store.remove(records[0].image_id)
            self.assertEqual(store.list_records(), [])
            self.assertFalse(Path(records[0].cache_path).exists())

    def test_image_validation_checks_content_not_only_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "not-an-image.png"
            fake.write_text("plain text", encoding="utf-8")
            self.assertFalse(is_supported_image_file(fake))

            image = Path(temporary) / "picture.png"
            self.assertTrue(QImage(2, 2, QImage.Format.Format_ARGB32).save(str(image), "PNG"))
            self.assertTrue(is_supported_image_file(image))

    def test_drop_area_labels_do_not_intercept_drag_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "file.txt"
            source.write_text("content", encoding="utf-8")
            area = DropArea()
            self.assertTrue(area.acceptDrops())
            self.assertTrue(all(
                label.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                for label in area.findChildren(QLabel)
            ))
            mime_data = QMimeData()
            mime_data.setUrls([QUrl.fromLocalFile(str(source))])
            event = QDragEnterEvent(
                QPoint(10, 10), Qt.DropAction.CopyAction, mime_data,
                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
            )
            QApplication.sendEvent(area, event)
            self.assertTrue(event.isAccepted())
