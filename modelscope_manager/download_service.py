from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .http_security import modelscope_token_headers
from .service import ModelScopeService, RemoteEntry, Repository


@dataclass(frozen=True)
class DownloadSpec:
    remote_path: str
    local_path: Path
    url: str
    size: int = 0
    sha256: str = ""
    token: str = ""


@dataclass(frozen=True)
class Aria2Tuning:
    small_limit_mb: float = 1.0
    small_segments: int = 1
    medium_segments: int = 32
    large_limit_mb: float = 100.0
    large_segments: int = 64

    def validated(self) -> "Aria2Tuning":
        if self.small_limit_mb <= 0 or self.large_limit_mb <= self.small_limit_mb:
            raise ValueError("大文件阈值必须大于小文件阈值")
        if not all(1 <= value <= 128 for value in (self.small_segments, self.medium_segments, self.large_segments)):
            raise ValueError("分段数必须介于 1 和 128 之间")
        return self

    def segments_for(self, size: int) -> int:
        size_mb = max(0, size) / (1024 * 1024)
        if size_mb <= self.small_limit_mb:
            return self.small_segments
        if size_mb >= self.large_limit_mb:
            return self.large_segments
        return self.medium_segments


def _safe_parts(remote_path: str) -> tuple[str, ...]:
    parts = tuple(part for part in PurePosixPath(remote_path.replace("\\", "/")).parts if part not in ("", ".", "/"))
    if not parts or ".." in parts:
        raise ValueError(f"不安全的仓库路径：{remote_path}")
    return parts


def build_download_specs(
    service: ModelScopeService,
    repo: Repository,
    entries: Iterable[RemoteEntry],
    selected: RemoteEntry,
    destination: Path,
) -> list[DownloadSpec]:
    destination = destination.resolve()
    files = [entry for entry in entries if not entry.is_dir]
    specs: list[DownloadSpec] = []

    if selected.is_dir:
        prefix = selected.path.strip("/")
        if prefix:
            chosen = [entry for entry in files if entry.path.startswith(prefix + "/")]
            container = PurePosixPath(prefix).name
            prefix_parts = len(_safe_parts(prefix))
        else:
            chosen = files
            container = repo.repo_id.rsplit("/", 1)[-1]
            prefix_parts = 0
        for entry in chosen:
            parts = _safe_parts(entry.path)
            relative = parts[prefix_parts:]
            local_path = destination.joinpath(container, *relative).resolve()
            if not local_path.is_relative_to(destination):
                raise ValueError(f"下载路径超出目标目录：{entry.path}")
            specs.append(
                DownloadSpec(
                    entry.path,
                    local_path,
                    service.get_download_url(repo, entry.path),
                    entry.size,
                    entry.sha256,
                    str(getattr(service, "token", "") or ""),
                )
            )
    else:
        parts = _safe_parts(selected.path)
        local_path = destination.joinpath(parts[-1]).resolve()
        if not local_path.is_relative_to(destination):
            raise ValueError(f"下载路径超出目标目录：{selected.path}")
        specs.append(
            DownloadSpec(
                selected.path,
                local_path,
                service.get_download_url(repo, selected.path),
                selected.size,
                selected.sha256,
                str(getattr(service, "token", "") or ""),
            )
        )
    return specs


ProgressCallback = Callable[[int, int, float, int], None]
ItemCallback = Callable[[DownloadSpec, str, int, int, str], None]


