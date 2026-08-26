import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from modelscope_manager.download_service import Aria2DownloadRunner, Aria2Tuning, DownloadSpec, build_download_specs
from modelscope_manager.service import RemoteEntry, Repository


class DownloadPlanTests(unittest.TestCase):
    def test_folder_plan_restores_selected_directory(self):
        service = MagicMock()
        service.get_download_url.side_effect = lambda repo, path: f"https://example.test/{path}"
        repo = Repository("alice/demo", "dataset")
        entries = [
            RemoteEntry("magia record", is_dir=True),
            RemoteEntry("magia record/a.bin", 7, "abc"),
            RemoteEntry("magia record/nested/b.bin", 9, "def"),
            RemoteEntry("other.bin", 2, "ghi"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            specs = build_download_specs(service, repo, entries, entries[0], Path(tmp))
            relative = [spec.local_path.relative_to(tmp).as_posix() for spec in specs]
        self.assertEqual(relative, ["magia record/a.bin", "magia record/nested/b.bin"])

    def test_file_plan_uses_basename(self):
        service = MagicMock()
        service.get_download_url.return_value = "https://example.test/a.bin"
        repo = Repository("alice/demo", "model")
        entry = RemoteEntry("folder/a.bin", 7, "abc")
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_download_specs(service, repo, [entry], entry, Path(tmp))[0]
            self.assertEqual(spec.local_path, Path(tmp).resolve() / "a.bin")


class VerificationTests(unittest.TestCase):
    def test_adaptive_segments_are_applied_per_rpc_download(self):
        tuning = Aria2Tuning(1.0, 1, 32, 100.0, 64)
        runner = Aria2DownloadRunner(Path("aria2-next.exe"), "", tuning)
        specs = [
            DownloadSpec("small", Path("small"), "https://modelscope.cn/small", 512 * 1024),
            DownloadSpec("medium", Path("medium"), "https://modelscope.cn/medium", 10 * 1024**2),
            DownloadSpec("large", Path("large"), "https://modelscope.cn/large", 100 * 1024**2),
        ]
        self.assertEqual([runner._aria2_options(spec)["split"] for spec in specs], ["1", "32", "64"])

    def test_invalid_threshold_order_is_rejected(self):
        with self.assertRaises(ValueError):
            Aria2Tuning(100.0, 1, 32, 1.0, 64).validated()

    def test_rpc_options_keep_each_accounts_token_with_its_file(self):
        runner = Aria2DownloadRunner(Path("aria2-next.exe"), "")
        specs = [
            DownloadSpec("a", Path("a"), "https://modelscope.cn/a", token="token-a"),
            DownloadSpec("b", Path("b"), "https://modelscope.cn/b", token="token-b"),
        ]
        headers = [runner._aria2_options(spec)["header"] for spec in specs]
        self.assertIn("Authorization: Bearer token-a", headers[0])
        self.assertIn("Authorization: Bearer token-b", headers[1])

    def test_rpc_snapshot_uses_aria2_aggregate_speed(self):
        runner = Aria2DownloadRunner(Path("aria2-next.exe"), "")
        active = [{
            "completedLength": "300",
            "totalLength": "1000",
            "files": [{"path": "C:/downloads/a.bin"}],
        }]
        stopped = [{
            "completedLength": "100",
            "totalLength": "100",
            "files": [{"path": "C:/downloads/small.bin"}],
        }]
        runner._rpc = MagicMock(side_effect=[active, [], stopped, {"downloadSpeed": "33554432"}])
        completed, total, speed, progress = runner._aria2_snapshot()
        self.assertEqual((completed, total, speed), (400, 1100, 33554432.0))
        self.assertEqual(len(progress), 2)

    def test_command_uses_bundled_loopback_rpc_for_controls(self):
        runner = Aria2DownloadRunner(Path("aria2-next.exe"), "")
        runner._rpc_port = 16800
        runner._rpc_secret = "secret"
        command = runner._command()
        self.assertIn("--enable-rpc=true", command)
        self.assertIn("--rpc-listen-all=false", command)
        self.assertIn("--pause=true", command)
        self.assertIn("--rpc-listen-port=16800", command)
        self.assertIn("--rpc-secret=secret", command)

    def test_command_and_runtime_apply_dynamic_download_limit(self):
        limits = [2 * 1024 * 1024]
        runner = Aria2DownloadRunner(Path("aria2-next.exe"), "", download_limit_supplier=lambda: limits[0])
        runner._rpc_port = 16800
        runner._rpc_secret = "secret"
        command = runner._command()
        self.assertIn("--max-overall-download-limit=2097152", command)
        runner._rpc = MagicMock()
        runner._apply_download_limit(force=True)
        limits[0] = 0
        runner._apply_download_limit()
        self.assertEqual(runner._rpc.call_args_list[-1].args, (
            "changeGlobalOption", [{"max-overall-download-limit": "0"}],
        ))

    def test_verification_checks_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.bin"
            data = b"verified"
            path.write_bytes(data)
            spec = DownloadSpec("a.bin", path, "https://example.test/a.bin", len(data), hashlib.sha256(data).hexdigest())
            updates = []
            runner = Aria2DownloadRunner(Path("aria2-next.exe"), "")
            result = runner._verify([spec], 0, lambda *args: updates.append(args))
        self.assertEqual(result, (1, 0))
        self.assertEqual(updates[-1][1], "completed")


if __name__ == "__main__":
    unittest.main()
