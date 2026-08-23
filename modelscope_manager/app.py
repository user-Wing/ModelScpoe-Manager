from __future__ import annotations

import json
import re
import secrets
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from PySide6.QtCore import QEvent, QProcess, QSettings, Qt, QThread, QTime, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QScrollArea,
    QStyle,
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

from .security import protect, unprotect
from .download_service import Aria2DownloadRunner, Aria2Tuning, DownloadSpec, build_download_specs
from .backup import BackupJob, BackupStore, LocalBackupFile
from .database import AccountRecord, AccountStore, IndexedEntry, initialize_database
from .folder_index import FolderSizeIndex
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
    MultiAccountService,
    RemoteEntry,
    Repository,
    normalize_remote_path,
    oversized_upload_files,
    parse_modelscope_repository_url,
    repository_directories,
    configure_upload_limit_supplier,
)
from .styles import QSS
from .storage import (
    APP_DIR,
    DEVICE_ID_PATH,
    FOLDER_INDEX_PATH,
    IMAGE_CACHE_DIR,
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


MEDIA_EXTENSIONS = {
    ".3gp", ".aac", ".ac3", ".aiff", ".alac", ".ape", ".avi", ".flac", ".flv",
    ".m2ts", ".m4a", ".m4v", ".mka", ".mkv", ".mov", ".mp3", ".mp4", ".mpeg",
    ".mpg", ".mts", ".ogg", ".ogv", ".opus", ".rm", ".rmvb", ".ts", ".wav",
    ".webm", ".wma", ".wmv",
}


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


def format_eta(seconds: int) -> str:
    if seconds < 0:
        return "--"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


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


class UploadThread(QThread):
    item_done = Signal(str, bool, str)
    progress_info = Signal(str, int, float, int)
    completed = Signal(int, int)

    def __init__(self, service: ModelScopeService, repo: Repository, paths: list[Path], target: str, keep_name: bool, skipped_files: set[Path] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.service = service
        self.repo = repo
        self.paths = paths
        self.target = target
        self.keep_name = keep_name
        self.skipped_files = {path.resolve() for path in (skipped_files or set())}

    def run(self) -> None:
        sizes = [self._path_size(path, self.skipped_files) for path in self.paths]
        total_size = max(1, sum(sizes))
        completed_size = 0
        started = time.monotonic()
        last_speed_time = started
        last_speed_bytes = 0
        current_speed = 0.0
        ok = failed = 0
        for path, item_size in zip(self.paths, sizes):
            current_size = 0
            progress_lock = threading.Lock()
            success_message = "上传完成"

            def report_bytes(amount: int) -> None:
                nonlocal current_size, last_speed_time, last_speed_bytes, current_speed
                with progress_lock:
                    current_size = min(item_size, current_size + max(0, amount))
                    percent = int((completed_size + current_size) * 100 / total_size)
                    transferred = completed_size + current_size
                now = time.monotonic()
                interval = now - last_speed_time
                if interval >= 0.15:
                    instant = max(0, transferred - last_speed_bytes) / interval
                    current_speed = instant if current_speed <= 0 else current_speed * 0.55 + instant * 0.45
                    last_speed_time = now
                    last_speed_bytes = transferred
                eta = int((total_size - transferred) / current_speed) if current_speed > 0 else -1
                self.progress_info.emit(str(path), min(99, percent), current_speed, eta)

            try:
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
                                    self.target,
                                    path.name if self.keep_name else "",
                                )
                                errors: list[str] = []
                                for safe_file in safe_files:
                                    relative_parent = safe_file.relative_to(path).parent.as_posix()
                                    try:
                                        self.service.upload_file(
                                            self.repo,
                                            safe_file,
                                            normalize_remote_path(remote_base, relative_parent),
                                        )
                                    except Exception as exc:
                                        errors.append(f"{safe_file}: {exc}")
                                if errors:
                                    raise RuntimeError("；".join(errors))
                        else:
                            self.service.upload_folder(self.repo, path, self.target, self.keep_name)
                    elif path.is_file():
                        self.service.upload_file(self.repo, path, self.target)
                    else:
                        raise FileNotFoundError(str(path))
            except Exception as exc:
                failed += 1
                self.item_done.emit(str(path), False, str(exc))
            else:
                ok += 1
                self.item_done.emit(str(path), True, success_message)
            completed_size += item_size
            elapsed = max(0.001, time.monotonic() - started)
            speed = current_speed or completed_size / elapsed
            eta = int((total_size - completed_size) / speed) if speed > 0 else 0
            self.progress_info.emit(str(path), min(99, int(completed_size * 100 / total_size)), speed, eta)
        self.progress_info.emit("", 100, 0.0, 0)
        self.completed.emit(ok, failed)

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
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self.account_id = account_id
        self.service = service
        self.repo = repo
        self.paths = paths
        self.destination = destination

    def run(self) -> None:
        ok = failed = 0
        date_folder = datetime.now().strftime("%Y/%m")
        for path in self.paths:
            if self.isInterruptionRequested():
                break
            remote_name = f"{uuid.uuid4().hex[:8]}_{path.name}"
            remote_path = normalize_remote_path(self.destination, date_folder, remote_name)
            try:
                if path.stat().st_size >= 50 * 1024**3:
                    raise ValueError("图片达到或超过 50 GB")
                self.service.upload_file_as(self.repo, path, remote_path)
                direct_url = self.service.get_download_url(self.repo, remote_path)
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
                if account_id and not public:
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
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        target = self._directory_item(item)
        if target is not None and event.mimeData().hasUrls():
            self.setCurrentItem(target)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        item = self.itemAt(event.position().toPoint())
        target = self._directory_item(item)
        if target is None:
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        entry = target.data(0, Qt.ItemDataRole.UserRole)
        if paths and isinstance(entry, RemoteEntry):
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

    def _set_dragging(self, value: bool) -> None:
        self.setProperty("dragging", value)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()
            self._set_dragging(True)

    def dragLeaveEvent(self, event) -> None:
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._set_dragging(False)
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.paths_dropped.emit(paths)
            event.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self):
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
        self.session_tokens: dict[str, str] = {}
        self.account_services: dict[str, ModelScopeService] = {}
        self.account_repositories: dict[str, list[Repository]] = {}
        self.active_account_id: str | None = None
        self.service: ModelScopeService | None = None
        self.repositories: list[Repository] = []
        self.selected_repo: Repository | None = None
        self.remote_entries: list[RemoteEntry] = []
        self.global_search_results: list[IndexedEntry] = []
        self.pending_search_path: str = ""
        self.upload_items: list[Path] = []
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
        self.resource_search_timer.setInterval(220)
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
        QApplication.instance().installEventFilter(self)
        self._restore_settings()
        self._apply_language()
        self._build_tray()
        if self.token_destroyed_on_start:
            self.account_label.setText(self._t("检测到设备变化，已销毁已保存的访问令牌"))
            self._log("检测到设备变化，已销毁已保存的访问令牌")
        if any(account.token for account in self.accounts):
            QTimer.singleShot(0, self.load_repositories)
        else:
            if self.alist_enabled.isChecked() and self.alist_auto_start.isChecked():
                QTimer.singleShot(0, self.apply_alist_settings)
            QTimer.singleShot(0, lambda: self._start_folder_indexing(True))
        self.backup_timer.start()
        self.transfer_policy_timer.start()

    def _build_ui(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        nav = QFrame(objectName="navSidebar")
        nav.setFixedWidth(190)
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(14, 20, 14, 18)
        nav_layout.setSpacing(6)
        brand = QLabel("ModelScope\nManager", objectName="brand")
        nav_layout.addWidget(brand)
        nav_layout.addSpacing(18)
        self.nav_buttons: list[QPushButton] = []
        for page, label in ((0, "▣  资源管理"), (3, "⌕  资源搜索"), (1, "⇅  传输列表"), (4, "▤  备份文件夹"), (5, "▧  图床")):
            button = QPushButton(label, objectName="navButton")
            button.setCheckable(True)
            button.setProperty("pageIndex", page)
            button.clicked.connect(lambda checked=False, target=page: self._navigate(target))
            nav_layout.addWidget(button)
            self.nav_buttons.append(button)
        nav_layout.addStretch()
        settings_nav = QPushButton("⚙  设置", objectName="navButton")
        settings_nav.setCheckable(True)
        settings_nav.setProperty("pageIndex", 2)
        settings_nav.clicked.connect(lambda checked=False: self._navigate(2))
        nav_layout.addWidget(settings_nav)
        self.nav_buttons.append(settings_nav)
        nav_layout.addSpacing(2)
        nav_layout.addWidget(QLabel("ModelScope Manager 1.0", objectName="navFooter"))
        shell.addWidget(nav)

        self.page_stack = QStackedWidget()
        shell.addWidget(self.page_stack, 1)

        # Resource manager page
        resource_page = QWidget()
        resource_layout = QVBoxLayout(resource_page)
        resource_layout.setContentsMargins(24, 20, 24, 22)
        resource_layout.setSpacing(14)
        resource_heading = QHBoxLayout()
        heading_text = QVBoxLayout()
        heading_text.addWidget(QLabel("资源管理", objectName="title"))
        self.repo_heading = QLabel("选择仓库并像使用网盘一样管理文件", objectName="subtitle")
        heading_text.addWidget(self.repo_heading)
        resource_heading.addLayout(heading_text)
        resource_heading.addStretch()
        self.resource_search_scope = QComboBox()
        self.resource_search_scope.addItem("全部仓库", "all")
        self.resource_search_scope.addItem("当前账户", "account")
        self.resource_search_scope.addItem("当前仓库", "repository")
        self.resource_search_scope.addItem("当前目录", "directory")
        self.resource_search_scope.setMinimumWidth(112)
        self.resource_search_scope.currentIndexChanged.connect(self._schedule_global_search)
        resource_heading.addWidget(self.resource_search_scope)
        self.resource_search_type = QComboBox()
        self.resource_search_type.addItem("全部类型", "all")
        self.resource_search_type.addItem("视频", "video")
        self.resource_search_type.addItem("图片", "image")
        self.resource_search_type.addItem("文档", "document")
        self.resource_search_type.addItem("压缩包", "archive")
        self.resource_search_type.setMinimumWidth(100)
        self.resource_search_type.currentIndexChanged.connect(self._schedule_global_search)
        resource_heading.addWidget(self.resource_search_type)
        self.resource_search_edit = QLineEdit()
        self.resource_search_edit.setPlaceholderText("搜索所有已索引文件")
        self.resource_search_edit.setClearButtonEnabled(True)
        self.resource_search_edit.setMinimumWidth(220)
        self.resource_search_edit.textChanged.connect(self._schedule_global_search)
        self.resource_search_edit.returnPressed.connect(self._perform_global_search)
        resource_heading.addWidget(self.resource_search_edit)
        self.update_index_button = QPushButton("更新索引")
        self.update_index_button.clicked.connect(self.update_all_indexes)
        resource_heading.addWidget(self.update_index_button)
        self.refresh_files_button = QPushButton("刷新目录")
        self.refresh_files_button.setEnabled(False)
        self.refresh_files_button.clicked.connect(self.load_remote_files)
        resource_heading.addWidget(self.refresh_files_button)
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
        repo_layout.addWidget(self.repo_list, 1)
        self.compact_view_button = QCheckBox("紧凑视图")
        self.compact_view_button.toggled.connect(self._compact_view_changed)
        repo_layout.addWidget(self.compact_view_button)
        self.refresh_repos_button = QPushButton("读取 / 刷新仓库")
        self.refresh_repos_button.clicked.connect(self.load_repositories)
        repo_layout.addWidget(self.refresh_repos_button)
        resource_splitter.addWidget(repo_card)

        explorer_card = QFrame(objectName="card")
        explorer_layout = QVBoxLayout(explorer_card)
        explorer_layout.setContentsMargins(16, 16, 16, 16)
        explorer_toolbar = QHBoxLayout()
        explorer_toolbar.addWidget(QLabel("仓库目录", objectName="section"))
        explorer_toolbar.addSpacing(12)
        self.resource_path_label = QLabel("/ 根目录", objectName="pathPill")
        explorer_toolbar.addWidget(self.resource_path_label, 1)
        self.new_folder_button = QPushButton("新建文件夹")
        self.new_folder_button.setEnabled(False)
        self.new_folder_button.clicked.connect(self.new_folder)
        explorer_toolbar.addWidget(self.new_folder_button)
        explorer_layout.addLayout(explorer_toolbar)
        self.resource_drop_hint = QLabel("将本地文件或文件夹拖到下方任意目录，即可直接上传到该目录", objectName="dropHint")
        explorer_layout.addWidget(self.resource_drop_hint)
        self.remote_tree = RepositoryTree()
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
        explorer_layout.addWidget(self.remote_tree, 1)
        self.global_search_label = QLabel("", objectName="subtitle")
        self.global_search_label.setVisible(False)
        explorer_layout.addWidget(self.global_search_label)
        self.global_search_tree = QTreeWidget()
        self.global_search_tree.setObjectName("repositoryTree")
        self.global_search_tree.setHeaderLabels(["名称", "类型", "大小", "仓库", "路径"])
        global_header = self.global_search_tree.header()
        global_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        global_header.setStretchLastSection(True)
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
        self.page_stack.addWidget(resource_page)

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
        self.queue_table = QTableWidget(0, 3)
        self.queue_table.setHorizontalHeaderLabels(["本地项目", "类型", "状态"])
        queue_header = self.queue_table.horizontalHeader()
        queue_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        queue_header.setMinimumSectionSize(70)
        queue_header.setStretchLastSection(True)
        self.queue_table.setColumnWidth(0, 470)
        self.queue_table.setColumnWidth(1, 90)
        self.queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
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
        self.page_stack.addWidget(transfer_page)

        # Settings page
        settings_page = QWidget()
        self.settings_page = settings_page
        settings_page_layout = QVBoxLayout(settings_page)
        settings_page_layout.setContentsMargins(0, 0, 0, 0)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_content = QWidget()
        settings_layout = QVBoxLayout(settings_content)
        settings_layout.setContentsMargins(28, 22, 28, 28)
        settings_layout.setSpacing(16)
        settings_layout.addWidget(QLabel("设置", objectName="title"))
        self.disable_settings_wheel = QCheckBox("禁用鼠标滚轮修改配置选项")
        self.disable_settings_wheel.setChecked(True)
        self.disable_settings_wheel.toggled.connect(self._wheel_setting_changed)
        settings_layout.addWidget(self.disable_settings_wheel)
        token_card = QFrame(objectName="card")
        token_layout = QVBoxLayout(token_card)
        token_layout.setContentsMargins(20, 18, 20, 20)
        account_heading = QHBoxLayout()
        account_heading.addWidget(QLabel("ModelScope 账户", objectName="section"))
        account_heading.addStretch()
        add_account_button = QPushButton("＋", objectName="symbolButton")
        add_account_button.setFixedSize(38, 36)
        add_account_button.setToolTip("添加账户")
        add_account_button.clicked.connect(self.add_account)
        account_heading.addWidget(add_account_button)
        remove_account_button = QPushButton("－", objectName="symbolButton")
        remove_account_button.setFixedSize(38, 36)
        remove_account_button.setToolTip("移除所选账户")
        remove_account_button.clicked.connect(self.remove_account)
        account_heading.addWidget(remove_account_button)
        token_layout.addLayout(account_heading)
        token_layout.addWidget(QLabel("Token 使用设备绑定加密；添加后会自动验证并取得用户名。", objectName="subtitle"))
        self.account_table = QTableWidget(0, 5)
        self.account_table.setHorizontalHeaderLabels(["账户名称", "用户名", "Token", "安全记住", "状态"])
        self.account_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.account_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.account_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
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
        settings_layout.addWidget(token_card)

        download_card = QFrame(objectName="card")
        download_setting_layout = QVBoxLayout(download_card)
        download_setting_layout.setContentsMargins(20, 18, 20, 20)
        download_setting_layout.addWidget(QLabel("默认下载路径", objectName="section"))
        download_path_row = QHBoxLayout()
        self.download_path_edit = QLineEdit()
        self.download_path_edit.setReadOnly(True)
        download_path_row.addWidget(self.download_path_edit, 1)
        change_download_button = QPushButton("修改")
        change_download_button.clicked.connect(self.change_download_path)
        download_path_row.addWidget(change_download_button)
        download_setting_layout.addLayout(download_path_row)
        settings_layout.addWidget(download_card)

        player_card = QFrame(objectName="card")
        player_layout = QVBoxLayout(player_card)
        player_layout.setContentsMargins(20, 18, 20, 20)
        player_heading = QHBoxLayout()
        player_heading.addWidget(QLabel("播放器", objectName="section"))
        player_heading.addStretch()
        add_player_button = QPushButton("＋")
        add_player_button.setObjectName("symbolButton")
        add_player_button.setFixedSize(38, 36)
        add_player_button.setToolTip("添加播放器")
        add_player_button.clicked.connect(self.add_external_player)
        player_heading.addWidget(add_player_button)
        remove_player_button = QPushButton("－")
        remove_player_button.setObjectName("symbolButton")
        remove_player_button.setFixedSize(38, 36)
        remove_player_button.setToolTip("删除所选播放器")
        remove_player_button.clicked.connect(self.remove_external_player)
        player_heading.addWidget(remove_player_button)
        player_layout.addLayout(player_heading)
        self.builtin_player_enabled = QCheckBox("使用本地 PotPlayer 作为默认视频/图片播放器")
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
        player_layout.addWidget(QLabel("第三方播放器", objectName="section"))
        player_layout.addWidget(QLabel("媒体右键菜单会在本地 PotPlayer 之后显示以下第三方播放器。", objectName="subtitle"))
        self.player_table = QTableWidget(0, 2)
        self.player_table.setHorizontalHeaderLabels(["播放器名称", "程序路径"])
        self.player_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.player_table.horizontalHeader().setStretchLastSection(True)
        self.player_table.setColumnWidth(0, 180)
        self.player_table.setMaximumHeight(150)
        self.player_table.itemChanged.connect(self._players_edited)
        player_layout.addWidget(self.player_table)
        settings_layout.addWidget(player_card)

        aria_card = QFrame(objectName="card")
        aria_layout = QVBoxLayout(aria_card)
        aria_layout.setContentsMargins(20, 18, 20, 20)
        aria_layout.addWidget(QLabel("aria2-next 详细配置", objectName="section"))
        aria_layout.addWidget(QLabel("按文件大小自动分配 HTTP 连接和下载分段，并为小文件仓库增加并行任务。", objectName="subtitle"))
        self.aria_strategy_combo = QComboBox()
        self.aria_strategy_combo.addItem("自动（按文件大小，推荐）", "adaptive")
        aria_layout.addWidget(self.aria_strategy_combo)
        aria_grid = QGridLayout()
        aria_grid.setHorizontalSpacing(12)
        aria_grid.setVerticalSpacing(10)
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
        self.speed_limit_enabled = QCheckBox("启用传输限速")
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
        settings_layout.addWidget(aria_card)

        general_card = QFrame(objectName="card")
        general_layout = QVBoxLayout(general_card)
        general_layout.setContentsMargins(20, 18, 20, 20)
        general_layout.addWidget(QLabel("界面与关闭行为", objectName="section"))
        general_grid = QGridLayout()
        general_grid.addWidget(QLabel("语言"), 0, 0)
        self.language_combo = QComboBox()
        self.language_combo.addItem("简体中文", "zh_CN")
        self.language_combo.addItem("English", "en_US")
        self.language_combo.currentIndexChanged.connect(self._language_changed)
        general_grid.addWidget(self.language_combo, 0, 1)
        general_grid.addWidget(QLabel("关闭窗口时"), 1, 0)
        self.close_behavior_combo = QComboBox()
        self.close_behavior_combo.addItem("第一次询问", "ask")
        self.close_behavior_combo.addItem("最小化到通知区域", "tray")
        self.close_behavior_combo.addItem("直接关闭程序", "close")
        self.close_behavior_combo.currentIndexChanged.connect(self._close_behavior_changed)
        general_grid.addWidget(self.close_behavior_combo, 1, 1)
        self.startup_checkbox = QCheckBox("开机自启")
        self.startup_checkbox.toggled.connect(self._startup_changed)
        general_grid.addWidget(self.startup_checkbox, 2, 1)
        general_layout.addLayout(general_grid)
        settings_layout.addWidget(general_card)

        index_card = QFrame(objectName="card")
        index_layout = QVBoxLayout(index_card)
        index_layout.setContentsMargins(20, 18, 20, 20)
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
        settings_layout.addWidget(index_card)

        alist_card = QFrame(objectName="card")
        alist_layout = QVBoxLayout(alist_card)
        alist_layout.setContentsMargins(20, 18, 20, 20)
        alist_layout.addWidget(QLabel("AList V3 挂载", objectName="section"))
        alist_warning = QLabel("使用 WebDAV 网关。受到 ModelScope 官方 API 限制，不支持删除、重命名、移动和复制操作。", objectName="subtitle")
        alist_warning.setWordWrap(True)
        alist_layout.addWidget(alist_warning)
        alist_grid = QGridLayout()
        alist_grid.addWidget(QLabel("配置方式"), 0, 0)
        self.alist_protocol_combo = QComboBox()
        self.alist_protocol_combo.addItem("WebDAV", "webdav")
        alist_grid.addWidget(self.alist_protocol_combo, 0, 1)
        alist_grid.addWidget(QLabel("监听范围"), 1, 0)
        self.alist_host_combo = QComboBox()
        self.alist_host_combo.addItem("仅本机（127.0.0.1）", "127.0.0.1")
        self.alist_host_combo.addItem("局域网 / Docker（0.0.0.0）", "0.0.0.0")
        alist_grid.addWidget(self.alist_host_combo, 1, 1)
        alist_grid.addWidget(QLabel("端口"), 2, 0)
        self.alist_port = QSpinBox()
        self.alist_port.setRange(1024, 65535)
        self.alist_port.setValue(9867)
        alist_grid.addWidget(self._stepper(self.alist_port), 2, 1)
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
        self.alist_enabled = QCheckBox("启用 WebDAV 网关")
        alist_actions.addWidget(self.alist_enabled)
        self.alist_auto_start = QCheckBox("启动后自动启动监听")
        self.alist_auto_start.toggled.connect(self._save_alist_settings)
        alist_actions.addWidget(self.alist_auto_start)
        alist_actions.addStretch()
        self.alist_apply_button = QPushButton("应用并启动")
        self.alist_apply_button.clicked.connect(self.apply_alist_settings)
        alist_actions.addWidget(self.alist_apply_button)
        alist_layout.addLayout(alist_actions)
        self.alist_status = QLabel("未启动", objectName="subtitle")
        alist_layout.addWidget(self.alist_status)
        for control in (self.alist_host_combo, self.alist_port, self.alist_username, self.alist_password):
            if isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self._update_alist_url)
            elif isinstance(control, QSpinBox):
                control.valueChanged.connect(self._update_alist_url)
            else:
                control.textChanged.connect(self._update_alist_url)
        settings_layout.addWidget(alist_card)
        settings_layout.addStretch()
        settings_scroll.setWidget(settings_content)
        settings_page_layout.addWidget(settings_scroll)
        self.page_stack.addWidget(settings_page)

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
        search_layout.addLayout(search_bar)
        search_card = QFrame(objectName="card")
        search_card_layout = QVBoxLayout(search_card)
        search_card_layout.setContentsMargins(16, 16, 16, 16)
        self.search_heading = QLabel("等待输入公开资源链接", objectName="section")
        search_card_layout.addWidget(self.search_heading)
        self.search_remote_tree = RepositoryTree()
        self.search_remote_tree.setAcceptDrops(False)
        self.search_remote_tree.setObjectName("repositoryTree")
        self.search_remote_tree.setHeaderLabels(["名称", "类型", "大小", "路径"])
        search_header = self.search_remote_tree.header()
        search_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        search_header.setStretchLastSection(True)
        self.search_remote_tree.setColumnWidth(0, 340)
        self.search_remote_tree.setColumnWidth(1, 90)
        self.search_remote_tree.setColumnWidth(2, 110)
        self.search_remote_tree.setColumnWidth(3, 330)
        self.search_remote_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_remote_tree.customContextMenuRequested.connect(self._search_context_menu)
        search_card_layout.addWidget(self.search_remote_tree, 1)
        search_layout.addWidget(search_card, 1)
        self.page_stack.addWidget(search_page)

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
        self.page_stack.addWidget(backup_page)

        # Image hosting page
        image_page = QWidget()
        image_layout = QVBoxLayout(image_page)
        image_layout.setContentsMargins(24, 20, 24, 22)
        image_layout.setSpacing(14)
        image_layout.addWidget(QLabel("图床", objectName="title"))
        image_layout.addWidget(QLabel(
            "拖入图片后上传到指定仓库，生成直链并保存本地缓存。",
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
        self.image_drop_area = DropArea("将图片拖到这里上传", "支持 PNG、JPEG、WebP、GIF、AVIF、SVG 等常见格式")
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
        self.page_stack.addWidget(image_page)
        self._navigate(0)

    def _restore_settings(self) -> None:
        self._restoring_settings = True
        self.target_edit.setText(str(self.settings.value("target_folder", "")))
        default_download = Path.home() / "Downloads"
        self.download_path_edit.setText(str(self.settings.value("download_path", str(default_download))))
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
        language = str(self.settings.value("language", "zh_CN"))
        language_index = self.language_combo.findData(language)
        self.language_combo.setCurrentIndex(max(0, language_index))
        behavior = str(self.settings.value("close_behavior", "ask"))
        behavior_index = self.close_behavior_combo.findData(behavior)
        self.close_behavior_combo.setCurrentIndex(max(0, behavior_index))
        self.startup_checkbox.setChecked(windows_startup_enabled())
        self.compact_view_button.setChecked(
            str(self.settings.value("compact_view", "false")).lower() == "true"
        )
        self.background_index_minutes.setValue(int(self.settings.value("index/background_minutes", 5)))
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
        host = str(self.settings.value("alist/host", "127.0.0.1"))
        host_index = self.alist_host_combo.findData(host)
        self.alist_host_combo.setCurrentIndex(max(0, host_index))
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
        self.alist_enabled.setChecked(str(self.settings.value("alist/enabled", "false")).lower() == "true")
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
        self.session_tokens = {account.account_id: account.token for account in self.accounts if account.token}
        self._render_accounts()
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

    def _render_accounts(self) -> None:
        if not hasattr(self, "account_table"):
            return
        selected_id = self._selected_account_id()
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
        for account in self.accounts:
            self.backup_account_combo.addItem(account.label or account.username, account.account_id)
        index = self.backup_account_combo.findData(selected)
        self.backup_account_combo.setCurrentIndex(index if index >= 0 else (0 if self.accounts else -1))
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
        for account in self.accounts:
            self.image_account_combo.addItem(account.label or account.username, account.account_id)
        index = self.image_account_combo.findData(selected)
        self.image_account_combo.setCurrentIndex(index if index >= 0 else (0 if self.accounts else -1))
        self.image_account_combo.blockSignals(False)
        self._render_image_repository_options()

    def _render_image_repository_options(self) -> None:
        if not hasattr(self, "image_repo_combo"):
            return
        selected = self.image_repo_combo.currentData()
        if not selected:
            selected = (
                str(self.settings.value("image/repo_type", "")),
                str(self.settings.value("image/repo_id", "")),
            )
        account_id = str(self.image_account_combo.currentData() or "")
        self.image_repo_combo.blockSignals(True)
        self.image_repo_combo.clear()
        for repo in self.account_repositories.get(account_id, []):
            self.image_repo_combo.addItem(repo.repo_id, (repo.repo_type, repo.repo_id))
        index = self.image_repo_combo.findData(selected)
        self.image_repo_combo.setCurrentIndex(index if index >= 0 else (0 if self.image_repo_combo.count() else -1))
        self.image_repo_combo.blockSignals(False)
        self._save_image_settings()

    def _save_image_settings(self) -> None:
        if self._restoring_settings or not hasattr(self, "image_account_combo"):
            return
        self.settings.setValue("image/account_id", self.image_account_combo.currentData() or "")
        repo_data = self.image_repo_combo.currentData()
        if isinstance(repo_data, tuple) and len(repo_data) == 2:
            self.settings.setValue("image/repo_type", repo_data[0])
            self.settings.setValue("image/repo_id", repo_data[1])
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

    def _upload_images(self, raw_paths: list[str]) -> None:
        if self.image_upload_thread and self.image_upload_thread.isRunning():
            QMessageBox.information(self, self._t("图片上传中"), self._t("请等待当前图片上传完成。"))
            return
        if (self.task and self.task.isRunning()) or (self.backup_thread and self.backup_thread.isRunning()):
            QMessageBox.information(self, self._t("请稍候"), self._t("当前传输完成后再上传图片。"))
            return
        paths = [Path(raw).resolve() for raw in raw_paths
                 if Path(raw).is_file() and Path(raw).suffix.lower() in IMAGE_EXTENSIONS]
        if not paths:
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
            QMessageBox.warning(self, self._t("图床配置无效"), self._t("请选择已经验证且可写入的账户仓库。"))
            return
        try:
            destination = normalize_remote_path(self.image_dest_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, self._t("路径无效"), str(exc))
            return
        worker = ImageUploadThread(self.image_store, account_id, service, repo, paths, destination, self)
        worker.uploaded.connect(self._image_uploaded)
        worker.item_done.connect(self._image_item_done)
        worker.completed.connect(lambda ok, failed, aid=account_id, target=repo: self._image_upload_completed(aid, target, ok, failed))
        worker.finished.connect(lambda: self._image_upload_finished(worker))
        worker.finished.connect(worker.deleteLater)
        self.image_upload_thread = worker
        self.image_status_label.setText(self._tf("正在上传 {count} 张图片…", count=len(paths)))
        self._save_image_settings()
        worker.start()

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
        self.page_stack.setCurrentIndex(index)
        for button in self.nav_buttons:
            button.setChecked(int(button.property("pageIndex")) == index)

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
        for combo in self.findChildren(QComboBox):
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
        for tree_name in ("remote_tree", "global_search_tree", "search_remote_tree"):
            tree = getattr(self, tree_name, None)
            if tree is not None:
                tree.setStyleSheet(f"QTreeWidget::item {{ min-height: {height}px; }}")
        if hasattr(self, "repo_list"):
            left_height = 24 if checked else 36
            self.repo_list.setStyleSheet(f"QTreeWidget::item {{ min-height: {left_height}px; }}")
        if hasattr(self, "resource_splitter"):
            self.resource_splitter.widget(0).setMinimumWidth(245 if checked else 405)
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

    def _apply_background_index_interval(self) -> None:
        if hasattr(self, "background_index_minutes"):
            self.background_index_timer.setInterval(max(1, self.background_index_minutes.value()) * 60000)
            if not self.background_index_timer.isActive():
                self.background_index_timer.start()

    def eventFilter(self, watched, event) -> bool:
        if (
            self.dirty_repositories
            and event.type() in {
                QEvent.Type.KeyPress, QEvent.Type.MouseButtonPress,
                QEvent.Type.MouseButtonRelease, QEvent.Type.Wheel,
            }
        ):
            self.index_idle_timer.start()
        if (
            event.type() == QEvent.Type.Wheel
            and hasattr(self, "disable_settings_wheel")
            and self.disable_settings_wheel.isChecked()
            and isinstance(watched, (QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit))
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
        self.alist_enabled.setChecked(not running)
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
        self.settings.setValue("alist/enabled", self.alist_enabled.isChecked())
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
        if not self.alist_enabled.isChecked():
            self.alist_status.setText(self._t("未启动"))
            self._refresh_tray_status()
            return
        username = self.alist_username.text().strip()
        password = self.alist_password.text()
        if not username or not password:
            QMessageBox.warning(self, self._t("AList 配置无效"), self._t("WebDAV 用户名和密码不能为空。"))
            return
        try:
            gateway_service = None
            if self.account_services:
                gateway_service = MultiAccountService(self.account_services, self.account_repositories)
            self.webdav = ModelScopeWebDAV(
                lambda service=gateway_service: service,
                str(self.alist_host_combo.currentData()),
                self.alist_port.value(),
                username,
                password,
                self.public_pool_store.repositories,
                self.folder_index,
            )
            self.webdav.start()
            with socket.create_connection(("127.0.0.1", self.alist_port.value()), timeout=2):
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
        if public_repos:
            public_service = ModelScopeService("", require_token=False)
            for repo in public_repos:
                key = ("", repo.repo_type, repo.repo_id, True)
                if force or key in self.dirty_repositories:
                    jobs.append((public_service, repo, True, ""))
                    job_keys.add(key)
        if not jobs:
            return
        self._index_refresh_pending = False
        self.index_inflight_keys = job_keys
        self.dirty_repositories.difference_update(job_keys)
        worker = FolderIndexThread(self.folder_index, self.account_store, jobs, self)
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
        if self.page_stack.currentIndex() == 0 and self.selected_repo and self.active_account_id:
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
        self.log.append(self._t(message))

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
        if self.webdav or (self.alist_enabled.isChecked() and self.alist_auto_start.isChecked()):
            self.apply_alist_settings()
        self._navigate(0)
        self.load_repositories()

    def load_repositories(self) -> None:
        account_tokens = {
            account.account_id: self.session_tokens.get(account.account_id, account.token)
            for account in self.accounts
            if account.enabled and self.session_tokens.get(account.account_id, account.token)
        }
        if not account_tokens:
            self._prompt_for_settings()
            return
        accounts = {account.account_id: account for account in self.accounts}

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
            self.account_store.cache_repositories(account_id, repos)
        for account_id, error in failures:
            account = next((item for item in self.accounts if item.account_id == account_id), None)
            if account:
                account.status = "failed"
            self._log(f"账户 {account.label if account else account_id} 验证失败：{error}")
        self.repositories = [repo for repos in self.account_repositories.values() for repo in repos]
        if self.active_account_id not in self.account_services and successes:
            self.active_account_id = successes[0][0]
        self.service = self.account_services.get(self.active_account_id) if self.active_account_id else None
        self._render_accounts()
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
        if self.webdav or (self.alist_enabled.isChecked() and self.alist_auto_start.isChecked()):
            self.apply_alist_settings()

    def _render_repositories(self) -> None:
        if not hasattr(self, "repo_list"):
            return
        selected_type = self.type_combo.currentData()
        self.repo_list.clear()
        labels = {"model": self._t("模型"), "dataset": self._t("数据集")}
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

    def _repo_selected(self) -> None:
        items = self.repo_list.selectedItems()
        if not items:
            return
        selected = items[0].data(0, Qt.ItemDataRole.UserRole)
        if not (isinstance(selected, tuple) and len(selected) == 2 and isinstance(selected[1], Repository)):
            return
        account_id, repo = selected
        service = self.account_services.get(account_id)
        if not service:
            return
        self.active_account_id = account_id
        self.service = service
        self.selected_repo = repo
        self.repo_heading.setText(f"{repo.repo_id} · {repo.repo_type}")
        self.target_edit.clear()
        self.resource_path_label.setText("/ 根目录")
        self.refresh_files_button.setEnabled(True)
        self.new_folder_button.setEnabled(True)
        self._update_upload_enabled()
        self.load_remote_files()

    def load_remote_files(self) -> None:
        if not self.service:
            self._prompt_for_settings()
            return
        if not self.selected_repo:
            return
        repo = self.selected_repo
        self._run_task(lambda: self.service.list_entries(repo), self._files_loaded, "正在读取仓库目录…")

    def _files_loaded(self, entries: list[RemoteEntry]) -> None:
        self.remote_entries = entries
        if self.selected_repo:
            self.folder_index.update_repository(self.selected_repo, entries)
            if self.active_account_id:
                self.account_store.cache_entries(self.active_account_id, self.selected_repo, entries)
                self.dirty_repositories.discard((
                    self.active_account_id, self.selected_repo.repo_type, self.selected_repo.repo_id, False
                ))
        paths = self._populate_remote_tree(self.remote_tree, entries)
        if self.selected_repo:
            self.repo_heading.setText(self._tf("{repo} · 已读取 {count} 项", repo=self.selected_repo.repo_id, count=len(paths)))
        self._log(f"仓库目录已刷新，共 {len(paths)} 项")
        if self.pending_search_path:
            pending = self.pending_search_path
            self.pending_search_path = ""
            iterator = QTreeWidgetItemIterator(self.remote_tree)
            while iterator.value():
                candidate = iterator.value()
                entry = candidate.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(entry, RemoteEntry) and entry.path == pending:
                    self.remote_tree.setCurrentItem(candidate)
                    self.remote_tree.scrollToItem(candidate)
                    break
                iterator += 1

    def _populate_remote_tree(self, tree: QTreeWidget, entries: list[RemoteEntry]) -> list[str]:
        paths = [entry.path for entry in entries]
        entries_by_path = {entry.path: entry for entry in entries}
        tree.clear()
        root = QTreeWidgetItem([self._t("根目录"), self._t("文件夹"), "", "/"])
        root.setData(0, Qt.ItemDataRole.UserRole, RemoteEntry("", is_dir=True))
        root.setExpanded(True)
        tree.addTopLevelItem(root)
        nodes: dict[str, QTreeWidgetItem] = {"": root}
        directory_paths = repository_directories(paths)
        for file_path in paths:
            parts = [part for part in file_path.replace("\\", "/").split("/") if part]
            parent_path = ""
            for index, part in enumerate(parts):
                current = "/".join(parts[: index + 1])
                if current not in nodes:
                    is_file = index == len(parts) - 1 and current not in directory_paths
                    entry = entries_by_path.get(current, RemoteEntry(current, is_dir=not is_file))
                    if not is_file and not entry.is_dir:
                        entry = RemoteEntry(entry.path, entry.size, entry.sha256, True)
                    item = QTreeWidgetItem([
                        part,
                        self._t("文件夹") if entry.is_dir else self._t("文件"),
                        "" if entry.is_dir else format_size(entry.size),
                        current,
                    ])
                    item.setData(0, Qt.ItemDataRole.UserRole, entry)
                    nodes[parent_path].addChild(item)
                    nodes[current] = item
                elif current in entries_by_path:
                    entry = entries_by_path[current]
                    if current in directory_paths and not entry.is_dir:
                        entry = RemoteEntry(entry.path, entry.size, entry.sha256, True)
                    node = nodes[current]
                    node.setData(0, Qt.ItemDataRole.UserRole, entry)
                    node.setText(1, self._t("文件夹") if entry.is_dir else self._t("文件"))
                    node.setText(2, "" if entry.is_dir else format_size(entry.size))
                    node.setText(3, current)
                parent_path = current
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
        self.target_edit.setText(target)
        self.resource_path_label.setText(f"/ {target}" if target else self._t("/ 根目录"))

    def _schedule_global_search(self) -> None:
        self.resource_search_timer.start()

    def _set_global_search_visible(self, visible: bool) -> None:
        self.remote_tree.setVisible(not visible)
        self.resource_drop_hint.setVisible(not visible)
        self.global_search_tree.setVisible(visible)
        self.global_search_label.setVisible(visible)

    def _perform_global_search(self) -> None:
        query = self.resource_search_edit.text().strip()
        file_type = str(self.resource_search_type.currentData() or "all")
        if not query and file_type == "all":
            self.global_search_results = []
            self.global_search_tree.clear()
            self._set_global_search_visible(False)
            return
        scope = str(self.resource_search_scope.currentData() or "all")
        account_id = None
        repo_type = repo_id = None
        path_prefix = None
        if scope == "account":
            account_id = self.active_account_id
            if not account_id:
                self.global_search_results = []
                self.global_search_tree.clear()
                self.global_search_label.setText(self._t("请先选择搜索账户"))
                self._set_global_search_visible(True)
                return
        elif scope in {"repository", "directory"}:
            account_id = self.active_account_id
            if self.selected_repo:
                repo_type, repo_id = self.selected_repo.repo_type, self.selected_repo.repo_id
                if scope == "directory":
                    path_prefix = self.target_edit.text().strip("/")
            else:
                self.global_search_results = []
                self.global_search_tree.clear()
                self.global_search_label.setText(self._t("请先选择搜索仓库"))
                self._set_global_search_visible(True)
                return
        self.global_search_results = self.account_store.search_entries(
            query, file_type, account_id, repo_type, repo_id, path_prefix
        )
        account_labels = {account.account_id: account.label for account in self.accounts}
        type_labels = {
            "video": self._t("视频"), "image": self._t("图片"),
            "document": self._t("文档"), "archive": self._t("压缩包"),
            "other": self._t("其他"),
        }
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
            "搜索结果：{count} 项（最多显示 500 项）", count=len(self.global_search_results)
        ))
        self._set_global_search_visible(True)

    def _global_search_context_menu(self, position) -> None:
        item = self.global_search_tree.itemAt(position)
        record = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not isinstance(record, IndexedEntry):
            return
        service = self.account_services.get(record.account_id)
        if not service:
            return
        repo = next((candidate for candidate in self.account_repositories.get(record.account_id, [])
                     if candidate.repo_type == record.repo_type and candidate.repo_id == record.repo_id), None)
        if repo is None:
            repo = Repository(record.repo_id, record.repo_type)
        entry = RemoteEntry(record.path, record.size, record.sha256, record.is_dir)
        self._show_remote_menu(self.global_search_tree, position, entry, service, repo, [entry])

    def _open_global_search_result(self, item: QTreeWidgetItem, column: int = 0) -> None:
        record = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(record, IndexedEntry):
            return
        service = self.account_services.get(record.account_id)
        if not service:
            return
        repo = next((candidate for candidate in self.account_repositories.get(record.account_id, [])
                     if candidate.repo_type == record.repo_type and candidate.repo_id == record.repo_id), None)
        if repo is None:
            repo = Repository(record.repo_id, record.repo_type)
        self.active_account_id = record.account_id
        self.service = service
        self.selected_repo = repo
        self.pending_search_path = record.path
        self.resource_search_edit.clear()
        self.resource_search_type.setCurrentIndex(0)
        self._set_global_search_visible(False)
        iterator = QTreeWidgetItemIterator(self.repo_list)
        self.repo_list.blockSignals(True)
        while iterator.value():
            candidate = iterator.value()
            data = candidate.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) == 2 and data[0] == record.account_id and data[1] == repo:
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
            link = self._repository_web_url(repo, entry.path) if entry.is_dir else service.get_download_url(repo, entry.path)
        except Exception as exc:
            QMessageBox.warning(self, self._t("复制链接失败"), str(exc))
            return
        QApplication.clipboard().setText(link)
        message = "文件夹链接已复制" if entry.is_dir else "文件直链已复制"
        self._log(f"{message}：{entry.path or '/'}")
        self.repo_heading.setText(self._t(message))

    def _remote_context_menu(self, position) -> None:
        item = self.remote_tree.itemAt(position)
        if item is None or not self.service or not self.selected_repo:
            return
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(entry, RemoteEntry):
            return
        self._show_remote_menu(
            self.remote_tree, position, entry, self.service, self.selected_repo, self.remote_entries
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
            )

    def _show_remote_menu(
        self,
        tree: QTreeWidget,
        position,
        entry: RemoteEntry,
        service: ModelScopeService,
        repo: Repository,
        entries: list[RemoteEntry],
    ) -> None:
        menu = QMenu(self)
        link_action = menu.addAction(self._t("复制链接" if entry.is_dir else "复制直链"))
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
        menu.addSeparator()
        web_action = menu.addAction(self._t("在网页端管理 / 删除"))
        chosen = menu.exec(tree.viewport().mapToGlobal(position))
        if chosen is link_action:
            self._copy_remote_link(entry, service, repo)
        elif chosen is download_action:
            self.add_remote_download(entry, service, repo, entries)
        elif builtin_action is not None and chosen is builtin_action:
            self.open_builtin_remote(entry, service, repo)
        elif chosen in player_actions:
            self.open_external_player(entry, service, repo, player_actions[chosen])
        elif chosen is web_action:
            QDesktopServices.openUrl(QUrl(self._repository_web_url(repo, entry.path if entry.is_dir else "")))

    def open_external_player(
        self,
        entry: RemoteEntry,
        service: ModelScopeService,
        repo: Repository,
        player_info: dict[str, str],
    ) -> None:
        if str(repo.visibility).lower() in {"private", "internal", "1"}:
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
        self._render_public_history()
        self.folder_index.update_repository(self.search_repo, self.search_entries, True)
        self.dirty_repositories.discard(("", self.search_repo.repo_type, self.search_repo.repo_id, True))
        self.search_url_edit.setEditText(search_url)
        count = len(self._populate_remote_tree(self.search_remote_tree, self.search_entries))
        self.search_heading.setText(self._tf("{repo} · 已读取 {count} 项", repo=self.search_repo.repo_id, count=count))
        self._log(f"已读取公开资源 {self.search_repo.repo_id}，共 {count} 项")

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
        if not self.service:
            self._prompt_for_settings()
            return
        if not self.selected_repo:
            QMessageBox.information(self, self._t("请选择仓库"), self._t("请先在左侧选择目标仓库。"))
            return
        if self.task and self.task.isRunning():
            QMessageBox.information(self, self._t("传输进行中"), self._t("已有任务正在运行，请完成后再拖放上传。"))
            return
        self.target_edit.setText(directory.path)
        self.resource_path_label.setText(f"/ {directory.path}" if directory.path else "/ 根目录")
        self.clear_queue()
        self.add_paths(raw_paths)
        if not self.upload_items:
            return
        self._navigate(1)
        self.queue_tabs.setCurrentIndex(0)
        self._log(f"拖放上传到 /{directory.path}")
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
        self.target_edit.setText(target)
        self.resource_path_label.setText(f"/ {target}")
        self.settings.setValue("target_folder", target)
        self._log(f"目标文件夹设为：/{target}（上传内容后会在仓库中创建）")

    def pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择要上传的文件")
        self.add_paths(files)

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择要上传的文件夹")
        if folder:
            self.add_paths([folder])

    def add_paths(self, raw_paths: list[str]) -> None:
        known = {str(path.resolve()).lower() for path in self.upload_items}
        for raw in raw_paths:
            path = Path(raw)
            try:
                resolved = path.resolve()
            except OSError:
                continue
            if not resolved.exists() or str(resolved).lower() in known:
                continue
            known.add(str(resolved).lower())
            self.upload_items.append(resolved)
            row = self.queue_table.rowCount()
            self.queue_table.insertRow(row)
            self.queue_table.setItem(row, 0, QTableWidgetItem(str(resolved)))
            self.queue_table.setItem(row, 1, QTableWidgetItem("文件夹" if resolved.is_dir() else "文件"))
            self.queue_table.setItem(row, 2, QTableWidgetItem("等待上传"))
        self._update_upload_enabled()

    def clear_queue(self) -> None:
        if self.task and self.task.isRunning():
            return
        self.upload_items.clear()
        self.queue_table.setRowCount(0)
        self._update_upload_enabled()

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
        active = bool(self.service and self.selected_repo and self.upload_items)
        if self.task and self.task.isRunning():
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
        if not self.service or not self.selected_repo or not self.upload_items:
            return
        try:
            target = normalize_remote_path(self.target_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, self._t("路径无效"), str(exc))
            return
        oversized: set[Path] = set()
        if self.selected_repo.repo_type == "model":
            oversized = set(oversized_upload_files(self.upload_items))
        worker_paths = [
            path for path in self.upload_items
            if not (path.is_file() and path.resolve() in oversized)
        ]
        if oversized:
            shown = "\n".join(str(path) for path in sorted(oversized)[:15])
            remainder = len(oversized) - 15
            if remainder > 0:
                shown += f"\n……另有 {remainder} 个文件"
            QMessageBox.warning(
                self,
                self._t("已跳过超过 50 GB 的文件"),
                self._t("模型仓库 API 不支持单个超过 50 GB 的文件。以下文件不会上传：\n\n") + shown,
            )
        self.settings.setValue("target_folder", target)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.upload_button.setEnabled(False)
        for row, path in enumerate(self.upload_items):
            status = self.queue_table.item(row, 2)
            if path.is_file() and path.resolve() in oversized:
                status.setText("已跳过（超过 50 GB）")
                status.setForeground(QColor("#c42b1c"))
            else:
                status.setText("准备上传")
        if not worker_paths:
            self.progress.setValue(100)
            self.upload_stats.setText("没有可上传的文件")
            self._update_upload_enabled()
            return
        worker = UploadThread(
            self.service,
            self.selected_repo,
            worker_paths,
            target,
            self.keep_folder_name.isChecked(),
            oversized,
            self,
        )
        worker.item_done.connect(self._upload_item_done)
        worker.progress_info.connect(self._upload_progress_info)
        worker.completed.connect(self._upload_completed)
        worker.finished.connect(worker.deleteLater)
        self.task = worker
        self.current_upload_speed = 0.0
        self._refresh_tray_status()
        self._update_download_enabled()
        self._log(f"开始上传 {len(self.upload_items)} 个项目到 /{target or ''}")
        worker.start()

    def _upload_progress_info(self, path: str, percent: int, speed: float, eta: int) -> None:
        self.current_upload_speed = max(0.0, speed)
        self.progress.setValue(percent)
        self.upload_stats.setText(self._tf("速度：{speed} · 剩余：{eta}", speed=format_speed(speed), eta=format_eta(eta)))
        if not path:
            return
        for row, item_path in enumerate(self.upload_items):
            if str(item_path) == path:
                status = self.queue_table.item(row, 2)
                if status and status.text() not in {"完成", "失败", "已跳过（超过 50 GB）"}:
                    status.setText(f"{percent}% · {format_speed(speed)} · {format_eta(eta)}")
                break

    def _upload_item_done(self, path: str, success: bool, message: str) -> None:
        for row, item_path in enumerate(self.upload_items):
            if str(item_path) == path:
                status = self.queue_table.item(row, 2)
                status.setText(self._t("完成") if success else self._t("失败"))
                status.setForeground(QColor("#0f7b0f" if success else "#c42b1c"))
                status.setToolTip(message)
                break
        self._log(f"{'完成' if success else '失败'}：{Path(path).name} · {message}")

    def _upload_completed(self, ok: int, failed: int) -> None:
        self.task = None
        self.current_upload_speed = 0.0
        self._refresh_tray_status()
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.upload_stats.setText("速度：0 B/s · 剩余：00:00")
        self._update_upload_enabled()
        self._update_download_enabled()
        self._log(f"上传结束：{ok} 个成功，{failed} 个失败")
        if failed:
            QMessageBox.warning(self, self._t("上传完成"), self._tf("{ok} 个项目上传成功，{failed} 个失败。\n请在操作记录中查看详情。", ok=ok, failed=failed))
        else:
            QMessageBox.information(self, self._t("上传完成"), self._tf("{ok} 个项目已成功上传。", ok=ok))
        if ok and self.selected_repo and self.active_account_id:
            self._mark_repository_dirty(self.active_account_id, self.selected_repo)
            self.repo_heading.setText(self._t("上传完成，目录将在空闲时自动刷新"))

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
    app = QApplication(sys.argv)
    app.setApplicationName("ModelScope Manager")
    app.setOrganizationName("ARXChem")
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#f3f3f3"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#cce8ff"))
    app.setPalette(palette)
    window = MainWindow()
    window.show()
    return app.exec()
