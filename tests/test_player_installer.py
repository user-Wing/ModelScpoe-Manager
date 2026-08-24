import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from modelscope_manager.player_installer import (
    POTPLAYER_ARCHIVE_SHA256,
    POTPLAYER_ARCHIVE_SIZE,
    POTPLAYER_REMOTE_PATH,
    find_potplayer,
    install_potplayer,
)


class PlayerInstallerTests(unittest.TestCase):
    def test_pinned_modelscope_archive_metadata(self):
        self.assertEqual(POTPLAYER_REMOTE_PATH, "! Software/PotPlayer.7z")
        self.assertEqual(POTPLAYER_ARCHIVE_SIZE, 68_984_476)
        self.assertEqual(POTPLAYER_ARCHIVE_SHA256, "4d58547cff31ec047eb26cc6ad86c2d98b0694ff48c79a7df29d415b6ad521ce")

    def test_find_potplayer_prefers_64_bit_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "PotPlayerMini.exe").touch()
            (root / "bin").mkdir()
            expected = root / "bin" / "PotPlayerMini64.exe"
            expected.touch()
            self.assertEqual(find_potplayer(root), expected.resolve())

    def test_install_extracts_to_managed_player_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "PotPlayer.7z"
            seven_zip = root / "7z.exe"
            archive.touch()
            seven_zip.touch()
            destination = root / "players" / "potplayer"

            def fake_run(command, **_kwargs):
                output = next(part[2:] for part in command if part.startswith("-o"))
                payload = Path(output) / "PotPlayer"
                payload.mkdir(parents=True)
                (payload / "PotPlayerMini64.exe").write_bytes(b"player")
                return type("Result", (), {"returncode": 0, "stdout": ""})()

            with patch("modelscope_manager.player_installer.verify_archive"), patch(
                "modelscope_manager.player_installer.subprocess.run", side_effect=fake_run
            ):
                installed = install_potplayer(archive, seven_zip, destination)
            self.assertEqual(installed, (destination / "PotPlayerMini64.exe").resolve())
            self.assertTrue(installed.is_file())
            self.assertFalse(archive.exists())


if __name__ == "__main__":
    unittest.main()
