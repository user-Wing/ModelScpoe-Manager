import shutil
import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings

from modelscope_manager.storage import DeviceIdentity, restore_device_bound_token


class StorageTests(unittest.TestCase):
    def test_copied_device_identity_destroys_only_the_copied_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            copied = root / "copied"
            protector = lambda value: "device-a:" + value
            unprotector = lambda value: value.removeprefix("device-a:")
            original_id, replaced = DeviceIdentity(
                original / "device.id", protector, unprotector
            ).load_or_create()
            self.assertFalse(replaced)

            original_settings = QSettings(str(original / "settings.ini"), QSettings.Format.IniFormat)
            original_settings.setValue("token", "encrypted-token")
            original_settings.setValue("token_device_id", original_id)
            original_settings.sync()
            shutil.copytree(original, copied)

            def reject_copied_identity(_value):
                raise OSError("different device")

            copied_id, copied_replaced = DeviceIdentity(
                copied / "device.id", lambda value: "device-b:" + value, reject_copied_identity
            ).load_or_create()
            copied_settings = QSettings(str(copied / "settings.ini"), QSettings.Format.IniFormat)
            token = restore_device_bound_token(copied_settings, copied_id, copied_replaced)

            self.assertTrue(copied_replaced)
            self.assertNotEqual(copied_id, original_id)
            self.assertEqual(token, "")
            self.assertFalse(copied_settings.contains("token"))
            original_settings.sync()
            self.assertEqual(original_settings.value("token"), "encrypted-token")
