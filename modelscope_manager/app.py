from __future__ import annotations

import json
import hashlib
import posixpath
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote
from urllib.request import Request, urlopen

from PySide6.QtCore import (
    QEvent, QProcess,
    QSettings, QSize, Qt, QThread, QTime, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QFont,
    QIcon, QImageReader, QKeySequence, QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QSpinBox,
    QScrollArea,
    QStyle,
    QStatusBar,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from qfluentwidgets import (
    FluentIcon as FIF,
    FluentWindow,
    NavigationItemPosition,
    ScrollArea as FluentScrollArea,
    SettingCardGroup,
    SpinBox as FluentSpinBox,
    ToolButton,
    Theme,
    setTheme,
    setThemeColor,
)

from .security import protect, unprotect
from .download_service import Aria2DownloadRunner, Aria2Tuning, DownloadSpec, build_download_specs
from .backup import BackupJob, BackupStore, LocalBackupFile
from .database import (
    AccountRecord, AccountStore, IndexedEntry, WebAccountRecord, classify_file,
    everything_search_match, initialize_database,
)
from .folder_index import FolderSizeIndex
from .fluent_ui import CleanComboBox, ControlSettingCard, FluentSwitchButton, PanelSettingCard
from .image_bed import IMAGE_EXTENSIONS, ImageRecord, ImageStore
from .localization import LocaleManager
from .media_proxy import AuthenticatedMediaProxy
from .player_installer import (
    POTPLAYER_ARCHIVE_SHA256,
    POTPLAYER_ARCHIVE_SIZE,
    POTPLAYER_REMOTE_PATH,
    POTPLAYER_REPOSITORY,
    find_potplayer,
    install_potplayer,
)
from .public_pools import PublicPoolStore
from .service import (
    ModelScopeService,
    ModelScopeWebService,
    MultiAccountService,
    RemoteEntry,
    Repository,
    normalize_remote_path,
    oversized_upload_files,
    parse_modelscope_repository_url,
    repository_directories,
    configure_upload_limit_supplier,
)
from .styles import theme_qss
from .storage import (
    APP_DIR,
    DEVICE_ID_PATH,
    FOLDER_INDEX_PATH,
    IMAGE_CACHE_DIR,
    THUMBNAIL_CACHE_DIR,
    MANAGER_DB_PATH,
    PLAYER_DOWNLOAD_DIR,
    POTPLAYER_DIR,
    PUBLIC_POOLS_PATH,
    SEVEN_ZIP_ZSTD_EXE,
    DeviceIdentity,
    destroy_saved_token,
    portable_settings,
    restore_device_bound_token,
)
from .startup import set_windows_startup, windows_startup_enabled
from .transfer_policy import SpeedRule, TransferPolicy
from .webdav_server import ModelScopeWebDAV
from .web_session import (
    DELETE_BATCH_SIZE, ModelScopeWebSession, delete_repository_file, delete_repository_files,
    fetch_web_user_info, list_repository_file_paths, web_session_username,
)


MEDIA_EXTENSIONS = {
    ".3gp", ".aac", ".ac3", ".aiff", ".alac", ".ape", ".avi", ".flac", ".flv",
    ".m2ts", ".m4a", ".m4v", ".mka", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg",
    ".mpg", ".mts", ".ogg", ".ogv", ".opus", ".rm", ".rmvb", ".ts", ".wav",
    ".webm", ".wma", ".wmv",
}

PUBLIC_ACCOUNT_ID = "__public__"
THUMBNAIL_RENDER_SIZE = (320, 180)
VIDEO_THUMBNAIL_SEEK_SECONDS = 1.5


def copy_name(name: str, is_dir: bool = False) -> str:
    """Add -copy before a file suffix, or after a directory name."""
    if is_dir:
        return f"{name}-copy"
    path = Path(name)
    return f"{path.stem}-copy{path.suffix}" if path.suffix else f"{name}-copy"


def thumbnail_batch_policy(entry_count: int) -> tuple[int, int, int]:
    """Return batch size, worker cap and inter-batch delay in milliseconds."""
    return (96, 32, 10) if entry_count > 100 else (48, 16, 20)


def repository_file_url(repo: Repository, path: str, public: bool) -> str:
    kind = "datasets" if repo.repo_type == "dataset" else "models"
    repo_id = quote(repo.repo_id, safe="/")
    remote_path = quote(normalize_remote_path(path), safe="/")
    if public:
        return f"https://modelscope.cn/{kind}/{repo_id}/resolve/master/{remote_path}"
    return f"https://modelscope.cn/api/v1/{kind}/{repo_id}/repo?Revision=master&FilePath={remote_path}"


def repository_is_public(repo: Repository, service_token: str = "") -> bool:
    """Interpret both SDK labels and ModelScope's numeric visibility values."""
    visibility = str(repo.visibility).strip().casefold()
    if visibility in {"public", "5"}:
        return True
    if visibility in {"private", "internal", "1", "3"}:
        return False
    return not service_token


def is_supported_image_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in IMAGE_EXTENSIONS
        and QImageReader(str(path)).canRead()
    )


def format_speed(value: float) -> str:
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    value = max(0.0, value)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024
    return "--"


def format_size(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(max(0, value))
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "--"


def local_paths_size(raw_paths: Iterable[str | Path]) -> int:
    """Return the total byte size of unique dropped files and folder contents."""
    candidates: list[Path] = []
    seen: set[str] = set()
    for raw in raw_paths:
        try:
            path = Path(raw).resolve()
        except OSError:
            continue
        key = str(path).casefold()
        if not path.exists() or key in seen:
            continue
        seen.add(key)
        candidates.append(path)

    roots: list[Path] = []
    for path in sorted(candidates, key=lambda value: len(value.parts)):
        if any(parent.is_dir() and path.is_relative_to(parent) for parent in roots):
            continue
        roots.append(path)

    total = 0
    for path in roots:
        files = path.rglob("*") if path.is_dir() else (path,)
        for file_path in files:
            if not file_path.is_file():
                continue
            try:
                total += max(0, file_path.stat().st_size)
            except OSError:
                continue
    return total


def breadcrumb_levels(path: str, root_text: str = "根目录") -> list[tuple[str, str]]:
    parts = [part for part in path.replace("\\", "/").strip("/").split("/") if part]
    levels = [(root_text, "")]
    for index, part in enumerate(parts):
        levels.append((part, "/".join(parts[:index + 1])))
    return levels


def format_eta(seconds: int) -> str:
    if seconds < 0:
        return "--"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def find_available_port(host: str, preferred: int, attempts: int = 40) -> int:
    candidates = [preferred]
    for offset in range(1, attempts + 1):
        candidates.extend((preferred + offset, preferred - offset))
    for port in candidates:
        if not 1024 <= port <= 65535:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
        return port
    raise OSError("未找到可用 WebDAV 端口")


def restore_combo_setting(settings: QSettings, key: str, combo, default) -> None:
    """Restore a combo choice and repair invalid values left in the INI file."""
    value = settings.value(key, default)
    index = combo.findData(value)
    if index < 0:
        value = default
        index = combo.findData(default)
        settings.setValue(key, default)
    combo.setCurrentIndex(max(0, index))


class PathBreadcrumb(QFrame):
    path_selected = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("pathPill")
        self._path = ""
        self._root_text = "根目录"
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(6, 2, 6, 2)
        self._layout.setSpacing(1)
        self.overflow_button = QPushButton("…")
        self._rebuild()

    def set_path(self, path: str, root_text: str = "根目录") -> None:
        self._path = path.replace("\\", "/").strip("/")
        self._root_text = root_text
        self.setToolTip(f"/ {self._path}" if self._path else f"/ {root_text}")
        self._rebuild()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rebuild()

    def _rebuild(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        levels = breadcrumb_levels(self._path, self._root_text)
        metrics = self.fontMetrics()

        def width(label: str) -> int:
            return max(42, min(220, metrics.horizontalAdvance(label) + 24))

        available = max(80, self.width() - 12)
        full_width = sum(width(label) for label, _ in levels) + max(0, len(levels) - 1) * 14
        visible_start = 0
        if full_width > available:
            visible_start = len(levels) - 1
            used = width(levels[-1][0])
            budget = max(42, available - 48)
            while visible_start > 0:
                previous = width(levels[visible_start - 1][0]) + 14
                if used + previous > budget:
                    break
                visible_start -= 1
                used += previous

        hidden = levels[:visible_start]
        self.overflow_button = QPushButton("…")
        self.overflow_button.setObjectName("breadcrumbButton")
        self.overflow_button.setToolTip("选择上级目录")
        if hidden:
            menu = QMenu(self.overflow_button)
            for label, target in hidden:
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda checked=False, value=target: self.path_selected.emit(value)
                )
            self.overflow_button.setMenu(menu)
            self._layout.addWidget(self.overflow_button)
        else:
            self.overflow_button.setVisible(False)

        for offset, (label, target) in enumerate(levels[visible_start:]):
            if offset or hidden:
                separator = QLabel("›")
                separator.setObjectName("breadcrumbSeparator")
                self._layout.addWidget(separator)
            button = QPushButton(label)
            button.setObjectName("breadcrumbButton")
            button.setProperty("breadcrumbPath", target)
            button.setMaximumWidth(width(label))
            button.clicked.connect(
                lambda checked=False, value=target: self.path_selected.emit(value)
            )
            self._layout.addWidget(button)
        self._layout.addStretch(1)


class TaskThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, action: Callable[[], Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.action = action

    def run(self) -> None:
        try:
            result = self.action()
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)


class ThumbnailThread(QThread):
    ready = Signal(dict)

    def __init__(
        self, service: ModelScopeService, repo: Repository, entries: list[RemoteEntry], maximum_size: int,
        workers: int = 32, parent=None,
    ):
        super().__init__(parent)
        self.service, self.repo, self.entries, self.maximum_size = service, repo, entries, maximum_size
        self.workers = max(1, workers)

    def run(self) -> None:
        THUMBNAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ffmpeg = shutil.which("ffmpeg")
        completed: dict[str, str] = {}
        candidates = [entry for entry in self.entries if self.is_eligible(entry, self.maximum_size)]
        executor = ThreadPoolExecutor(max_workers=min(self.workers, len(candidates) or 1), thread_name_prefix="thumbnail")
        futures = [executor.submit(self._create_thumbnail, entry, ffmpeg) for entry in candidates]
        try:
            for future in as_completed(futures):
                if self.isInterruptionRequested():
                    break
                result = future.result()
                if result:
                    path, thumbnail = result
                    completed[path] = thumbnail
        finally:
            executor.shutdown(wait=not self.isInterruptionRequested(), cancel_futures=True)
        self.ready.emit(completed)

    @staticmethod
    def is_eligible(entry: RemoteEntry, maximum_size: int) -> bool:
        suffix = Path(entry.path).suffix.lower()
        is_video = suffix in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv"}
        return not entry.is_dir and (suffix in IMAGE_EXTENSIONS or is_video) and (is_video or entry.size <= maximum_size)

    def _create_thumbnail(self, entry: RemoteEntry, ffmpeg: str | None) -> tuple[str, str] | None:
        if self.isInterruptionRequested() or not ffmpeg:
            return None
        width, height = THUMBNAIL_RENDER_SIZE
        key = hashlib.sha256(
            f"16x9-v3-{width}x{height}-at{VIDEO_THUMBNAIL_SEEK_SECONDS}/"
            f"{self.repo.repo_type}/{self.repo.repo_id}/{entry.path}".encode()
        ).hexdigest()
        target = THUMBNAIL_CACHE_DIR / f"{key}.jpg"
        try:
            if not target.exists():
                url = self.service.get_download_url(self.repo, entry.path)
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
                if Path(entry.path).suffix.lower() in IMAGE_EXTENSIONS:
                    headers = {"Authorization": f"Bearer {self.service.token}"} if self.service.token else {}
                    with urlopen(Request(url, headers=headers), timeout=20) as response:
                        image_bytes = response.read()
                    subprocess.run(command + ["-f", "image2pipe", "-i", "pipe:0", "-frames:v", "1", "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black", "-q:v", "3", str(target)], input=image_bytes, capture_output=True, timeout=45, check=True, creationflags=creationflags)
                else:
                    subprocess.run(command + ["-ss", str(VIDEO_THUMBNAIL_SEEK_SECONDS), "-probesize", "32k", "-analyzeduration", "0", "-i", url, "-map", "0:v:0", "-frames:v", "1", "-an", "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black", "-q:v", "3", str(target)], capture_output=True, timeout=45, check=True, creationflags=creationflags)
            return (entry.path, str(target)) if target.exists() else None
        except Exception:
            return None


class CopyThread(QThread):
    completed = Signal(int, int)
    failed = Signal(str)

    def __init__(self, source_service, source_repo, source_entries, selected, destination_service, destination_repo, destination_folder, parent=None):
        super().__init__(parent)
        self.source_service, self.source_repo, self.source_entries, self.selected = source_service, source_repo, source_entries, selected
        self.destination_service, self.destination_repo, self.destination_folder = destination_service, destination_repo, destination_folder

    def run(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="modelscope-copy-"))
        ok = failed = 0
        try:
            files = [entry for entry in self.source_entries if not entry.is_dir]
            if self.selected.is_dir:
                prefix = self.selected.path.strip("/")
                files = [entry for entry in files if entry.path.startswith(prefix + "/")]
                base = Path(prefix).name
            else:
                files = [self.selected]
                base = ""
            source_parent = self.selected.path.strip("/").rpartition("/")[0]
            same_location = (
                self.source_repo.repo_type == self.destination_repo.repo_type
                and self.source_repo.repo_id == self.destination_repo.repo_id
                and source_parent == normalize_remote_path(self.destination_folder)
            )
            if same_location and self.selected.is_dir:
                base = copy_name(base, is_dir=True)
            for entry in files:
                if self.isInterruptionRequested():
                    break
                relative = entry.path[len(self.selected.path.strip("/")):].strip("/") if self.selected.is_dir else Path(entry.path).name
                if same_location and not self.selected.is_dir:
                    relative = copy_name(relative)
                local = temporary / (base if self.selected.is_dir else "") / relative
                local.parent.mkdir(parents=True, exist_ok=True)
                try:
                    if hasattr(self.source_service, "download_to_file"):
                        self.source_service.download_to_file(self.source_repo, entry.path, local)
                    else:
                        headers = {"Authorization": f"Bearer {self.source_service.token}"} if self.source_service.token else {}
                        request = Request(self.source_service.get_download_url(self.source_repo, entry.path), headers=headers)
                        with urlopen(request, timeout=30) as response, local.open("wb") as output:
                            while chunk := response.read(1024 * 1024):
                                output.write(chunk)
                    target = normalize_remote_path(self.destination_folder, base if self.selected.is_dir else "", relative)
                    self.destination_service.upload_file_as(self.destination_repo, local, target)
                except Exception:
                    failed += 1
                else:
                    ok += 1
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        self.completed.emit(ok, failed)


class DeleteThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, session, repo, paths, parent=None):
        super().__init__(parent)
        self.session, self.repo, self.paths = session, repo, list(paths)

    def run(self) -> None:
        deleted: list[str] = []
        failures: dict[str, str] = {}
        for offset in range(0, len(self.paths), DELETE_BATCH_SIZE):
            batch = self.paths[offset:offset + DELETE_BATCH_SIZE]
            try:
                delete_repository_files(self.session, self.repo.repo_id, self.repo.repo_type, batch)
            except Exception as exc:
                root = posixpath.commonpath(batch)
                if root in batch:
                    root = str(PurePosixPath(root).parent)
                try:
                    present = set(list_repository_file_paths(
                        self.session, self.repo.repo_id, self.repo.repo_type, "" if root == "." else root,
                    ))
                except Exception:
                    present = set(batch)
                missing = [path for path in batch if path not in present]
                deleted.extend(missing)
                for path in (path for path in batch if path in present):
                    try:
                        delete_repository_file(self.session, self.repo.repo_id, self.repo.repo_type, path)
                    except Exception as item_exc:
                        failures[path] = str(item_exc or exc)
                    else:
                        deleted.append(path)
            else:
                deleted.extend(batch)
        self.completed.emit({"deleted": deleted, "failures": failures})


class RelocateThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self, source_service, source_repo, destination_service, destination_repo,
        mappings: dict[str, str], delete_source: Callable[[str], None], parent=None,
    ):
        super().__init__(parent)
        self.source_service, self.source_repo = source_service, source_repo
        self.destination_service, self.destination_repo = destination_service, destination_repo
        self.mappings = dict(mappings)
        self.delete_source = delete_source
        self.result: dict[str, Any] = {}

    def run(self) -> None:
        temporary = Path(tempfile.mkdtemp(prefix="modelscope-relocate-"))
        downloaded: dict[str, Path] = {}
        upload_failed: list[str] = []
        deleted: list[str] = []
        delete_failed: dict[str, str] = {}
        try:
            for index, source in enumerate(self.mappings):
                local = temporary / str(index) / Path(source).name
                local.parent.mkdir(parents=True, exist_ok=True)
                self.source_service.download_to_file(self.source_repo, source, local)
                downloaded[source] = local
            for source, target in self.mappings.items():
                try:
                    self.destination_service.upload_file_as(self.destination_repo, downloaded[source], target)
                except Exception:
                    upload_failed.append(source)
            if not upload_failed:
                for source in sorted(self.mappings, reverse=True):
                    try:
                        self.delete_source(source)
                    except Exception as exc:
                        delete_failed[source] = str(exc)
                    else:
                        deleted.append(source)
        except Exception as exc:
            self.failed.emit(str(exc))
            return
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        self.result = {
            "mappings": self.mappings,
            "upload_failed": upload_failed,
            "deleted": deleted,
            "delete_failed": delete_failed,
        }
        self.completed.emit(self.result)


@dataclass
class UploadQueueItem:
    path: Path
    target: str
    status: str = "waiting"


class UploadCancelled(Exception):
    pass