class Aria2DownloadRunner:
    def __init__(
        self,
        executable: Path,
        token: str,
        tuning: Aria2Tuning | None = None,
        download_limit_supplier: Callable[[], int] | None = None,
    ):
        self.executable = executable.resolve()
        self.token = token
        self.tuning = (tuning or Aria2Tuning()).validated()
        self.download_limit_supplier = download_limit_supplier or (lambda: 0)
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._paused = False
        self._stop_requested = False
        self._rpc_port = 0
        self._rpc_secret = ""
        self._current_specs: list[DownloadSpec] = []
        self._applied_download_limit = -1

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stop_requested

    def pause(self) -> bool:
        """Pause all downloads through aria2-next's loopback-only control interface."""
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or self._paused:
                return False
            self._rpc("pauseAll")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if not self._rpc("tellActive"):
                    break
                time.sleep(0.05)
            self._paused = True
            return True

    def resume(self) -> bool:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None or not self._paused:
                return False
            self._rpc("unpauseAll")
            self._paused = False
            return True

    def stop(self) -> bool:
        """Stop aria2-next while retaining downloaded bytes and .aria2 control files."""
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._stop_requested = True
            try:
                if not self._paused:
                    self._rpc("pauseAll")
                self._rpc("forceShutdown")
            except Exception:
                process.terminate()
            self._paused = False
            return True

    def _rpc(self, method: str, params: list | None = None):
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": "modelscope-manager",
            "method": f"aria2.{method}",
            "params": [f"token:{self._rpc_secret}", *(params or [])],
        }).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self._rpc_port, timeout=2.0)
        try:
            connection.request("POST", "/jsonrpc", body=payload, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            result = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()
        if "error" in result:
            raise RuntimeError(str(result["error"].get("message", "aria2-next 控制失败")))
        return result.get("result")

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            return int(server.getsockname()[1])

    def _wait_for_rpc(self, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and process.poll() is None:
            try:
                self._rpc("getVersion")
                return
            except Exception:
                time.sleep(0.05)
        raise RuntimeError("aria2-next 控制接口启动失败")

    def run(
        self,
        specs: list[DownloadSpec],
        progress_callback: ProgressCallback,
        item_callback: ItemCallback,
    ) -> tuple[int, int]:
        if not self.executable.is_file():
            raise FileNotFoundError(f"未找到 aria2-next：{self.executable}")
        if not specs:
            return 0, 0

        for spec in specs:
            spec.local_path.parent.mkdir(parents=True, exist_ok=True)
            item_callback(spec, "waiting", self._local_size(spec), spec.size, "等待下载")

        process: subprocess.Popen | None = None
        try:
            self._rpc_port = self._available_port()
            self._rpc_secret = uuid.uuid4().hex
            self._current_specs = list(specs)
            process = subprocess.Popen(
                self._command(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self._lock:
                self._process = process
                self._paused = False
                self._stop_requested = False
            self._wait_for_rpc(process)
            self._enqueue_specs(specs)
            self._apply_download_limit(force=True)
            self._rpc("unpauseAll")
            total = max(1, sum(max(0, spec.size) for spec in specs))
            while process.poll() is None:
                completed, rpc_total, speed, item_progress = self._aria2_snapshot()
                if rpc_total > 0:
                    total = rpc_total
                if completed < 0:
                    completed = self._report_items(specs, item_callback)
                else:
                    self._report_items(specs, item_callback, item_progress)
                eta = int(max(0, total - completed) / speed) if speed > 0 else -1
                progress_callback(min(completed, total), total, speed, eta)
                try:
                    self._apply_download_limit()
                    statistics = self._rpc("getGlobalStat")
                    active = int(statistics.get("numActive", 0))
                    waiting = int(statistics.get("numWaiting", 0))
                    if active == 0 and waiting == 0:
                        self._rpc("shutdown")
                        break
                except Exception:
                    if process.poll() is not None:
                        break
                time.sleep(0.4)

            try:
                return_code = process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                try:
                    self._rpc("forceShutdown")
                except Exception:
                    process.terminate()
                return_code = process.wait(timeout=3)
            completed = self._report_items(specs, item_callback)
            progress_callback(min(completed, total), total, 0.0, 0)
            if self.stopped:
                return self._mark_stopped(specs, item_callback)
            return self._verify(specs, return_code, item_callback)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
            with self._lock:
                self._process = None
                self._paused = False

    def _enqueue_specs(self, specs: list[DownloadSpec]) -> None:
        """Add downloads over loopback RPC so account credentials never touch disk."""
        for spec in specs:
            self._rpc("addUri", [[spec.url], self._aria2_options(spec)])

    def _aria2_options(self, spec: DownloadSpec) -> dict:
        segments = self.tuning.segments_for(spec.size)
        options = {
            "dir": str(spec.local_path.parent),
            "out": spec.local_path.name,
            "continue": "true",
            "allow-overwrite": "true",
            "auto-file-renaming": "false",
            "file-allocation": "none",
            "check-integrity": "true",
            "split": str(segments),
            "max-connection-per-server": str(segments),
            "pause": "true",
        }
        if spec.sha256:
            options["checksum"] = f"sha-256={spec.sha256}"
        token = spec.token or self.token
        headers = modelscope_token_headers(spec.url, token, include_session_cookie=True)
        if headers:
            options["header"] = [f"{name}: {value}" for name, value in headers.items()]
        return options

    def _command(self) -> list[str]:
        connection_budget = 64
        concurrent = 0
        used_connections = 0
        # Keep enough small-file tasks active to fill the connection budget
        # active to fill the connection budget without multiplying large-file splits.
        # A minimum of three tasks prevents metadata-heavy repositories from idling.
        for spec in getattr(self, "_current_specs", []):
            connections = self.tuning.segments_for(spec.size)
            if concurrent and used_connections + connections > connection_budget:
                break
            concurrent += 1
            used_connections += connections
        concurrent = max(3, concurrent)
        command = [
            str(self.executable),
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            "--file-allocation=none",
            "--check-integrity=true",
            f"--max-concurrent-downloads={concurrent}",
            "--max-connection-per-server=1",
            "--split=1",
            "--min-split-size=1M",
            "--max-tries=5",
            "--retry-wait=3",
            "--connect-timeout=10",
            "--timeout=30",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--download-result=hide",
            "--enable-rpc=true",
            "--pause=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self._rpc_port}",
            f"--rpc-secret={self._rpc_secret}",
        ]
        initial_limit = max(0, int(self.download_limit_supplier()))
        if initial_limit:
            command.append(f"--max-overall-download-limit={initial_limit}")
        return command

    def _apply_download_limit(self, force: bool = False) -> None:
        limit = max(0, int(self.download_limit_supplier()))
        if not force and limit == self._applied_download_limit:
            return
        self._rpc("changeGlobalOption", [{"max-overall-download-limit": str(limit)}])
        self._applied_download_limit = limit

    @staticmethod
    def _local_size(spec: DownloadSpec) -> int:
        try:
            size = spec.local_path.stat().st_size
        except OSError:
            return 0
        return min(size, spec.size) if spec.size > 0 else size

    def _aria2_snapshot(self) -> tuple[int, int, float, dict[str, int]]:
        fields = ["totalLength", "completedLength", "downloadSpeed", "files"]
        try:
            tasks = []
            tasks.extend(self._rpc("tellActive", [fields]))
            tasks.extend(self._rpc("tellWaiting", [0, 10000, fields]))
            tasks.extend(self._rpc("tellStopped", [0, 10000, fields]))
            statistics = self._rpc("getGlobalStat")
        except Exception:
            return -1, 0, 0.0, {}
        completed = sum(int(task.get("completedLength", 0)) for task in tasks)
        total = sum(int(task.get("totalLength", 0)) for task in tasks)
        progress: dict[str, int] = {}
        for task in tasks:
            files = task.get("files") or []
            if files and files[0].get("path"):
                progress[os.path.normcase(os.path.abspath(files[0]["path"]))] = int(task.get("completedLength", 0))
        return completed, total, float(statistics.get("downloadSpeed", 0)), progress

    def _report_items(
        self,
        specs: list[DownloadSpec],
        callback: ItemCallback,
        item_progress: dict[str, int] | None = None,
    ) -> int:
        completed = 0
        for spec in specs:
            key = os.path.normcase(os.path.abspath(spec.local_path))
            current = (item_progress or {}).get(key, self._local_size(spec))
            completed += current
            message = "已暂停" if self.paused else "下载中"
            if spec.size > 0 and current >= spec.size:
                message = "等待校验"
            callback(spec, "paused" if self.paused else "downloading", current, spec.size, message)
        return completed

    def _mark_stopped(self, specs: list[DownloadSpec], callback: ItemCallback) -> tuple[int, int]:
        for spec in specs:
            current = self._local_size(spec)
            callback(spec, "stopped", current, spec.size, "已停止，可保留断点后继续")
        return 0, 0

    def _verify(self, specs: list[DownloadSpec], return_code: int, callback: ItemCallback) -> tuple[int, int]:
        ok = failed = 0
        for spec in specs:
            callback(spec, "verifying", self._local_size(spec), spec.size, "正在校验")
            error = ""
            if not spec.local_path.is_file():
                error = "文件未生成"
            elif spec.size > 0 and spec.local_path.stat().st_size != spec.size:
                error = f"大小不匹配：{spec.local_path.stat().st_size}/{spec.size} 字节"
            elif spec.sha256:
                digest = hashlib.sha256()
                with spec.local_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest().lower() != spec.sha256.lower():
                    error = "SHA-256 校验失败"
            elif return_code != 0:
                error = f"aria2-next 返回代码 {return_code}"

            if error:
                failed += 1
                callback(spec, "failed", self._local_size(spec), spec.size, error)
            else:
                ok += 1
                callback(spec, "completed", spec.size, spec.size, "下载完成，校验通过")
        return ok, failed