class UploadThread(QThread):
    item_done = Signal(str, bool, str)
    progress_info = Signal(str, int, float, int)
    cancelled = Signal(str)

    def __init__(
        self,
        service: ModelScopeService,
        repo: Repository,
        item: UploadQueueItem,
        keep_name: bool,
        skipped_files: set[Path] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.service = service
        self.repo = repo
        self.item = item
        self.keep_name = keep_name
        self.skipped_files = {path.resolve() for path in (skipped_files or set())}
        self._resume = threading.Event()
        self._resume.set()
        self._cancel = threading.Event()

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def cancel(self) -> None:
        self._cancel.set()
        self._resume.set()

    def _wait_until_resumed(self) -> None:
        while not self._resume.wait(0.1):
            if self._cancel.is_set():
                raise UploadCancelled()
        if self._cancel.is_set():
            raise UploadCancelled()

    def run(self) -> None:
        path = self.item.path
        item_size = self._path_size(path, self.skipped_files)
        started = time.monotonic()
        last_speed_time = started
        last_speed_bytes = 0
        current_speed = 0.0
        current_size = 0
        progress_lock = threading.Lock()
        success_message = "上传完成"

        def report_bytes(amount: int) -> None:
            nonlocal current_size, last_speed_time, last_speed_bytes, current_speed
            self._wait_until_resumed()
            with progress_lock:
                current_size = min(item_size, current_size + max(0, amount))
            now = time.monotonic()
            interval = now - last_speed_time
            if interval >= 0.15:
                instant = max(0, current_size - last_speed_bytes) / interval
                current_speed = instant if current_speed <= 0 else current_speed * 0.55 + instant * 0.45
                last_speed_time = now
                last_speed_bytes = current_size
            eta = int((item_size - current_size) / current_speed) if current_speed > 0 else -1
            self.progress_info.emit(str(path), min(99, int(current_size * 100 / item_size)), current_speed, eta)

        try:
            self._wait_until_resumed()
            with self.service.track_upload_progress(report_bytes):
                if path.is_dir():
                    skipped_inside = [item for item in self.skipped_files if item.is_relative_to(path)]
                    if skipped_inside:
                        safe_files = [
                            item for item in path.rglob("*")
                            if item.is_file() and item.resolve() not in self.skipped_files
                        ]
                        if not safe_files:
                            success_message = "所有文件均超过 50 GB，已跳过"
                        else:
                            remote_base = normalize_remote_path(
                                self.item.target,
                                path.name if self.keep_name else "",
                            )
                            errors: list[str] = []
                            for safe_file in safe_files:
                                self._wait_until_resumed()
                                relative_parent = safe_file.relative_to(path).parent.as_posix()
                                try:
                                    self.service.upload_file(
                                        self.repo,
                                        safe_file,
                                        normalize_remote_path(remote_base, relative_parent),
                                    )
                                except UploadCancelled:
                                    raise
                                except Exception as exc:
                                    errors.append(f"{safe_file}: {exc}")
                            if errors:
                                raise RuntimeError("；".join(errors))
                    else:
                        self.service.upload_folder(self.repo, path, self.item.target, self.keep_name)
                elif path.is_file():
                    self.service.upload_file(self.repo, path, self.item.target)
                else:
                    raise FileNotFoundError(str(path))
        except UploadCancelled:
            self.cancelled.emit(str(path))
        except Exception as exc:
            self.item_done.emit(str(path), False, str(exc))
        else:
            self.progress_info.emit(str(path), 100, current_speed, 0)
            self.item_done.emit(str(path), True, success_message)

    @staticmethod
    def _path_size(path: Path, skipped_files: set[Path] | None = None) -> int:
        skipped_files = skipped_files or set()
        if path.is_file():
            if path.resolve() in skipped_files:
                return 0
            try:
                return max(1, path.stat().st_size)
            except OSError:
                return 1
        total = 0
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_file():
                    if child.resolve() in skipped_files:
                        continue
                    try:
                        total += child.stat().st_size
                    except OSError:
                        total += 1
        return max(1, total)


class DownloadThread(QThread):
    progress_info = Signal(int, int, float, int)
    item_update = Signal(str, str, int, int, str)
    completed = Signal(int, int)
    failed = Signal(str)

    def __init__(self, runner: Aria2DownloadRunner, specs: list[DownloadSpec], parent: QWidget | None = None):
        super().__init__(parent)
        self.runner = runner
        self.specs = specs

    def run(self) -> None:
        try:
            result = self.runner.run(
                self.specs,
                lambda completed, total, speed, eta: self.progress_info.emit(completed, total, speed, eta),
                lambda spec, status, completed, total, message: self.item_update.emit(
                    str(spec.local_path), status, completed, total, message
                ),
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(*result)


class PotPlayerInstallThread(QThread):
    completed = Signal(str)
    failed = Signal(str)

    def __init__(self, archive: Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.archive = archive

    def run(self) -> None:
        try:
            executable = install_potplayer(
                self.archive, SEVEN_ZIP_ZSTD_EXE, POTPLAYER_DIR,
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        else:
            self.completed.emit(str(executable))


class BackupThread(QThread):
    item_done = Signal(str, bool, str)
    progress_info = Signal(int, int, float, int)
    completed = Signal(str, int, int, int)

    def __init__(
        self,
        store: BackupStore,
        job: BackupJob,
        service: ModelScopeService,
        repo: Repository,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self.job = job
        self.service = service
        self.repo = repo

    def run(self) -> None:
        uploaded = failed = 0
        try:
            changed, oversized = self.store.scan_changes(self.job)
        except Exception as exc:
            self.store.mark_scan(self.job.job_id)
            self.item_done.emit(self.job.local_path, False, str(exc))
            self.completed.emit(self.job.job_id, 0, 1, 0)
            return
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        prefix = normalize_remote_path(
            self.job.dest_dir,
            timestamp if self.job.mode == "incremental" else "",
        )
        total = max(1, sum(item.size for item in changed))
        completed_bytes = 0
        started = time.monotonic()
        for item in changed:
            if self.isInterruptionRequested():
                break
            remote_path = normalize_remote_path(prefix, item.relative_path)
            try:
                self.service.upload_file_as(self.repo, item.path, remote_path)
                self.store.mark_uploaded(self.job.job_id, item, remote_path)
            except Exception as exc:
                failed += 1
                self.item_done.emit(item.relative_path, False, str(exc))
            else:
                uploaded += 1
                self.item_done.emit(item.relative_path, True, remote_path)
            completed_bytes += item.size
            elapsed = max(0.001, time.monotonic() - started)
            speed = completed_bytes / elapsed
            eta = int((total - completed_bytes) / speed) if speed > 0 else -1
            self.progress_info.emit(completed_bytes, total, speed, eta)
        self.store.mark_scan(self.job.job_id)
        self.completed.emit(self.job.job_id, uploaded, failed, len(oversized))


class ImageUploadThread(QThread):
    uploaded = Signal(object)
    item_done = Signal(str, bool, str)
    completed = Signal(int, int)

    def __init__(
        self,
        store: ImageStore,
        account_id: str,
        service: ModelScopeService,
        repo: Repository,
        paths: list[Path],
        destination: str,
        temporary_paths: set[Path] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self.account_id = account_id
        self.service = service
        self.repo = repo
        self.paths = paths
        self.destination = destination
        self.temporary_paths = temporary_paths or set()

    def run(self) -> None:
        ok = failed = 0
        date_folder = datetime.now().strftime("%Y/%m")
        try:
            for path in self.paths:
                if self.isInterruptionRequested():
                    break
                remote_name = f"{uuid.uuid4().hex[:8]}_{path.name}"
                remote_path = normalize_remote_path(self.destination, date_folder, remote_name)
                try:
                    if path.stat().st_size >= 50 * 1024**3:
                        raise ValueError("图片达到或超过 50 GB")
                    self.service.upload_file_as(self.repo, path, remote_path)
                    direct_url = repository_file_url(
                        self.repo,
                        remote_path,
                        repository_is_public(self.repo, self.service.token),
                    )
                    record = self.store.add(
                        self.account_id, self.repo.repo_type, self.repo.repo_id,
                        remote_path, direct_url, path,
                    )
                except Exception as exc:
                    failed += 1
                    self.item_done.emit(str(path), False, str(exc))
                else:
                    ok += 1
                    self.uploaded.emit(record)
                    self.item_done.emit(str(path), True, direct_url)
        finally:
            for path in self.temporary_paths:
                path.unlink(missing_ok=True)
        self.completed.emit(ok, failed)


class FolderIndexThread(QThread):
    repository_indexed = Signal(str)
    completed = Signal(int, int)

    def __init__(
        self,
        index: FolderSizeIndex,
        entry_store: AccountStore,
        jobs: list[tuple[ModelScopeService, Repository, bool, str]],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.index = index
        self.entry_store = entry_store
        self.jobs = jobs

    def run(self) -> None:
        ok = failed = 0
        for service, repo, public, account_id in self.jobs:
            if self.isInterruptionRequested():
                break
            try:
                entries = service.list_entries(repo)
                self.index.update_repository(repo, entries, public)
                if account_id:
                    self.entry_store.cache_entries(account_id, repo, entries)
            except Exception:
                failed += 1
            else:
                ok += 1
                self.repository_indexed.emit(repo.repo_id)
        self.completed.emit(ok, failed)


class RepositoryTree(QTreeWidget):
    paths_dropped = Signal(list, object)

    def __init__(self):
        super().__init__()
        self.drop_directory: RemoteEntry | None = None
        self.setAcceptDrops(True)

    def set_drop_directory(self, directory: RemoteEntry | None) -> None:
        self.drop_directory = directory if directory and directory.is_dir else None

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        target = self._directory_item(item)
        directory = target.data(0, Qt.ItemDataRole.UserRole) if target is not None else self.drop_directory
        if isinstance(directory, RemoteEntry) and directory.is_dir and event.mimeData().hasUrls():
            if target is not None:
                self.setCurrentItem(target)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        item = self.itemAt(event.position().toPoint())
        target = self._directory_item(item)
        entry = target.data(0, Qt.ItemDataRole.UserRole) if target is not None else self.drop_directory
        if not isinstance(entry, RemoteEntry) or not entry.is_dir:
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths, entry)
            event.acceptProposedAction()

    @staticmethod
    def _directory_item(item: QTreeWidgetItem | None) -> QTreeWidgetItem | None:
        while item is not None:
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(entry, RemoteEntry) and entry.is_dir:
                return item
            item = item.parent()
        return None


class RepositoryList(QListWidget):
    paths_dropped = Signal(list, object)

    def __init__(self):
        super().__init__()
        self.drop_directory: RemoteEntry | None = None
        self.setAcceptDrops(True)

    def set_drop_directory(self, directory: RemoteEntry | None) -> None:
        self.drop_directory = directory if directory and directory.is_dir else None

    def _drop_directory_at(self, position) -> RemoteEntry | None:
        item = self.itemAt(position)
        entry = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return entry if isinstance(entry, RemoteEntry) and entry.is_dir else self.drop_directory

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if self._drop_directory_at(event.position().toPoint()) is not None and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        directory = self._drop_directory_at(event.position().toPoint())
        if directory is None:
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths, directory)
            event.acceptProposedAction()


class DropArea(QFrame):
    paths_dropped = Signal(list)

    def __init__(self, title_text: str = "将文件或文件夹拖到这里", note_text: str = "也可以使用下方按钮选择，可一次添加多个项目"):
        super().__init__()
        self.setObjectName("dropArea")
        self.setProperty("dragging", False)
        self.setAcceptDrops(True)
        self.setMinimumHeight(112)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon = QLabel("⇩")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet("font-size: 31px; color: #0067c0; font-weight: 300;")
        title = QLabel(title_text)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        note = QLabel(note_text)
        note.setObjectName("subtitle")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon)
        layout.addWidget(title)
        layout.addWidget(note)
        for child in (icon, title, note):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def _set_dragging(self, value: bool) -> None:
        self.setProperty("dragging", value)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            self._set_dragging(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_dragging(False)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()


class ModelScopeLoginDialog(QDialog):
    session_captured = Signal(object, object)
    session_url = "https://www.modelscope.cn/datasets/ARXChem/Animations-List/tree/master/Violet%20Evergarden"

    def __init__(self, account_label: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"ModelScope 在线登录 · {account_label}")
        self.resize(1080, 760)
        self.setMinimumSize(820, 600)
        self._cookies: dict[str, str] = {}
        self._preparing_session = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        toolbar = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setReadOnly(True)
        toolbar.addWidget(self.address, 1)
        reload_button = QPushButton("刷新")
        reload_button.clicked.connect(lambda: self.web_view.reload())
        toolbar.addWidget(reload_button)
        layout.addLayout(toolbar)

        self.status_label = QLabel("请在下方 ModelScope 官方页面完成短信或账密登录。", objectName="subtitle")
        layout.addWidget(self.status_label)

        self.profile = QWebEngineProfile(self)
        self.profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.MemoryHttpCache)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies)
        self.profile.cookieStore().cookieAdded.connect(self._cookie_added)
        self.profile.cookieStore().cookieRemoved.connect(self._cookie_removed)
        self.page = QWebEnginePage(self.profile, self)
        self.web_view = QWebEngineView(self)
        self.web_view.setPage(self.page)
        self.web_view.urlChanged.connect(lambda url: self.address.setText(url.toString()))
        self.web_view.loadFinished.connect(self._page_loaded)
        layout.addWidget(self.web_view, 1)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        actions.addWidget(cancel_button)
        self.save_button = QPushButton("保存登录信息", objectName="primary")
        self.save_button.clicked.connect(self._save_session)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)
        self.web_view.setUrl(QUrl("https://www.modelscope.cn/login"))

    @staticmethod
    def _cookie_text(value) -> str:
        if isinstance(value, str):
            return value
        return bytes(value).decode("utf-8", errors="ignore")

    @classmethod
    def _cookie_parts(cls, cookie) -> tuple[str, str, str]:
        return (
            cls._cookie_text(cookie.name()),
            cls._cookie_text(cookie.value()),
            cls._cookie_text(cookie.domain()).lstrip("."),
        )

    def _cookie_added(self, cookie) -> None:
        name, value, domain = self._cookie_parts(cookie)
        if domain == "modelscope.cn" or domain.endswith(".modelscope.cn"):
            if name in {"m_session_id", "csrf_session", "csrf_token"}:
                self._cookies[name] = value
                self._update_capture_status()

    def _cookie_removed(self, cookie) -> None:
        name, value, domain = self._cookie_parts(cookie)
        if (domain == "modelscope.cn" or domain.endswith(".modelscope.cn")) and self._cookies.get(name) == value:
            self._cookies.pop(name, None)
            self._update_capture_status()

    def _update_capture_status(self) -> None:
        missing = [name for name in ("m_session_id", "csrf_session", "csrf_token") if not self._cookies.get(name)]
        if not missing:
            self.status_label.setText("已检测到登录会话。请确认页面已登录成功，然后保存登录信息。")
        elif self._preparing_session:
            self.status_label.setText("正在从仓库页面取得删除凭据，尚缺少：" + "、".join(missing))

    def _page_loaded(self, ok: bool) -> None:
        if not ok or not self._preparing_session:
            return
        self.profile.cookieStore().loadAllCookies()
        QTimer.singleShot(800, self._finish_preparing_session)

    def _finish_preparing_session(self) -> None:
        self._preparing_session = False
        missing = [name for name in ("m_session_id", "csrf_session", "csrf_token") if not self._cookies.get(name)]
        if missing:
            self.status_label.setText("登录信息仍不完整，缺少：" + "、".join(missing))
            QMessageBox.warning(
                self,
                "登录信息不完整",
                "未能取得删除所需的网页登录信息：" + "、".join(missing) + "。请确认页面显示已登录后重试。",
            )
            return
        self._validate_and_save_session()

    def _save_session(self) -> None:
        self.profile.cookieStore().loadAllCookies()
        missing = [name for name in ("m_session_id", "csrf_session", "csrf_token") if not self._cookies.get(name)]
        if missing:
            if not self._cookies.get("m_session_id"):
                QMessageBox.information(self, "尚未登录", "尚未检测到 ModelScope 登录会话，请先完成登录。")
                return
            self._preparing_session = True
            self.status_label.setText("正在进入仓库页面取得删除凭据…")
            self.web_view.setUrl(QUrl(self.session_url))
            return
        self._validate_and_save_session()

    def _validate_and_save_session(self) -> None:
        try:
            session = ModelScopeWebSession(
                self._cookies.get("m_session_id", ""),
                self._cookies.get("csrf_session", ""),
                self._cookies.get("csrf_token", ""),
            )
            self.status_label.setText("正在验证网页登录状态…")
            QApplication.processEvents()
            user_info = fetch_web_user_info(session)
        except Exception as exc:
            self.status_label.setText("尚未取得有效登录状态，请完成登录后重试。")
            QMessageBox.warning(self, "在线登录验证失败", str(exc))
            return
        self.session_captured.emit(session, user_info)
        self.accept()

    def done(self, result: int) -> None:
        self.profile.cookieStore().deleteAllCookies()
        super().done(result)


class MainWindow(FluentWindow):
    def __init__(self):
        self._event_filter_ready = False
        super().__init__()
        self.setWindowTitle("ModelScope Manager")
        self.resize(1180, 760)
        self.setMinimumSize(980, 650)
        self.settings = portable_settings()
        self.device_id, identity_replaced = DeviceIdentity(DEVICE_ID_PATH).load_or_create()
        self.token_destroyed_on_start = bool(identity_replaced and self.settings.contains("token"))
        initialize_database(MANAGER_DB_PATH, FOLDER_INDEX_PATH)
        self.account_store = AccountStore(MANAGER_DB_PATH, self.device_id, identity_replaced)
        self.backup_store = BackupStore(MANAGER_DB_PATH)
        self.image_store = ImageStore(MANAGER_DB_PATH, IMAGE_CACHE_DIR)
        self.token_destroyed_on_start = self.token_destroyed_on_start or self.account_store.tokens_destroyed
        self.locale = LocaleManager(str(self.settings.value("language", "zh_CN")))
        self.public_pool_store = PublicPoolStore(PUBLIC_POOLS_PATH)
        self.folder_index = FolderSizeIndex(MANAGER_DB_PATH)
        self.accounts: list[AccountRecord] = []
        self.web_accounts: list[WebAccountRecord] = []
        self.session_tokens: dict[str, str] = {}
        self.account_services: dict[str, ModelScopeService] = {}
        self.account_repositories: dict[str, list[Repository]] = {}
        self.active_account_id: str | None = None
        self.active_account_kind: str | None = None
        self.service: ModelScopeService | None = None
        self.repositories: list[Repository] = []
        self.selected_repo: Repository | None = None
        self.selected_repo_public = False
        self.remote_entries: list[RemoteEntry] = []
        self.remote_direct_cache: dict[str, list[RemoteEntry]] = {}
        self.current_directory_path = ""
        self.directory_history: list[str] = []
        self.resource_view_mode = "details"
        self.detail_sort_column = 0
        self.detail_sort_order = Qt.SortOrder.AscendingOrder
        self.global_search_sort_column = 0
        self.global_search_sort_order = Qt.SortOrder.AscendingOrder
        self.public_search_sort_column = 0
        self.public_search_sort_order = Qt.SortOrder.AscendingOrder
        self.thumbnail_task: ThumbnailThread | None = None
        self.thumbnail_paths: dict[str, str] = {}
        self.thumbnail_attempted: set[str] = set()
        self.thumbnail_queue: deque[RemoteEntry] = deque()
        self.thumbnail_queued: set[str] = set()
        self._last_user_interaction = time.monotonic()
        self.thumbnail_timer = QTimer(self)
        self.thumbnail_timer.setSingleShot(True)
        self.thumbnail_timer.setInterval(100)
        self.thumbnail_timer.timeout.connect(self._load_visible_thumbnails)
        self.copy_source: tuple[ModelScopeService, Repository, list[RemoteEntry], RemoteEntry] | None = None
        self.copy_task: CopyThread | None = None
        self.move_source: tuple[str, ModelScopeService, Repository, list[RemoteEntry], RemoteEntry] | None = None
        self.delete_task: DeleteThread | None = None
        self.relocate_task: RelocateThread | None = None
        self.relocate_context: tuple[str, Repository, str, Repository, list[RemoteEntry]] | None = None
        self.global_search_results: list[IndexedEntry] = []
        self.pending_search_path: str = ""
        self.upload_items: list[UploadQueueItem] = []
        self.upload_session_service: ModelScopeService | None = None
        self.upload_session_repo: Repository | None = None
        self.upload_session_account_id: str | None = None
        self.upload_ok = 0
        self.upload_failed = 0
        self.upload_cancelled = 0
        self.download_specs: list[DownloadSpec] = []
        self.download_states: dict[str, str] = {}
        self.active_download_specs: list[DownloadSpec] = []
        self.download_runner: Aria2DownloadRunner | None = None
        self.backup_jobs: list[BackupJob] = []
        self.backup_thread: BackupThread | None = None
        self.backup_automatic = False
        self.backup_sync_job_paths: dict[str, str] = {}
        self.image_records: list[ImageRecord] = []
        self.image_upload_thread: ImageUploadThread | None = None
        self.media_proxy = AuthenticatedMediaProxy()
        self.potplayer_install_archive: Path | None = None
        self.potplayer_install_thread: PotPlayerInstallThread | None = None
        self.search_service: ModelScopeService | None = None
        self.search_repo: Repository | None = None
        self.search_entries: list[RemoteEntry] = []
        self.external_players: list[dict[str, str]] = []
        self._image_repository_selections: dict[str, tuple[str, str]] = {}
        self.search_history_window: QWidget | None = None
        self.transfer_policy = TransferPolicy()
        self.webdav: ModelScopeWebDAV | None = None
        self.index_task: FolderIndexThread | None = None
        self._index_refresh_pending = False
        self.index_inflight_keys: set[tuple[str, str, str, bool]] = set()
        self.dirty_repositories: set[tuple[str, str, str, bool]] = set()
        self.current_upload_speed = 0.0
        self.current_download_speed = 0.0
        self._force_close = False
        self._restoring_settings = False
        self.task: QThread | None = None
        self.resource_search_timer = QTimer(self)
        self.resource_search_timer.setSingleShot(True)
        self.resource_search_timer.setInterval(80)
        self.resource_search_timer.timeout.connect(self._perform_global_search)
        self.backup_timer = QTimer(self)
        self.backup_timer.setInterval(30000)
        self.backup_timer.timeout.connect(self._check_backup_schedule)
        self.index_idle_timer = QTimer(self)
        self.index_idle_timer.setSingleShot(True)
        self.index_idle_timer.setInterval(5000)
        self.index_idle_timer.timeout.connect(self._run_idle_index_refresh)
        self.background_index_timer = QTimer(self)
        self.background_index_timer.timeout.connect(self._background_index_tick)
        self.transfer_policy_timer = QTimer(self)
        self.transfer_policy_timer.setInterval(30000)
        self.transfer_policy_timer.timeout.connect(self._refresh_transfer_limit_status)
        self._build_ui()
        hints = QApplication.instance().styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._system_theme_changed)
        QApplication.instance().installEventFilter(self)
        self._restore_settings()
        self._apply_language()
        self._build_tray()
        if self.token_destroyed_on_start:
            self.account_label.setText(self._t("检测到设备变化，已销毁已保存的访问令牌"))
            self._log("检测到设备变化，已销毁已保存的访问令牌")
        if any(account.token for account in self.accounts) or any(
            self.account_store.load_web_session(account.account_id) for account in self.web_accounts
        ):
            QTimer.singleShot(0, self.load_repositories)
        else:
            if self.alist_auto_start.isChecked():
                QTimer.singleShot(0, self.apply_alist_settings)
            QTimer.singleShot(0, lambda: self._start_folder_indexing(True))
        self.backup_timer.start()
        self.transfer_policy_timer.start()
        self._event_filter_ready = True

    def _build_ui(self) -> None:
        self.page_stack = self.stackedWidget
        self.navigationInterface.setExpandWidth(210)
        self.navigationInterface.setMinimumExpandWidth(920)
        self.navigationInterface.setAcrylicEnabled(True)
        self.status_bar = QStatusBar(self)
        self.status_bar.setObjectName("fluentStatusBar")
        self.status_bar.showMessage("ModelScope Manager 1.0.3")
        self.widgetLayout.removeWidget(self.stackedWidget)
        content_layout = QVBoxLayout()
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        content_layout.addWidget(self.stackedWidget, 1)
        content_layout.addWidget(self.status_bar)
        self.widgetLayout.addLayout(content_layout, 1)

        # Resource manager page
        resource_page = QWidget()
        self.resource_page = resource_page
        resource_layout = QVBoxLayout(resource_page)
        resource_layout.setContentsMargins(24, 20, 24, 22)
        resource_layout.setSpacing(14)
        # 保留后台状态承载控件，避免移除页面标题后打断异步任务状态链路。
        self.repo_heading = QLabel()
        self.repo_heading.setVisible(False)
        resource_heading = QHBoxLayout()
        self.view_button = QPushButton("查看 ▾")
        view_menu = QMenu(self)
        detailed_action = view_menu.addAction("详细信息模式")
        thumbnail_action = view_menu.addAction("缩略图模式")
        detailed_action.triggered.connect(lambda: self._set_view_mode("details"))
        thumbnail_action.triggered.connect(lambda: self._set_view_mode("thumbnails"))
        self.view_button.setMenu(view_menu)
        self.refresh_repos_button = QPushButton("读取 / 刷新仓库")
        self.refresh_repos_button.clicked.connect(self.load_repositories)
        resource_heading.addWidget(self.refresh_repos_button)
        self.update_index_button = QPushButton("更新索引")
        self.update_index_button.clicked.connect(self.update_all_indexes)
        resource_heading.addWidget(self.update_index_button)
        self.refresh_files_button = QPushButton("刷新目录")
        self.refresh_files_button.setEnabled(False)
        self.refresh_files_button.clicked.connect(self.load_remote_files)
        resource_heading.addWidget(self.refresh_files_button)
        resource_heading.addWidget(self.view_button)
        resource_heading.addWidget(QLabel("分组依据"))
        self.group_by_combo = QComboBox()
        self.group_by_combo.addItem("无", "")
        self.group_by_combo.addItem("名称", "name")
        self.group_by_combo.addItem("类型", "type")
        self.group_by_combo.addItem("大小", "size")
        self.group_by_combo.currentIndexChanged.connect(self._render_remote_details)
        resource_heading.addWidget(self.group_by_combo)
        self.compact_view_button = QCheckBox("紧凑视图")
        self.compact_view_button.toggled.connect(self._compact_view_changed)
        resource_heading.addWidget(self.compact_view_button)
        resource_heading.addStretch()
        self.resource_search_scope = QComboBox()
        self.resource_search_scope.addItem("全部仓库", "all")
        self.resource_search_scope.addItem("当前账户", "account")
        self.resource_search_scope.addItem("当前仓库", "repository")
        self.resource_search_scope.addItem("当前目录", "directory")
        self.resource_search_scope.setMinimumWidth(112)
        self.resource_search_scope.currentIndexChanged.connect(self._schedule_global_search)
        self.resource_search_type = QComboBox()
        self.resource_search_type.addItem("全部类型", "all")
        self.resource_search_type.addItem("视频", "video")
        self.resource_search_type.addItem("图片", "image")
        self.resource_search_type.addItem("文档", "document")
        self.resource_search_type.addItem("压缩包", "archive")
        self.resource_search_type.setMinimumWidth(100)
        self.resource_search_type.currentIndexChanged.connect(self._schedule_global_search)
        self.resource_search_tag = QComboBox()
        self.resource_search_tag.setMinimumWidth(108)
        self.resource_search_tag.currentIndexChanged.connect(self._schedule_global_search)
        self.resource_search_edit = QLineEdit()
        self.resource_search_edit.setPlaceholderText("高级搜索：路径片段 文件名前段 后段")
        self.resource_search_edit.setClearButtonEnabled(True)
        self.resource_search_edit.setMinimumWidth(220)
        self.resource_search_edit.textChanged.connect(self._schedule_global_search)
        self.resource_search_edit.returnPressed.connect(self._perform_global_search)
        self.resource_search_button = QPushButton("搜索")
        self.resource_search_button.clicked.connect(self.show_resource_search)
        self.resource_back_button = QPushButton("返回上一级")
        self.resource_back_button.setEnabled(False)
        self.resource_back_button.clicked.connect(self._go_to_parent_directory)
        resource_heading.addWidget(self.resource_back_button)
        resource_heading.addWidget(self.resource_search_button)
        resource_layout.addLayout(resource_heading)

        resource_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.resource_splitter = resource_splitter
        resource_splitter.setChildrenCollapsible(False)
        resource_splitter.setHandleWidth(9)
        repo_card = QFrame(objectName="card")
        repo_card.setMinimumWidth(245)
        repo_layout = QVBoxLayout(repo_card)
        repo_layout.setContentsMargins(16, 16, 16, 16)
        repo_layout.addWidget(QLabel("仓库", objectName="section"))
        self.type_combo = QComboBox()
        self.type_combo.addItem("全部（模型 + 数据集）", "all")
        self.type_combo.addItem("模型仓库", "model")
        self.type_combo.addItem("数据集仓库", "dataset")
        self.type_combo.currentIndexChanged.connect(self._render_repositories)
        repo_layout.addWidget(self.type_combo)
        self.repo_list = QTreeWidget()
        self.repo_list.setHeaderHidden(True)
        self.repo_list.itemSelectionChanged.connect(self._repo_selected)
        self.repo_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.repo_list.customContextMenuRequested.connect(self._repository_context_menu)
        repo_layout.addWidget(self.repo_list, 1)
        resource_splitter.addWidget(repo_card)

        explorer_card = QFrame(objectName="card")
        explorer_layout = QVBoxLayout(explorer_card)
        explorer_layout.setContentsMargins(16, 16, 16, 16)
        explorer_toolbar = QHBoxLayout()
        self.resource_path_label = PathBreadcrumb()
        self.resource_path_label.path_selected.connect(self._go_to_directory)
        explorer_toolbar.addWidget(self.resource_path_label, 1)
        self.new_folder_button = QPushButton("新建文件夹")
        self.new_folder_button.setEnabled(False)
        self.new_folder_button.clicked.connect(self.new_folder)
        explorer_toolbar.addWidget(self.new_folder_button)
        self.download_selected_button = QPushButton("下载文件")
        self.download_selected_button.setEnabled(False)
        self.download_selected_button.clicked.connect(self._download_selected_remote)
        explorer_toolbar.addWidget(self.download_selected_button)
        self.web_manage_button = QPushButton("删除")
        self.web_manage_button.setEnabled(False)
        self.web_manage_button.clicked.connect(self._delete_selected_remote)
        explorer_toolbar.addWidget(self.web_manage_button)
        explorer_layout.addLayout(explorer_toolbar)
        self.resource_drop_hint = QLabel("将本地文件或文件夹拖到下方任意目录，即可直接上传到该目录", objectName="dropHint")
        explorer_layout.addWidget(self.resource_drop_hint)
        self.remote_tree = RepositoryTree()
        self.remote_tree.setParent(explorer_card)
        self.remote_tree.setObjectName("repositoryTree")
        self.remote_tree.setHeaderLabels(["名称", "类型", "大小", "路径"])
        remote_header = self.remote_tree.header()
        remote_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        remote_header.setStretchLastSection(True)
        self.remote_tree.setColumnWidth(0, 300)
        self.remote_tree.setColumnWidth(1, 90)
        self.remote_tree.setColumnWidth(2, 100)
        self.remote_tree.setColumnWidth(3, 260)
        self.remote_tree.itemSelectionChanged.connect(self._remote_selected)
        self.remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_tree.customContextMenuRequested.connect(self._remote_context_menu)
        self.remote_tree.paths_dropped.connect(self._repository_paths_dropped)
        self.remote_tree.setVisible(False)
        self.remote_detail_tree = RepositoryTree()
        self.remote_detail_tree.setObjectName("repositoryTree")
        self.remote_detail_tree.setHeaderLabels(["名称", "类型", "大小"])
        detail_header = self.remote_detail_tree.header()
        detail_header.setSortIndicatorShown(True)
        detail_header.setSectionsClickable(True)
        detail_header.sectionClicked.connect(self._change_detail_sort)
        self.remote_detail_tree.setColumnWidth(0, 280)
        self.remote_detail_tree.setColumnWidth(1, 90)
        self.remote_detail_tree.itemSelectionChanged.connect(self._remote_detail_selected)
        self.remote_detail_tree.itemDoubleClicked.connect(self._open_remote_detail)
        self.remote_detail_tree.paths_dropped.connect(self._repository_paths_dropped)
        self.remote_detail_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_detail_tree.customContextMenuRequested.connect(self._remote_detail_context_menu)
        explorer_layout.addWidget(self.remote_detail_tree, 1)
        self.remote_thumbnail_list = RepositoryList()
        self.remote_thumbnail_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.remote_thumbnail_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.remote_thumbnail_list.setMovement(QListWidget.Movement.Static)
        self.remote_thumbnail_list.setWordWrap(True)
        self.remote_thumbnail_list.setIconSize(QSize(208, 117))
        self.remote_thumbnail_list.setGridSize(QSize(224, 154))
        self.remote_thumbnail_list.setSpacing(6)
        self.remote_thumbnail_list.setUniformItemSizes(True)
        self.remote_thumbnail_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.remote_thumbnail_list.itemSelectionChanged.connect(self._remote_thumbnail_selected)
        self.remote_thumbnail_list.itemDoubleClicked.connect(self._open_remote_thumbnail)
        self.remote_thumbnail_list.paths_dropped.connect(self._repository_paths_dropped)
        self.remote_thumbnail_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.remote_thumbnail_list.customContextMenuRequested.connect(self._remote_thumbnail_context_menu)
        self.remote_thumbnail_list.setVisible(False)
        explorer_layout.addWidget(self.remote_thumbnail_list, 1)
        self.global_search_label = QLabel("", objectName="subtitle")
        self.global_search_label.setVisible(False)
        explorer_layout.addWidget(self.global_search_label)
        self.global_search_tree = QTreeWidget()
        self.global_search_tree.setObjectName("repositoryTree")
        self.global_search_tree.setHeaderLabels(["名称", "类型", "大小", "仓库", "路径"])
        global_header = self.global_search_tree.header()
        global_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        global_header.setStretchLastSection(True)
        global_header.setSortIndicatorShown(True)
        global_header.sectionClicked.connect(self._change_global_search_sort)
        self.global_search_tree.setColumnWidth(0, 240)
        self.global_search_tree.setColumnWidth(1, 90)
        self.global_search_tree.setColumnWidth(2, 100)
        self.global_search_tree.setColumnWidth(3, 230)
        self.global_search_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.global_search_tree.customContextMenuRequested.connect(self._global_search_context_menu)
        self.global_search_tree.itemDoubleClicked.connect(self._open_global_search_result)
        self.global_search_tree.setVisible(False)
        explorer_layout.addWidget(self.global_search_tree, 1)
        resource_splitter.addWidget(explorer_card)
        resource_splitter.setSizes([270, 820])
        resource_layout.addWidget(resource_splitter, 1)

        # Transfer page
        transfer_page = QWidget()
        transfer_layout = QVBoxLayout(transfer_page)
        transfer_layout.setContentsMargins(24, 20, 24, 22)
        transfer_layout.setSpacing(12)
        transfer_layout.addWidget(QLabel("传输列表", objectName="title"))
        self.queue_tabs = QTabWidget()
        upload_page = QWidget()
        upload_layout = QVBoxLayout(upload_page)
        upload_layout.setContentsMargins(8, 12, 8, 8)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("上传到："))
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("根目录")
        target_row.addWidget(self.target_edit, 1)
        upload_layout.addLayout(target_row)
        self.drop_area = DropArea()
        self.drop_area.paths_dropped.connect(self.add_paths)
        upload_layout.addWidget(self.drop_area)
        pick_row = QHBoxLayout()
        files_button = QPushButton("选择文件")
        files_button.clicked.connect(self.pick_files)
        folder_button = QPushButton("选择文件夹")
        folder_button.clicked.connect(self.pick_folder)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self.clear_queue)
        pick_row.addWidget(files_button)
        pick_row.addWidget(folder_button)
        pick_row.addWidget(clear_button)
        pick_row.addStretch()
        upload_layout.addLayout(pick_row)
        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setHorizontalHeaderLabels(["本地项目", "类型", "目标路径", "状态"])
        queue_header = self.queue_table.horizontalHeader()
        queue_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        queue_header.setMinimumSectionSize(70)
        queue_header.setStretchLastSection(False)
        self.queue_table.setColumnWidth(0, 360)
        self.queue_table.setColumnWidth(1, 90)
        self.queue_table.setColumnWidth(2, 260)
        self.queue_table.setColumnWidth(3, 130)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.EditKeyPressed)
        upload_layout.addWidget(self.queue_table, 1)
        upload_options = QHBoxLayout()
        self.keep_folder_name = QCheckBox("保留文件夹名称")
        self.keep_folder_name.setChecked(True)
        upload_options.addWidget(self.keep_folder_name)
        upload_options.addStretch()
        self.upload_button = QPushButton("开始上传", objectName="primary")
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self.start_upload)
        upload_options.addWidget(self.upload_button)
        self.pause_upload_button = QPushButton("暂停")
        self.pause_upload_button.setEnabled(False)
        self.pause_upload_button.clicked.connect(self.pause_upload)
        upload_options.addWidget(self.pause_upload_button)
        self.resume_upload_button = QPushButton("恢复")
        self.resume_upload_button.setEnabled(False)
        self.resume_upload_button.clicked.connect(self.resume_upload)
        upload_options.addWidget(self.resume_upload_button)
        self.cancel_upload_button = QPushButton("取消")
        self.cancel_upload_button.setEnabled(False)
        self.cancel_upload_button.clicked.connect(self.cancel_upload)
        upload_options.addWidget(self.cancel_upload_button)
        upload_layout.addLayout(upload_options)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFormat("%p%")
        upload_layout.addWidget(self.progress)
        self.upload_stats = QLabel("速度：-- · 剩余：--", objectName="subtitle")
        upload_layout.addWidget(self.upload_stats)
        self.queue_tabs.addTab(upload_page, "上传")

        download_page = QWidget()
        download_layout = QVBoxLayout(download_page)
        download_layout.setContentsMargins(8, 12, 8, 8)
        download_layout.addWidget(QLabel("在资源管理页面右键文件或文件夹，可添加到此队列。", objectName="subtitle"))
        self.download_table = QTableWidget(0, 3)
        self.download_table.setHorizontalHeaderLabels(["远端资源", "本地位置", "状态"])
        download_header = self.download_table.horizontalHeader()
        download_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        download_header.setMinimumSectionSize(80)
        download_header.setStretchLastSection(True)
        self.download_table.setColumnWidth(0, 300)
        self.download_table.setColumnWidth(1, 420)
        self.download_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.download_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        download_layout.addWidget(self.download_table, 1)
        download_buttons = QHBoxLayout()
        clear_download_button = QPushButton("清空")
        clear_download_button.clicked.connect(self.clear_download_queue)
        download_buttons.addWidget(clear_download_button)
        download_buttons.addStretch()
        self.download_button = QPushButton("开始下载", objectName="primary")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.start_download)
        download_buttons.addWidget(self.download_button)
        self.pause_download_button = QPushButton("暂停")
        self.pause_download_button.setEnabled(False)
        self.pause_download_button.clicked.connect(self.pause_download)
        download_buttons.addWidget(self.pause_download_button)
        self.resume_download_button = QPushButton("恢复")
        self.resume_download_button.setEnabled(False)
        self.resume_download_button.clicked.connect(self.resume_download)
        download_buttons.addWidget(self.resume_download_button)
        self.stop_download_button = QPushButton("停止")
        self.stop_download_button.setEnabled(False)
        self.stop_download_button.clicked.connect(self.stop_download)
        download_buttons.addWidget(self.stop_download_button)
        download_layout.addLayout(download_buttons)
        self.download_progress = QProgressBar()
        self.download_progress.setRange(0, 100)
        self.download_progress.setFormat("%p%")
        download_layout.addWidget(self.download_progress)
        self.download_stats = QLabel("速度：-- · 剩余：--", objectName="subtitle")
        download_layout.addWidget(self.download_stats)
        self.queue_tabs.addTab(download_page, "下载")
        transfer_layout.addWidget(self.queue_tabs, 1)
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(110)
        self.log.setPlaceholderText("传输记录会显示在这里")
        transfer_layout.addWidget(self.log)

        # Settings page
        settings_page = QWidget()
        self.settings_page = settings_page
        settings_page_layout = QVBoxLayout(settings_page)
        settings_page_layout.setContentsMargins(0, 0, 0, 0)
        settings_scroll = FluentScrollArea()
        settings_scroll.setObjectName("settingsScroll")
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_content = QWidget()
        settings_content.setObjectName("settingsContent")
        settings_layout = QVBoxLayout(settings_content)
        settings_layout.setContentsMargins(28, 24, 28, 28)
        settings_layout.setSpacing(18)
        settings_layout.addWidget(QLabel("设置", objectName="title"))
        settings_notice_row = QHBoxLayout()
        self.disable_settings_wheel = FluentSwitchButton()
        self.disable_settings_wheel.setChecked(True)
        self.disable_settings_wheel.toggled.connect(self._wheel_setting_changed)
        settings_notice_row.addWidget(self.disable_settings_wheel)
        settings_notice_row.addWidget(QLabel("本页内容自动保存", objectName="subtitle"))
        settings_notice_row.addStretch()
        token_card = QFrame(objectName="card")
        token_layout = QVBoxLayout(token_card)
        token_layout.setContentsMargins(20, 18, 20, 20)
        token_layout.addWidget(QLabel("账号设置 · ModelScope 账户", objectName="panelTitle"))
        token_heading = QHBoxLayout()
        self.token_heading_label = QLabel("Token 登录", objectName="section")
        token_heading.addWidget(self.token_heading_label, 0, Qt.AlignmentFlag.AlignTop)
        token_heading.addStretch()
        self.add_account_button = ToolButton(FIF.ADD)
        self.add_account_button.setFixedSize(38, 36)
        self.add_account_button.setToolTip("添加账户")
        self.add_account_button.clicked.connect(self.add_account)
        token_heading.addWidget(self.add_account_button, 0, Qt.AlignmentFlag.AlignTop)
        self.remove_account_button = ToolButton(FIF.REMOVE)
        self.remove_account_button.setFixedSize(38, 36)
        self.remove_account_button.setToolTip("移除所选账户")
        self.remove_account_button.clicked.connect(self.remove_account)
        token_heading.addWidget(self.remove_account_button, 0, Qt.AlignmentFlag.AlignTop)
        token_layout.addLayout(token_heading)
        token_layout.addWidget(QLabel("Token 使用设备绑定加密；添加后会自动验证并取得用户名。", objectName="subtitle"))
        self.account_table = QTableWidget(0, 5)
        self.account_table.setHorizontalHeaderLabels(["账户名称", "用户名", "Token", "安全记住", "状态"])
        self.account_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.account_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.account_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.account_table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.account_table.verticalHeader().setDefaultSectionSize(42)
        self.account_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.account_table.horizontalHeader().setStretchLastSection(True)
        self.account_table.setColumnWidth(0, 160)
        self.account_table.setColumnWidth(1, 150)
        self.account_table.setColumnWidth(2, 190)
        self.account_table.setColumnWidth(3, 90)
        self.account_table.setMaximumHeight(190)
        self.account_table.itemSelectionChanged.connect(self._account_selected)
        token_layout.addWidget(self.account_table)
        self.account_name_edit = QLineEdit()
        self.account_name_edit.setPlaceholderText("账户名称（可选）")
        token_layout.addWidget(self.account_name_edit)
        self.token_edit = QLineEdit()
        self.token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_edit.setPlaceholderText("输入 ModelScope Access Token")
        self.token_edit.returnPressed.connect(self.connect_account)
        token_layout.addWidget(self.token_edit)
        token_options = QHBoxLayout()
        self.show_token = QCheckBox("显示")
        self.show_token.toggled.connect(lambda checked: self.token_edit.setEchoMode(QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password))
        self.remember_token = QCheckBox("安全记住")
        self.remember_token.setChecked(True)
        token_options.addWidget(self.show_token)
        token_options.addWidget(self.remember_token)
        token_options.addStretch()
        token_layout.addLayout(token_options)
        self.connect_button = QPushButton("保存并验证账户", objectName="primary")
        self.connect_button.clicked.connect(self.connect_account)
        token_layout.addWidget(self.connect_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.account_label = QLabel("尚未连接", objectName="subtitle")
        token_layout.addWidget(self.account_label)
        token_layout.addSpacing(12)
        self.online_separator = QFrame()
        self.online_separator.setFrameShape(QFrame.Shape.HLine)
        token_layout.addWidget(self.online_separator)
        online_heading = QHBoxLayout()
        online_heading.addWidget(QLabel("ModelScope 账户 在线登录", objectName="section"))
        online_heading.addStretch()
        add_web_account_button = ToolButton(FIF.ADD)
        add_web_account_button.setFixedSize(38, 36)
        add_web_account_button.setToolTip("添加网页登录账户")
        add_web_account_button.clicked.connect(self.add_web_account)
        online_heading.addWidget(add_web_account_button)
        remove_web_account_button = ToolButton(FIF.REMOVE)
        remove_web_account_button.setFixedSize(38, 36)
        remove_web_account_button.setToolTip("移除所选网页登录账户")
        remove_web_account_button.clicked.connect(self.remove_web_account)
        online_heading.addWidget(remove_web_account_button)
        token_layout.addLayout(online_heading)
        token_layout.addWidget(QLabel(
            "网页登录账户独立保存；密码和短信验证码仅输入 ModelScope 官方页面。",
            objectName="subtitle",
        ))
        self.web_account_table = QTableWidget(0, 4)
        self.web_account_table.setHorizontalHeaderLabels(["账户名称", "用户名", "状态", "操作"])
        self.web_account_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.web_account_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.web_account_table.verticalHeader().setDefaultSectionSize(42)
        self.web_account_table.horizontalHeader().setStretchLastSection(True)
        self.web_account_table.setColumnWidth(0, 190)
        self.web_account_table.setColumnWidth(1, 160)
        self.web_account_table.setColumnWidth(2, 90)
        self.web_account_table.setMaximumHeight(190)
        self.web_account_table.itemChanged.connect(self._web_account_item_changed)
        token_layout.addWidget(self.web_account_table)

        download_card = QFrame(objectName="card")
        download_setting_layout = QVBoxLayout(download_card)
        download_setting_layout.setContentsMargins(20, 18, 20, 20)
        download_setting_layout.setSpacing(14)
        download_setting_layout.addWidget(QLabel("下载设置", objectName="panelTitle"))
        download_setting_layout.addWidget(QLabel("默认下载路径", objectName="subtitle"))
        download_path_row = QHBoxLayout()
        self.download_path_edit = QLineEdit()
        self.download_path_edit.setReadOnly(True)
        download_path_row.addWidget(self.download_path_edit, 1)
        change_download_button = QPushButton("修改")
        change_download_button.clicked.connect(self.change_download_path)
        download_path_row.addWidget(change_download_button)
        download_setting_layout.addLayout(download_path_row)
        drop_threshold_row = QHBoxLayout()
        drop_threshold_row.addWidget(QLabel("拖放上传阈值", objectName="subtitle"))
        self.drop_upload_threshold_mb = QSpinBox()
        self.drop_upload_threshold_mb.setRange(1, 1024 * 1024)
        self.drop_upload_threshold_mb.setValue(1024)
        self.drop_upload_threshold_mb.setSuffix(" MB")
        self.drop_upload_threshold_mb.valueChanged.connect(self._drop_upload_threshold_changed)
        drop_threshold_row.addWidget(self._stepper(self.drop_upload_threshold_mb), 1)
        download_setting_layout.addLayout(drop_threshold_row)

        player_card = QFrame(objectName="card")
        player_layout = QVBoxLayout(player_card)
        player_layout.setContentsMargins(20, 18, 20, 20)
        player_layout.addWidget(QLabel("播放设置", objectName="panelTitle"))
        self.builtin_player_enabled = FluentSwitchButton("使用本地 PotPlayer 作为默认视频/图片播放器")
        self.builtin_player_enabled.setChecked(True)
        self.builtin_player_enabled.toggled.connect(self._builtin_player_setting_changed)
        player_layout.addWidget(self.builtin_player_enabled)
        player_layout.addWidget(QLabel(
            "播放器不随程序预装。点击下载后将使用内置 aria2-next 获取并校验 PotPlayer.7z，再通过 7z-zstd 解压到本地。",
            objectName="subtitle",
        ))
        player_install_row = QHBoxLayout()
        self.potplayer_install_button = QPushButton("下载并安装 PotPlayer", objectName="primary")
        self.potplayer_install_button.clicked.connect(self.install_potplayer_from_modelscope)
        player_install_row.addWidget(self.potplayer_install_button)
        self.potplayer_folder_button = QPushButton("打开播放器目录")
        self.potplayer_folder_button.clicked.connect(self.open_potplayer_folder)
        player_install_row.addWidget(self.potplayer_folder_button)
        player_install_row.addStretch()
        player_layout.addLayout(player_install_row)
        self.builtin_player_status = QLabel("PotPlayer：正在检查", objectName="subtitle")
        player_layout.addWidget(self.builtin_player_status)
        player_heading = QHBoxLayout()
        self.player_heading_label = QLabel("第三方播放器", objectName="section")
        player_heading.addWidget(self.player_heading_label, 0, Qt.AlignmentFlag.AlignTop)
        player_heading.addStretch()
        self.add_player_button = ToolButton(FIF.ADD)
        self.add_player_button.setFixedSize(38, 36)
        self.add_player_button.setToolTip("添加播放器")
        self.add_player_button.clicked.connect(self.add_external_player)
        player_heading.addWidget(self.add_player_button, 0, Qt.AlignmentFlag.AlignTop)
        self.remove_player_button = ToolButton(FIF.REMOVE)
        self.remove_player_button.setFixedSize(38, 36)
        self.remove_player_button.setToolTip("删除所选播放器")
        self.remove_player_button.clicked.connect(self.remove_external_player)
        player_heading.addWidget(self.remove_player_button, 0, Qt.AlignmentFlag.AlignTop)
        player_layout.addLayout(player_heading)
        player_layout.addWidget(QLabel("媒体右键菜单会在本地 PotPlayer 之后显示以下第三方播放器。", objectName="subtitle"))
        self.player_table = QTableWidget(0, 2)
        self.player_table.setHorizontalHeaderLabels(["播放器名称", "程序路径"])
        self.player_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.player_table.horizontalHeader().setStretchLastSection(True)
        self.player_table.setColumnWidth(0, 180)
        self.player_table.setMaximumHeight(150)
        self.player_table.itemChanged.connect(self._players_edited)
        player_layout.addWidget(self.player_table)

        aria_card = QFrame(objectName="card")
        aria_layout = QVBoxLayout(aria_card)
        aria_layout.setContentsMargins(20, 18, 20, 20)
        aria_layout.setSpacing(14)
        aria_layout.addWidget(QLabel("aria2-next 详细配置", objectName="section"))
        aria_layout.addWidget(QLabel("按文件大小自动分配 HTTP 连接和下载分段，并为小文件仓库增加并行任务。", objectName="subtitle"))
        self.aria_strategy_combo = CleanComboBox()
        self.aria_strategy_combo.addItem("自动（按文件大小，推荐）", userData="adaptive")
        aria_layout.addWidget(self.aria_strategy_combo)
        aria_grid = QGridLayout()
        aria_grid.setHorizontalSpacing(12)
        aria_grid.setVerticalSpacing(14)
        aria_grid.addWidget(QLabel("文件类别"), 0, 0)
        aria_grid.addWidget(QLabel("大小阈值"), 0, 1)
        aria_grid.addWidget(QLabel("HTTP 连接 / 分段数"), 0, 2)
        aria_grid.addWidget(QLabel("小文件"), 1, 0)
        self.aria_small_limit = QDoubleSpinBox()
        self.aria_small_limit.setRange(0.01, 102400.0)
        self.aria_small_limit.setDecimals(2)
        self.aria_small_limit.setSuffix(" MB 以下")
        self.aria_small_limit.setValue(1.0)
        aria_grid.addWidget(self._stepper(self.aria_small_limit), 1, 1)
        self.aria_small_segments = QSpinBox()
        self.aria_small_segments.setRange(1, 128)
        self.aria_small_segments.setValue(1)
        aria_grid.addWidget(self._stepper(self.aria_small_segments), 1, 2)
        aria_grid.addWidget(QLabel("中等文件"), 2, 0)
        aria_grid.addWidget(QLabel("介于小文件和大文件之间", objectName="subtitle"), 2, 1)
        self.aria_medium_segments = QSpinBox()
        self.aria_medium_segments.setRange(1, 128)
        self.aria_medium_segments.setValue(32)
        aria_grid.addWidget(self._stepper(self.aria_medium_segments), 2, 2)
        aria_grid.addWidget(QLabel("大文件"), 3, 0)
        self.aria_large_limit = QDoubleSpinBox()
        self.aria_large_limit.setRange(0.02, 1024000.0)
        self.aria_large_limit.setDecimals(2)
        self.aria_large_limit.setSuffix(" MB 以上")
        self.aria_large_limit.setValue(100.0)
        aria_grid.addWidget(self._stepper(self.aria_large_limit), 3, 1)
        self.aria_large_segments = QSpinBox()
        self.aria_large_segments.setRange(1, 128)
        self.aria_large_segments.setValue(64)
        aria_grid.addWidget(self._stepper(self.aria_large_segments), 3, 2)
        aria_layout.addLayout(aria_grid)
        aria_note = QLabel("提示：aria2-next 的最小分片为 1 MB；分段数是上限，较小文件会按实际大小使用可用分片。", objectName="subtitle")
        aria_note.setWordWrap(True)
        aria_layout.addWidget(aria_note)
        reset_aria_button = QPushButton("重置 aria2-next 配置到默认值")
        reset_aria_button.clicked.connect(self.reset_aria2_settings)
        aria_layout.addWidget(reset_aria_button, alignment=Qt.AlignmentFlag.AlignLeft)
        for control in (
            self.aria_small_limit,
            self.aria_small_segments,
            self.aria_medium_segments,
            self.aria_large_limit,
            self.aria_large_segments,
        ):
            control.valueChanged.connect(self._save_aria2_settings)

        aria_layout.addWidget(QLabel("上传 / 下载限速", objectName="section"))
        aria_layout.addWidget(QLabel(
            "0 表示不限速。分时时段按每天重复执行，跨越午夜的时段同样有效；多条重叠时以最后一条为准。",
            objectName="subtitle",
        ))
        self.speed_limit_enabled = FluentSwitchButton("启用传输限速")
        self.speed_limit_enabled.toggled.connect(self._save_transfer_policy)
        aria_layout.addWidget(self.speed_limit_enabled)
        base_limit_row = QHBoxLayout()
        base_limit_row.addWidget(QLabel("默认上传"))
        self.base_upload_limit = QDoubleSpinBox()
        self.base_upload_limit.setRange(0, 102400)
        self.base_upload_limit.setDecimals(2)
        self.base_upload_limit.setSuffix(" MB/s")
        self.base_upload_limit.valueChanged.connect(self._save_transfer_policy)
        base_limit_row.addWidget(self._stepper(self.base_upload_limit))
        base_limit_row.addWidget(QLabel("默认下载"))
        self.base_download_limit = QDoubleSpinBox()
        self.base_download_limit.setRange(0, 102400)
        self.base_download_limit.setDecimals(2)
        self.base_download_limit.setSuffix(" MB/s")
        self.base_download_limit.valueChanged.connect(self._save_transfer_policy)
        base_limit_row.addWidget(self._stepper(self.base_download_limit))
        base_limit_row.addStretch()
        aria_layout.addLayout(base_limit_row)
        self.speed_rule_table = QTableWidget(0, 4)
        self.speed_rule_table.setHorizontalHeaderLabels(["开始时间", "结束时间", "上传 MB/s", "下载 MB/s"])
        self.speed_rule_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.speed_rule_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.speed_rule_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.speed_rule_table.setMaximumHeight(190)
        aria_layout.addWidget(self.speed_rule_table)
        speed_rule_actions = QHBoxLayout()
        add_speed_rule = QPushButton("＋ 添加分时时段")
        add_speed_rule.clicked.connect(lambda: self._add_speed_rule())
        speed_rule_actions.addWidget(add_speed_rule)
        remove_speed_rule = QPushButton("－ 删除所选时段")
        remove_speed_rule.clicked.connect(self._remove_speed_rule)
        speed_rule_actions.addWidget(remove_speed_rule)
        reset_speed_limits = QPushButton("重置限速配置")
        reset_speed_limits.clicked.connect(self._reset_transfer_policy)
        speed_rule_actions.addWidget(reset_speed_limits)
        speed_rule_actions.addStretch()
        aria_layout.addLayout(speed_rule_actions)
        self.speed_limit_status = QLabel("当前：上传不限速 · 下载不限速", objectName="subtitle")
        aria_layout.addWidget(self.speed_limit_status)

        general_card = QFrame(objectName="card")
        general_layout = QVBoxLayout(general_card)
        general_layout.setContentsMargins(20, 18, 20, 20)
        general_layout.addWidget(QLabel("基本设置", objectName="section"))
        general_grid = QGridLayout()
        general_grid.addWidget(QLabel("语言"), 0, 0)
        self.language_combo = CleanComboBox()
        self.language_combo.addItem("简体中文", userData="zh_CN")
        self.language_combo.addItem("English", userData="en_US")
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        general_grid.addWidget(self.language_combo, 0, 1)
        general_grid.addWidget(QLabel("关闭窗口时"), 1, 0)
        self.close_behavior_combo = CleanComboBox()
        self.close_behavior_combo.addItem("第一次询问", userData="ask")
        self.close_behavior_combo.addItem("最小化到通知区域", userData="tray")
        self.close_behavior_combo.addItem("直接关闭程序", userData="close")
        self.close_behavior_combo.currentIndexChanged.connect(self._close_behavior_changed)
        general_grid.addWidget(self.close_behavior_combo, 1, 1)
        self.startup_checkbox = FluentSwitchButton()
        self.startup_checkbox.toggled.connect(self._startup_changed)
        general_grid.addWidget(self.startup_checkbox, 2, 1)
        general_layout.addLayout(general_grid)

        theme_card = QFrame(objectName="card")
        theme_layout = QVBoxLayout(theme_card)
        theme_layout.setContentsMargins(20, 18, 20, 20)
        theme_layout.addWidget(QLabel("主题设置", objectName="section"))
        self.gpu_acceleration_checkbox = FluentSwitchButton()
        self.gpu_acceleration_checkbox.toggled.connect(self._graphics_settings_changed)
        theme_layout.addWidget(self.gpu_acceleration_checkbox)
        self.acrylic_checkbox = FluentSwitchButton()
        self.acrylic_checkbox.toggled.connect(self._graphics_settings_changed)
        theme_layout.addWidget(self.acrylic_checkbox)
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("颜色主题"))
        self.theme_combo = CleanComboBox()
        self.theme_combo.addItem("跟随系统", userData="system")
        self.theme_combo.addItem("浅色", userData="light")
        self.theme_combo.addItem("深色", userData="dark")
        self.theme_combo.currentIndexChanged.connect(self._theme_changed)
        theme_row.addWidget(self.theme_combo)
        theme_row.addStretch()
        theme_layout.addLayout(theme_row)
        self.font_size_spin = FluentSpinBox()
        self.font_size_spin.setRange(9, 18)
        self.font_size_spin.setValue(10)
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.valueChanged.connect(self._font_size_changed)
        self.graphics_status = QLabel("亚克力效果由 Windows GPU 合成器处理。", objectName="subtitle")
        theme_layout.addWidget(self.graphics_status)

        index_card = QFrame(objectName="card")
        index_layout = QVBoxLayout(index_card)
        index_layout.setContentsMargins(20, 18, 20, 20)
        index_layout.addWidget(QLabel("索引和预览", objectName="panelTitle"))
        index_layout.addWidget(QLabel("索引更新", objectName="section"))
        index_layout.addWidget(QLabel(
            "启动时完整更新一次；日常修改会等到空闲或程序进入后台后再更新。",
            objectName="subtitle",
        ))
        index_row = QHBoxLayout()
        index_row.addWidget(QLabel("后台索引间隔"))
        self.background_index_minutes = QSpinBox()
        self.background_index_minutes.setRange(1, 1440)
        self.background_index_minutes.setValue(5)
        self.background_index_minutes.setSuffix(" 分钟")
        self.background_index_minutes.valueChanged.connect(self._background_index_interval_changed)
        index_row.addWidget(self._stepper(self.background_index_minutes))
        index_row.addStretch()
        index_layout.addLayout(index_row)

        preview_card = QFrame(objectName="card")
        preview_layout = QVBoxLayout(preview_card)
        preview_layout.setContentsMargins(20, 18, 20, 20)
        preview_layout.addWidget(QLabel("预览", objectName="section"))
        preview_layout.addWidget(QLabel("当前目录优先生成；大目录和子目录会在用户空闲时分批递归处理。原图仅短暂驻留内存。", objectName="subtitle"))
        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("自动对大小小于"))
        self.thumbnail_maximum_mb = QDoubleSpinBox()
        self.thumbnail_maximum_mb.setRange(0.1, 10240)
        self.thumbnail_maximum_mb.setDecimals(1)
        self.thumbnail_maximum_mb.setValue(100.0)
        self.thumbnail_maximum_mb.setSuffix(" MB 的图片生成缩略图")
        self.thumbnail_maximum_mb.valueChanged.connect(self._save_preview_settings)
        preview_row.addWidget(self._stepper(self.thumbnail_maximum_mb))
        preview_row.addStretch()
        preview_layout.addLayout(preview_row)
        thumbnail_workers_row = QHBoxLayout()
        thumbnail_workers_row.addWidget(QLabel("缩略图生成线程数"))
        self.thumbnail_workers = QSpinBox()
        self.thumbnail_workers.setRange(1, 128)
        self.thumbnail_workers.setValue(16)
        self.thumbnail_workers.valueChanged.connect(self._save_preview_settings)
        thumbnail_workers_row.addWidget(self._stepper(self.thumbnail_workers))
        thumbnail_workers_row.addStretch()
        preview_layout.addLayout(thumbnail_workers_row)
        copy_row = QHBoxLayout()
        copy_row.addWidget(QLabel("复制阈值"))
        self.copy_threshold_value = QDoubleSpinBox()
        self.copy_threshold_value.setRange(0.1, 10240)
        self.copy_threshold_value.setDecimals(1)
        self.copy_threshold_value.setValue(100.0)
        self.copy_threshold_value.valueChanged.connect(self._save_preview_settings)
        copy_row.addWidget(self._stepper(self.copy_threshold_value))
        self.copy_threshold_unit = CleanComboBox()
        self.copy_threshold_unit.addItem("MB", userData=1024 ** 2)
        self.copy_threshold_unit.addItem("GB", userData=1024 ** 3)
        self.copy_threshold_unit.currentIndexChanged.connect(self._save_preview_settings)
        copy_row.addWidget(self.copy_threshold_unit)
        copy_row.addStretch()
        preview_layout.addLayout(copy_row)

        alist_card = QFrame(objectName="card")
        alist_layout = QVBoxLayout(alist_card)
        alist_layout.setContentsMargins(20, 18, 20, 20)
        alist_layout.addWidget(QLabel("WebDAV 设置", objectName="section"))
        alist_layout.addWidget(QLabel("AList V3 挂载", objectName="section"))
        alist_warning = QLabel("使用 WebDAV 网关。受到 ModelScope 官方 API 限制，不支持删除、重命名、移动和复制操作。", objectName="subtitle")
        alist_warning.setWordWrap(True)
        alist_layout.addWidget(alist_warning)
        alist_grid = QGridLayout()
        alist_grid.addWidget(QLabel("配置方式"), 0, 0)
        self.alist_protocol_combo = CleanComboBox()
        self.alist_protocol_combo.addItem("WebDAV", userData="webdav")
        alist_grid.addWidget(self.alist_protocol_combo, 0, 1)
        alist_grid.addWidget(QLabel("监听范围"), 1, 0)
        self.alist_host_combo = CleanComboBox()
        self.alist_host_combo.addItem("仅本机（127.0.0.1）", userData="127.0.0.1")
        self.alist_host_combo.addItem("局域网 / Docker（0.0.0.0）", userData="0.0.0.0")
        alist_grid.addWidget(self.alist_host_combo, 1, 1)
        self.alist_port_label = QLabel("端口")
        alist_grid.addWidget(self.alist_port_label, 2, 0)
        self.alist_port = QSpinBox()
        self.alist_port.setRange(1024, 65535)
        self.alist_port.setValue(9867)
        self.alist_port_control = self._stepper(self.alist_port)
        alist_grid.addWidget(self.alist_port_control, 2, 1)
        alist_grid.addWidget(QLabel("用户名"), 3, 0)
        self.alist_username = QLineEdit("modelscope")
        alist_grid.addWidget(self.alist_username, 3, 1)
        alist_grid.addWidget(QLabel("密码"), 4, 0)
        self.alist_password = QLineEdit()
        self.alist_password.setEchoMode(QLineEdit.EchoMode.Password)
        alist_grid.addWidget(self.alist_password, 4, 1)
        self.alist_show_password = QCheckBox("显示密码")
        self.alist_show_password.toggled.connect(
            lambda checked: self.alist_password.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        alist_grid.addWidget(self.alist_show_password, 5, 1)
        alist_layout.addLayout(alist_grid)
        self.alist_url_label = QLabel("WebDAV 地址：--", objectName="pathPill")
        self.alist_url_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        alist_layout.addWidget(self.alist_url_label)
        alist_help = QLabel("AList 后台添加存储：驱动选择 WebDAV，厂商选择“其他”，地址填写上方 URL，根文件夹路径填写 / 或留空。若 AList 在 Docker 中，可使用 host.docker.internal。其他设备连接时必须选择“局域网 / Docker”监听；若仍然连接超时，请在 Windows Defender 防火墙中允许本程序。", objectName="subtitle")
        alist_help.setWordWrap(True)
        alist_layout.addWidget(alist_help)
        alist_actions = QHBoxLayout()
        self.alist_auto_start = FluentSwitchButton("启动程序后自动启动监听")
        self.alist_auto_start.toggled.connect(self._save_alist_settings)
        alist_actions.addWidget(self.alist_auto_start)
        alist_actions.addStretch()
        self.alist_start_button = QPushButton("启动")
        self.alist_start_button.clicked.connect(self.apply_alist_settings)
        alist_actions.addWidget(self.alist_start_button)
        self.alist_stop_button = QPushButton("停止")
        self.alist_stop_button.clicked.connect(self.stop_alist)
        alist_actions.addWidget(self.alist_stop_button)
        alist_layout.addLayout(alist_actions)
        self.alist_status = QLabel("未启动", objectName="subtitle")
        alist_layout.addWidget(self.alist_status)
        for control in (self.alist_host_combo, self.alist_port, self.alist_username, self.alist_password):
            if isinstance(control, (QComboBox, CleanComboBox)):
                control.currentIndexChanged.connect(self._update_alist_url)
                control.currentIndexChanged.connect(self._save_alist_settings)
            elif isinstance(control, QSpinBox):
                control.valueChanged.connect(self._update_alist_url)
                control.valueChanged.connect(self._save_alist_settings)
            else:
                control.textChanged.connect(self._update_alist_url)
                control.textChanged.connect(self._save_alist_settings)
        aria_card.setObjectName("")
        aria_layout.setContentsMargins(0, 16, 0, 0)
        download_setting_layout.addWidget(aria_card)
        preview_card.setObjectName("")
        preview_layout.setContentsMargins(0, 16, 0, 0)
        index_layout.addWidget(preview_card)
        basic_group = SettingCardGroup("基本设置", settings_content)
        basic_group.addSettingCards((
            ControlSettingCard(FIF.LANGUAGE, "语言", "修改后立即应用，无需重启", self.language_combo, trailing_margin=40),
            ControlSettingCard(FIF.CLOSE, "关闭窗口时", "选择询问、最小化到通知区域或直接退出", self.close_behavior_combo, trailing_margin=40),
            ControlSettingCard(FIF.POWER_BUTTON, "开机自启", "登录 Windows 后自动启动 ModelScope Manager", self.startup_checkbox, trailing_margin=40),
            ControlSettingCard(FIF.SCROLL, "滚轮保护", "避免滚动设置页时意外修改数值", self.disable_settings_wheel, trailing_margin=40),
        ))

        appearance_group = SettingCardGroup("个性化", settings_content)
        appearance_group.addSettingCards((
            ControlSettingCard(FIF.PALETTE, "应用主题", "跟随系统，或固定使用浅色/深色主题", self.theme_combo, trailing_margin=40),
            ControlSettingCard(FIF.FONT_SIZE, "全局字号", "同步缩放 Qt、Fluent 控件和 Matplotlib", self.font_size_spin, trailing_margin=40),
            ControlSettingCard(FIF.SPEED_HIGH, "GPU 加速", "桌面 OpenGL 设置在下次启动后生效", self.gpu_acceleration_checkbox, trailing_margin=40),
            ControlSettingCard(FIF.TRANSPARENT, "Blur 亚克力", "由 Windows 桌面合成器渲染半透明背景", self.acrylic_checkbox, trailing_margin=40),
            ControlSettingCard(FIF.INFO, "渲染状态", "当前窗口合成与背景策略", self.graphics_status, trailing_margin=40),
        ))

        panel_specs = (
            ("账号设置", FIF.PEOPLE, "ModelScope 账户", "Token 与网页登录信息使用设备绑定加密保存", token_card),
            ("下载设置", FIF.DOWNLOAD, "下载与传输", "默认目录、aria2-next 分段和共享限速", download_card),
            ("播放设置", FIF.PLAY, "媒体播放器", "内置 PotPlayer 与第三方播放器", player_card),
            ("索引和预览", FIF.SEARCH, "索引与预览", "后台索引、缩略图和复制阈值", index_card),
        )
        settings_layout.addWidget(basic_group)
        settings_layout.addWidget(appearance_group)
        for group_title, icon, title, description, panel in panel_specs:
            for label in panel.findChildren(QLabel, options=Qt.FindChildOption.FindDirectChildrenOnly):
                if label.objectName() == "panelTitle":
                    label.hide()
                    break
            group = SettingCardGroup(group_title, settings_content)
            group.addSettingCard(PanelSettingCard(icon, title, description, panel, group))
            settings_layout.addWidget(group)
        self.alist_port_label.hide()
        webdav_group = SettingCardGroup("WebDAV 设置", settings_content)
        webdav_group.addSettingCards((
            ControlSettingCard(
                FIF.CONNECT, "监听端口", "端口冲突时会自动寻找相邻可用端口并立即保存",
                self.alist_port_control, webdav_group,
            ),
            PanelSettingCard(
                FIF.GLOBE, "WebDAV 网关", "监听范围、账户凭据与启动状态",
                alist_card, webdav_group,
            ),
        ))
        settings_layout.insertWidget(settings_layout.count() - 1, webdav_group)
        settings_layout.addStretch()
        settings_scroll.setWidget(settings_content)
        settings_page_layout.addWidget(settings_scroll)

        # Public resource search page
        search_page = QWidget()
        search_layout = QVBoxLayout(search_page)
        search_layout.setContentsMargins(24, 20, 24, 22)
        search_layout.setSpacing(14)
        search_layout.addWidget(QLabel("资源搜索", objectName="title"))
        search_layout.addWidget(QLabel("输入他人的公开数据集或模型链接，无需登录即可浏览和下载。", objectName="subtitle"))
        search_bar = QHBoxLayout()
        self.search_url_edit = QComboBox()
        self.search_url_edit.setEditable(True)
        self.search_url_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.search_url_edit.lineEdit().setPlaceholderText("https://www.modelscope.cn/datasets/账户/仓库")
        self.search_url_edit.lineEdit().returnPressed.connect(self.load_public_resource)
        self.search_url_edit.activated.connect(lambda: self.load_public_resource())
        search_bar.addWidget(self.search_url_edit, 1)
        self.search_load_button = QPushButton("加载", objectName="primary")
        self.search_load_button.clicked.connect(self.load_public_resource)
        search_bar.addWidget(self.search_load_button)
        search_history_button = QPushButton("搜索历史")
        search_history_button.clicked.connect(self.show_search_history)
        search_bar.addWidget(search_history_button)
        search_layout.addLayout(search_bar)
        search_card = QFrame(objectName="card")
        search_card_layout = QVBoxLayout(search_card)
        search_card_layout.setContentsMargins(16, 16, 16, 16)
        self.search_heading = QLabel("等待输入公开资源链接", objectName="section")
        search_card_layout.addWidget(self.search_heading)
        file_search_row = QHBoxLayout()
        self.public_file_search_edit = QLineEdit()
        self.public_file_search_edit.setClearButtonEnabled(True)
        self.public_file_search_edit.setPlaceholderText(
            "搜索文件：空格分词且全部匹配；支持 path:、name:、ext:、type: 和 * ?"
        )
        self.public_file_search_edit.textChanged.connect(self._render_public_search_results)
        file_search_row.addWidget(self.public_file_search_edit, 1)
        file_search_row.addWidget(QLabel("排序"))
        self.public_search_sort_combo = QComboBox()
        for label, column in (("名称", 0), ("类型", 1), ("大小", 2), ("路径", 3)):
            self.public_search_sort_combo.addItem(label, column)
        self.public_search_sort_combo.currentIndexChanged.connect(self._public_search_sort_combo_changed)
        file_search_row.addWidget(self.public_search_sort_combo)
        self.public_search_direction_button = QPushButton("升序")
        self.public_search_direction_button.clicked.connect(self._toggle_public_search_direction)
        file_search_row.addWidget(self.public_search_direction_button)
        search_card_layout.addLayout(file_search_row)
        self.public_search_count_label = QLabel("", objectName="subtitle")
        search_card_layout.addWidget(self.public_search_count_label)
        self.search_remote_tree = RepositoryTree()
        self.search_remote_tree.setAcceptDrops(False)
        self.search_remote_tree.setObjectName("repositoryTree")
        self.search_remote_tree.setSortingEnabled(False)
        self.search_remote_tree.setRootIsDecorated(False)
        self.search_remote_tree.setUniformRowHeights(True)
        self.search_remote_tree.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.search_remote_tree.setHeaderLabels(["名称", "类型", "大小", "路径"])
        search_header = self.search_remote_tree.header()
        search_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        search_header.setStretchLastSection(True)
        search_header.setSectionsClickable(True)
        search_header.setSortIndicatorShown(True)
        search_header.sectionClicked.connect(self._change_public_search_sort)
        self.search_remote_tree.setColumnWidth(0, 340)
        self.search_remote_tree.setColumnWidth(1, 90)
        self.search_remote_tree.setColumnWidth(2, 110)
        self.search_remote_tree.setColumnWidth(3, 330)
        self.search_remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_remote_tree.customContextMenuRequested.connect(self._search_context_menu)
        search_card_layout.addWidget(self.search_remote_tree, 1)
        search_layout.addWidget(search_card, 1)

        # Backup folders page
        backup_page = QWidget()
        backup_layout = QVBoxLayout(backup_page)
        backup_layout.setContentsMargins(24, 20, 24, 22)
        backup_layout.setSpacing(14)
        backup_layout.addWidget(QLabel("备份文件夹", objectName="title"))
        backup_note = QLabel(
            "定期扫描本地变动并上传。增量备份创建时间戳目录；同路径覆盖会更新远端文件，但不会删除远端多余文件。",
            objectName="subtitle",
        )
        backup_note.setWordWrap(True)
        backup_layout.addWidget(backup_note)
        self.backup_table = QTableWidget(0, 8)
        self.backup_table.setHorizontalHeaderLabels([
            "名称", "本地文件夹", "账户", "仓库", "目标目录", "模式", "周期", "状态"
        ])
        self.backup_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.backup_table.horizontalHeader().setStretchLastSection(True)
        self.backup_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.backup_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.backup_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.backup_table.setColumnWidth(0, 130)
        self.backup_table.setColumnWidth(1, 230)
        self.backup_table.setColumnWidth(2, 110)
        self.backup_table.setColumnWidth(3, 190)
        self.backup_table.itemSelectionChanged.connect(self._backup_selected)
        backup_layout.addWidget(self.backup_table, 1)

        backup_editor = QFrame(objectName="card")
        backup_editor_layout = QGridLayout(backup_editor)
        backup_editor_layout.setContentsMargins(18, 16, 18, 16)
        backup_editor_layout.setHorizontalSpacing(12)
        backup_editor_layout.setVerticalSpacing(10)
        backup_editor_layout.addWidget(QLabel("任务名称"), 0, 0)
        self.backup_name_edit = QLineEdit()
        self.backup_name_edit.setPlaceholderText("例如：项目保险箱")
        backup_editor_layout.addWidget(self.backup_name_edit, 0, 1)
        backup_editor_layout.addWidget(QLabel("本地文件夹"), 1, 0)
        local_row = QHBoxLayout()
        self.backup_local_edit = QLineEdit()
        self.backup_local_edit.setReadOnly(True)
        local_row.addWidget(self.backup_local_edit, 1)
        backup_browse = QPushButton("选择")
        backup_browse.clicked.connect(self._browse_backup_folder)
        local_row.addWidget(backup_browse)
        backup_editor_layout.addLayout(local_row, 1, 1, 1, 3)
        backup_editor_layout.addWidget(QLabel("账户"), 2, 0)
        self.backup_account_combo = QComboBox()
        self.backup_account_combo.currentIndexChanged.connect(self._render_backup_repository_options)
        backup_editor_layout.addWidget(self.backup_account_combo, 2, 1)
        backup_editor_layout.addWidget(QLabel("仓库"), 2, 2)
        self.backup_repo_combo = QComboBox()
        backup_editor_layout.addWidget(self.backup_repo_combo, 2, 3)
        backup_editor_layout.addWidget(QLabel("目标目录"), 3, 0)
        self.backup_dest_edit = QLineEdit()
        self.backup_dest_edit.setPlaceholderText("例如：backup/project")
        backup_editor_layout.addWidget(self.backup_dest_edit, 3, 1)
        backup_editor_layout.addWidget(QLabel("备份模式"), 3, 2)
        self.backup_mode_combo = QComboBox()
        self.backup_mode_combo.addItem("增量备份（时间戳）", "incremental")
        self.backup_mode_combo.addItem("同路径覆盖", "replace")
        backup_editor_layout.addWidget(self.backup_mode_combo, 3, 3)
        backup_editor_layout.addWidget(QLabel("扫描周期"), 4, 0)
        interval_row = QHBoxLayout()
        self.backup_interval_value = QDoubleSpinBox()
        self.backup_interval_value.setRange(0.1, 100000)
        self.backup_interval_value.setDecimals(1)
        self.backup_interval_value.setValue(30)
        interval_row.addWidget(self._stepper(self.backup_interval_value), 1)
        self.backup_interval_unit = QComboBox()
        self.backup_interval_unit.addItem("分钟", "minute")
        self.backup_interval_unit.addItem("小时", "hour")
        interval_row.addWidget(self.backup_interval_unit)
        backup_editor_layout.addLayout(interval_row, 4, 1)
        backup_editor_layout.addWidget(QLabel("云端同步阈值"), 4, 2)
        self.backup_download_limit = QDoubleSpinBox()
        self.backup_download_limit.setRange(0.01, 1024000)
        self.backup_download_limit.setDecimals(2)
        self.backup_download_limit.setValue(10)
        self.backup_download_limit.setSuffix(" MB 以下")
        backup_editor_layout.addWidget(self._stepper(self.backup_download_limit), 4, 3)
        self.backup_enabled = QCheckBox("启用定时备份")
        self.backup_enabled.setChecked(True)
        backup_editor_layout.addWidget(self.backup_enabled, 5, 1)
        backup_actions = QHBoxLayout()
        backup_new = QPushButton("新建")
        backup_new.clicked.connect(self._new_backup_job)
        backup_actions.addWidget(backup_new)
        backup_save = QPushButton("保存任务", objectName="primary")
        backup_save.clicked.connect(self._save_backup_job)
        backup_actions.addWidget(backup_save)
        backup_remove = QPushButton("移除")
        backup_remove.clicked.connect(self._remove_backup_job)
        backup_actions.addWidget(backup_remove)
        backup_actions.addStretch()
        self.backup_scan_button = QPushButton("立即扫描并上传")
        self.backup_scan_button.clicked.connect(self._scan_selected_backup)
        backup_actions.addWidget(self.backup_scan_button)
        self.backup_sync_button = QPushButton("从云端同步到本地")
        self.backup_sync_button.clicked.connect(self._sync_selected_backup)
        backup_actions.addWidget(self.backup_sync_button)
        backup_editor_layout.addLayout(backup_actions, 6, 0, 1, 4)
        self.backup_status_label = QLabel("尚未选择备份任务", objectName="subtitle")
        backup_editor_layout.addWidget(self.backup_status_label, 7, 0, 1, 4)
        backup_layout.addWidget(backup_editor)

        # Image hosting page
        image_page = QWidget()
        self.image_page = image_page
        image_layout = QVBoxLayout(image_page)
        image_layout.setContentsMargins(24, 20, 24, 22)
        image_layout.setSpacing(14)
        image_layout.addWidget(QLabel("图床", objectName="title"))
        image_layout.addWidget(QLabel(
            "拖入图片或按 Ctrl+V 上传到指定仓库，生成直链并保存本地缓存。",
            objectName="subtitle",
        ))
        image_config = QFrame(objectName="card")
        image_config_layout = QHBoxLayout(image_config)
        image_config_layout.setContentsMargins(16, 14, 16, 14)
        image_config_layout.addWidget(QLabel("账户"))
        self.image_account_combo = QComboBox()
        self.image_account_combo.currentIndexChanged.connect(self._render_image_repository_options)
        self.image_account_combo.currentIndexChanged.connect(self._save_image_settings)
        image_config_layout.addWidget(self.image_account_combo)
        image_config_layout.addWidget(QLabel("仓库"))
        self.image_repo_combo = QComboBox()
        self.image_repo_combo.currentIndexChanged.connect(self._save_image_settings)
        image_config_layout.addWidget(self.image_repo_combo, 1)
        image_config_layout.addWidget(QLabel("存储路径"))
        self.image_dest_edit = QLineEdit()
        self.image_dest_edit.setPlaceholderText("images")
        self.image_dest_edit.setText("images")
        self.image_dest_edit.editingFinished.connect(self._save_image_settings)
        image_config_layout.addWidget(self.image_dest_edit, 1)
        image_layout.addWidget(image_config)
        self.image_drop_area = DropArea("将图片拖到这里上传", "也可按 Ctrl+V；上传前会验证文件确实是图片")
        self.image_drop_area.paths_dropped.connect(self._upload_images)
        image_layout.addWidget(self.image_drop_area)
        image_actions = QHBoxLayout()
        image_pick = QPushButton("选择图片")
        image_pick.clicked.connect(self._pick_images)
        image_actions.addWidget(image_pick)
        image_actions.addStretch()
        image_copy = QPushButton("复制所选直链")
        image_copy.clicked.connect(self._copy_selected_image_link)
        image_actions.addWidget(image_copy)
        image_open = QPushButton("缓存打开")
        image_open.clicked.connect(self._open_selected_image)
        image_actions.addWidget(image_open)
        image_remove = QPushButton("移除记录")
        image_remove.clicked.connect(self._remove_selected_image)
        image_actions.addWidget(image_remove)
        image_layout.addLayout(image_actions)
        self.image_table = QTableWidget(0, 6)
        self.image_table.setHorizontalHeaderLabels(["文件", "仓库", "远端路径", "直链", "缓存", "上传时间"])
        self.image_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.image_table.horizontalHeader().setStretchLastSection(True)
        self.image_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.image_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.image_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.image_table.setColumnWidth(0, 180)
        self.image_table.setColumnWidth(1, 200)
        self.image_table.setColumnWidth(2, 260)
        self.image_table.setColumnWidth(3, 280)
        self.image_table.doubleClicked.connect(self._open_selected_image)
        image_layout.addWidget(self.image_table, 1)
        self.image_status_label = QLabel("等待上传图片", objectName="subtitle")
        image_layout.addWidget(self.image_status_label)
        page_specs = (
            (resource_page, "resourceInterface", FIF.FOLDER, "资源管理", NavigationItemPosition.TOP),
            (transfer_page, "transferInterface", FIF.SYNC, "传输列表", NavigationItemPosition.TOP),
            (settings_page, "settingsInterface", FIF.SETTING, "设置", NavigationItemPosition.BOTTOM),
            (search_page, "searchInterface", FIF.SEARCH, "资源搜索", NavigationItemPosition.TOP),
            (backup_page, "backupInterface", FIF.SAVE, "备份文件夹", NavigationItemPosition.TOP),
            (image_page, "imageInterface", FIF.PHOTO, "图床", NavigationItemPosition.TOP),
        )
        for page, name, icon, text, position in page_specs:
            page.setObjectName(name)
            self.addSubInterface(page, icon, text, position=position, isTransparent=False)
        top_layout = self.navigationInterface.panel.topLayout
        for offset, route_key in enumerate((
            "resourceInterface", "searchInterface", "transferInterface",
            "backupInterface", "imageInterface",
        )):
            item = self.navigationInterface.widget(route_key)
            top_layout.removeWidget(item)
            top_layout.insertWidget(offset + 2, item, 0, Qt.AlignmentFlag.AlignTop)
        self._page_by_id = {
            0: resource_page,
            1: transfer_page,
            2: settings_page,
            3: search_page,
            4: backup_page,
            5: image_page,
        }
        self._navigate(0)

    def _restore_settings(self) -> None:
        self._restoring_settings = True
        self.target_edit.setText(str(self.settings.value("target_folder", "")))
        default_download = Path.home() / "Downloads"
        self.download_path_edit.setText(str(self.settings.value("download_path", str(default_download))))
        self.drop_upload_threshold_mb.setValue(int(self.settings.value("upload/drop_threshold_mb", 1024)))
        self.image_dest_edit.setText(str(self.settings.value("image/destination", "images")))
        self.aria_small_limit.setValue(float(self.settings.value("aria2/small_limit_mb", 1.0)))
        self.aria_small_segments.setValue(int(self.settings.value("aria2/small_segments", 1)))
        self.aria_medium_segments.setValue(int(self.settings.value("aria2/medium_segments", 32)))
        self.aria_large_limit.setValue(float(self.settings.value("aria2/large_limit_mb", 100.0)))
        self.aria_large_segments.setValue(int(self.settings.value("aria2/large_segments", 64)))
        raw_transfer_policy = str(self.settings.value("transfer/speed_policy", ""))
        try:
            self.transfer_policy = TransferPolicy.from_dict(json.loads(raw_transfer_policy) if raw_transfer_policy else {})
        except (TypeError, ValueError, json.JSONDecodeError):
            self.transfer_policy = TransferPolicy()
        self.speed_limit_enabled.setChecked(self.transfer_policy.enabled)
        self.base_upload_limit.setValue(self.transfer_policy.upload_mib)
        self.base_download_limit.setValue(self.transfer_policy.download_mib)
        self.speed_rule_table.setRowCount(0)
        for rule in self.transfer_policy.rules:
            self._add_speed_rule(rule.start, rule.end, rule.upload_mib, rule.download_mib)
        restore_combo_setting(self.settings, "language", self.language_combo, "zh_CN")
        restore_combo_setting(self.settings, "theme", self.theme_combo, "system")
        self.font_size_spin.setValue(int(self.settings.value("font_size", 10)))
        self.gpu_acceleration_checkbox.setChecked(
            str(self.settings.value("graphics/gpu_acceleration", "true")).lower() == "true"
        )
        self.acrylic_checkbox.setChecked(
            str(self.settings.value("graphics/acrylic", "true")).lower() == "true"
        )
        restore_combo_setting(self.settings, "close_behavior", self.close_behavior_combo, "ask")
        self.startup_checkbox.setChecked(windows_startup_enabled())
        self.compact_view_button.setChecked(
            str(self.settings.value("compact_view", "false")).lower() == "true"
        )
        self.background_index_minutes.setValue(int(self.settings.value("index/background_minutes", 5)))
        self.thumbnail_maximum_mb.setValue(float(self.settings.value("preview/thumbnail_maximum_mb", 100.0)))
        self.thumbnail_workers.setValue(int(self.settings.value("preview/thumbnail_workers", 16)))
        self.copy_threshold_value.setValue(float(self.settings.value("copy/threshold_value", 100.0)))
        restore_combo_setting(self.settings, "copy/threshold_unit", self.copy_threshold_unit, 1024 ** 2)
        raw_players = str(self.settings.value("external_players", ""))
        try:
            players = json.loads(raw_players) if raw_players else []
        except (TypeError, ValueError):
            players = []
        legacy_player = str(self.settings.value("external_player", ""))
        if not players and legacy_player:
            players = [{"name": Path(legacy_player).stem or "播放器 1", "path": legacy_player}]
            self.settings.remove("external_player")
        self.external_players = players or [{"name": "播放器 1", "path": ""}]
        self._render_players()
        self.builtin_player_enabled.setChecked(
            str(self.settings.value("builtin_player_enabled", "true")).lower() == "true"
        )
        self._refresh_builtin_player_status()
        if str(self.settings.value("player/potplayer_install_pending", "false")).lower() == "true":
            archive = PLAYER_DOWNLOAD_DIR / "PotPlayer.7z"
            self.potplayer_install_archive = archive
            if archive.is_file() and archive.stat().st_size == POTPLAYER_ARCHIVE_SIZE:
                QTimer.singleShot(0, lambda: self._start_potplayer_extraction(archive))
            else:
                self.builtin_player_status.setText(self._t("PotPlayer：下载未完成，点击按钮继续"))
        restore_combo_setting(self.settings, "alist/host", self.alist_host_combo, "127.0.0.1")
        self.alist_port.setValue(int(self.settings.value("alist/port", 9867)))
        self.alist_username.setText(str(self.settings.value("alist/username", "modelscope")))
        encrypted_alist_password = str(self.settings.value("alist/password", ""))
        if encrypted_alist_password:
            try:
                alist_password = unprotect(encrypted_alist_password)
            except Exception:
                alist_password = secrets.token_urlsafe(12)
        else:
            alist_password = secrets.token_urlsafe(12)
        self.alist_password.setText(alist_password)
        self.alist_auto_start.setChecked(
            str(self.settings.value("alist/auto_start", "false")).lower() == "true"
        )
        self.disable_settings_wheel.setChecked(
            str(self.settings.value("disable_settings_wheel", "true")).lower() == "true"
        )
        token = restore_device_bound_token(
            self.settings,
            self.device_id,
            self.token_destroyed_on_start,
        )
        existing_accounts = self.account_store.list_accounts()
        if token and not existing_accounts:
            migrated = AccountRecord("", "默认账户", token=token, remember=True)
            self.account_store.save(migrated)
            destroy_saved_token(self.settings)
        self.accounts = self.account_store.list_accounts()
        self.web_accounts = self.account_store.list_web_accounts()
        self.session_tokens = {account.account_id: account.token for account in self.accounts if account.token}
        self._render_accounts()
        self._render_web_accounts()
        self.backup_jobs = self.backup_store.list_jobs()
        self.image_records = self.image_store.list_records()
        self._render_backup_account_options()
        self._render_backup_jobs()
        self._render_image_account_options()
        self._render_image_records()
        if self.accounts:
            self.account_table.selectRow(0)
        self._restoring_settings = False
        self._save_players()
        self._save_alist_settings()
        self._update_alist_url()
        self._render_public_history()
        self._refresh_tag_filter()
        self._apply_theme()
        self._compact_view_changed(self.compact_view_button.isChecked())
        self._apply_background_index_interval()
        configure_upload_limit_supplier(lambda: self.transfer_policy.limits()[0])
        self._refresh_transfer_limit_status()

    def _render_public_history(self) -> None:
        current = self.search_url_edit.currentText().strip()
        self.search_url_edit.blockSignals(True)
        self.search_url_edit.clear()
        for item in self.public_pool_store.load():
            label = item.get("url") or f"https://www.modelscope.cn/{item['repo_type']}s/{item['repo_id']}"
            self.search_url_edit.addItem(label, item)
        if current:
            self.search_url_edit.setEditText(current)
        self.search_url_edit.blockSignals(False)

    def show_search_history(self) -> None:
        if self.search_history_window and self.search_history_window.isVisible():
            self.search_history_window.raise_()
            self.search_history_window.activateWindow()
            return
        window = QWidget(None, Qt.WindowType.Window)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        window.setWindowTitle("搜索历史")
        window.resize(720, 420)
        layout = QVBoxLayout(window)
        clear_button = QPushButton("清空搜索历史")
        clear_button.clicked.connect(self._clear_search_history)
        layout.addWidget(clear_button, alignment=Qt.AlignmentFlag.AlignLeft)
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["仓库", "类型", "链接", ""])
        table.horizontalHeader().setStretchLastSection(True)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setColumnWidth(3, 72)
        self._populate_search_history_table(table)
        layout.addWidget(table)
        window.destroyed.connect(lambda: setattr(self, "search_history_window", None))
        self.search_history_window = window
        window.show()

    def _populate_search_history_table(self, table: QTableWidget) -> None:
        table.setRowCount(0)
        for item in self.public_pool_store.load():
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(item["repo_id"]))
            table.setItem(row, 1, QTableWidgetItem(item["repo_type"]))
            table.setItem(row, 2, QTableWidgetItem(item.get("url", "")))
            remove = QPushButton("删除")
            repo = Repository(item["repo_id"], item["repo_type"], "public")
            remove.clicked.connect(lambda checked=False, value=repo: self._remove_public_repository(value))
            table.setCellWidget(row, 3, remove)

    def _refresh_search_history_window(self) -> None:
        if self.search_history_window and self.search_history_window.isVisible():
            table = self.search_history_window.findChild(QTableWidget)
            if table:
                self._populate_search_history_table(table)

    def _remove_public_repository(self, repo: Repository) -> None:
        self.public_pool_store.remove(repo)
        self.account_store.remove_repository_entries(PUBLIC_ACCOUNT_ID, repo.repo_type, repo.repo_id)
        self.folder_index.remove_repository(repo, True)
        if self.webdav:
            self.webdav.refresh_public_pools()
        if self.selected_repo_public and self.selected_repo == repo:
            self.selected_repo = None
            self.selected_repo_public = False
            self.remote_entries = []
            self.remote_tree.clear()
            self.refresh_files_button.setEnabled(False)
            self.new_folder_button.setEnabled(False)
        self._render_public_history()
        self._render_repositories()
        self._refresh_tag_filter()
        self._refresh_search_history_window()

    def _clear_search_history(self) -> None:
        for repo in self.public_pool_store.repositories():
            self.account_store.remove_repository_entries(PUBLIC_ACCOUNT_ID, repo.repo_type, repo.repo_id)
            self.folder_index.remove_repository(repo, True)
        self.public_pool_store.clear()
        if self.webdav:
            self.webdav.refresh_public_pools()
        if self.selected_repo_public:
            self.selected_repo = None
            self.selected_repo_public = False
            self.remote_entries = []
            self.remote_tree.clear()
            self.refresh_files_button.setEnabled(False)
            self.new_folder_button.setEnabled(False)
        self._render_public_history()
        self._render_repositories()
        self._refresh_tag_filter()
        self._refresh_search_history_window()

    def _render_accounts(self) -> None:
        if not hasattr(self, "account_table"):
            return
        selected_id = self._selected_account_id()
        self.account_table.setUpdatesEnabled(False)
        self.account_table.blockSignals(True)
        self.account_table.setRowCount(len(self.accounts))
        for row, account in enumerate(self.accounts):
            label_item = QTableWidgetItem(account.label)
            label_item.setData(Qt.ItemDataRole.UserRole, account.account_id)
            self.account_table.setItem(row, 0, label_item)
            self.account_table.setItem(row, 1, QTableWidgetItem(account.username or "--"))
            token = self.session_tokens.get(account.account_id, account.token)
            token_widget = QWidget()
            token_layout = QHBoxLayout(token_widget)
            token_layout.setContentsMargins(2, 0, 2, 0)
            token_label = QLabel("••••••••" if token else "未保存")
            token_layout.addWidget(token_label, 1)
            show_button = QPushButton("显示")
            show_button.setEnabled(bool(token))
            show_button.setFixedWidth(54)

            def toggle_token(checked=False, value=token, label=token_label, button=show_button):
                showing = button.text() == self._t("显示")
                label.setText(value if showing else "••••••••")
                button.setText(self._t("隐藏") if showing else self._t("显示"))

            show_button.clicked.connect(toggle_token)
            token_layout.addWidget(show_button)
            self.account_table.setCellWidget(row, 2, token_widget)
            remember = QCheckBox()
            remember.setChecked(account.remember)
            remember.setEnabled(bool(token))
            remember.toggled.connect(
                lambda checked, account_id=account.account_id: self._account_remember_changed(account_id, checked)
            )
            remember_wrapper = QWidget()
            remember_layout = QHBoxLayout(remember_wrapper)
            remember_layout.setContentsMargins(0, 0, 0, 0)
            remember_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            remember_layout.addWidget(remember)
            self.account_table.setCellWidget(row, 3, remember_wrapper)
            status_labels = {
                "connected": self._t("已连接"),
                "failed": self._t("验证失败"),
                "token_required": self._t("需要 Token"),
                "waiting": self._t("等待验证"),
            }
            self.account_table.setItem(row, 4, QTableWidgetItem(status_labels.get(account.status, account.status)))
            if account.account_id == selected_id:
                self.account_table.selectRow(row)
        self.account_table.blockSignals(False)
        self.account_table.setUpdatesEnabled(True)
        self.account_table.viewport().update()

    def _selected_account_id(self) -> str | None:
        row = self.account_table.currentRow() if hasattr(self, "account_table") else -1
        item = self.account_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _account_selected(self) -> None:
        account_id = self._selected_account_id()
        account = next((item for item in self.accounts if item.account_id == account_id), None)
        if not account:
            return
        self.active_account_id = account.account_id
        self.account_name_edit.setText(account.label)
        self.token_edit.setText(self.session_tokens.get(account.account_id, account.token))
        self.remember_token.setChecked(account.remember)

    @staticmethod
    def _web_account_key(account_id: str) -> str:
        return f"web:{account_id}"

    def _token_service_for_repo(self, repo: Repository) -> ModelScopeService | None:
        for account in self.accounts:
            service = self.account_services.get(account.account_id)
            if not service:
                continue
            if any(
                candidate.repo_type == repo.repo_type and candidate.repo_id == repo.repo_id
                for candidate in self.account_repositories.get(account.account_id, [])
            ):
                return service
        return None

    def _selected_web_account_id(self) -> str | None:
        row = self.web_account_table.currentRow() if hasattr(self, "web_account_table") else -1
        item = self.web_account_table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _render_web_accounts(self) -> None:
        if not hasattr(self, "web_account_table"):
            return
        selected_id = self._selected_web_account_id()
        table = self.web_account_table
        table.blockSignals(True)
        table.setRowCount(len(self.web_accounts))
        for row, account in enumerate(self.web_accounts):
            label_item = QTableWidgetItem(account.label)
            label_item.setData(Qt.ItemDataRole.UserRole, account.account_id)
            table.setItem(row, 0, label_item)
            username_item = QTableWidgetItem(account.username or "--")
            username_item.setFlags(username_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 1, username_item)
            status = "成功" if self.account_store.load_web_session(account.account_id) else "尚未登录"
            status_item = QTableWidgetItem(status)
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            table.setItem(row, 2, status_item)
            login_button = QPushButton("在线登录", objectName="primary")
            login_button.clicked.connect(
                lambda checked=False, account_id=account.account_id: self.open_online_login(account_id)
            )
            table.setCellWidget(row, 3, login_button)
            if account.account_id == selected_id:
                table.selectRow(row)
        table.blockSignals(False)

    def _web_account_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        account_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        account = next((value for value in self.web_accounts if value.account_id == account_id), None)
        if not account:
            return
        account.label = item.text().strip() or account.username or "ModelScope 账户"
        self.account_store.save_web_account(account)
        self._render_repositories()

    def add_web_account(self) -> None:
        index = len(self.web_accounts) + 1
        account = self.account_store.save_web_account(
            WebAccountRecord("", f"网页登录账户 {index}", "", "login_required")
        )
        self.web_accounts.append(account)
        self._render_web_accounts()
        self.web_account_table.selectRow(len(self.web_accounts) - 1)
        self.web_account_table.editItem(self.web_account_table.item(len(self.web_accounts) - 1, 0))

    def remove_web_account(self) -> None:
        account_id = self._selected_web_account_id()
        account = next((value for value in self.web_accounts if value.account_id == account_id), None)
        if not account:
            return
        answer = QMessageBox.question(
            self, "移除网页登录账户",
            f"确定移除网页登录账户 {account.label}？本地保存的 Cookie 将被销毁。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        key = self._web_account_key(account.account_id)
        self.account_store.remove_web_account(account.account_id)
        self.web_accounts = [value for value in self.web_accounts if value.account_id != account.account_id]
        self.account_services.pop(key, None)
        self.account_repositories.pop(key, None)
        self._render_web_accounts()
        self._render_repositories()

    def open_online_login(self, account_id: str | None = None) -> None:
        account_id = account_id or self._selected_web_account_id()
        account = next((item for item in self.web_accounts if item.account_id == account_id), None)
        if not account:
            QMessageBox.information(self, self._t("请选择账户"), self._t("请先添加一个网页登录账户。"))
            return
        dialog = ModelScopeLoginDialog(account.label or account.username, self)
        dialog.session_captured.connect(
            lambda session, info, current_id=account.account_id: self._online_login_completed(current_id, session, info)
        )
        dialog.finished.connect(dialog.deleteLater)
        self.online_login_dialog = dialog
        dialog.show()

    def _online_login_completed(
        self,
        account_id: str,
        session: ModelScopeWebSession,
        user_info: dict,
    ) -> None:
        account = next((item for item in self.web_accounts if item.account_id == account_id), None)
        if not account:
            return
        web_username = web_session_username(user_info)
        account.username = web_username
        account.status = "connected"
        self.account_store.save_web_account(account)
        self.account_store.save_web_session(account_id, session)
        self._render_web_accounts()
        self._log(f"网页登录信息已安全保存：{account.label}")
        QMessageBox.information(self, self._t("在线登录成功"), self._t("网页登录信息已使用设备绑定加密保存。"))
        QTimer.singleShot(0, self.load_repositories)

    def add_account(self) -> None:
        self.account_table.clearSelection()
        self.account_table.setCurrentItem(None)
        self.active_account_id = None
        self.account_name_edit.clear()
        self.token_edit.clear()
        self.remember_token.setChecked(True)
        self.account_label.setText(self._t("输入新账户 Token 后保存并验证"))
        self.token_edit.setFocus()

    def remove_account(self) -> None:
        account_id = self._selected_account_id()
        account = next((item for item in self.accounts if item.account_id == account_id), None)
        if not account:
            return
        answer = QMessageBox.question(
            self,
            self._t("移除账户"),
            self._tf("确定移除账户 {name}？本地保存的 Token 将被销毁。", name=account.label),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.account_store.remove(account.account_id)
        self.session_tokens.pop(account.account_id, None)
        self.account_services.pop(account.account_id, None)
        self.account_repositories.pop(account.account_id, None)
        self.accounts = [item for item in self.accounts if item.account_id != account.account_id]
        if self.active_account_id == account.account_id:
            self.active_account_id = None
            self.service = None
            self.selected_repo = None
            self.remote_entries = []
            self.remote_tree.clear()
            self.refresh_files_button.setEnabled(False)
            self.new_folder_button.setEnabled(False)
            self._update_upload_enabled()
        self._render_accounts()
        self._render_repositories()
        self._render_backup_account_options()
        self._render_backup_jobs()
        self._render_image_account_options()
        self.add_account()

    def _account_remember_changed(self, account_id: str, checked: bool) -> None:
        if self._restoring_settings:
            return
        account = next((item for item in self.accounts if item.account_id == account_id), None)
        if not account:
            return
        account.remember = checked
        account.token = self.session_tokens.get(account_id, account.token)
        try:
            self.account_store.save(account)
        except Exception as exc:
            self._log(f"账户安全设置保存失败：{exc}")

    def _render_backup_account_options(self) -> None:
        if not hasattr(self, "backup_account_combo"):
            return
        selected = self.backup_account_combo.currentData()
        self.backup_account_combo.blockSignals(True)
        self.backup_account_combo.clear()
        account_options = [(account.account_id, account.label or account.username) for account in self.accounts]
        for account_id, label in account_options:
            self.backup_account_combo.addItem(label, account_id)
        index = self.backup_account_combo.findData(selected)
        self.backup_account_combo.setCurrentIndex(index if index >= 0 else (0 if account_options else -1))
        self.backup_account_combo.blockSignals(False)
        self._render_backup_repository_options()

    def _render_backup_repository_options(self) -> None:
        if not hasattr(self, "backup_repo_combo"):
            return
        selected = self.backup_repo_combo.currentData()
        account_id = str(self.backup_account_combo.currentData() or "")
        self.backup_repo_combo.clear()
        for repo in self.account_repositories.get(account_id, []):
            self.backup_repo_combo.addItem(
                f"{repo.repo_id} · {self._t('数据集') if repo.repo_type == 'dataset' else self._t('模型')}",
                (repo.repo_type, repo.repo_id),
            )
        index = self.backup_repo_combo.findData(selected)
        if index >= 0:
            self.backup_repo_combo.setCurrentIndex(index)

    def _render_backup_jobs(self) -> None:
        if not hasattr(self, "backup_table"):
            return
        account_labels = {account.account_id: account.label for account in self.accounts}
        self.backup_table.setRowCount(0)
        for job in self.backup_jobs:
            row = self.backup_table.rowCount()
            self.backup_table.insertRow(row)
            name_item = QTableWidgetItem(job.name)
            name_item.setData(Qt.ItemDataRole.UserRole, job.job_id)
            self.backup_table.setItem(row, 0, name_item)
            self.backup_table.setItem(row, 1, QTableWidgetItem(job.local_path))
            self.backup_table.setItem(row, 2, QTableWidgetItem(account_labels.get(job.account_id, job.account_id)))
            self.backup_table.setItem(row, 3, QTableWidgetItem(job.repo_id))
            self.backup_table.setItem(row, 4, QTableWidgetItem(job.dest_dir or "/"))
            mode = "增量备份" if job.mode == "incremental" else "同路径覆盖"
            self.backup_table.setItem(row, 5, QTableWidgetItem(self._t(mode)))
            unit = "小时" if job.interval_unit == "hour" else "分钟"
            self.backup_table.setItem(row, 6, QTableWidgetItem(f"{job.interval_value:g} {self._t(unit)}"))
            if not job.enabled:
                status = self._t("已停用")
            elif job.last_scan:
                status = self._tf("上次扫描：{time}", time=datetime.fromtimestamp(job.last_scan).strftime("%Y-%m-%d %H:%M"))
            else:
                status = self._t("等待首次扫描")
            self.backup_table.setItem(row, 7, QTableWidgetItem(status))

    def _selected_backup_job(self) -> BackupJob | None:
        row = self.backup_table.currentRow() if hasattr(self, "backup_table") else -1
        item = self.backup_table.item(row, 0) if row >= 0 else None
        job_id = str(item.data(Qt.ItemDataRole.UserRole)) if item else ""
        return next((job for job in self.backup_jobs if job.job_id == job_id), None)

    def _backup_selected(self) -> None:
        job = self._selected_backup_job()
        if not job:
            return
        self.backup_name_edit.setText(job.name)
        self.backup_local_edit.setText(job.local_path)
        account_index = self.backup_account_combo.findData(job.account_id)
        if account_index >= 0:
            self.backup_account_combo.setCurrentIndex(account_index)
        self._render_backup_repository_options()
        repo_index = self.backup_repo_combo.findData((job.repo_type, job.repo_id))
        if repo_index >= 0:
            self.backup_repo_combo.setCurrentIndex(repo_index)
        self.backup_dest_edit.setText(job.dest_dir)
        mode_index = self.backup_mode_combo.findData(job.mode)
        self.backup_mode_combo.setCurrentIndex(max(0, mode_index))
        self.backup_interval_value.setValue(job.interval_value)
        unit_index = self.backup_interval_unit.findData(job.interval_unit)
        self.backup_interval_unit.setCurrentIndex(max(0, unit_index))
        self.backup_download_limit.setValue(job.download_limit_mb)
        self.backup_enabled.setChecked(job.enabled)
        self.backup_status_label.setText(self._t("备份任务已加载"))

    def _new_backup_job(self) -> None:
        self.backup_table.clearSelection()
        self.backup_name_edit.clear()
        self.backup_local_edit.clear()
        self.backup_dest_edit.clear()
        self.backup_mode_combo.setCurrentIndex(0)
        self.backup_interval_value.setValue(30)
        self.backup_interval_unit.setCurrentIndex(0)
        self.backup_download_limit.setValue(10)
        self.backup_enabled.setChecked(True)
        self.backup_status_label.setText(self._t("填写后保存新的备份任务"))

    def _browse_backup_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self, self._t("选择备份文件夹"), self.backup_local_edit.text().strip()
        )
        if selected:
            self.backup_local_edit.setText(selected)
            if not self.backup_name_edit.text().strip():
                self.backup_name_edit.setText(Path(selected).name)

    def _save_backup_job(self) -> None:
        local_path = Path(self.backup_local_edit.text().strip())
        repo_data = self.backup_repo_combo.currentData()
        if not local_path.is_dir():
            QMessageBox.warning(self, self._t("备份配置无效"), self._t("请选择存在的本地文件夹。"))
            return
        if not (isinstance(repo_data, tuple) and len(repo_data) == 2):
            QMessageBox.warning(self, self._t("备份配置无效"), self._t("请选择可写入的目标仓库。"))
            return
        existing = self._selected_backup_job()
        job = BackupJob(
            existing.job_id if existing else "",
            self.backup_name_edit.text().strip() or local_path.name,
            str(self.backup_account_combo.currentData() or ""),
            str(local_path.resolve()),
            str(repo_data[0]), str(repo_data[1]),
            self.backup_dest_edit.text().replace("\\", "/").strip("/"),
            str(self.backup_mode_combo.currentData()),
            self.backup_interval_value.value(),
            str(self.backup_interval_unit.currentData()),
            self.backup_download_limit.value(),
            self.backup_enabled.isChecked(),
        )
        self.backup_store.save_job(job)
        self.backup_jobs = self.backup_store.list_jobs()
        self._render_backup_jobs()
        self.backup_status_label.setText(self._t("备份任务已保存"))

    def _remove_backup_job(self) -> None:
        job = self._selected_backup_job()
        if not job:
            return
        answer = QMessageBox.question(
            self, self._t("移除备份任务"),
            self._tf("确定移除备份任务 {name}？远端和本地文件不会被删除。", name=job.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.backup_store.remove_job(job.job_id)
        self.backup_jobs = self.backup_store.list_jobs()
        self._render_backup_jobs()
        self._new_backup_job()

    def _scan_selected_backup(self) -> None:
        job = self._selected_backup_job()
        if job:
            self._start_backup_job(job)

    def _check_backup_schedule(self) -> None:
        if self.backup_thread and self.backup_thread.isRunning():
            return
        if self.task and self.task.isRunning():
            return
        for job in self.backup_store.due_jobs():
            if job.account_id in self.account_services:
                self._start_backup_job(job, automatic=True)
                break

    def _start_backup_job(self, job: BackupJob, automatic: bool = False) -> None:
        if self.backup_thread and self.backup_thread.isRunning():
            if not automatic:
                QMessageBox.information(self, self._t("备份进行中"), self._t("请等待当前备份任务完成。"))
            return
        if self.task and self.task.isRunning():
            if not automatic:
                QMessageBox.information(self, self._t("请稍候"), self._t("当前传输完成后再执行备份。"))
            return
        service = self.account_services.get(job.account_id)
        repo = next((candidate for candidate in self.account_repositories.get(job.account_id, [])
                     if candidate.repo_type == job.repo_type and candidate.repo_id == job.repo_id), None)
        if not service or not repo:
            if not automatic:
                QMessageBox.warning(self, self._t("无法开始备份"), self._t("目标账户或仓库当前不可用。"))
            return
        worker = BackupThread(self.backup_store, job, service, repo, self)
        worker.item_done.connect(self._backup_item_done)
        worker.progress_info.connect(self._backup_progress)
        worker.completed.connect(self._backup_completed)
        worker.finished.connect(lambda: self._backup_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.backup_thread = worker
        self.backup_automatic = automatic
        self.backup_scan_button.setEnabled(False)
        self.backup_status_label.setText(self._tf("正在扫描并备份：{name}", name=job.name))
        self._log(f"开始备份任务：{job.name}")
        worker.start()

    def _backup_item_done(self, relative_path: str, success: bool, message: str) -> None:
        self._log(f"备份 {'完成' if success else '失败'}：{relative_path} · {message}")

    def _backup_progress(self, completed: int, total: int, speed: float, eta: int) -> None:
        self.current_upload_speed = speed
        percent = int(completed * 100 / max(1, total))
        self.backup_status_label.setText(self._tf(
            "备份中：{percent}% · {speed} · 剩余 {eta}",
            percent=percent, speed=format_speed(speed), eta=format_eta(eta),
        ))

    def _backup_completed(self, job_id: str, uploaded: int, failed: int, skipped: int) -> None:
        self.current_upload_speed = 0.0
        self.backup_jobs = self.backup_store.list_jobs()
        job = next((candidate for candidate in self.backup_jobs if candidate.job_id == job_id), None)
        if job and uploaded:
            repo = next((candidate for candidate in self.account_repositories.get(job.account_id, [])
                         if candidate.repo_type == job.repo_type and candidate.repo_id == job.repo_id), None)
            if repo:
                self._mark_repository_dirty(job.account_id, repo)
        self._render_backup_jobs()
        self.backup_status_label.setText(self._tf(
            "备份完成：{uploaded} 个上传，{failed} 个失败，{skipped} 个超过或等于 50 GB 已跳过",
            uploaded=uploaded, failed=failed, skipped=skipped,
        ))
        if skipped and not self.backup_automatic:
            QMessageBox.warning(
                self, self._t("已跳过超大文件"),
                self._tf("{count} 个文件达到或超过 50 GB，未上传。", count=skipped),
            )

    def _backup_finished(self, worker: BackupThread) -> None:
        if self.backup_thread is worker:
            self.backup_thread = None
        self.backup_automatic = False
        self.backup_scan_button.setEnabled(True)

    def _render_image_account_options(self) -> None:
        if not hasattr(self, "image_account_combo"):
            return
        selected = self.image_account_combo.currentData() or self.settings.value("image/account_id", "")
        self.image_account_combo.blockSignals(True)
        self.image_account_combo.clear()
        account_options = [(account.account_id, account.label or account.username) for account in self.accounts]
        for account_id, label in account_options:
            self.image_account_combo.addItem(label, account_id)
        index = self.image_account_combo.findData(selected)
        self.image_account_combo.setCurrentIndex(index if index >= 0 else (0 if account_options else -1))
        self.image_account_combo.blockSignals(False)
        self._render_image_repository_options()

    def _render_image_repository_options(self) -> None:
        if not hasattr(self, "image_repo_combo"):
            return
        account_id = str(self.image_account_combo.currentData() or "")
        selected = self._image_repository_selections.get(account_id)
        if not selected:
            selected = (
                str(self.settings.value(
                    f"image/repositories/{account_id}/repo_type",
                    self.settings.value("image/repo_type", ""),
                )),
                str(self.settings.value(
                    f"image/repositories/{account_id}/repo_id",
                    self.settings.value("image/repo_id", ""),
                )),
            )
        self.image_repo_combo.blockSignals(True)
        self.image_repo_combo.clear()
        for repo in self.account_repositories.get(account_id, []):
            self.image_repo_combo.addItem(repo.repo_id, (repo.repo_type, repo.repo_id))
        index = next((
            candidate for candidate in range(self.image_repo_combo.count())
            if self.image_repo_combo.itemData(candidate) == selected
        ), -1)
        self.image_repo_combo.setCurrentIndex(index if index >= 0 else (0 if self.image_repo_combo.count() else -1))
        self.image_repo_combo.blockSignals(False)
        self._save_image_settings()

    def _save_image_settings(self) -> None:
        if self._restoring_settings or not hasattr(self, "image_account_combo"):
            return
        self.settings.setValue("image/account_id", self.image_account_combo.currentData() or "")
        repo_data = self.image_repo_combo.currentData()
        if isinstance(repo_data, tuple) and len(repo_data) == 2:
            account_id = str(self.image_account_combo.currentData() or "")
            self._image_repository_selections[account_id] = repo_data
            self.settings.setValue("image/repo_type", repo_data[0])
            self.settings.setValue("image/repo_id", repo_data[1])
            self.settings.setValue(f"image/repositories/{account_id}/repo_type", repo_data[0])
            self.settings.setValue(f"image/repositories/{account_id}/repo_id", repo_data[1])
        self.settings.setValue("image/destination", self.image_dest_edit.text().strip("/"))

    def _render_image_records(self) -> None:
        if not hasattr(self, "image_table"):
            return
        self.image_table.setRowCount(0)
        for record in self.image_records:
            row = self.image_table.rowCount()
            self.image_table.insertRow(row)
            name_item = QTableWidgetItem(Path(record.remote_path).name)
            name_item.setData(Qt.ItemDataRole.UserRole, record.image_id)
            cache = Path(record.cache_path)
            if cache.is_file():
                name_item.setIcon(QIcon(str(cache)))
            self.image_table.setItem(row, 0, name_item)
            self.image_table.setItem(row, 1, QTableWidgetItem(record.repo_id))
            self.image_table.setItem(row, 2, QTableWidgetItem(record.remote_path))
            link_item = QTableWidgetItem(record.direct_url)
            link_item.setToolTip(record.direct_url)
            self.image_table.setItem(row, 3, link_item)
            self.image_table.setItem(row, 4, QTableWidgetItem(self._t("已缓存") if cache.is_file() else self._t("缓存缺失")))
            self.image_table.setItem(row, 5, QTableWidgetItem(datetime.fromtimestamp(record.created_at).strftime("%Y-%m-%d %H:%M")))

    def _selected_image_record(self) -> ImageRecord | None:
        row = self.image_table.currentRow() if hasattr(self, "image_table") else -1
        item = self.image_table.item(row, 0) if row >= 0 else None
        image_id = str(item.data(Qt.ItemDataRole.UserRole)) if item else ""
        return next((record for record in self.image_records if record.image_id == image_id), None)

    def _pick_images(self) -> None:
        extensions = " ".join(f"*{extension}" for extension in sorted(IMAGE_EXTENSIONS))
        paths, _ = QFileDialog.getOpenFileNames(self, self._t("选择图片"), "", f"Images ({extensions});;All files (*)")
        self._upload_images(paths)

    def _upload_images(self, raw_paths: list[str], temporary_paths: set[Path] | None = None) -> None:
        def discard_temporary_paths() -> None:
            for path in temporary_paths or set():
                path.unlink(missing_ok=True)

        if self.image_upload_thread and self.image_upload_thread.isRunning():
            discard_temporary_paths()
            QMessageBox.information(self, self._t("图片上传中"), self._t("请等待当前图片上传完成。"))
            return
        if (self.task and self.task.isRunning()) or (self.backup_thread and self.backup_thread.isRunning()):
            discard_temporary_paths()
            QMessageBox.information(self, self._t("请稍候"), self._t("当前传输完成后再上传图片。"))
            return
        paths = [Path(raw).resolve() for raw in raw_paths if is_supported_image_file(Path(raw))]
        if not paths:
            discard_temporary_paths()
            QMessageBox.information(self, self._t("没有可上传图片"), self._t("请选择支持的常见图片格式。"))
            return
        account_id = str(self.image_account_combo.currentData() or "")
        repo_data = self.image_repo_combo.currentData()
        service = self.account_services.get(account_id)
        repo = None
        if isinstance(repo_data, tuple) and len(repo_data) == 2:
            repo = next((candidate for candidate in self.account_repositories.get(account_id, [])
                         if candidate.repo_type == repo_data[0] and candidate.repo_id == repo_data[1]), None)
        if not service or not repo:
            discard_temporary_paths()
            QMessageBox.warning(self, self._t("图床配置无效"), self._t("请选择已经验证且可写入的账户仓库。"))
            return
        try:
            destination = normalize_remote_path(self.image_dest_edit.text())
        except ValueError as exc:
            discard_temporary_paths()
            QMessageBox.warning(self, self._t("路径无效"), str(exc))
            return
        worker = ImageUploadThread(
            self.image_store, account_id, service, repo, paths, destination,
            temporary_paths, self,
        )
        worker.uploaded.connect(self._image_uploaded)
        worker.item_done.connect(self._image_item_done)
        worker.completed.connect(lambda ok, failed, aid=account_id, target=repo: self._image_upload_completed(aid, target, ok, failed))
        worker.finished.connect(lambda: self._image_upload_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.image_upload_thread = worker
        self.image_status_label.setText(self._tf("正在上传 {count} 张图片…", count=len(paths)))
        self._save_image_settings()
        worker.start()

    def _paste_images_from_clipboard(self) -> None:
        mime_data = QApplication.clipboard().mimeData()
        if mime_data.hasUrls():
            paths = [url.toLocalFile() for url in mime_data.urls() if url.isLocalFile()]
            self._upload_images(paths)
            return
        if mime_data.hasImage():
            image = QApplication.clipboard().image()
            if not image.isNull():
                with tempfile.NamedTemporaryFile(prefix="modelscope-clipboard-", suffix=".png", delete=False) as handle:
                    path = Path(handle.name).resolve()
                if image.save(str(path), "PNG"):
                    self._upload_images([str(path)], {path})
                    return
                path.unlink(missing_ok=True)
        QMessageBox.information(self, self._t("没有可上传图片"), self._t("剪贴板内容不是图片。"))

    def _image_uploaded(self, record: ImageRecord) -> None:
        self.image_records.insert(0, record)
        self._render_image_records()
        QApplication.clipboard().setText(record.direct_url)
        self.image_status_label.setText(self._t("上传成功，最新图片直链已复制"))

    def _image_item_done(self, path: str, success: bool, message: str) -> None:
        self._log(f"图床 {'完成' if success else '失败'}：{Path(path).name} · {message}")

    def _image_upload_completed(self, account_id: str, repo: Repository, ok: int, failed: int) -> None:
        if ok:
            self._mark_repository_dirty(account_id, repo)
        self.image_status_label.setText(self._tf("图片上传完成：{ok} 个成功，{failed} 个失败", ok=ok, failed=failed))

    def _image_upload_finished(self, worker: ImageUploadThread) -> None:
        if self.image_upload_thread is worker:
            self.image_upload_thread = None

    def _copy_selected_image_link(self) -> None:
        record = self._selected_image_record()
        if record:
            QApplication.clipboard().setText(record.direct_url)
            self.image_status_label.setText(self._t("图片直链已复制"))

    def _open_selected_image(self) -> None:
        record = self._selected_image_record()
        if not record:
            return
        cache_path = Path(record.cache_path)
        if cache_path.is_file():
            self._open_local_media(cache_path)
        else:
            QMessageBox.information(self, self._t("缓存缺失"), self._t("本地缓存已不存在，请重新上传或从仓库下载。"))

    def _remove_selected_image(self) -> None:
        record = self._selected_image_record()
        if not record:
            return
        answer = QMessageBox.question(
            self, self._t("移除图床记录"),
            self._t("仅移除本地记录和缓存，远端文件不会删除。是否继续？"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.image_store.remove(record.image_id)
        self.image_records = self.image_store.list_records()
        self._render_image_records()

    def _sync_selected_backup(self) -> None:
        job = self._selected_backup_job()
        if not job:
            return
        service = self.account_services.get(job.account_id)
        repo = next((candidate for candidate in self.account_repositories.get(job.account_id, [])
                     if candidate.repo_type == job.repo_type and candidate.repo_id == job.repo_id), None)
        if not service or not repo:
            QMessageBox.warning(self, self._t("无法同步"), self._t("目标账户或仓库当前不可用。"))
            return
        if not Path(job.local_path).is_dir():
            QMessageBox.warning(self, self._t("无法同步"), self._t("本地备份文件夹不存在。"))
            return
        self._run_task(
            lambda: (job, service, repo, service.list_entries(repo)),
            self._backup_cloud_entries_loaded,
            "正在读取云端备份…",
        )

    def _backup_cloud_entries_loaded(self, result) -> None:
        job, service, repo, entries = result
        base = job.dest_dir.strip("/")
        candidates: list[tuple[RemoteEntry, str]] = []
        if job.mode == "incremental":
            timestamps: set[str] = set()
            for entry in entries:
                path = entry.path.strip("/")
                relative = path[len(base) + 1:] if base and path.startswith(base + "/") else (path if not base else "")
                first = relative.split("/", 1)[0] if relative else ""
                if re.fullmatch(r"\d{8}-\d{6}", first):
                    timestamps.add(first)
            if not timestamps:
                QMessageBox.information(self, self._t("没有云端备份"), self._t("目标目录中没有时间戳备份。"))
                return
            latest = max(timestamps)
            prefix = normalize_remote_path(base, latest)
        else:
            prefix = base
        for entry in entries:
            if entry.is_dir or entry.size > int(job.download_limit_mb * 1024**2):
                continue
            path = entry.path.strip("/")
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                relative = path[len(prefix) + 1:]
            else:
                relative = path
            if relative:
                candidates.append((entry, relative))
        root = Path(job.local_path).resolve()
        specs: list[DownloadSpec] = []
        for entry, relative in candidates:
            local_path = root.joinpath(*[part for part in relative.split("/") if part]).resolve()
            if not local_path.is_relative_to(root):
                continue
            specs.append(DownloadSpec(
                entry.path,
                local_path,
                service.get_download_url(repo, entry.path),
                entry.size,
                entry.sha256,
                str(service.token or ""),
            ))
        if not specs:
            QMessageBox.information(
                self, self._t("没有可同步文件"),
                self._tf("没有小于或等于 {limit:g} MB 的云端文件。", limit=job.download_limit_mb),
            )
            return
        # This callback runs just before the repository-listing worker emits
        # finished. Give that worker time to release the shared transfer slot.
        added = self._enqueue_download_specs(specs, auto_start_delay_ms=150)
        if added:
            for spec in specs:
                if self.download_states.get(str(spec.local_path)) == "waiting":
                    self.backup_sync_job_paths[str(spec.local_path)] = job.job_id
            self.backup_store.mark_sync(job.job_id)
            self.backup_jobs = self.backup_store.list_jobs()
            self._render_backup_jobs()
            self.backup_status_label.setText(self._tf("已添加 {count} 个云端文件到下载队列", count=added))


    @staticmethod
    def _stepper(control: QSpinBox | QDoubleSpinBox) -> QWidget:
        control.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(control, 1)
        increase = QPushButton("增大")
        increase.clicked.connect(control.stepUp)
        layout.addWidget(increase)
        decrease = QPushButton("减小")
        decrease.clicked.connect(control.stepDown)
        layout.addWidget(decrease)
        return wrapper

    def _navigate(self, index: int) -> None:
        page = self._page_by_id.get(index)
        if page is not None and self.page_stack.currentWidget() is not page:
            self.switchTo(page)

    def switchTo(self, interface: QWidget) -> None:
        """Use a shorter Fluent transition to avoid repainting complex pages for 300 ms."""
        self.stackedWidget.view.setCurrentWidget(interface, duration=160)

    def _set_view_mode(self, mode: str) -> None:
        self.resource_view_mode = mode
        self.view_button.setText("查看：缩略图 ▾" if mode == "thumbnails" else "查看：详细信息 ▾")
        self.remote_detail_tree.setVisible(mode == "details")
        self.remote_thumbnail_list.setVisible(mode == "thumbnails")
        self._render_remote_details()
        if mode == "thumbnails":
            self._schedule_visible_thumbnails()

    def _schedule_visible_thumbnails(self) -> None:
        direct = self.remote_direct_cache.get(self.current_directory_path)
        if direct is None:
            direct = self._direct_remote_entries(self.remote_entries, self.current_directory_path)
            self.remote_direct_cache[self.current_directory_path] = direct
        self._enqueue_thumbnail_entries(direct, prioritize=True)
        if not (self.thumbnail_task and self.thumbnail_task.isRunning()):
            self.thumbnail_timer.start(350 if len(direct) <= 100 else 900)

    def _enqueue_thumbnail_entries(self, entries: list[RemoteEntry], prioritize: bool = False) -> None:
        maximum_size = int(self.thumbnail_maximum_mb.value() * 1024 * 1024)
        eligible = [
            entry for entry in entries
            if ThumbnailThread.is_eligible(entry, maximum_size)
            and entry.path not in self.thumbnail_paths
            and entry.path not in self.thumbnail_attempted
        ]
        if prioritize:
            priority_paths = {entry.path for entry in eligible}
            self.thumbnail_queue = deque(
                entry for entry in self.thumbnail_queue if entry.path not in priority_paths
            )
            for entry in reversed(eligible):
                self.thumbnail_queue.appendleft(entry)
            self.thumbnail_queued.update(priority_paths)
            return
        for entry in eligible:
            if entry.path in self.thumbnail_queued:
                continue
            self.thumbnail_queue.append(entry)
            self.thumbnail_queued.add(entry.path)

    def _reset_thumbnail_queue(self, entries: list[RemoteEntry]) -> None:
        if self.thumbnail_task and self.thumbnail_task.isRunning():
            try:
                self.thumbnail_task.ready.disconnect(self._thumbnails_ready)
            except RuntimeError:
                pass
            self.thumbnail_task.requestInterruption()
        self.thumbnail_timer.stop()
        self.thumbnail_queue.clear()
        self.thumbnail_queued.clear()
        self.thumbnail_paths.clear()
        self.thumbnail_attempted.clear()
        recursive = sorted(entries, key=lambda entry: (entry.path.count("/"), entry.path.casefold()))
        self._enqueue_thumbnail_entries(recursive)
        if self.thumbnail_queue:
            self.thumbnail_timer.start(150)

    def _load_visible_thumbnails(self) -> None:
        if not self.service or not self.selected_repo or self.thumbnail_task:
            return
        idle_for = time.monotonic() - self._last_user_interaction
        if idle_for < 0.15:
            self.thumbnail_timer.start(max(30, int((0.15 - idle_for) * 1000)))
            return
        batch_size, worker_limit, batch_delay = thumbnail_batch_policy(len(self.remote_entries))
        visible: list[RemoteEntry] = []
        while self.thumbnail_queue and len(visible) < batch_size:
            entry = self.thumbnail_queue.popleft()
            self.thumbnail_queued.discard(entry.path)
            if entry.path not in self.thumbnail_paths and entry.path not in self.thumbnail_attempted:
                visible.append(entry)
        maximum_size = int(self.thumbnail_maximum_mb.value() * 1024 * 1024)
        if not visible:
            return
        self.thumbnail_task = ThumbnailThread(
            self.service, self.selected_repo, visible, maximum_size,
            min(self.thumbnail_workers.value(), worker_limit), self,
        )
        self.thumbnail_task.ready.connect(self._thumbnails_ready)
        self.thumbnail_task.finished.connect(self.thumbnail_task.deleteLater)
        task = self.thumbnail_task
        self.thumbnail_task.finished.connect(lambda: self._thumbnail_task_finished(task))
        self.thumbnail_task.start()

    def _thumbnail_task_finished(self, task: ThumbnailThread) -> None:
        if not task.isInterruptionRequested():
            self.thumbnail_attempted.update(entry.path for entry in task.entries)
        if self.thumbnail_task is task:
            self.thumbnail_task = None
        if self.thumbnail_queue:
            self.thumbnail_timer.start(thumbnail_batch_policy(len(self.remote_entries))[2])

    def _thumbnails_ready(self, paths: dict[str, str]) -> None:
        self.thumbnail_paths.update(paths)
        for index in range(self.remote_thumbnail_list.count()):
            item = self.remote_thumbnail_list.item(index)
            entry = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(entry, RemoteEntry) and entry.path in paths:
                item.setIcon(QIcon(paths[entry.path]))

    def show_resource_search(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("搜索已索引资源")
        dialog.resize(820, 520)
        layout = QVBoxLayout(dialog)
        filters = QHBoxLayout()
        scope = QComboBox()
        scope.addItem("全部仓库", "all")
        scope.addItem("当前账户", "account")
        scope.addItem("当前仓库", "repository")
        scope.addItem("当前目录", "directory")
        kind = QComboBox()
        kind.addItem("全部类型", "all")
        kind.addItem("视频", "video")
        kind.addItem("图片", "image")
        kind.addItem("文档", "document")
        kind.addItem("压缩包", "archive")
        tag = QComboBox()
        tag.addItem("全部标签", "")
        for value in self.account_store.all_tags():
            tag.addItem(value, value)
        query = QLineEdit()
        query.setPlaceholderText("高级搜索：路径片段 文件名前段 后段")
        filters.addWidget(scope)
        filters.addWidget(kind)
        filters.addWidget(tag)
        filters.addWidget(query, 1)
        layout.addLayout(filters)
        results = QTreeWidget()
        results.setHeaderLabels(["名称", "类型", "大小", "仓库", "路径"])
        results.header().setSortIndicatorShown(True)
        results.header().setStretchLastSection(True)
        layout.addWidget(results, 1)
        sort_column = 0
        sort_order = Qt.SortOrder.AscendingOrder

        def show_result_menu(position) -> None:
            item = results.itemAt(position)
            record = item.data(0, Qt.ItemDataRole.UserRole) if item else None
            if not isinstance(record, IndexedEntry):
                return
            service = ModelScopeService("", require_token=False) if record.account_id == PUBLIC_ACCOUNT_ID else self.account_services.get(record.account_id)
            if service is None:
                return
            repo = next((candidate for candidate in self.account_repositories.get(record.account_id, [])
                         if candidate.repo_type == record.repo_type and candidate.repo_id == record.repo_id), None)
            repo = repo or Repository(record.repo_id, record.repo_type, "public" if record.account_id == PUBLIC_ACCOUNT_ID else "")
            entries = [RemoteEntry(value.path, value.size, value.sha256, value.is_dir) for value in self.account_store.repository_entries(record.account_id, record.repo_type, record.repo_id)]
            entry = RemoteEntry(record.path, record.size, record.sha256, record.is_dir)
            self._show_remote_menu(results, position, entry, service, repo, entries or [entry], record.account_id)

        def search() -> None:
            account_id = repo_type = repo_id = path_prefix = None
            selected_scope = str(scope.currentData())
            if selected_scope != "all":
                account_id = PUBLIC_ACCOUNT_ID if self.selected_repo_public else self.active_account_id
            if selected_scope in {"repository", "directory"} and self.selected_repo:
                repo_type, repo_id = self.selected_repo.repo_type, self.selected_repo.repo_id
            if selected_scope == "directory":
                path_prefix = self.current_directory_path
            records = self.account_store.search_entries(
                query.text().strip(), str(kind.currentData()), account_id, repo_type, repo_id,
                path_prefix, str(tag.currentData() or ""),
            )
            keys = (
                lambda record: record.name.casefold(),
                lambda record: record.file_type.casefold(),
                lambda record: record.size,
                lambda record: record.repo_id.casefold(),
                lambda record: record.path.casefold(),
            )
            records.sort(key=keys[sort_column], reverse=sort_order == Qt.SortOrder.DescendingOrder)
            results.clear()
            for record in records:
                item = QTreeWidgetItem([record.name, record.file_type, format_size(record.size), record.repo_id, record.path])
                item.setData(0, Qt.ItemDataRole.UserRole, record)
                results.addTopLevelItem(item)

        def change_sort(column: int) -> None:
            nonlocal sort_column, sort_order
            sort_order = Qt.SortOrder.DescendingOrder if column == sort_column and sort_order == Qt.SortOrder.AscendingOrder else Qt.SortOrder.AscendingOrder
            sort_column = column
            results.header().setSortIndicator(column, sort_order)
            search()

        for control in (scope, kind, tag):
            control.currentIndexChanged.connect(search)
        query.textChanged.connect(search)
        query.returnPressed.connect(search)
        results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        results.customContextMenuRequested.connect(show_result_menu)
        results.header().sectionClicked.connect(change_sort)
        search()
        dialog.exec()

    def _t(self, source: str) -> str:
        return self.locale.text(source)

    def _tf(self, source: str, **values) -> str:
        return self._t(source).format(**values)

    def _apply_language(self) -> None:
        language = str(self.language_combo.currentData()) if hasattr(self, "language_combo") else "zh_CN"
        self.locale.load(language)
        text_widgets = self.findChildren(QLabel) + self.findChildren(QPushButton) + self.findChildren(QCheckBox)
        for widget in text_widgets:
            source = widget.property("i18nSourceText")
            if source is None:
                source = widget.text()
                widget.setProperty("i18nSourceText", source)
            widget.setText(self._t(str(source)))
            tooltip_source = widget.property("i18nSourceTooltip")
            if tooltip_source is None and widget.toolTip():
                tooltip_source = widget.toolTip()
                widget.setProperty("i18nSourceTooltip", tooltip_source)
            if tooltip_source:
                widget.setToolTip(self._t(str(tooltip_source)))
        for edit in self.findChildren(QLineEdit):
            source = edit.property("i18nPlaceholder")
            if source is None and edit.placeholderText():
                source = edit.placeholderText()
                edit.setProperty("i18nPlaceholder", source)
            if source:
                edit.setPlaceholderText(self._t(str(source)))
        for combo in self.findChildren(QComboBox) + self.findChildren(CleanComboBox):
            sources = combo.property("i18nItems")
            if sources is None:
                sources = [combo.itemText(index) for index in range(combo.count())]
                combo.setProperty("i18nItems", sources)
            for index, source in enumerate(sources):
                if index < combo.count():
                    combo.setItemText(index, self._t(str(source)))
        for tabs in self.findChildren(QTabWidget):
            sources = tabs.property("i18nTabs")
            if sources is None:
                sources = [tabs.tabText(index) for index in range(tabs.count())]
                tabs.setProperty("i18nTabs", sources)
            for index, source in enumerate(sources):
                tabs.setTabText(index, self._t(str(source)))
        for table in self.findChildren(QTableWidget):
            for column in range(table.columnCount()):
                item = table.horizontalHeaderItem(column)
                if item is None:
                    continue
                source = item.data(Qt.ItemDataRole.UserRole + 10)
                if source is None:
                    source = item.text()
                    item.setData(Qt.ItemDataRole.UserRole + 10, source)
                item.setText(self._t(str(source)))
        for tree in self.findChildren(QTreeWidget):
            item = tree.headerItem()
            if item is None:
                continue
            for column in range(tree.columnCount()):
                source = item.data(column, Qt.ItemDataRole.UserRole + 10)
                if source is None:
                    source = item.text(column)
                    item.setData(column, Qt.ItemDataRole.UserRole + 10, source)
                item.setText(column, self._t(str(source)))
        english = self.locale.language == "en_US"
        self.aria_small_limit.setSuffix(" MB or less" if english else " MB 以下")
        self.aria_large_limit.setSuffix(" MB or more" if english else " MB 以上")
        self.backup_download_limit.setSuffix(" MB or less" if english else " MB 以下")
        self.background_index_minutes.setSuffix(" min" if english else " 分钟")
        self.drop_upload_threshold_mb.setSuffix(" MB")
        self.resource_path_label.set_path(self.current_directory_path, self._t("根目录"))
        self._update_alist_url()
        if hasattr(self, "tray_show_action"):
            self.tray_show_action.setText(self._t("显示 ModelScope Manager"))
            self.tray_quit_action.setText(self._t("退出"))
            self._refresh_tray_status()

    def _language_changed(self) -> None:
        if self._restoring_settings:
            return
        language = str(self.language_combo.currentData())
        self.settings.setValue("language", language)
        self.locale.load(language)
        self._apply_language()
        self._log(self._t("语言设置已立即应用"))

    def _system_is_dark(self) -> bool:
        hints = QApplication.instance().styleHints()
        if hasattr(hints, "colorScheme"):
            return hints.colorScheme() == Qt.ColorScheme.Dark
        return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _apply_theme(self) -> None:
        mode = str(self.theme_combo.currentData()) if hasattr(self, "theme_combo") else "system"
        dark = self._system_is_dark() if mode == "system" else mode == "dark"
        acrylic = bool(hasattr(self, "acrylic_checkbox") and self.acrylic_checkbox.isChecked())
        setTheme(Theme.DARK if dark else Theme.LIGHT)
        setThemeColor("#0078D4")
        app = QApplication.instance()
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#202124" if dark else "#f3f3f3"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#24272c" if dark else "#ffffff"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#2a2d32" if dark else "#f7f7f7"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#e8e8e8" if dark else "#202020"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#e8e8e8" if dark else "#202020"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#30333a" if dark else "#ffffff"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ededed" if dark else "#202020"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#174d73" if dark else "#cce8ff"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff" if dark else "#202020"))
        app.setPalette(palette)
        app.setStyleSheet("")
        app.setStyleSheet(theme_qss(dark, acrylic))
        if hasattr(self, "queue_table"):
            self._render_upload_queue()
        self.setProperty("theme", "dark" if dark else "light")
        self._apply_window_effects(dark, acrylic)
        self._sync_matplotlib_theme(dark)
        if hasattr(self, "remote_thumbnail_list"):
            self.remote_thumbnail_list.style().unpolish(self.remote_thumbnail_list)
            self.remote_thumbnail_list.style().polish(self.remote_thumbnail_list)
            self.remote_thumbnail_list.viewport().update()

    def _apply_window_effects(self, dark: bool | None = None, acrylic: bool | None = None) -> None:
        if not hasattr(self, "acrylic_checkbox"):
            return
        if dark is None:
            mode = str(self.theme_combo.currentData())
            dark = self._system_is_dark() if mode == "system" else mode == "dark"
        if acrylic is None:
            acrylic = self.acrylic_checkbox.isChecked()
        # FluentWindow already owns the Windows compositor effect. A second DWM
        # backdrop plus a translucent Qt top-level causes stale backing-store frames.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        supported = sys.platform == "win32" and sys.getwindowsversion().build >= 22000
        self.setMicaEffectEnabled(bool(acrylic and supported))
        if hasattr(self, "graphics_status"):
            if acrylic and supported:
                self.graphics_status.setText("Mica 由 FluentWindow 和 Windows 合成器单层渲染。")
            elif acrylic:
                self.graphics_status.setText("当前系统不支持 Mica，已使用不透明背景以避免残影。")
            else:
                self.graphics_status.setText("Mica 已关闭，使用低开销不透明背景。")

    def _graphics_settings_changed(self) -> None:
        if not self._restoring_settings:
            self.settings.setValue("graphics/gpu_acceleration", self.gpu_acceleration_checkbox.isChecked())
            self.settings.setValue("graphics/acrylic", self.acrylic_checkbox.isChecked())
        self._apply_theme()

    def _theme_changed(self) -> None:
        if not self._restoring_settings:
            self.settings.setValue("theme", self.theme_combo.currentData())
        self._apply_theme()

    def _font_size_changed(self, value: int) -> None:
        if not self._restoring_settings:
            self.settings.setValue("font_size", value)
        self._apply_font_scale(value)

    def _apply_font_scale(self, point_size: int) -> None:
        app = QApplication.instance()
        base_size = 10.0
        widgets = app.allWidgets()
        for widget in widgets:
            if widget.property("fluentBasePointSize") is None:
                current_size = widget.font().pointSizeF()
                widget.setProperty(
                    "fluentBasePointSize", current_size if current_size > 0 else base_size
                )
        app_font = QFont("Microsoft YaHei UI")
        app_font.setPointSize(point_size)
        app.setFont(app_font)
        for widget in widgets:
            font = widget.font()
            baseline = widget.property("fluentBasePointSize")
            font.setPointSizeF(max(8.0, float(baseline) * point_size / base_size))
            font.setFamily("Microsoft YaHei UI")
            widget.setFont(font)
        self._sync_matplotlib_theme(
            self.property("theme") == "dark" if self.property("theme") else self._system_is_dark()
        )

    def _sync_matplotlib_theme(self, dark: bool) -> None:
        if "matplotlib" not in sys.modules:
            return
        import matplotlib as mpl

        size = self.font_size_spin.value() if hasattr(self, "font_size_spin") else 10
        background = "#202124" if dark else "#f3f3f3"
        foreground = "#e8e8e8" if dark else "#202020"
        mpl.rcParams.update({
            "font.family": ["Microsoft YaHei UI", "Microsoft YaHei", "sans-serif"],
            "font.size": size,
            "figure.facecolor": background,
            "axes.facecolor": background,
            "axes.edgecolor": foreground,
            "axes.labelcolor": foreground,
            "text.color": foreground,
            "xtick.color": foreground,
            "ytick.color": foreground,
        })
        for widget in QApplication.instance().allWidgets():
            figure = getattr(widget, "figure", None)
            if figure is not None:
                figure.set_facecolor(background)
                widget.draw_idle()

    def _system_theme_changed(self, _scheme) -> None:
        if str(self.theme_combo.currentData()) == "system":
            self._apply_theme()

    def _close_behavior_changed(self) -> None:
        if not self._restoring_settings:
            self.settings.setValue("close_behavior", self.close_behavior_combo.currentData())

    def _startup_changed(self, checked: bool) -> None:
        if self._restoring_settings:
            return
        try:
            set_windows_startup(checked, APP_DIR)
        except OSError as exc:
            self.startup_checkbox.blockSignals(True)
            self.startup_checkbox.setChecked(not checked)
            self.startup_checkbox.blockSignals(False)
            QMessageBox.warning(self, self._t("开机自启设置失败"), str(exc))

    def _compact_view_changed(self, checked: bool) -> None:
        height = 26 if checked else 32
        for tree_name in ("remote_tree", "remote_detail_tree", "global_search_tree", "search_remote_tree"):
            tree = getattr(self, tree_name, None)
            if tree is not None:
                tree.setStyleSheet(f"QTreeWidget::item {{ min-height: {height}px; }}")
        if hasattr(self, "repo_list"):
            left_height = 24 if checked else 36
            self.repo_list.setStyleSheet(f"QTreeWidget::item {{ min-height: {left_height}px; }}")
        if hasattr(self, "resource_splitter"):
            self.resource_splitter.widget(0).setMinimumWidth(245 if checked else 180)
            self.resource_splitter.setSizes([270, 820] if checked else [405, 685])
        if not self._restoring_settings:
            self.settings.setValue("compact_view", checked)

    def _wheel_setting_changed(self, checked: bool) -> None:
        if not self._restoring_settings:
            self.settings.setValue("disable_settings_wheel", checked)

    def _background_index_interval_changed(self, value: int) -> None:
        if not self._restoring_settings:
            self.settings.setValue("index/background_minutes", value)
        self._apply_background_index_interval()

    def _drop_upload_threshold_changed(self, value: int) -> None:
        if not self._restoring_settings:
            self.settings.setValue("upload/drop_threshold_mb", value)

    def _save_preview_settings(self) -> None:
        if self._restoring_settings:
            return
        self.settings.setValue("preview/thumbnail_maximum_mb", self.thumbnail_maximum_mb.value())
        self.settings.setValue("preview/thumbnail_workers", self.thumbnail_workers.value())
        self.settings.setValue("copy/threshold_value", self.copy_threshold_value.value())
        self.settings.setValue("copy/threshold_unit", self.copy_threshold_unit.currentData())

    def _copy_threshold_bytes(self) -> int:
        return int(self.copy_threshold_value.value() * int(self.copy_threshold_unit.currentData() or 1024 ** 2))

    def _apply_background_index_interval(self) -> None:
        if hasattr(self, "background_index_minutes"):
            self.background_index_timer.setInterval(max(1, self.background_index_minutes.value()) * 60000)
            if not self.background_index_timer.isActive():
                self.background_index_timer.start()

    def eventFilter(self, watched, event) -> bool:
        if not self._event_filter_ready:
            return super().eventFilter(watched, event)
        interaction_events = {
            QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease, QEvent.Type.Wheel,
        }
        if event.type() in interaction_events:
            self._last_user_interaction = time.monotonic()
            if self.thumbnail_queue and not (self.thumbnail_task and self.thumbnail_task.isRunning()):
                self.thumbnail_timer.start(900)
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Backspace
            and hasattr(self, "resource_page")
            and self.page_stack.currentWidget() is self.resource_page
            and isinstance(watched, QWidget)
            and self.resource_page.isAncestorOf(watched)
            and not isinstance(watched, (QLineEdit, QTextEdit, QAbstractSpinBox))
            and self._go_to_previous_directory()
        ):
            return True
        if (
            event.type() == QEvent.Type.KeyPress
            and hasattr(self, "image_page")
            and self.page_stack.currentWidget() is self.image_page
            and event.matches(QKeySequence.StandardKey.Paste)
        ):
            self._paste_images_from_clipboard()
            return True
        if (
            self.dirty_repositories
            and event.type() in interaction_events
        ):
            self.index_idle_timer.start()
        if (
            event.type() == QEvent.Type.Wheel
            and hasattr(self, "disable_settings_wheel")
            and self.disable_settings_wheel.isChecked()
            and isinstance(watched, (QComboBox, CleanComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit))
            and hasattr(self, "settings_page")
            and self.settings_page.isAncestorOf(watched)
        ):
            bar = self.settings_page.findChild(QScrollArea).verticalScrollBar()
            bar.setValue(bar.value() - int(event.angleDelta().y() / 2))
            return True
        return super().eventFilter(watched, event)

    def _build_tray(self) -> None:
        icon = self.windowIcon()
        if icon.isNull():
            icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DriveNetIcon)
            self.setWindowIcon(icon)
        self.tray_icon = QSystemTrayIcon(icon, self)
        tray_menu = QMenu(self)
        self.tray_menu = tray_menu

        status_widget = QWidget()
        status_layout = QHBoxLayout(status_widget)
        status_layout.setContentsMargins(10, 5, 8, 5)
        status_layout.setSpacing(7)
        self.tray_webdav_dot = QLabel("●")
        self.tray_webdav_dot.setFixedWidth(13)
        status_layout.addWidget(self.tray_webdav_dot)
        self.tray_webdav_label = QLabel()
        status_layout.addWidget(self.tray_webdav_label, 1)
        self.tray_webdav_button = QPushButton()
        self.tray_webdav_button.setFixedHeight(27)
        self.tray_webdav_button.clicked.connect(self._toggle_webdav_from_tray)
        status_layout.addWidget(self.tray_webdav_button)
        status_action = QWidgetAction(self)
        status_action.setDefaultWidget(status_widget)
        tray_menu.addAction(status_action)

        speed_widget = QWidget()
        speed_layout = QHBoxLayout(speed_widget)
        speed_layout.setContentsMargins(30, 3, 10, 7)
        self.tray_speed_label = QLabel()
        speed_layout.addWidget(self.tray_speed_label)
        speed_action = QWidgetAction(self)
        speed_action.setDefaultWidget(speed_widget)
        tray_menu.addAction(speed_action)
        tray_menu.addSeparator()

        self.tray_show_action = QAction(self._t("显示 ModelScope Manager"), self)
        self.tray_show_action.triggered.connect(self._show_from_tray)
        tray_menu.addAction(self.tray_show_action)
        self.tray_quit_action = QAction(self._t("退出"), self)
        self.tray_quit_action.triggered.connect(self._quit_from_tray)
        tray_menu.addAction(self.tray_quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(
            lambda reason: self._show_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        self.tray_refresh_timer = QTimer(self)
        self.tray_refresh_timer.setInterval(500)
        self.tray_refresh_timer.timeout.connect(self._refresh_tray_status)
        self.tray_refresh_timer.start()
        self._refresh_tray_status()

    def _refresh_tray_status(self) -> None:
        if not hasattr(self, "tray_webdav_label"):
            return
        running = bool(self.webdav and self.webdav.running)
        self.tray_webdav_dot.setStyleSheet(f"color: {'#16a34a' if running else '#d13438'}; font-size: 15px;")
        self.tray_webdav_label.setText(self._t(
            "WebDAV 监听已开启" if running else "WebDAV 监听已关闭"
        ))
        self.tray_webdav_button.setText(self._t("关闭" if running else "开启"))
        if self._has_active_transfer():
            self.tray_speed_label.setText(
                f"↑ {format_speed(self.current_upload_speed)}  ↓ {format_speed(self.current_download_speed)}"
            )
        else:
            self.tray_speed_label.setText(self._t("当前无任务"))

    def _toggle_webdav_from_tray(self) -> None:
        running = bool(self.webdav and self.webdav.running)
        if running:
            self.stop_alist()
        else:
            self.apply_alist_settings()
        self._refresh_tray_status()

    def _show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_from_tray(self) -> None:
        self._force_close = True
        self.close()

    def _has_active_transfer(self) -> bool:
        return bool(
            (self.backup_thread and self.backup_thread.isRunning())
            or (self.image_upload_thread and self.image_upload_thread.isRunning())
            or (self.potplayer_install_thread and self.potplayer_install_thread.isRunning())
            or
            self.task
            and self.task.isRunning()
            and isinstance(self.task, (UploadThread, DownloadThread))
        )

    def _confirm_terminate_transfers(self) -> bool:
        if not self._has_active_transfer():
            return True
        answer = QMessageBox.question(
            self,
            self._t("传输正在进行"),
            self._t("当前有任务在进行，是否终止所有任务并关闭？"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _shutdown_services(self) -> None:
        if self.download_runner:
            try:
                self.download_runner.stop()
            except Exception:
                pass
        if self.webdav:
            self.webdav.stop()
            self.webdav = None
        if self.index_task and self.index_task.isRunning():
            self.index_task.requestInterruption()
            self.index_task.wait(3000)
        if self.backup_thread and self.backup_thread.isRunning():
            self.backup_thread.requestInterruption()
            self.backup_thread.wait(3000)
        if self.image_upload_thread and self.image_upload_thread.isRunning():
            self.image_upload_thread.requestInterruption()
            self.image_upload_thread.wait(3000)
        if self.potplayer_install_thread and self.potplayer_install_thread.isRunning():
            self.potplayer_install_thread.wait(3000)
        self.media_proxy.stop()
        if hasattr(self, "tray_icon"):
            self.tray_icon.hide()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._apply_window_effects)

    def closeEvent(self, event) -> None:
        behavior = "close" if self._force_close else str(self.close_behavior_combo.currentData())
        remember = False
        if behavior == "ask":
            box = QMessageBox(self)
            box.setWindowTitle(self._t("关闭窗口"))
            box.setText(self._t("关闭程序，还是最小化到通知区域？"))
            close_button = box.addButton(self._t("关闭程序"), QMessageBox.ButtonRole.AcceptRole)
            tray_button = box.addButton(self._t("最小化"), QMessageBox.ButtonRole.ActionRole)
            box.addButton(self._t("取消"), QMessageBox.ButtonRole.RejectRole)
            remember_box = QCheckBox(self._t("是否记住"))
            box.setCheckBox(remember_box)
            box.exec()
            clicked = box.clickedButton()
            if clicked is close_button:
                behavior = "close"
            elif clicked is tray_button:
                behavior = "tray"
            else:
                event.ignore()
                self._force_close = False
                return
            remember = remember_box.isChecked()
        if behavior == "tray" and QSystemTrayIcon.isSystemTrayAvailable():
            if remember:
                self.settings.setValue("close_behavior", "tray")
                self.close_behavior_combo.setCurrentIndex(self.close_behavior_combo.findData("tray"))
            self.tray_icon.show()
            self.hide()
            self.tray_icon.showMessage(
                "ModelScope Manager",
                self._t("程序仍在通知区域运行，传输任务不会中断。"),
                QSystemTrayIcon.MessageIcon.Information,
                3000,
            )
            event.ignore()
            self._force_close = False
            return
        if not self._confirm_terminate_transfers():
            event.ignore()
            self._force_close = False
            return
        if remember:
            self.settings.setValue("close_behavior", "close")
            self.close_behavior_combo.setCurrentIndex(self.close_behavior_combo.findData("close"))
        self._shutdown_services()
        event.accept()

    def _save_aria2_settings(self) -> None:
        if self._restoring_settings:
            return
        self.settings.setValue("aria2/small_limit_mb", self.aria_small_limit.value())
        self.settings.setValue("aria2/small_segments", self.aria_small_segments.value())
        self.settings.setValue("aria2/medium_segments", self.aria_medium_segments.value())
        self.settings.setValue("aria2/large_limit_mb", self.aria_large_limit.value())
        self.settings.setValue("aria2/large_segments", self.aria_large_segments.value())

    def reset_aria2_settings(self) -> None:
        self.aria_small_limit.setValue(1.0)
        self.aria_small_segments.setValue(1)
        self.aria_medium_segments.setValue(32)
        self.aria_large_limit.setValue(100.0)
        self.aria_large_segments.setValue(64)
        self._save_aria2_settings()
        self._log("aria2-next 配置已重置为默认值")

    def _aria2_tuning(self) -> Aria2Tuning:
        return Aria2Tuning(
            self.aria_small_limit.value(),
            self.aria_small_segments.value(),
            self.aria_medium_segments.value(),
            self.aria_large_limit.value(),
            self.aria_large_segments.value(),
        ).validated()

    def _add_speed_rule(
        self,
        start: str = "22:00",
        end: str = "08:00",
        upload_mib: float = 0.0,
        download_mib: float = 0.0,
    ) -> None:
        row = self.speed_rule_table.rowCount()
        self.speed_rule_table.insertRow(row)
        start_edit = QTimeEdit(QTime.fromString(start, "HH:mm"))
        end_edit = QTimeEdit(QTime.fromString(end, "HH:mm"))
        for editor in (start_edit, end_edit):
            editor.setDisplayFormat("HH:mm")
            editor.timeChanged.connect(self._save_transfer_policy)
        upload_edit = QDoubleSpinBox()
        download_edit = QDoubleSpinBox()
        for editor, value in ((upload_edit, upload_mib), (download_edit, download_mib)):
            editor.setRange(0, 102400)
            editor.setDecimals(2)
            editor.setValue(value)
            editor.valueChanged.connect(self._save_transfer_policy)
        self.speed_rule_table.setCellWidget(row, 0, start_edit)
        self.speed_rule_table.setCellWidget(row, 1, end_edit)
        self.speed_rule_table.setCellWidget(row, 2, upload_edit)
        self.speed_rule_table.setCellWidget(row, 3, download_edit)
        self._save_transfer_policy()

    def _remove_speed_rule(self) -> None:
        row = self.speed_rule_table.currentRow()
        if row < 0:
            return
        self.speed_rule_table.removeRow(row)
        self._save_transfer_policy()

    def _transfer_policy_from_controls(self) -> TransferPolicy:
        rules: list[SpeedRule] = []
        for row in range(self.speed_rule_table.rowCount()):
            start = self.speed_rule_table.cellWidget(row, 0)
            end = self.speed_rule_table.cellWidget(row, 1)
            upload = self.speed_rule_table.cellWidget(row, 2)
            download = self.speed_rule_table.cellWidget(row, 3)
            rules.append(SpeedRule(
                start.time().toString("HH:mm"), end.time().toString("HH:mm"),
                upload.value(), download.value(),
            ))
        return TransferPolicy(
            self.speed_limit_enabled.isChecked(),
            self.base_upload_limit.value(), self.base_download_limit.value(), rules,
        )

    def _save_transfer_policy(self, *_args) -> None:
        if self._restoring_settings:
            return
        try:
            self.transfer_policy = self._transfer_policy_from_controls()
        except ValueError as exc:
            self.speed_limit_status.setText(str(exc))
            return
        self.settings.setValue(
            "transfer/speed_policy",
            json.dumps(self.transfer_policy.to_dict(), ensure_ascii=False),
        )
        configure_upload_limit_supplier(lambda: self.transfer_policy.limits()[0])
        self._refresh_transfer_limit_status()

    def _reset_transfer_policy(self) -> None:
        restoring = self._restoring_settings
        self._restoring_settings = True
        self.speed_limit_enabled.setChecked(False)
        self.base_upload_limit.setValue(0)
        self.base_download_limit.setValue(0)
        self.speed_rule_table.setRowCount(0)
        self._restoring_settings = restoring
        self._save_transfer_policy()

    def _refresh_transfer_limit_status(self) -> None:
        if not hasattr(self, "speed_limit_status"):
            return
        upload, download = self.transfer_policy.limits()
        upload_text = format_speed(upload) if upload else self._t("不限速")
        download_text = format_speed(download) if download else self._t("不限速")
        self.speed_limit_status.setText(self._tf(
            "当前：上传 {upload} · 下载 {download}", upload=upload_text, download=download_text,
        ))

    def _render_players(self) -> None:
        restoring = self._restoring_settings
        self._restoring_settings = True
        self.player_table.setRowCount(0)
        for player in self.external_players:
            row = self.player_table.rowCount()
            self.player_table.insertRow(row)
            self.player_table.setItem(row, 0, QTableWidgetItem(player.get("name", f"播放器 {row + 1}")))
            path_item = QTableWidgetItem(player.get("path", ""))
            path_item.setToolTip(player.get("path", ""))
            self.player_table.setItem(row, 1, path_item)
        self._restoring_settings = restoring

    def _players_edited(self) -> None:
        if self._restoring_settings:
            return
        players = []
        for row in range(self.player_table.rowCount()):
            name = self.player_table.item(row, 0).text().strip() or f"播放器 {row + 1}"
            path = self.player_table.item(row, 1).text().strip()
            players.append({"name": name, "path": path})
        self.external_players = players or [{"name": "播放器 1", "path": ""}]
        self._save_players()

    def _save_players(self) -> None:
        self.settings.setValue("external_players", json.dumps(self.external_players, ensure_ascii=False))

    @staticmethod
    def _builtin_player_available() -> bool:
        return find_potplayer(POTPLAYER_DIR) is not None

    def _refresh_builtin_player_status(self) -> None:
        if not hasattr(self, "builtin_player_status"):
            return
        available = self._builtin_player_available()
        self.builtin_player_status.setText(self._t(
            "PotPlayer：已安装" if available else "PotPlayer：尚未安装"
        ))
        self.potplayer_install_button.setText(self._t(
            "重新安装 PotPlayer" if available else "下载并安装 PotPlayer"
        ))
        self.potplayer_folder_button.setEnabled(available)

    def _builtin_player_setting_changed(self, checked: bool) -> None:
        if not self._restoring_settings:
            self.settings.setValue("builtin_player_enabled", checked)
        self._refresh_builtin_player_status()

    def _open_local_media(self, path: Path) -> None:
        path = path.resolve()
        if self.builtin_player_enabled.isChecked() and self._launch_builtin_target(str(path)):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _launch_builtin_target(self, target: str) -> bool:
        executable = find_potplayer(POTPLAYER_DIR)
        if executable is None:
            return False
        started = QProcess.startDetached(
            str(executable), [target], str(executable.parent),
        )
        return started[0] if isinstance(started, tuple) else bool(started)

    def open_builtin_remote(self, entry: RemoteEntry, service: ModelScopeService, repo: Repository) -> None:
        try:
            direct_url = service.get_download_url(repo, entry.path)
            playback_url = (
                self.media_proxy.stream_url(direct_url, service.token)
                if service.token else direct_url
            )
        except Exception as exc:
            QMessageBox.warning(self, self._t("无法打开媒体"), str(exc))
            return
        if not self._launch_builtin_target(playback_url):
            QMessageBox.warning(self, self._t("无法打开媒体"), self._t("PotPlayer 尚未安装或启动失败。"))
            return
        self._log(f"PotPlayer 在线打开：{entry.path}")

    def install_potplayer_from_modelscope(self) -> None:
        if self.potplayer_install_thread and self.potplayer_install_thread.isRunning():
            QMessageBox.information(self, self._t("正在安装 PotPlayer"), self._t("请等待当前解压安装完成。"))
            return
        if self.task and self.task.isRunning():
            QMessageBox.information(
                self, self._t("传输正在进行"),
                self._t("PotPlayer 将加入下载队列，并在当前下载完成后自动开始。"),
            )
        PLAYER_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        archive = PLAYER_DOWNLOAD_DIR / "PotPlayer.7z"
        service = ModelScopeService("", require_token=False)
        repo = Repository(POTPLAYER_REPOSITORY, "dataset", "public")
        spec = DownloadSpec(
            POTPLAYER_REMOTE_PATH,
            archive,
            service.get_download_url(repo, POTPLAYER_REMOTE_PATH),
            POTPLAYER_ARCHIVE_SIZE,
            POTPLAYER_ARCHIVE_SHA256,
        )
        self.potplayer_install_archive = archive
        self.settings.setValue("player/potplayer_install_pending", True)
        self.builtin_player_status.setText(self._t("PotPlayer：等待下载"))
        self._enqueue_download_specs([spec])

    def _start_potplayer_extraction(self, archive: Path) -> None:
        if self.potplayer_install_thread and self.potplayer_install_thread.isRunning():
            return
        self.builtin_player_status.setText(self._t("PotPlayer：正在校验并解压"))
        self.potplayer_install_button.setEnabled(False)
        worker = PotPlayerInstallThread(archive, self)
        worker.completed.connect(self._potplayer_installed)
        worker.failed.connect(self._potplayer_install_failed)
        worker.finished.connect(self._potplayer_install_finished)
        worker.finished.connect(worker.deleteLater)
        self.potplayer_install_thread = worker
        worker.start()

    def _potplayer_installed(self, executable: str) -> None:
        self.settings.remove("player/potplayer_install_pending")
        self.settings.setValue("player/potplayer_executable", executable)
        self.potplayer_install_archive = None
        self.builtin_player_status.setText(self._t("PotPlayer：已安装"))
        self._log(f"PotPlayer 安装完成：{executable}")
        QMessageBox.information(self, self._t("PotPlayer 安装完成"), self._t("播放器已经可以作为默认内置播放器使用。"))

    def _potplayer_install_failed(self, error: str) -> None:
        self.builtin_player_status.setText(self._t("PotPlayer：安装失败"))
        self._log(f"PotPlayer 安装失败：{error}")
        QMessageBox.warning(self, self._t("PotPlayer 安装失败"), error)

    def _potplayer_install_finished(self) -> None:
        self.potplayer_install_thread = None
        self.potplayer_install_button.setEnabled(True)
        self._refresh_builtin_player_status()

    def open_potplayer_folder(self) -> None:
        executable = find_potplayer(POTPLAYER_DIR)
        if executable:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(executable.parent)))

    def add_external_player(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择 mpv、PotPlayer 或其他播放器", "", "播放器程序 (*.exe);;所有文件 (*)"
        )
        if not selected:
            return
        player = {"name": Path(selected).stem or f"播放器 {len(self.external_players) + 1}", "path": selected}
        if len(self.external_players) == 1 and not self.external_players[0].get("path"):
            self.external_players[0] = player
        else:
            self.external_players.append(player)
        self._render_players()
        self._save_players()

    def remove_external_player(self) -> None:
        row = self.player_table.currentRow()
        if row < 0:
            return
        self.external_players.pop(row)
        if not self.external_players:
            self.external_players = [{"name": "播放器 1", "path": ""}]
        self._render_players()
        self._save_players()

    @staticmethod
    def _local_ip() -> str:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("8.8.8.8", 80))
                return str(probe.getsockname()[0])
        except OSError:
            return "127.0.0.1"

    def _update_alist_url(self) -> None:
        host = str(self.alist_host_combo.currentData())
        shown_host = "127.0.0.1" if host == "127.0.0.1" else self._local_ip()
        self.alist_url_label.setText(self._tf(
            "WebDAV 地址：{url}", url=f"http://{shown_host}:{self.alist_port.value()}/"
        ))

    def _save_alist_settings(self) -> None:
        if self._restoring_settings:
            return
        self.settings.setValue("alist/auto_start", self.alist_auto_start.isChecked())
        self.settings.setValue("alist/host", self.alist_host_combo.currentData())
        self.settings.setValue("alist/port", self.alist_port.value())
        self.settings.setValue("alist/username", self.alist_username.text().strip())
        try:
            self.settings.setValue("alist/password", protect(self.alist_password.text()))
        except Exception as exc:
            self._log(f"AList 密码未能安全保存：{exc}")

    def apply_alist_settings(self) -> None:
        self._save_alist_settings()
        if self.webdav:
            self.webdav.stop()
            self.webdav = None
        username = self.alist_username.text().strip()
        password = self.alist_password.text()
        if not username or not password:
            QMessageBox.warning(self, self._t("AList 配置无效"), self._t("WebDAV 用户名和密码不能为空。"))
            return
        try:
            requested_port = self.alist_port.value()
            selected_port = find_available_port(str(self.alist_host_combo.currentData()), requested_port)
            if selected_port != requested_port:
                self.alist_port.setValue(selected_port)
                self._show_top_notice(f"端口 {requested_port} 已被占用，已切换到 {selected_port}")
            gateway_service = None
            if self.account_services:
                gateway_service = MultiAccountService(self.account_services, self.account_repositories)
            self.webdav = ModelScopeWebDAV(
                lambda service=gateway_service: service,
                str(self.alist_host_combo.currentData()),
                selected_port,
                username,
                password,
                self.public_pool_store.repositories,
                self.folder_index,
            )
            self.webdav.start()
            with socket.create_connection(("127.0.0.1", selected_port), timeout=2):
                pass
        except Exception as exc:
            self.webdav = None
            self.alist_status.setText(self._t("启动失败"))
            self._refresh_tray_status()
            QMessageBox.warning(self, self._t("WebDAV 启动失败"), str(exc))
            return
        bind_host = str(self.alist_host_combo.currentData())
        self.alist_status.setText(self._tf(
            "运行中 · 正在监听 {host}:{port}", host=bind_host, port=self.alist_port.value()
        ))
        self._log(self._t("AList WebDAV 网关已启动"))
        self._refresh_tray_status()

    def _show_top_notice(self, message: str) -> None:
        notice = getattr(self, "_top_notice", None)
        if notice is None:
            notice = QLabel(self)
            notice.setObjectName("pathPill")
            notice.setStyleSheet("font-weight: 600;")
            notice.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._top_notice = notice
        notice.setText(message)
        notice.adjustSize()
        notice.setFixedWidth(max(300, notice.width() + 28))
        notice.move(max(8, (self.width() - notice.width()) // 2), 8)
        notice.show()
        notice.raise_()
        QTimer.singleShot(4200, notice.hide)

    def stop_alist(self) -> None:
        if self.webdav:
            self.webdav.stop()
            self.webdav = None
        self.alist_status.setText(self._t("未启动"))
        self._refresh_tray_status()

    def _start_folder_indexing(self, force: bool = False) -> None:
        if self.index_task and self.index_task.isRunning():
            if force:
                self._index_refresh_pending = True
            return
        jobs: list[tuple[ModelScopeService, Repository, bool, str]] = []
        job_keys: set[tuple[str, str, str, bool]] = set()
        for account_id, repos in self.account_repositories.items():
            token = self.session_tokens.get(account_id, "")
            if not token:
                continue
            private_service = ModelScopeService(token)
            current_service = self.account_services.get(account_id)
            if current_service:
                private_service.user = current_service.user
            for repo in repos:
                key = (account_id, repo.repo_type, repo.repo_id, False)
                if force or key in self.dirty_repositories:
                    jobs.append((private_service, repo, False, account_id))
                    job_keys.add(key)
        public_repos = self.public_pool_store.repositories()
        if force and public_repos:
            public_service = ModelScopeService("", require_token=False)
            for repo in public_repos:
                key = ("", repo.repo_type, repo.repo_id, True)
                if force or key in self.dirty_repositories:
                    jobs.append((public_service, repo, True, PUBLIC_ACCOUNT_ID))
                    job_keys.add(key)
        if not jobs:
            return
        self._index_refresh_pending = False
        self.index_inflight_keys = job_keys
        self.dirty_repositories.difference_update(job_keys)
        worker = FolderIndexThread(self.folder_index, self.account_store, jobs, self)
        worker.repository_indexed.connect(self._folder_indexed)
        worker.completed.connect(self._folder_index_completed)
        worker.finished.connect(lambda: self._folder_index_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.index_task = worker
        self.update_index_button.setEnabled(False)
        self._log(f"正在更新文件夹大小索引，共 {len(jobs)} 个仓库")
        worker.start()

    def _folder_index_completed(self, ok: int, failed: int) -> None:
        if failed:
            self.dirty_repositories.update(self.index_inflight_keys)
        self._log(f"文件夹大小索引已更新：{ok} 个成功，{failed} 个失败")

    def _folder_indexed(self, repo_id: str) -> None:
        if self.selected_repo and self.selected_repo.repo_id == repo_id:
            self._populate_remote_tree(
                self.remote_tree, self.remote_entries, self.selected_repo, self.selected_repo_public,
            )
            self._select_remote_directory(self.current_directory_path)
            self._render_remote_details()
        if self.search_repo and self.search_repo.repo_id == repo_id:
            self._render_public_search_results()

    def _folder_index_finished(self, worker: FolderIndexThread) -> None:
        if self.index_task is worker:
            self.index_task = None
        self.index_inflight_keys.clear()
        self.update_index_button.setEnabled(True)
        if self._index_refresh_pending:
            QTimer.singleShot(0, lambda: self._start_folder_indexing(True))

    def update_all_indexes(self) -> None:
        self._start_folder_indexing(True)

    def _mark_repository_dirty(self, account_id: str, repo: Repository, public: bool = False) -> None:
        self.dirty_repositories.add((account_id, repo.repo_type, repo.repo_id, public))
        self.index_idle_timer.start()

    def _run_idle_index_refresh(self) -> None:
        if not self.dirty_repositories:
            return
        if (self.task and self.task.isRunning()) or (self.backup_thread and self.backup_thread.isRunning()):
            self.index_idle_timer.start()
            return
        if self.page_stack.currentWidget() is self.resource_page and self.selected_repo and self.active_account_id:
            key = (self.active_account_id, self.selected_repo.repo_type, self.selected_repo.repo_id, False)
            if key in self.dirty_repositories:
                self.load_remote_files()
                return
        self._start_folder_indexing(False)

    def _background_index_tick(self) -> None:
        if self.isHidden() or not self.isActiveWindow():
            self._start_folder_indexing(False)

    def _prompt_for_settings(self) -> None:
        answer = QMessageBox.question(
            self,
            self._t("需要访问令牌"),
            self._t("尚未设置并验证 ModelScope 访问令牌。是否转到设置？"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._navigate(2)
            self.token_edit.setFocus()

    def change_download_path(self) -> None:
        current = self.download_path_edit.text().strip() or str(Path.home() / "Downloads")
        selected = QFileDialog.getExistingDirectory(self, "选择默认下载路径", current)
        if selected:
            self.download_path_edit.setText(selected)
            self.settings.setValue("download_path", selected)

    def _log(self, message: str) -> None:
        translated = self._t(message)
        self.log.append(translated)
        if hasattr(self, "status_bar"):
            self.status_bar.showMessage(translated, 5000)

    def _busy(self, value: bool, message: str = "") -> None:
        self.connect_button.setEnabled(not value)
        self.refresh_repos_button.setEnabled(not value)
        self.refresh_files_button.setEnabled(not value and self.selected_repo is not None)
        if value:
            self.repo_heading.setText(self._t(message or "正在处理…"))

    def _run_task(self, action: Callable[[], Any], success: Callable[[Any], None], label: str) -> None:
        self._busy(True, label)
        worker = TaskThread(action, self)
        worker.succeeded.connect(success)
        worker.failed.connect(self._task_failed)
        worker.finished.connect(lambda: self._task_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.task = worker
        worker.start()

    def _task_finished(self, worker: QThread) -> None:
        if self.task is worker:
            self.task = None
        self._busy(False)
        self._update_upload_enabled()
        self._update_download_enabled()

    def _task_failed(self, error: str) -> None:
        self.repo_heading.setText(self._t("操作失败"))
        if self.service is None:
            self.account_label.setText(self._t("令牌验证失败"))
            self.account_label.setObjectName("error")
            self.account_label.style().unpolish(self.account_label)
            self.account_label.style().polish(self.account_label)
        self._log(f"失败：{error}")
        QMessageBox.warning(self, self._t("操作失败"), error)

    def connect_account(self) -> None:
        token = self.token_edit.text().strip()
        if not token:
            QMessageBox.information(self, self._t("需要令牌"), self._t("请输入 ModelScope Access Token。"))
            return
        self.account_label.setText(self._t("正在验证令牌…"))
        account_id = self._selected_account_id() or ""
        label = self.account_name_edit.text().strip()
        remember = self.remember_token.isChecked()

        def action():
            service = ModelScopeService(token)
            return service, service.verify(), account_id, label, token, remember

        self._run_task(action, self._connected, "正在验证令牌…")

    def _connected(self, result: tuple[ModelScopeService, str, str, str, str, bool]) -> None:
        service, username, account_id, label, token, remember = result
        account = next((item for item in self.accounts if item.account_id == account_id), None)
        if account is None:
            account = AccountRecord(account_id, label or username, username, token, remember, True, "connected")
            self.accounts.append(account)
        else:
            account.label = label or account.label or username
            account.username = username
            account.token = token
            account.remember = remember
            account.status = "connected"
        self.account_store.save(account)
        self.session_tokens[account.account_id] = token
        self.account_services[account.account_id] = service
        self.active_account_id = account.account_id
        self.active_account_kind = "token"
        self.service = service
        self._render_accounts()
        for row in range(self.account_table.rowCount()):
            if self.account_table.item(row, 0).data(Qt.ItemDataRole.UserRole) == account.account_id:
                self.account_table.selectRow(row)
                break
        self.account_label.setText(self._tf("已连接：{username}", username=username))
        self.account_label.setObjectName("success")
        self.account_label.style().unpolish(self.account_label)
        self.account_label.style().polish(self.account_label)
        self._log(f"令牌验证成功，账户：{username}")
        self.refresh_repos_button.setEnabled(True)
        if self.webdav or self.alist_auto_start.isChecked():
            self.apply_alist_settings()
        self._navigate(0)
        QTimer.singleShot(0, self.load_repositories)

    def load_repositories(self) -> None:
        account_tokens = {
            account.account_id: self.session_tokens.get(account.account_id, account.token)
            for account in self.accounts
            if account.enabled and self.session_tokens.get(account.account_id, account.token)
        }
        web_sessions = {
            self._web_account_key(account.account_id): self.account_store.load_web_session(account.account_id)
            for account in self.web_accounts
            if self.account_store.load_web_session(account.account_id)
        }
        if not account_tokens and not web_sessions:
            self._prompt_for_settings()
            return

        def action():
            successes = []
            failures = []
            for account_id, token in account_tokens.items():
                try:
                    service = ModelScopeService(token)
                    username = service.verify()
                    repos = service.list_repositories()
                    successes.append((account_id, service, username, repos))
                except Exception as exc:
                    failures.append((account_id, str(exc)))
            for account_key, session in web_sessions.items():
                try:
                    service = ModelScopeWebService(session)
                    username = service.verify()
                    repos = service.list_repositories()
                    successes.append((account_key, service, username, repos))
                except Exception as exc:
                    failures.append((account_key, str(exc)))
            return successes, failures

        self._run_task(action, self._repositories_loaded, "正在读取所有账户仓库…")

    def _repositories_loaded(self, result) -> None:
        successes, failures = result
        self.account_services.clear()
        self.account_repositories.clear()
        total = 0
        for account_id, service, username, repos in successes:
            self.account_services[account_id] = service
            self.account_repositories[account_id] = repos
            total += len(repos)
            account = next((item for item in self.accounts if item.account_id == account_id), None)
            if account:
                account.username = username
                account.status = "connected"
                account.token = self.session_tokens.get(account_id, account.token)
                self.account_store.save(account)
            elif account_id.startswith("web:"):
                raw_id = account_id.removeprefix("web:")
                web_account = next((item for item in self.web_accounts if item.account_id == raw_id), None)
                if web_account:
                    web_account.username = username
                    web_account.status = "connected"
                    self.account_store.save_web_account(web_account)
            self.account_store.cache_repositories(account_id, repos)
        for account_id, error in failures:
            account = next((item for item in self.accounts if item.account_id == account_id), None)
            if account:
                account.status = "failed"
            web_account = None
            if account_id.startswith("web:"):
                raw_id = account_id.removeprefix("web:")
                web_account = next((item for item in self.web_accounts if item.account_id == raw_id), None)
                if web_account:
                    web_account.status = "failed"
            self._log(f"账户 {(account or web_account).label if (account or web_account) else account_id} 验证失败：{error}")
        self.repositories = [repo for repos in self.account_repositories.values() for repo in repos]
        if self.active_account_id not in self.account_services and successes:
            self.active_account_id = successes[0][0]
        self.active_account_kind = (
            "web" if str(self.active_account_id or "").startswith("web:")
            else "token" if self.active_account_id else None
        )
        self.service = self.account_services.get(self.active_account_id) if self.active_account_id else None
        self._render_accounts()
        self._render_web_accounts()
        self._render_repositories()
        self._render_backup_account_options()
        self._render_backup_jobs()
        self._render_image_account_options()
        self.account_label.setText(self._tf("已连接 · {accounts} 个账户 · {count} 个仓库", accounts=len(successes), count=total))
        self.account_label.setObjectName("success")
        self.account_label.style().unpolish(self.account_label)
        self.account_label.style().polish(self.account_label)
        self.repo_heading.setText(self._tf("已读取 {count} 个仓库，请选择一个仓库", count=total))
        self._log(f"已读取 {len(successes)} 个账户、{total} 个模型/数据集仓库")
        self._start_folder_indexing(True)
        if self.webdav or self.alist_auto_start.isChecked():
            self.apply_alist_settings()

    def _render_repositories(self) -> None:
        if not hasattr(self, "repo_list"):
            return
        selected_type = self.type_combo.currentData()
        self.repo_list.clear()
        labels = {"model": self._t("模型"), "dataset": self._t("数据集")}
        public_repos = self.public_pool_store.repositories()
        public_node = QTreeWidgetItem(["Public"])
        public_node.setData(0, Qt.ItemDataRole.UserRole, ("public", ""))
        public_node.setExpanded(True)
        self.repo_list.addTopLevelItem(public_node)
        for repo_type in ("model", "dataset"):
            if selected_type != "all" and repo_type != selected_type:
                continue
            matching = [repo for repo in public_repos if repo.repo_type == repo_type]
            if not matching:
                continue
            group = QTreeWidgetItem([labels[repo_type]])
            group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            group.setExpanded(True)
            public_node.addChild(group)
            for repo in matching:
                child = QTreeWidgetItem([repo.repo_id])
                child.setData(0, Qt.ItemDataRole.UserRole, ("public", repo))
                child.setToolTip(0, f"{repo.repo_type} · public · 只读")
                group.addChild(child)

        separator = QTreeWidgetItem(["────────────────────────"])
        separator.setFlags(separator.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.repo_list.addTopLevelItem(separator)
        token_heading = QTreeWidgetItem(["Token 登录账户（删除/移动/重命名不可用）"])
        token_heading.setFlags(token_heading.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        token_heading.setToolTip(0, "请使用在线登录列表执行")
        self.repo_list.addTopLevelItem(token_heading)
        for account in self.accounts:
            repos = self.account_repositories.get(account.account_id, [])
            account_node = QTreeWidgetItem([account.label or account.username])
            account_node.setData(0, Qt.ItemDataRole.UserRole, ("account", account.account_id))
            account_node.setExpanded(True)
            self.repo_list.addTopLevelItem(account_node)
            for repo_type in ("model", "dataset"):
                if selected_type != "all" and repo_type != selected_type:
                    continue
                matching = [repo for repo in repos if repo.repo_type == repo_type]
                group = QTreeWidgetItem([labels[repo_type]])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                group.setExpanded(True)
                account_node.addChild(group)
                for repo in matching:
                    child = QTreeWidgetItem([repo.repo_id])
                    child.setData(0, Qt.ItemDataRole.UserRole, (account.account_id, repo))
                    child.setToolTip(0, f"{repo.repo_type} · {repo.visibility}")
                    group.addChild(child)

        separator = QTreeWidgetItem(["────────────────────────"])
        separator.setFlags(separator.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.repo_list.addTopLevelItem(separator)
        web_heading = QTreeWidgetItem(["网页登录账户（删除/移动/重命名支持）"])
        web_heading.setFlags(web_heading.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        self.repo_list.addTopLevelItem(web_heading)
        for account in self.web_accounts:
            account_key = self._web_account_key(account.account_id)
            repos = self.account_repositories.get(account_key, [])
            account_node = QTreeWidgetItem([account.label or account.username])
            account_node.setData(0, Qt.ItemDataRole.UserRole, ("web-account", account.account_id))
            account_node.setExpanded(True)
            self.repo_list.addTopLevelItem(account_node)
            for repo_type in ("model", "dataset"):
                if selected_type != "all" and repo_type != selected_type:
                    continue
                matching = [repo for repo in repos if repo.repo_type == repo_type]
                group = QTreeWidgetItem([labels[repo_type]])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                group.setExpanded(True)
                account_node.addChild(group)
                for repo in matching:
                    child = QTreeWidgetItem([repo.repo_id])
                    child.setData(0, Qt.ItemDataRole.UserRole, (account_key, repo))
                    child.setToolTip(0, f"{repo.repo_type} · {repo.visibility} · 网页登录可写")
                    group.addChild(child)

    def _repo_selected(self) -> None:
        items = self.repo_list.selectedItems()
        if not items:
            return
        selected = items[0].data(0, Qt.ItemDataRole.UserRole)
        if isinstance(selected, RemoteEntry) and selected.is_dir:
            iterator = QTreeWidgetItemIterator(self.remote_tree)
            while iterator.value():
                candidate = iterator.value()
                entry = candidate.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(entry, RemoteEntry) and entry.path == selected.path:
                    self.remote_tree.setCurrentItem(candidate)
                    return
                iterator += 1
            return
        if not (isinstance(selected, tuple) and len(selected) == 2 and isinstance(selected[1], Repository)):
            return
        account_id, repo = selected
        public = account_id == "public"
        service = ModelScopeService("", require_token=False) if public else self.account_services.get(account_id)
        if service is None:
            return
        self.active_account_id = None if public else account_id
        self.active_account_kind = "public" if public else ("web" if str(account_id).startswith("web:") else "token")
        self.service = service
        self.selected_repo = repo
        self.selected_repo_public = public
        self.repo_heading.setText(f"{repo.repo_id} · {repo.repo_type}")
        self.directory_history.clear()
        self._set_current_directory("", remember=False)
        self.refresh_files_button.setEnabled(True)
        self.new_folder_button.setEnabled(not public)
        self._update_upload_enabled()
        self.load_remote_files()

    def _populate_repository_directories(self, entries: list[RemoteEntry]) -> None:
        """Attach the selected repository's directory navigation to the left tree only."""
        selected_items = self.repo_list.selectedItems()
        repo_item = selected_items[0] if selected_items else None
        if repo_item is None:
            return
        data = repo_item.data(0, Qt.ItemDataRole.UserRole)
        if not (isinstance(data, tuple) and len(data) == 2 and isinstance(data[1], Repository)):
            return
        while repo_item.childCount():
            repo_item.removeChild(repo_item.child(0))
        root = QTreeWidgetItem(["根目录"])
        root.setData(0, Qt.ItemDataRole.UserRole, RemoteEntry("", is_dir=True))
        repo_item.addChild(root)
        nodes = {"": root}
        for folder in sorted(repository_directories(entry.path for entry in entries), key=lambda value: (value.count("/"), value.lower())):
            parent_path, _, name = folder.rpartition("/")
            parent = nodes.get(parent_path)
            if parent is None:
                continue
            item = QTreeWidgetItem([name])
            item.setData(0, Qt.ItemDataRole.UserRole, RemoteEntry(folder, is_dir=True))
            parent.addChild(item)
            nodes[folder] = item
        repo_item.setExpanded(True)
        root.setExpanded(True)

    def _repository_context_menu(self, position) -> None:
        item = self.repo_list.itemAt(position)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if isinstance(data, RemoteEntry) and data.is_dir and self.service and self.selected_repo:
            self._show_remote_menu(
                self.repo_list, position, data, self.service, self.selected_repo, self.remote_entries,
                PUBLIC_ACCOUNT_ID if self.selected_repo_public else str(self.active_account_id or ""),
            )
            return
        if not (isinstance(data, tuple) and len(data) == 2 and isinstance(data[1], Repository)):
            return
        account_id, repo = data
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        open_action = menu.addAction("打开网页端")
        remove_action = menu.addAction("移除") if account_id == "public" else None
        menu.addSeparator()
        delete_action = menu.addAction("删除")
        move_action = menu.addAction("移动")
        rename_action = menu.addAction("重命名（区分大小写）")
        tooltip = (
            "这是只读" if account_id == "public"
            else "请在仓库内选择文件或文件夹" if str(account_id).startswith("web:")
            else "请使用在线登录列表执行"
        )
        for action in (delete_action, move_action, rename_action):
            action.setEnabled(False)
            action.setToolTip(tooltip)
        chosen = menu.exec(self.repo_list.viewport().mapToGlobal(position))
        if chosen is open_action:
            QDesktopServices.openUrl(QUrl(self._repository_web_url(repo)))
        elif remove_action is not None and chosen is remove_action:
            self._remove_public_repository(repo)

    def load_remote_files(self) -> None:
        if not self.service:
            self._prompt_for_settings()
            return
        if not self.selected_repo:
            return
        repo = self.selected_repo
        if self.selected_repo_public:
            cached = self.account_store.repository_entries(PUBLIC_ACCOUNT_ID, repo.repo_type, repo.repo_id)
            if cached:
                self._files_loaded([
                    RemoteEntry(entry.path, entry.size, entry.sha256, entry.is_dir) for entry in cached
                ])
                return
        self._run_task(lambda: self.service.list_entries(repo), self._files_loaded, "正在读取仓库目录…")

    def _files_loaded(self, entries: list[RemoteEntry], persist: bool = True) -> None:
        self.remote_entries = entries
        self.remote_direct_cache.clear()
        self._reset_thumbnail_queue(entries)
        if self.selected_repo and persist:
            self.account_store.cache_entries(
                PUBLIC_ACCOUNT_ID if self.selected_repo_public else str(self.active_account_id or ""),
                self.selected_repo,
                entries,
            )
        if self.selected_repo:
            self._refresh_tag_filter()
        paths = self._populate_remote_tree(
            self.remote_tree, entries, self.selected_repo, self.selected_repo_public,
        )
        self._populate_repository_directories(entries)
        self._select_remote_directory(self.current_directory_path)
        self._render_remote_details()
        if self.selected_repo:
            self.repo_heading.setText(self._tf("{repo} · 已读取 {count} 项", repo=self.selected_repo.repo_id, count=len(paths)))
        self._log(f"仓库目录已刷新，共 {len(paths)} 项")
        if self.pending_search_path:
            pending = self.pending_search_path
            self.pending_search_path = ""
            directories = repository_directories(entry.path for entry in entries)
            self._select_remote_directory(pending if pending in directories else pending.rpartition("/")[0])

    def _populate_remote_tree(
        self,
        tree: QTreeWidget,
        entries: list[RemoteEntry],
        repo: Repository | None = None,
        public: bool = False,
    ) -> list[str]:
        paths = [entry.path for entry in entries]
        tree.clear()
        root_size = self.folder_index.cached_folder_size(repo, "", public) if repo else None
        root = QTreeWidgetItem([
            self._t("根目录"), self._t("文件夹"),
            format_size(root_size) if root_size is not None else "--", "/",
        ])
        root.setData(0, Qt.ItemDataRole.UserRole, RemoteEntry("", is_dir=True))
        root.setExpanded(True)
        tree.addTopLevelItem(root)
        nodes: dict[str, QTreeWidgetItem] = {"": root}
        directory_paths = repository_directories(paths)
        for folder in sorted(directory_paths, key=lambda path: (path.count("/"), path.casefold())):
            parent_path, _, name = folder.rpartition("/")
            parent = nodes.get(parent_path)
            if parent is None:
                continue
            cached_size = self.folder_index.cached_folder_size(repo, folder, public) if repo else None
            item = QTreeWidgetItem([
                name, self._t("文件夹"), format_size(cached_size) if cached_size is not None else "--", folder,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, RemoteEntry(folder, is_dir=True))
            parent.addChild(item)
            nodes[folder] = item
        tree.resizeColumnToContents(1)
        return paths

    def _remote_selected(self) -> None:
        items = self.remote_tree.selectedItems()
        if not items:
            return
        entry = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, RemoteEntry):
            return
        if entry.is_dir:
            target = entry.path
        else:
            target = entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
        self._set_current_directory(target)
        self._render_remote_details()
        self._schedule_visible_thumbnails()

    def _render_remote_details(self) -> None:
        if not hasattr(self, "remote_detail_tree"):
            return
        folder = self.current_directory_path
        cached_direct = self.remote_direct_cache.get(folder)
        if cached_direct is None:
            cached_direct = self._direct_remote_entries(self.remote_entries, folder)
            self.remote_direct_cache[folder] = cached_direct
        direct = list(cached_direct)
        self.remote_detail_tree.setUpdatesEnabled(False)
        self.remote_detail_tree.clear()
        if self.resource_view_mode == "thumbnails":
            self.remote_thumbnail_list.setUpdatesEnabled(False)
            self.remote_thumbnail_list.clear()
        group_by = str(self.group_by_combo.currentData() or "") if hasattr(self, "group_by_combo") else ""
        parents: dict[str, QTreeWidgetItem] = {}
        direct.sort(key=self._detail_sort_key, reverse=self.detail_sort_order == Qt.SortOrder.DescendingOrder)
        for entry in direct:
            name = entry.path.rsplit("/", 1)[-1]
            size = self.folder_index.cached_folder_size(self.selected_repo, entry.path, self.selected_repo_public) if entry.is_dir and self.selected_repo else entry.size
            item = QTreeWidgetItem([
                name,
                self._t("文件夹") if entry.is_dir else self._t("文件"),
                format_size(size) if size is not None else "--",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            if group_by:
                key = name[:1].upper() if group_by == "name" else ("文件夹" if entry.is_dir else Path(name).suffix.lower() or "文件") if group_by == "type" else self._size_group(size)
                parent = parents.get(key)
                if parent is None:
                    parent = QTreeWidgetItem([key])
                    parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                    parent.setExpanded(True)
                    parents[key] = parent
                    self.remote_detail_tree.addTopLevelItem(parent)
                parent.addChild(item)
            else:
                self.remote_detail_tree.addTopLevelItem(item)
            if self.resource_view_mode == "thumbnails":
                thumbnail_item = QListWidgetItem(name)
                thumbnail_item.setData(Qt.ItemDataRole.UserRole, entry)
                thumbnail_item.setToolTip(f"{self._t('文件夹') if entry.is_dir else self._t('文件')} · {format_size(size) if size is not None else '--'}")
                thumbnail = self.thumbnail_paths.get(entry.path)
                if thumbnail:
                    thumbnail_item.setIcon(QIcon(thumbnail))
                else:
                    icon = QStyle.StandardPixmap.SP_DirIcon if entry.is_dir else QStyle.StandardPixmap.SP_FileIcon
                    thumbnail_item.setIcon(self.style().standardIcon(icon))
                self.remote_thumbnail_list.addItem(thumbnail_item)
        self.remote_detail_tree.setUpdatesEnabled(True)
        if self.resource_view_mode == "thumbnails":
            self.remote_thumbnail_list.setUpdatesEnabled(True)

    @staticmethod
    def _direct_remote_entries(entries: list[RemoteEntry], folder: str) -> list[RemoteEntry]:
        """Return only the immediate children of a normalized directory path."""
        folder = folder.strip("/")
        directories = repository_directories(entry.path for entry in entries)
        entries_by_path = {entry.path: entry for entry in entries}
        direct: list[RemoteEntry] = []
        known: set[str] = set()
        prefix = f"{folder}/" if folder else ""
        for entry in entries:
            if folder and not entry.path.startswith(prefix):
                continue
            relative = entry.path[len(prefix):] if folder else entry.path
            if not relative:
                continue
            name = relative.split("/", 1)[0]
            path = f"{folder}/{name}".strip("/")
            if name in known:
                continue
            known.add(name)
            source = entries_by_path.get(path)
            is_dir = path in directories or bool(source and source.is_dir)
            direct.append(source or RemoteEntry(path, is_dir=is_dir))
        return direct

    def _detail_sort_key(self, entry: RemoteEntry) -> tuple:
        name = entry.path.rsplit("/", 1)[-1]
        if self.detail_sort_column == 1:
            return (self._t("文件夹") if entry.is_dir else self._t("文件"), name.casefold())
        if self.detail_sort_column == 2:
            size = self.folder_index.cached_folder_size(self.selected_repo, entry.path, self.selected_repo_public) if entry.is_dir and self.selected_repo else entry.size
            return (size is None, size if size is not None else 0, name.casefold())
        return (name.casefold(),)

    def _change_detail_sort(self, column: int) -> None:
        self.detail_sort_order = (
            Qt.SortOrder.DescendingOrder if column == self.detail_sort_column and self.detail_sort_order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
        self.detail_sort_column = column
        self.remote_detail_tree.header().setSortIndicator(column, self.detail_sort_order)
        self._render_remote_details()

    @staticmethod
    def _size_group(size: int | None) -> str:
        if size is None:
            return "未索引"
        if size < 1024 * 1024:
            return "<1MB"
        if size <= 1024 * 1024 * 1024:
            return "1MB-1GB"
        return ">1GB"

    def _set_current_directory(self, path: str, remember: bool = True) -> None:
        target = path.strip("/")
        if remember and target != self.current_directory_path and self.selected_repo:
            if not self.directory_history or self.directory_history[-1] != self.current_directory_path:
                self.directory_history.append(self.current_directory_path)
        self.current_directory_path = target
        self.target_edit.setText(self.current_directory_path)
        self.resource_path_label.set_path(self.current_directory_path, self._t("根目录"))
        self.resource_back_button.setEnabled(bool(self.selected_repo and self.current_directory_path))
        directory = RemoteEntry(self.current_directory_path, is_dir=True)
        self.remote_detail_tree.set_drop_directory(directory)
        self.remote_thumbnail_list.set_drop_directory(directory)

    def _go_to_directory(self, path: str) -> bool:
        if not self.selected_repo:
            return False
        target = path.replace("\\", "/").strip("/")
        if target == self.current_directory_path:
            return True
        directories = repository_directories(entry.path for entry in self.remote_entries)
        directories.update(entry.path for entry in self.remote_entries if entry.is_dir)
        if target and target not in directories:
            return False
        self._set_current_directory(target)
        self._select_remote_directory(target)
        self._render_remote_details()
        self._schedule_visible_thumbnails()
        return True

    def _go_to_parent_directory(self) -> bool:
        if not self.selected_repo or not self.current_directory_path:
            return False
        parent = self.current_directory_path.rpartition("/")[0]
        return self._go_to_directory(parent)

    def _go_to_previous_directory(self) -> bool:
        if not self.selected_repo:
            return False
        directories = repository_directories(entry.path for entry in self.remote_entries)
        directories.update(entry.path for entry in self.remote_entries if entry.is_dir)
        while self.directory_history:
            target = self.directory_history.pop()
            if target == self.current_directory_path or (target and target not in directories):
                continue
            self._set_current_directory(target, remember=False)
            self._select_remote_directory(target)
            self._render_remote_details()
            self._schedule_visible_thumbnails()
            return True
        return False

    def _select_remote_directory(self, path: str) -> None:
        target = path.strip("/")
        iterator = QTreeWidgetItemIterator(self.remote_tree)
        while iterator.value():
            item = iterator.value()
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(entry, RemoteEntry) and entry.is_dir and entry.path == target:
                self.remote_tree.setCurrentItem(item)
                return
            iterator += 1
        self._set_current_directory("", remember=False)

    def _remote_thumbnail_selected(self) -> None:
        item = self.remote_thumbnail_list.currentItem()
        entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        enabled = isinstance(entry, RemoteEntry)
        self.download_selected_button.setEnabled(enabled)
        self.web_manage_button.setEnabled(
            enabled and self.selected_repo is not None and self.active_account_kind == "web"
        )

    def _open_remote_thumbnail(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(entry, RemoteEntry) and entry.is_dir:
            self._set_current_directory(entry.path)
            self._select_remote_directory(entry.path)
            self._render_remote_details()
            self._schedule_visible_thumbnails()

    def _remote_thumbnail_context_menu(self, position) -> None:
        item = self.remote_thumbnail_list.itemAt(position)
        entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(entry, RemoteEntry) and self.service and self.selected_repo:
            self._show_remote_menu(
                self.remote_thumbnail_list, position, entry, self.service, self.selected_repo,
                self.remote_entries, PUBLIC_ACCOUNT_ID if self.selected_repo_public else str(self.active_account_id or ""),
            )
        elif self.service and self.selected_repo:
            self._show_paste_menu(self.remote_thumbnail_list, position, self.service, self.selected_repo, self.current_directory_path)

    def _remote_detail_selected(self) -> None:
        items = self.remote_detail_tree.selectedItems()
        entry = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        enabled = isinstance(entry, RemoteEntry)
        self.download_selected_button.setEnabled(enabled)
        self.web_manage_button.setEnabled(
            enabled and self.selected_repo is not None and self.active_account_kind == "web"
        )

    def _open_remote_detail(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, RemoteEntry) or not entry.is_dir:
            return
        iterator = QTreeWidgetItemIterator(self.remote_tree)
        while iterator.value():
            candidate = iterator.value()
            data = candidate.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, RemoteEntry) and data.path == entry.path:
                self.remote_tree.setCurrentItem(candidate)
                candidate.setExpanded(True)
                return
            iterator += 1

    def _remote_detail_context_menu(self, position) -> None:
        item = self.remote_detail_tree.itemAt(position)
        entry = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if isinstance(entry, RemoteEntry) and self.service and self.selected_repo:
            self._show_remote_menu(
                self.remote_detail_tree, position, entry, self.service, self.selected_repo,
                self.remote_entries, PUBLIC_ACCOUNT_ID if self.selected_repo_public else str(self.active_account_id or ""),
            )
        elif self.service and self.selected_repo:
            self._show_paste_menu(self.remote_detail_tree, position, self.service, self.selected_repo, self.current_directory_path)

    def _download_selected_remote(self) -> None:
        items = self.remote_detail_tree.selectedItems()
        entry = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        if not isinstance(entry, RemoteEntry):
            item = self.remote_thumbnail_list.currentItem()
            entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(entry, RemoteEntry):
            self.add_remote_download(entry)

    def _delete_selected_remote(self) -> None:
        items = self.remote_detail_tree.selectedItems()
        entry = items[0].data(0, Qt.ItemDataRole.UserRole) if items else None
        if not isinstance(entry, RemoteEntry):
            item = self.remote_thumbnail_list.currentItem()
            entry = item.data(Qt.ItemDataRole.UserRole) if item else None
        if isinstance(entry, RemoteEntry) and self.selected_repo:
            self._delete_remote_entry(
                str(self.active_account_id or ""), self.selected_repo, entry, self.remote_entries,
            )

    def _schedule_global_search(self) -> None:
        self.resource_search_timer.start()

    def _refresh_tag_filter(self) -> None:
        if not hasattr(self, "resource_search_tag"):
            return
        current = self.resource_search_tag.currentData()
        self.resource_search_tag.blockSignals(True)
        self.resource_search_tag.clear()
        self.resource_search_tag.addItem("全部标签", "")
        for tag in self.account_store.all_tags():
            self.resource_search_tag.addItem(tag, tag)
        index = self.resource_search_tag.findData(current)
        self.resource_search_tag.setCurrentIndex(max(0, index))
        self.resource_search_tag.blockSignals(False)

    def _set_global_search_visible(self, visible: bool) -> None:
        self.remote_tree.setVisible(not visible)
        self.resource_drop_hint.setVisible(not visible)
        self.remote_detail_tree.setVisible(not visible and self.resource_view_mode == "details")
        self.remote_thumbnail_list.setVisible(not visible and self.resource_view_mode == "thumbnails")
        self.global_search_tree.setVisible(visible)
        self.global_search_label.setVisible(visible)

    def _perform_global_search(self) -> None:
        query = self.resource_search_edit.text().strip()
        file_type = str(self.resource_search_type.currentData() or "all")
        tag_name = str(self.resource_search_tag.currentData() or "")
        if not query and file_type == "all" and not tag_name:
            self.global_search_results = []
            self.global_search_tree.clear()
            self._set_global_search_visible(False)
            return
        scope = str(self.resource_search_scope.currentData() or "all")
        account_id = None
        repo_type = repo_id = None
        path_prefix = None
        if scope == "account":
            account_id = PUBLIC_ACCOUNT_ID if self.selected_repo_public else self.active_account_id
            if not account_id:
                self.global_search_results = []
                self.global_search_tree.clear()
                self.global_search_label.setText(self._t("请先选择搜索账户"))
                self._set_global_search_visible(True)
                return
        elif scope in {"repository", "directory"}:
            account_id = PUBLIC_ACCOUNT_ID if self.selected_repo_public else self.active_account_id
            if self.selected_repo:
                repo_type, repo_id = self.selected_repo.repo_type, self.selected_repo.repo_id
                if scope == "directory":
                    path_prefix = self.current_directory_path
            else:
                self.global_search_results = []
                self.global_search_tree.clear()
                self.global_search_label.setText(self._t("请先选择搜索仓库"))
                self._set_global_search_visible(True)
                return
        self.global_search_results = self.account_store.search_entries(
            query, file_type, account_id, repo_type, repo_id, path_prefix, tag_name
        )
        account_labels = {account.account_id: account.label for account in self.accounts}
        account_labels.update({self._web_account_key(account.account_id): account.label for account in self.web_accounts})
        account_labels[PUBLIC_ACCOUNT_ID] = "Public"
        type_labels = {
            "video": self._t("视频"), "image": self._t("图片"),
            "document": self._t("文档"), "archive": self._t("压缩包"),
            "other": self._t("其他"),
        }
        self.global_search_results.sort(
            key=self._global_search_sort_key,
            reverse=self.global_search_sort_order == Qt.SortOrder.DescendingOrder,
        )
        self.global_search_tree.clear()
        for record in self.global_search_results:
            item = QTreeWidgetItem([
                record.name,
                type_labels.get(record.file_type, record.file_type),
                format_size(record.size),
                f"{account_labels.get(record.account_id, record.account_id)} · {record.repo_id}",
                record.path,
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, record)
            self.global_search_tree.addTopLevelItem(item)
        self.global_search_label.setText(self._tf(
            "搜索结果：{count} 项", count=len(self.global_search_results)
        ))
        self._set_global_search_visible(True)

    def _global_search_sort_key(self, record: IndexedEntry) -> tuple:
        values = (
            record.name.casefold(),
            record.file_type.casefold(),
            record.size,
            record.repo_id.casefold(),
            record.path.casefold(),
        )
        return (values[self.global_search_sort_column], record.path.casefold())

    def _change_global_search_sort(self, column: int) -> None:
        self.global_search_sort_order = (
            Qt.SortOrder.DescendingOrder if column == self.global_search_sort_column and self.global_search_sort_order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
        self.global_search_sort_column = column
        self.global_search_tree.header().setSortIndicator(column, self.global_search_sort_order)
        self._perform_global_search()

    def _global_search_context_menu(self, position) -> None:
        item = self.global_search_tree.itemAt(position)
        record = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(record, IndexedEntry):
            return
        service = self.account_services.get(record.account_id)
        if record.account_id == PUBLIC_ACCOUNT_ID:
            service = ModelScopeService("", require_token=False)
        elif not service:
            return
        repo = next((candidate for candidate in self.account_repositories.get(record.account_id, [])
                     if candidate.repo_type == record.repo_type and candidate.repo_id == record.repo_id), None)
        if repo is None:
            repo = Repository(record.repo_id, record.repo_type)
        entry = RemoteEntry(record.path, record.size, record.sha256, record.is_dir)
        indexed_entries = self.account_store.repository_entries(record.account_id, repo.repo_type, repo.repo_id)
        entries = [
            RemoteEntry(value.path, value.size, value.sha256, value.is_dir) for value in indexed_entries
        ] or [entry]
        self._show_remote_menu(self.global_search_tree, position, entry, service, repo, entries, record.account_id)

    def _open_global_search_result(self, item: QTreeWidgetItem, column: int = 0) -> None:
        record = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(record, IndexedEntry):
            return
        service = self.account_services.get(record.account_id)
        public = record.account_id == PUBLIC_ACCOUNT_ID
        if public:
            service = ModelScopeService("", require_token=False)
        elif not service:
            return
        repo = next((candidate for candidate in ([] if public else self.account_repositories.get(record.account_id, []))
                     if candidate.repo_type == record.repo_type and candidate.repo_id == record.repo_id), None)
        if repo is None:
            repo = Repository(record.repo_id, record.repo_type)
        self.active_account_id = None if public else record.account_id
        self.active_account_kind = "public" if public else ("web" if record.account_id.startswith("web:") else "token")
        self.service = service
        self.selected_repo = repo
        self.selected_repo_public = public
        self.pending_search_path = record.path
        self.resource_search_edit.clear()
        self.resource_search_type.setCurrentIndex(0)
        self._set_global_search_visible(False)
        iterator = QTreeWidgetItemIterator(self.repo_list)
        self.repo_list.blockSignals(True)
        while iterator.value():
            candidate = iterator.value()
            data = candidate.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2 and data[0] == ("public" if public else record.account_id) and data[1] == repo:
                self.repo_list.setCurrentItem(candidate)
                break
            iterator += 1
        self.repo_list.blockSignals(False)
        self.repo_heading.setText(f"{repo.repo_id} · {repo.repo_type}")
        self.load_remote_files()

    @staticmethod
    def _repository_web_url(repo: Repository, folder_path: str = "") -> str:
        kind = "datasets" if repo.repo_type == "dataset" else "models"
        base = f"https://www.modelscope.cn/{kind}/{repo.repo_id}/files"
        return f"{base}?path={quote(folder_path, safe='/')}" if folder_path else base

    def _copy_remote_link(
        self, entry: RemoteEntry, service: ModelScopeService, repo: Repository
    ) -> None:
        try:
            if entry.is_dir:
                link = self._repository_web_url(repo, entry.path)
            else:
                public = repository_is_public(repo, service.token)
                link = repository_file_url(repo, entry.path, public)
        except Exception as exc:
            QMessageBox.warning(self, self._t("复制链接失败"), str(exc))
            return
        QApplication.clipboard().setText(link)
        message = "文件夹链接已复制" if entry.is_dir else "文件直链已复制"
        self._log(f"{message}：{entry.path or '/'}")
        if not entry.is_dir and not public:
            QMessageBox.information(
                self,
                "私有资源 API 直链",
                "直链将以 API 形式复制。链接中不包含 Token，但访问者仍需拥有该私有仓库的权限。",
            )
            self.repo_heading.setText("私有资源 API 直链已复制")
        else:
            self.repo_heading.setText(self._t(message))

    def _remote_context_menu(self, position) -> None:
        item = self.remote_tree.itemAt(position)
        if item is None or not self.service or not self.selected_repo:
            return
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, RemoteEntry):
            return
        self._show_remote_menu(
            self.remote_tree, position, entry, self.service, self.selected_repo, self.remote_entries,
            PUBLIC_ACCOUNT_ID if self.selected_repo_public else str(self.active_account_id or ""),
        )

    def _search_context_menu(self, position) -> None:
        item = self.search_remote_tree.itemAt(position)
        if item is None or not self.search_service or not self.search_repo:
            return
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(entry, RemoteEntry):
            self._show_remote_menu(
                self.search_remote_tree,
                position,
                entry,
                self.search_service,
                self.search_repo,
                self.search_entries,
                PUBLIC_ACCOUNT_ID,
            )

    def _show_remote_menu(
        self,
        tree: QTreeWidget,
        position,
        entry: RemoteEntry,
        service: ModelScopeService,
        repo: Repository,
        entries: list[RemoteEntry],
        tag_account_id: str,
    ) -> None:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        link_action = menu.addAction(self._t("复制链接" if entry.is_dir else "复制直链"))
        copy_action = menu.addAction("复制")
        paste_action = menu.addAction("粘贴") if self.copy_source and not self.selected_repo_public else None
        paste_move_action = menu.addAction("粘贴移动") if self.move_source and not self.selected_repo_public else None
        download_action = menu.addAction(self._t("添加到下载队列"))
        builtin_action = None
        player_actions: dict[QAction, dict[str, str]] = {}
        if not entry.is_dir and Path(entry.path).suffix.lower() in MEDIA_EXTENSIONS | IMAGE_EXTENSIONS:
            if self.builtin_player_enabled.isChecked():
                builtin_action = menu.addAction(self._t("使用本地 PotPlayer 打开"))
            player_menu = menu.addMenu(self._t("使用第三方播放器打开"))
            for index, player in enumerate(self.external_players):
                name = player.get("name") or f"播放器 {index + 1}"
                action = player_menu.addAction(name)
                player_actions[action] = player
        tag_menu = menu.addMenu("标签")
        assigned_tags = set(self.account_store.tags_for_entry(tag_account_id, repo.repo_type, repo.repo_id, entry.path))
        tag_actions: dict[QAction, str] = {}
        for tag in self.account_store.all_tags():
            action = tag_menu.addAction(tag)
            action.setCheckable(True)
            action.setChecked(tag in assigned_tags)
            tag_actions[action] = tag
        new_tag_action = tag_menu.addAction("新建标签")
        menu.addSeparator()
        delete_action = menu.addAction(self._t("删除"))
        move_action = menu.addAction(self._t("移动"))
        rename_action = menu.addAction(self._t("重命名（区分大小写）"))
        web_writable = tag_account_id.startswith("web:")
        if not web_writable:
            tooltip = "这是只读" if tag_account_id == PUBLIC_ACCOUNT_ID else "请使用在线登录列表执行"
            for action in (delete_action, move_action, rename_action):
                action.setEnabled(False)
                action.setToolTip(tooltip)
        chosen = menu.exec(tree.viewport().mapToGlobal(position))
        if chosen is link_action:
            self._copy_remote_link(entry, service, repo)
        elif chosen is copy_action:
            self.copy_source = (service, repo, list(entries), entry)
            self._log(f"已复制：{entry.path or '/'}；请选择可写目录后右键粘贴")
        elif paste_action is not None and chosen is paste_action:
            destination = entry.path if entry.is_dir else entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
            self._paste_remote_copy(service, repo, destination)
        elif paste_move_action is not None and chosen is paste_move_action:
            destination = entry.path if entry.is_dir else entry.path.rsplit("/", 1)[0] if "/" in entry.path else ""
            self._paste_remote_move(tag_account_id, service, repo, destination)
        elif chosen is download_action:
            self.add_remote_download(entry, service, repo, entries)
        elif builtin_action is not None and chosen is builtin_action:
            self.open_builtin_remote(entry, service, repo)
        elif chosen in player_actions:
            self.open_external_player(entry, service, repo, player_actions[chosen])
        elif chosen in tag_actions:
            tag = tag_actions[chosen]
            tags = assigned_tags ^ {tag}
            self._save_entry_tags(tag_account_id, repo, entry.path, list(tags))
        elif chosen is new_tag_action:
            name, accepted = QInputDialog.getText(self, "新建标签", "标签名称：")
            if accepted and name.strip():
                self._save_entry_tags(tag_account_id, repo, entry.path, list(assigned_tags) + [name])
        elif chosen is delete_action:
            self._delete_remote_entry(tag_account_id, repo, entry, entries)
        elif chosen is move_action:
            self.move_source = (tag_account_id, service, repo, list(entries), entry)
            self._log(f"已选择移动：{entry.path}；请选择目标目录后右键粘贴移动")
        elif chosen is rename_action:
            self._rename_remote_entry(tag_account_id, service, repo, entry, entries)

    def _show_paste_menu(
        self, view: QWidget, position, service: ModelScopeService, repo: Repository, destination: str,
    ) -> None:
        menu = QMenu(self)
        menu.setToolTipsVisible(True)
        paste_action = menu.addAction("粘贴")
        paste_action.setEnabled(bool(self.copy_source) and not self.selected_repo_public)
        paste_move_action = menu.addAction("粘贴移动")
        paste_move_action.setEnabled(bool(self.move_source) and not self.selected_repo_public)
        chosen = menu.exec(view.viewport().mapToGlobal(position))
        if chosen is paste_action and paste_action.isEnabled():
            self._paste_remote_copy(service, repo, destination)
        elif chosen is paste_move_action and paste_move_action.isEnabled():
            account_key = str(self.active_account_id or "")
            self._paste_remote_move(account_key, service, repo, destination)

    @staticmethod
    def _entry_file_paths(entry: RemoteEntry, entries: list[RemoteEntry]) -> list[str]:
        if not entry.is_dir:
            return [entry.path]
        prefix = entry.path.strip("/")
        return sorted(
            [value.path for value in entries if not value.is_dir and value.path.startswith(prefix + "/")],
            reverse=True,
        )

    def _web_session_for_key(self, account_key: str) -> ModelScopeWebSession | None:
        if not account_key.startswith("web:"):
            return None
        return self.account_store.load_web_session(account_key.removeprefix("web:"))

    def _delete_remote_entry(
        self, account_key: str, repo: Repository, entry: RemoteEntry, entries: list[RemoteEntry],
    ) -> None:
        session = self._web_session_for_key(account_key)
        if session is None:
            QMessageBox.information(self, "需要在线登录", "转到设置页面添加账号。")
            return
        paths = self._entry_file_paths(entry, entries)
        if not paths:
            QMessageBox.information(self, "删除", "文件夹中没有可删除的文件。")
            return
        answer = QMessageBox.warning(
            self,
            "确认删除",
            f"此操作不可逆，确定删除“{entry.path}”？" + (f"\n将递归删除 {len(paths)} 个文件。" if entry.is_dir else ""),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.delete_task = DeleteThread(session, repo, paths, self)
        self.delete_task.completed.connect(
            lambda result, key=account_key, value=repo, path=entry.path: self._remote_delete_completed(
                key, value, result, path,
            )
        )
        self.delete_task.failed.connect(lambda error: QMessageBox.warning(self, "删除失败", error))
        self.delete_task.finished.connect(self.delete_task.deleteLater)
        self.delete_task.start()
        self._log(f"开始删除：{repo.repo_id}/{entry.path} · {len(paths)} 个文件")

    def _cached_remote_entries(self, account_key: str, repo: Repository) -> list[RemoteEntry]:
        if self.selected_repo == repo and (
            (self.selected_repo_public and account_key == PUBLIC_ACCOUNT_ID)
            or (not self.selected_repo_public and account_key == self.active_account_id)
        ):
            return list(self.remote_entries)
        return [
            RemoteEntry(value.path, value.size, value.sha256, value.is_dir)
            for value in self.account_store.repository_entries(account_key, repo.repo_type, repo.repo_id)
        ]

    def _apply_local_remote_changes(
        self,
        account_key: str,
        repo: Repository,
        removed: Iterable[str] = (),
        removed_prefixes: Iterable[str] = (),
        additions: Iterable[RemoteEntry] = (),
    ) -> None:
        removed_set = set(removed)
        prefixes = list(dict.fromkeys(path.strip("/") for path in removed_prefixes if path.strip("/")))
        before = self._cached_remote_entries(account_key, repo)
        values = {
            entry.path: entry
            for entry in before
            if entry.path not in removed_set
            and not any(entry.path == prefix or entry.path.startswith(prefix + "/") for prefix in prefixes)
        }
        additions = list(additions)
        for entry in additions:
            values[entry.path] = entry
        updated = sorted(values.values(), key=lambda value: value.path)
        if additions:
            self.account_store.cache_entries(account_key, repo, updated)
            self.folder_index.update_repository(repo, updated, False)
        else:
            removed_entries = [entry for entry in before if entry.path not in values]
            self.account_store.remove_entry_prefixes(
                account_key, repo.repo_type, repo.repo_id, [*removed_set, *prefixes],
            )
            self.folder_index.remove_entries(repo, removed_entries, prefixes, False)
        current = self.selected_repo == repo and not self.selected_repo_public and account_key == self.active_account_id
        if current:
            self._files_loaded(updated, persist=False)

    def _remote_delete_completed(
        self, account_key: str, repo: Repository, result: dict, selected_path: str = "",
    ) -> None:
        self.delete_task = None
        deleted = list(result.get("deleted", []))
        failures = dict(result.get("failures", {}))
        remove_prefixes = [selected_path] if selected_path and not failures else []
        self._apply_local_remote_changes(
            account_key, repo, removed=deleted, removed_prefixes=remove_prefixes,
        )
        self._log(f"删除完成：{len(deleted)} 个成功，{len(failures)} 个失败")
        if failures:
            QMessageBox.warning(self, "删除完成", f"已删除 {len(deleted)} 个文件，{len(failures)} 个失败。")
        else:
            QMessageBox.information(self, "删除完成", f"已删除 {len(deleted)} 个文件，当前目录已刷新。")

    def _confirm_relocate_threshold(self, entry: RemoteEntry, entries: list[RemoteEntry], verb: str) -> bool:
        if entry.is_dir:
            total_size = sum(
                value.size for value in entries
                if not value.is_dir and value.path.startswith(entry.path.strip("/") + "/")
            )
        else:
            total_size = entry.size
        if total_size <= self._copy_threshold_bytes():
            return True
        answer = QMessageBox.question(
            self,
            f"{verb}较大资源",
            f"当前文件/文件夹大小为 {format_size(total_size)}，超过下载阈值。是否先下载到临时目录再执行{verb}？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    @staticmethod
    def _relocate_mappings(entry: RemoteEntry, entries: list[RemoteEntry], target_path: str) -> dict[str, str]:
        if not entry.is_dir:
            return {entry.path: target_path}
        prefix = entry.path.strip("/")
        return {
            value.path: normalize_remote_path(target_path, value.path[len(prefix):].strip("/"))
            for value in entries
            if not value.is_dir and value.path.startswith(prefix + "/")
        }

    def _paste_remote_move(
        self, destination_key: str, destination_service: ModelScopeService,
        destination_repo: Repository, destination_folder: str,
    ) -> None:
        if not self.move_source:
            return
        source_key, source_service, source_repo, source_entries, selected = self.move_source
        if self._web_session_for_key(source_key) is None:
            QMessageBox.information(self, "需要在线登录", "转到设置页面添加账号。")
            return
        upload_service = self._token_service_for_repo(destination_repo)
        if upload_service is None:
            QMessageBox.information(
                self, "需要 Token 账户", "请先添加可访问目标仓库的 Token 账户；上传不会使用网页登录接口。",
            )
            return
        target = normalize_remote_path(destination_folder, Path(selected.path).name)
        if source_repo == destination_repo and source_key == destination_key:
            source_path = selected.path.strip("/")
            if target == source_path:
                QMessageBox.information(self, "移动", "目标路径与原路径相同。")
                return
            if selected.is_dir and (destination_folder == source_path or destination_folder.startswith(source_path + "/")):
                QMessageBox.warning(self, "移动", "不能把文件夹移动到其自身或子目录中。")
                return
        if not self._confirm_relocate_threshold(selected, source_entries, "移动"):
            return
        mappings = self._relocate_mappings(selected, source_entries, target)
        self._start_relocate(
            source_key, source_service, source_repo, destination_key,
            upload_service, destination_repo, source_entries, mappings,
        )

    def _rename_remote_entry(
        self, account_key: str, service: ModelScopeService, repo: Repository,
        entry: RemoteEntry, entries: list[RemoteEntry],
    ) -> None:
        current_name = Path(entry.path).name
        new_name, accepted = QInputDialog.getText(
            self, "重命名（区分大小写）", "新名称（区分大小写）：", text=current_name,
        )
        new_name = new_name.strip()
        if not accepted:
            return
        if not new_name or new_name in {".", ".."} or "/" in new_name or "\\" in new_name:
            QMessageBox.warning(self, "重命名", "请输入不包含路径分隔符的有效名称。")
            return
        if new_name == current_name:
            QMessageBox.information(self, "重命名", "新名称与原名称相同。")
            return
        if not self._confirm_relocate_threshold(entry, entries, "重命名"):
            return
        upload_service = self._token_service_for_repo(repo)
        if upload_service is None:
            QMessageBox.information(
                self, "需要 Token 账户", "请先添加可访问该仓库的 Token 账户；上传不会使用网页登录接口。",
            )
            return
        parent = entry.path.rpartition("/")[0]
        target = normalize_remote_path(parent, new_name)
        mappings = self._relocate_mappings(entry, entries, target)
        self._start_relocate(
            account_key, service, repo, account_key, upload_service, repo, entries, mappings,
        )

    def _start_relocate(
        self,
        source_key: str,
        source_service: ModelScopeService,
        source_repo: Repository,
        destination_key: str,
        destination_service: ModelScopeService,
        destination_repo: Repository,
        source_entries: list[RemoteEntry],
        mappings: dict[str, str],
    ) -> None:
        if not mappings:
            QMessageBox.information(self, "操作", "没有可传输的文件。")
            return
        session = self._web_session_for_key(source_key)
        if session is None:
            QMessageBox.information(self, "需要在线登录", "转到设置页面添加账号。")
            return
        self.relocate_context = (source_key, source_repo, destination_key, destination_repo, source_entries)
        self.relocate_task = RelocateThread(
            source_service,
            source_repo,
            destination_service,
            destination_repo,
            mappings,
            lambda path: delete_repository_file(session, source_repo.repo_id, source_repo.repo_type, path),
            self,
        )
        self.relocate_task.completed.connect(self._relocate_completed)
        self.relocate_task.failed.connect(lambda error: QMessageBox.warning(self, "操作失败", error))
        self.relocate_task.finished.connect(self.relocate_task.deleteLater)
        self.relocate_task.start()
        self._log(f"开始下载、上传并移动：{len(mappings)} 个文件")

    def _relocate_completed(self, result: dict) -> None:
        context = self.relocate_context
        self.relocate_task = None
        self.relocate_context = None
        if context is None:
            return
        source_key, source_repo, destination_key, destination_repo, source_entries = context
        mappings = dict(result.get("mappings", {}))
        upload_failed = set(result.get("upload_failed", []))
        uploaded = set(mappings) - upload_failed
        deleted = list(result.get("deleted", []))
        source_by_path = {entry.path: entry for entry in source_entries}
        additions = [
            RemoteEntry(mappings[path], source_by_path[path].size, source_by_path[path].sha256, False)
            for path in uploaded if path in source_by_path
        ]
        self._apply_local_remote_changes(destination_key, destination_repo, additions=additions)
        if deleted:
            self._apply_local_remote_changes(source_key, source_repo, removed=deleted)
        delete_failed = dict(result.get("delete_failed", {}))
        if upload_failed:
            message = f"{len(uploaded)} 个上传成功，{len(upload_failed)} 个上传失败；源文件未删除。"
            QMessageBox.warning(self, "操作未完成", message)
        elif delete_failed:
            message = f"全部上传成功；{len(deleted)} 个源文件已删除，{len(delete_failed)} 个删除失败。"
            QMessageBox.warning(self, "操作部分完成", message)
        else:
            message = f"{len(uploaded)} 个文件上传完成并已删除原路径。"
            QMessageBox.information(self, "操作完成", message)
            self.move_source = None
        self._log(message)

    def _paste_remote_copy(self, destination_service: ModelScopeService, destination_repo: Repository, destination_folder: str) -> None:
        if not self.copy_source or self.selected_repo_public:
            return
        source_service, source_repo, source_entries, selected = self.copy_source
        upload_service = self._token_service_for_repo(destination_repo)
        if upload_service is None:
            QMessageBox.information(
                self, "需要 Token 账户", "请先添加可访问目标仓库的 Token 账户；上传不会使用网页登录接口。",
            )
            return
        if selected.is_dir:
            total_size = self.folder_index.update_folder(
                source_repo, selected.path, source_entries, repository_is_public(source_repo, source_service.token),
            )
        else:
            total_size = selected.size
        if total_size > self._copy_threshold_bytes():
            answer = QMessageBox.question(
                self, "复制较大资源",
                f"当前文件/文件夹大小为 {format_size(total_size)}，是否在后台下载以实施复制操作？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.copy_task = CopyThread(
            source_service, source_repo, source_entries, selected,
            upload_service, destination_repo, destination_folder, self,
        )
        self.copy_task.completed.connect(self._remote_copy_completed)
        self.copy_task.failed.connect(lambda error: QMessageBox.warning(self, "复制失败", error))
        self.copy_task.finished.connect(self.copy_task.deleteLater)
        self.copy_task.start()
        self._log(f"开始后台复制：{selected.path or '/'} → {destination_repo.repo_id}/{destination_folder}")

    def _remote_copy_completed(self, ok: int, failed: int) -> None:
        self.copy_task = None
        self._log(f"复制完成：{ok} 个文件成功，{failed} 个失败；临时文件已清除")
        QMessageBox.information(self, "复制完成", f"{ok} 个文件成功，{failed} 个失败。临时文件已清除。")
        self.load_remote_files()

    def _save_entry_tags(self, account_id: str, repo: Repository, path: str, tags: list[str]) -> None:
        try:
            saved = self.account_store.set_entry_tags(account_id, repo.repo_type, repo.repo_id, path, tags)
        except ValueError as exc:
            QMessageBox.warning(self, "标签", str(exc))
            return
        self._refresh_tag_filter()
        self._log(f"标签已更新：{path or '/'} · {', '.join(saved) or '无'}")

    def open_external_player(
        self,
        entry: RemoteEntry,
        service: ModelScopeService,
        repo: Repository,
        player_info: dict[str, str],
    ) -> None:
        if not repository_is_public(repo, service.token):
            QMessageBox.information(
                self,
                self._t("私有资源无法直接播放"),
                self._t("外部播放器无法安全接收 ModelScope 访问令牌。请先下载该文件，再从本地播放。"),
            )
            return
        saved = player_info.get("path", "")
        player = Path(saved) if saved else Path()
        if not saved or not player.is_file():
            selected, _ = QFileDialog.getOpenFileName(
                self,
                "选择 mpv、PotPlayer 或其他播放器",
                "",
                "播放器程序 (*.exe);;所有文件 (*)",
            )
            if not selected:
                return
            player = Path(selected)
            player_info["path"] = str(player)
            if not player_info.get("name") or player_info["name"].startswith("播放器 "):
                player_info["name"] = player.stem
            self._render_players()
            self._save_players()
        try:
            url = service.get_download_url(repo, entry.path)
            started = QProcess.startDetached(str(player), [url])
        except Exception as exc:
            QMessageBox.warning(self, self._t("无法打开播放器"), str(exc))
            return
        success = started[0] if isinstance(started, tuple) else bool(started)
        if not success:
            QMessageBox.warning(self, self._t("无法打开播放器"), self._t("播放器未能启动，请重新选择播放器程序。"))
            player_info["path"] = ""
            self._render_players()
            self._save_players()
            return
        self._log(f"已交给外部播放器：{entry.path}")

    def load_public_resource(self) -> None:
        if self.task and self.task.isRunning():
            QMessageBox.information(self, self._t("请稍候"), self._t("当前操作完成后再加载公开资源。"))
            return
        try:
            search_url = self.search_url_edit.currentText().strip()
            repo = parse_modelscope_repository_url(search_url)
        except ValueError as exc:
            QMessageBox.warning(self, self._t("链接无效"), str(exc))
            return
        self.search_heading.setText(self._tf("正在读取 {repo}…", repo=repo.repo_id))
        self.search_load_button.setEnabled(False)

        def action():
            service = ModelScopeService("", require_token=False)
            return service, repo, service.list_entries(repo), search_url

        worker = TaskThread(action, self)
        worker.succeeded.connect(self._public_resource_loaded)
        worker.failed.connect(self._public_resource_failed)
        worker.finished.connect(lambda: self._public_resource_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.task = worker
        worker.start()

    def _public_resource_loaded(self, result: tuple[ModelScopeService, Repository, list[RemoteEntry], str]) -> None:
        self.search_service, self.search_repo, self.search_entries, search_url = result
        self.public_pool_store.add(search_url, self.search_repo)
        if self.webdav:
            self.webdav.refresh_public_pools()
        self.account_store.cache_entries(PUBLIC_ACCOUNT_ID, self.search_repo, self.search_entries)
        self.folder_index.update_repository(self.search_repo, self.search_entries, True)
        self._render_public_history()
        self._render_repositories()
        self._refresh_tag_filter()
        self.search_url_edit.setEditText(search_url)
        self.public_file_search_edit.blockSignals(True)
        self.public_file_search_edit.clear()
        self.public_file_search_edit.blockSignals(False)
        self._render_public_search_results()
        count = len(self.search_entries)
        self.search_heading.setText(self._tf("{repo} · 已读取 {count} 项", repo=self.search_repo.repo_id, count=count))
        self._log(f"已读取公开资源 {self.search_repo.repo_id}，共 {count} 项")

    def _public_search_sort_key(self, entry: RemoteEntry) -> tuple:
        name = entry.path.rsplit("/", 1)[-1]
        size = self.folder_index.cached_folder_size(self.search_repo, entry.path, True) if entry.is_dir and self.search_repo else entry.size
        extension, file_type = classify_file(entry.path, entry.is_dir)
        values = (
            name.casefold(),
            (file_type, extension, name.casefold()),
            (size is None, size or 0),
            entry.path.casefold(),
        )
        return (values[self.public_search_sort_column], entry.path.casefold())

    def _change_public_search_sort(self, column: int) -> None:
        self.public_search_sort_order = (
            Qt.SortOrder.DescendingOrder
            if column == self.public_search_sort_column and self.public_search_sort_order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
        self.public_search_sort_column = column
        self.search_remote_tree.header().setSortIndicator(column, self.public_search_sort_order)
        self._sync_public_search_sort_controls()
        self._render_public_search_results()

    def _public_search_sort_combo_changed(self, _index: int = -1) -> None:
        column = int(self.public_search_sort_combo.currentData() or 0)
        if column == self.public_search_sort_column:
            return
        self.public_search_sort_column = column
        self.search_remote_tree.header().setSortIndicator(column, self.public_search_sort_order)
        self._render_public_search_results()

    def _toggle_public_search_direction(self) -> None:
        self.public_search_sort_order = (
            Qt.SortOrder.DescendingOrder
            if self.public_search_sort_order == Qt.SortOrder.AscendingOrder
            else Qt.SortOrder.AscendingOrder
        )
        self.search_remote_tree.header().setSortIndicator(
            self.public_search_sort_column, self.public_search_sort_order,
        )
        self._sync_public_search_sort_controls()
        self._render_public_search_results()

    def _sync_public_search_sort_controls(self) -> None:
        index = self.public_search_sort_combo.findData(self.public_search_sort_column)
        self.public_search_sort_combo.blockSignals(True)
        self.public_search_sort_combo.setCurrentIndex(max(0, index))
        self.public_search_sort_combo.blockSignals(False)
        self.public_search_direction_button.setText(
            "升序" if self.public_search_sort_order == Qt.SortOrder.AscendingOrder else "降序"
        )

    def _render_public_search_results(self) -> None:
        if not hasattr(self, "search_remote_tree"):
            return
        query = self.public_file_search_edit.text().strip() if hasattr(self, "public_file_search_edit") else ""
        matched = [
            entry for entry in self.search_entries
            if everything_search_match(entry.path, entry.is_dir, query)
        ]
        entries = sorted(
            matched,
            key=self._public_search_sort_key,
            reverse=self.public_search_sort_order == Qt.SortOrder.DescendingOrder,
        )
        self.search_remote_tree.setUpdatesEnabled(False)
        self.search_remote_tree.clear()
        items: list[QTreeWidgetItem] = []
        type_labels = {
            "folder": self._t("文件夹"), "video": self._t("视频"), "image": self._t("图片"),
            "document": self._t("文档"), "archive": self._t("压缩包"), "other": self._t("其他"),
        }
        for entry in entries:
            name = entry.path.rsplit("/", 1)[-1]
            size = self.folder_index.cached_folder_size(self.search_repo, entry.path, True) if entry.is_dir and self.search_repo else entry.size
            extension, file_type = classify_file(entry.path, entry.is_dir)
            kind = type_labels[file_type]
            if extension:
                kind = f"{kind} · {extension.lstrip('.').upper()}"
            item = QTreeWidgetItem([name, kind, format_size(size) if size is not None else "--", entry.path])
            item.setData(0, Qt.ItemDataRole.UserRole, entry)
            items.append(item)
        self.search_remote_tree.addTopLevelItems(items)
        self.search_remote_tree.setUpdatesEnabled(True)
        self.search_remote_tree.viewport().update()
        self.search_remote_tree.scrollToTop()
        if hasattr(self, "public_search_count_label"):
            self.public_search_count_label.setText(
                f"显示 {len(entries)} / {len(self.search_entries)} 项"
                + (" · 空格分隔的条件需全部匹配" if query else "")
            )

    def _public_resource_failed(self, error: str) -> None:
        self.search_heading.setText(self._t("公开资源加载失败"))
        self._log(f"公开资源加载失败：{error}")
        QMessageBox.warning(self, self._t("加载失败"), error)

    def _public_resource_finished(self, worker: QThread) -> None:
        if self.task is worker:
            self.task = None
        self.search_load_button.setEnabled(True)
        self._update_upload_enabled()
        self._update_download_enabled()

    def _repository_paths_dropped(self, raw_paths: list[str], directory: RemoteEntry) -> None:
        if self.selected_repo_public:
            QMessageBox.information(self, "Public", "Public 仓库为只读挂载，不能上传。")
            return
        if not self.service:
            self._prompt_for_settings()
            return
        if not self.selected_repo:
            QMessageBox.information(self, self._t("请选择仓库"), self._t("请先在左侧选择目标仓库。"))
            return
        if self.task and self.task.isRunning() and not isinstance(self.task, UploadThread):
            QMessageBox.information(self, self._t("传输进行中"), self._t("已有任务正在运行，请完成后再拖放上传。"))
            return
        if self.upload_session_repo and self.upload_session_repo != self.selected_repo:
            QMessageBox.information(self, self._t("传输进行中"), "请在当前上传队列完成后再切换目标仓库。")
            return
        self.target_edit.setText(directory.path)
        total_size = local_paths_size(raw_paths)
        added = self.add_paths(raw_paths, directory.path)
        if not added:
            return
        self.queue_tabs.setCurrentIndex(0)
        threshold = self.drop_upload_threshold_mb.value() * 1024 * 1024
        if total_size > threshold:
            monitor = QMessageBox.question(
                self,
                self._t("大文件上传"),
                self._tf(
                    "拖放内容总大小约为 {size}，已超过 {threshold} MB。是否跳转到传输列表监控？",
                    size=format_size(total_size),
                    threshold=self.drop_upload_threshold_mb.value(),
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if monitor == QMessageBox.StandardButton.Yes:
                self._navigate(1)
        self._log(f"拖放上传到 /{directory.path}")
        if not (self.task and self.task.isRunning()):
            QTimer.singleShot(0, self.start_upload)

    def add_remote_download(
        self,
        entry: RemoteEntry,
        service: ModelScopeService | None = None,
        repo: Repository | None = None,
        entries: list[RemoteEntry] | None = None,
    ) -> None:
        service = service or self.service
        repo = repo or self.selected_repo
        entries = self.remote_entries if entries is None else entries
        if not service or not repo:
            return
        destination = self.download_path_edit.text().strip()
        if not destination:
            QMessageBox.information(self, self._t("需要下载路径"), self._t("请先在设置中选择默认下载路径。"))
            self._navigate(2)
            return
        try:
            Path(destination).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, self._t("下载路径不可用"), str(exc))
            self._navigate(2)
            return
        try:
            specs = build_download_specs(
                service,
                repo,
                entries,
                entry,
                Path(destination),
            )
        except Exception as exc:
            QMessageBox.warning(self, self._t("无法添加下载"), str(exc))
            return
        if not specs:
            QMessageBox.information(self, self._t("空文件夹"), self._t("所选目录中没有可下载的文件。"))
            return
        self._enqueue_download_specs(specs)

    def _enqueue_download_specs(self, specs: list[DownloadSpec], auto_start_delay_ms: int = 0) -> int:
        known = {str(spec.local_path).lower(): index for index, spec in enumerate(self.download_specs)}
        added = 0
        for spec in specs:
            key = str(spec.local_path).lower()
            if key in known:
                row = known[key]
                state = self.download_states.get(str(self.download_specs[row].local_path), "waiting")
                if state not in {"completed", "failed", "stopped"}:
                    continue
                old_path = str(self.download_specs[row].local_path)
                self.download_specs[row] = spec
                self.download_states.pop(old_path, None)
                self.download_states[str(spec.local_path)] = "waiting"
                self.download_table.item(row, 0).setText(spec.remote_path)
                self.download_table.item(row, 1).setText(str(spec.local_path))
                self.download_table.item(row, 2).setText(self._t("等待下载"))
                added += 1
                continue
            known[key] = len(self.download_specs)
            self.download_specs.append(spec)
            self.download_states[str(spec.local_path)] = "waiting"
            row = self.download_table.rowCount()
            self.download_table.insertRow(row)
            self.download_table.setItem(row, 0, QTableWidgetItem(spec.remote_path))
            local_item = QTableWidgetItem(str(spec.local_path))
            local_item.setToolTip(str(spec.local_path))
            self.download_table.setItem(row, 1, local_item)
            self.download_table.setItem(row, 2, QTableWidgetItem("等待下载"))
            added += 1
        self._navigate(1)
        self.queue_tabs.setCurrentIndex(1)
        self._update_download_enabled()
        self._log(f"已添加 {added} 个文件到下载队列")
        if added:
            QTimer.singleShot(auto_start_delay_ms, self._auto_start_download)
        return added

    def new_folder(self) -> None:
        name, accepted = QInputDialog.getText(self, "新建文件夹", "文件夹名称：")
        if not accepted or not name.strip():
            return
        try:
            target = normalize_remote_path(self.target_edit.text(), name.strip())
        except ValueError as exc:
            QMessageBox.warning(self, self._t("路径无效"), str(exc))
            return
        self._set_current_directory(target)
        self.settings.setValue("target_folder", target)
        self._log(f"目标文件夹设为：/{target}（上传内容后会在仓库中创建）")

    def pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件")
        self.add_paths(files)

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if folder:
            self.add_paths([folder])

    def add_paths(self, raw_paths: list[str], target: str | None = None) -> int:
        try:
            target = normalize_remote_path(self.target_edit.text() if target is None else target)
        except ValueError as exc:
            QMessageBox.warning(self, self._t("路径无效"), str(exc))
            return 0
        known = {(str(item.path).lower(), item.target) for item in self.upload_items}
        added = 0
        for raw in raw_paths:
            path = Path(raw)
            try:
                resolved = path.resolve()
            except OSError:
                continue
            key = (str(resolved).lower(), target)
            if not resolved.exists() or key in known:
                continue
            known.add(key)
            self.upload_items.append(UploadQueueItem(resolved, target))
            added += 1
        self._render_upload_queue()
        self._update_upload_enabled()
        return added

    def clear_queue(self) -> None:
        self.upload_items = [
            item for item in self.upload_items if item.status in {"uploading", "paused"}
        ]
        self._render_upload_queue()
        self._update_upload_enabled()

    def _render_upload_queue(self) -> None:
        self.queue_table.setRowCount(0)
        labels = {
            "waiting": self._t("等待"),
            "uploading": self._t("上传中"),
            "paused": self._t("已暂停"),
            "completed": self._t("完成"),
            "failed": self._t("失败"),
            "cancelled": self._t("已取消"),
            "skipped": self._t("已跳过（超过 50 GB）"),
        }
        for item in self.upload_items:
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            local = QTableWidgetItem(str(item.path))
            local.setFlags(local.flags() & ~Qt.ItemFlag.ItemIsEditable)
            kind = QTableWidgetItem("文件夹" if item.path.is_dir() else "文件")
            kind.setFlags(kind.flags() & ~Qt.ItemFlag.ItemIsEditable)
            target = QTableWidgetItem(item.target or "/")
            status = QTableWidgetItem(labels.get(item.status, item.status))
            status.setFlags(status.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if item.status == "completed":
                status.setForeground(QColor("#0f7b0f"))
            elif item.status in {"failed", "cancelled", "skipped"}:
                status.setForeground(QColor("#c42b1c"))
            else:
                status.setForeground(self.queue_table.palette().color(QPalette.ColorRole.Text))
            self.queue_table.setItem(row, 0, local)
            self.queue_table.setItem(row, 1, kind)
            self.queue_table.setItem(row, 2, target)
            self.queue_table.setItem(row, 3, status)

    def _set_upload_status(self, item: UploadQueueItem, status: str, text: str | None = None, message: str = "") -> None:
        item.status = status
        row = self.upload_items.index(item)
        status_item = self.queue_table.item(row, 3)
        if status_item is None:
            self._render_upload_queue()
            status_item = self.queue_table.item(row, 3)
        status_item.setText(text or self._t({
            "waiting": "等待", "uploading": "上传中", "paused": "已暂停",
            "completed": "完成", "failed": "失败", "cancelled": "已取消",
            "skipped": "已跳过（超过 50 GB）",
        }.get(status, status)))
        status_item.setToolTip(message)
        if status == "completed":
            status_item.setForeground(QColor("#0f7b0f"))
        elif status in {"failed", "cancelled", "skipped"}:
            status_item.setForeground(QColor("#c42b1c"))
        else:
            status_item.setForeground(self.queue_table.palette().color(QPalette.ColorRole.Text))

    def clear_download_queue(self) -> None:
        if self.task and self.task.isRunning():
            return
        self.download_specs.clear()
        self.download_states.clear()
        self.backup_sync_job_paths.clear()
        self.active_download_specs.clear()
        self.download_table.setRowCount(0)
        self.download_progress.setValue(0)
        self.download_stats.setText("速度：-- · 剩余：--")
        self._update_download_enabled()

    def _update_upload_enabled(self) -> None:
        active = bool(
            self.service and self.selected_repo
            and not self.selected_repo_public
            and any(item.status == "waiting" for item in self.upload_items)
        )
        if self.task and self.task.isRunning() and not isinstance(self.task, UploadThread):
            active = False
        if self.backup_thread and self.backup_thread.isRunning():
            active = False
        self.upload_button.setEnabled(active)

    def _update_download_enabled(self) -> None:
        active = any(
            self.download_states.get(str(spec.local_path), "waiting") in {"waiting", "failed", "stopped"}
            for spec in self.download_specs
        )
        if self.task and self.task.isRunning():
            active = False
        if self.backup_thread and self.backup_thread.isRunning():
            active = False
        self.download_button.setEnabled(active)

    def start_upload(self) -> None:
        if isinstance(self.task, UploadThread) and self.task.isRunning():
            return
        if not self.upload_items or not any(item.status == "waiting" for item in self.upload_items):
            return
        if self.upload_session_service is None:
            if not self.service or not self.selected_repo:
                return
            upload_service = self._token_service_for_repo(self.selected_repo)
            if upload_service is None:
                QMessageBox.information(
                    self, "需要 Token 账户", "请先添加可访问该仓库的 Token 账户；上传不会使用网页登录接口。",
                )
                return
            for row, item in enumerate(self.upload_items):
                if item.status != "waiting":
                    continue
                try:
                    item.target = normalize_remote_path(self.queue_table.item(row, 2).text())
                except ValueError as exc:
                    QMessageBox.warning(self, self._t("路径无效"), str(exc))
                    return
            self.upload_session_service = upload_service
            self.upload_session_repo = self.selected_repo
            self.upload_session_account_id = self.active_account_id
            self.upload_ok = self.upload_failed = self.upload_cancelled = 0
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._start_next_upload()

    def _start_next_upload(self) -> None:
        if self.task and self.task.isRunning():
            return
        item = next((item for item in self.upload_items if item.status == "waiting"), None)
        if item is None:
            self._finish_upload_queue()
            return
        if not self.upload_session_service or not self.upload_session_repo:
            return
        oversized = set(oversized_upload_files([item.path])) if self.upload_session_repo.repo_type == "model" else set()
        if item.path.is_file() and item.path.resolve() in oversized:
            self._set_upload_status(item, "skipped")
            self.upload_failed += 1
            self._log(f"已跳过超过 50 GB 的文件：{item.path}")
            QTimer.singleShot(0, self._start_next_upload)
            return
        self.settings.setValue("target_folder", item.target)
        self._set_upload_status(item, "uploading", "0%")
        worker = UploadThread(
            self.upload_session_service,
            self.upload_session_repo,
            item,
            self.keep_folder_name.isChecked(),
            oversized,
            self,
        )
        worker.item_done.connect(self._upload_item_done)
        worker.cancelled.connect(self._upload_cancelled)
        worker.progress_info.connect(self._upload_progress_info)
        worker.finished.connect(lambda: self._upload_thread_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.task = worker
        self.current_upload_speed = 0.0
        self._refresh_tray_status()
        self.pause_upload_button.setEnabled(True)
        self.resume_upload_button.setEnabled(False)
        self.cancel_upload_button.setEnabled(True)
        self._update_upload_enabled()
        self._update_download_enabled()
        self._log(f"开始上传 {item.path.name} 到 /{item.target}")
        worker.start()

    def _upload_progress_info(self, path: str, percent: int, speed: float, eta: int) -> None:
        self.current_upload_speed = max(0.0, speed)
        self.progress.setValue(percent)
        self.upload_stats.setText(self._tf("速度：{speed} · 剩余：{eta}", speed=format_speed(speed), eta=format_eta(eta)))
        if not path:
            return
        for item in self.upload_items:
            if str(item.path) == path and item.status == "uploading":
                self._set_upload_status(item, "uploading", f"{percent}%")
                break

    def _upload_item_done(self, path: str, success: bool, message: str) -> None:
        for item in self.upload_items:
            if str(item.path) == path and item.status in {"uploading", "paused"}:
                self._set_upload_status(item, "completed" if success else "failed", message=message)
                self.upload_ok += int(success)
                self.upload_failed += int(not success)
                break
        self._log(f"{'完成' if success else '失败'}：{Path(path).name} · {message}")

    def _upload_cancelled(self, path: str) -> None:
        for item in self.upload_items:
            if str(item.path) == path and item.status in {"uploading", "paused"}:
                self._set_upload_status(item, "cancelled")
                self.upload_cancelled += 1
                break
        self._log(f"已取消：{Path(path).name}")

    def _upload_thread_finished(self, worker: UploadThread) -> None:
        if self.task is worker:
            self.task = None
        self.current_upload_speed = 0.0
        self._refresh_tray_status()
        self.pause_upload_button.setEnabled(False)
        self.resume_upload_button.setEnabled(False)
        self.cancel_upload_button.setEnabled(False)
        self._update_upload_enabled()
        self._update_download_enabled()
        QTimer.singleShot(0, self._start_next_upload)

    def pause_upload(self) -> None:
        if not isinstance(self.task, UploadThread) or not self.task.isRunning():
            return
        self.task.pause()
        item = next((item for item in self.upload_items if item.status == "uploading"), None)
        if item:
            self._set_upload_status(item, "paused")
        self.pause_upload_button.setEnabled(False)
        self.resume_upload_button.setEnabled(True)

    def resume_upload(self) -> None:
        if not isinstance(self.task, UploadThread) or not self.task.isRunning():
            return
        self.task.resume()
        item = next((item for item in self.upload_items if item.status == "paused"), None)
        if item:
            self._set_upload_status(item, "uploading")
        self.pause_upload_button.setEnabled(True)
        self.resume_upload_button.setEnabled(False)

    def cancel_upload(self) -> None:
        if isinstance(self.task, UploadThread) and self.task.isRunning():
            self.task.cancel()
            self.pause_upload_button.setEnabled(False)
            self.resume_upload_button.setEnabled(False)
            self.cancel_upload_button.setEnabled(False)

    def _finish_upload_queue(self) -> None:
        if self.upload_session_service is None:
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.upload_stats.setText("速度：0 B/s · 剩余：00:00")
        if self.upload_ok and self.upload_session_repo and self.upload_session_account_id:
            self._mark_repository_dirty(self.upload_session_account_id, self.upload_session_repo)
            self.repo_heading.setText(self._t("上传完成，目录将在空闲时自动刷新"))
        self._log(f"上传队列结束：{self.upload_ok} 个成功，{self.upload_failed} 个失败，{self.upload_cancelled} 个取消")
        self.upload_session_service = None
        self.upload_session_repo = None
        self.upload_session_account_id = None
        self._update_upload_enabled()

    def start_download(self) -> None:
        specs = [
            spec for spec in self.download_specs
            if self.download_states.get(str(spec.local_path), "waiting") in {"waiting", "failed", "stopped"}
        ]
        self._start_download_specs(specs)

    def _auto_start_download(self) -> None:
        if self.task and self.task.isRunning():
            return
        specs = [
            spec for spec in self.download_specs
            if self.download_states.get(str(spec.local_path), "waiting") == "waiting"
        ]
        self._start_download_specs(specs)

    def _start_download_specs(self, specs: list[DownloadSpec]) -> None:
        if not specs or (self.task and self.task.isRunning()):
            return
        aria2_path = Path(__file__).resolve().parent.parent / "runtime" / "tools" / "aria2-next.exe"
        try:
            tuning = self._aria2_tuning()
        except ValueError as exc:
            QMessageBox.warning(self, self._t("aria2-next 配置无效"), str(exc))
            self._navigate(2)
            return
        runner = Aria2DownloadRunner(
            aria2_path, "", tuning,
            download_limit_supplier=lambda: self.transfer_policy.limits()[1],
        )
        self.download_runner = runner
        self.current_download_speed = 0.0
        self.active_download_specs = list(specs)
        self.download_progress.setValue(0)
        self.download_stats.setText("速度：0 B/s · 剩余：--")
        active_paths = {str(spec.local_path) for spec in specs}
        for row, spec in enumerate(self.download_specs):
            if str(spec.local_path) in active_paths:
                self.download_states[str(spec.local_path)] = "waiting"
                self.download_table.item(row, 2).setText("准备下载")
        worker = DownloadThread(runner, specs, self)
        worker.progress_info.connect(self._download_progress_info)
        worker.item_update.connect(self._download_item_update)
        worker.completed.connect(self._download_completed)
        worker.failed.connect(self._download_failed)
        worker.finished.connect(worker.deleteLater)
        self.task = worker
        self._refresh_tray_status()
        self.pause_download_button.setEnabled(True)
        self.resume_download_button.setEnabled(False)
        self.stop_download_button.setEnabled(True)
        self._update_upload_enabled()
        self._update_download_enabled()
        self._log(f"开始下载 {len(specs)} 个文件（aria2-next）")
        worker.start()

    def pause_download(self) -> None:
        if not self.download_runner:
            return
        try:
            changed = self.download_runner.pause()
        except Exception as exc:
            QMessageBox.warning(self, self._t("无法暂停"), str(exc))
            return
        if changed:
            self.current_download_speed = 0.0
            self.pause_download_button.setEnabled(False)
            self.resume_download_button.setEnabled(True)
            self.download_stats.setText("已暂停 · 已下载内容会保留")
            self._log("下载已暂停")

    def resume_download(self) -> None:
        if not self.download_runner:
            return
        try:
            changed = self.download_runner.resume()
        except Exception as exc:
            QMessageBox.warning(self, self._t("无法恢复"), str(exc))
            return
        if changed:
            self.pause_download_button.setEnabled(True)
            self.resume_download_button.setEnabled(False)
            self._log("下载已恢复")

    def stop_download(self) -> None:
        if not self.download_runner:
            return
        try:
            changed = self.download_runner.stop()
        except Exception as exc:
            QMessageBox.warning(self, self._t("无法停止"), str(exc))
            return
        if changed:
            self.current_download_speed = 0.0
            self.pause_download_button.setEnabled(False)
            self.resume_download_button.setEnabled(False)
            self.stop_download_button.setEnabled(False)
            self.download_stats.setText("正在停止… · 已下载内容不会删除")
            self._log("正在停止下载，已下载内容和断点文件将保留")

    def _download_progress_info(self, completed: int, total: int, speed: float, eta: int) -> None:
        self.current_download_speed = max(0.0, speed)
        percent = int(completed * 100 / max(1, total))
        self.download_progress.setValue(min(100, percent))
        self.download_stats.setText(self._tf("速度：{speed} · 剩余：{eta}", speed=format_speed(speed), eta=format_eta(eta)))

    def _download_item_update(self, local_path: str, state: str, completed: int, total: int, message: str) -> None:
        self.download_states[local_path] = state
        for row, spec in enumerate(self.download_specs):
            if str(spec.local_path) != local_path:
                continue
            status = self.download_table.item(row, 2)
            percent = int(completed * 100 / total) if total > 0 else 0
            labels = {
                "waiting": self._t("等待下载"),
                "downloading": f"{percent}% · {self._t(message)}",
                "paused": self._tf("{percent}% · 已暂停", percent=percent),
                "verifying": self._t("正在校验"),
                "completed": self._t("完成 · 校验通过"),
                "failed": self._tf("失败 · {message}", message=self._t(message)),
                "stopped": self._tf("已停止 · {percent}%（可继续）", percent=percent),
            }
            status.setText(labels.get(state, message))
            status.setToolTip(message)
            if state in {"completed", "failed", "stopped"}:
                color = "#0f7b0f" if state == "completed" else ("#9a6700" if state == "stopped" else "#c42b1c")
                status.setForeground(QColor(color))
            if state == "completed":
                job_id = self.backup_sync_job_paths.pop(local_path, "")
                job = next((candidate for candidate in self.backup_jobs if candidate.job_id == job_id), None)
                local_file = Path(local_path)
                if job and local_file.is_file():
                    try:
                        stat = local_file.stat()
                        relative = local_file.resolve().relative_to(Path(job.local_path).resolve()).as_posix()
                        self.backup_store.mark_uploaded(
                            job.job_id,
                            LocalBackupFile(local_file.resolve(), relative, stat.st_size, stat.st_mtime_ns),
                            spec.remote_path,
                        )
                    except (OSError, ValueError):
                        pass
                if self.potplayer_install_archive and local_file.resolve() == self.potplayer_install_archive.resolve():
                    QTimer.singleShot(0, lambda media=local_file: self._start_potplayer_extraction(media))
            break

    def _download_completed(self, ok: int, failed: int) -> None:
        stopped = bool(self.download_runner and self.download_runner.stopped)
        self.task = None
        self.download_runner = None
        self.current_download_speed = 0.0
        self._refresh_tray_status()
        self.active_download_specs.clear()
        self.pause_download_button.setEnabled(False)
        self.resume_download_button.setEnabled(False)
        self.stop_download_button.setEnabled(False)
        if stopped:
            self.download_stats.setText("已停止 · 已下载内容和断点已保留")
        else:
            self.download_progress.setValue(100)
            self.download_stats.setText("速度：0 B/s · 剩余：00:00")
        self._update_upload_enabled()
        self._update_download_enabled()
        if stopped:
            self._log("下载已停止，可点击“开始下载”从断点继续")
        elif failed:
            self._log(f"下载结束：{ok} 个成功，{failed} 个失败")
            QMessageBox.warning(self, self._t("下载完成"), self._tf("{ok} 个文件成功，{failed} 个失败。", ok=ok, failed=failed))
        else:
            self._log(f"下载结束：{ok} 个成功，{failed} 个失败")
            QMessageBox.information(self, self._t("下载完成"), self._tf("{ok} 个文件已下载并通过校验。", ok=ok))
        if not stopped:
            QTimer.singleShot(0, self._auto_start_download)

    def _download_failed(self, error: str) -> None:
        for spec in self.active_download_specs:
            path = str(spec.local_path)
            if self.download_states.get(path) not in {"completed", "stopped"}:
                self.download_states[path] = "failed"
        self.task = None
        self.download_runner = None
        self.current_download_speed = 0.0
        self._refresh_tray_status()
        self.active_download_specs.clear()
        self.pause_download_button.setEnabled(False)
        self.resume_download_button.setEnabled(False)
        self.stop_download_button.setEnabled(False)
        self._update_upload_enabled()
        self._update_download_enabled()
        self._log(f"下载失败：{error}")
        QMessageBox.warning(self, self._t("下载失败"), error)


def run() -> int:
    launch_settings = portable_settings()
    gpu_acceleration = str(launch_settings.value("graphics/gpu_acceleration", "true")).lower() == "true"
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_UseDesktopOpenGL
        if gpu_acceleration else Qt.ApplicationAttribute.AA_UseSoftwareOpenGL,
        True,
    )
    app = QApplication(sys.argv)
    app.setApplicationName("ModelScope Manager")
    app.setOrganizationName("ARXChem")
    initial_font = QFont("Microsoft YaHei UI")
    initial_font.setPointSize(int(launch_settings.value("font_size", 10)))
    app.setFont(initial_font)
    window = MainWindow()
    window.show()
    return app.exec()
