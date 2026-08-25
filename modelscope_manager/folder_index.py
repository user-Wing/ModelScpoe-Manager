from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Iterable

from .service import RemoteEntry, Repository, repository_directories


def calculate_folder_sizes(entries: Iterable[RemoteEntry]) -> dict[str, int]:
    """Aggregate every file into its repository root and all ancestor folders."""
    entries = list(entries)
    sizes = {"": 0}
    for directory in repository_directories(entry.path for entry in entries):
        sizes.setdefault(directory, 0)
    for entry in entries:
        if entry.is_dir:
            sizes.setdefault(entry.path.strip("/"), 0)
            continue
        size = max(0, int(entry.size))
        sizes[""] += size
        parent = PurePosixPath(entry.path).parent
        while str(parent) not in {"", "."}:
            sizes[str(parent)] = sizes.get(str(parent), 0) + size
            parent = parent.parent
    return sizes


class FolderSizeIndex:
    """Persistent folder-size cache shared by the GUI and WebDAV gateway."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._cache: dict[tuple[bool, str, str], dict[str, int]] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS folder_sizes (
                        public INTEGER NOT NULL,
                        repo_type TEXT NOT NULL,
                        repo_id TEXT NOT NULL,
                        folder_path TEXT NOT NULL,
                        total_size INTEGER NOT NULL,
                        indexed_at INTEGER NOT NULL,
                        PRIMARY KEY (public, repo_type, repo_id, folder_path)
                    )
                    """
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS folder_sizes_repo ON folder_sizes(public, repo_type, repo_id)"
                )
                connection.commit()
            finally:
                connection.close()

    def update_repository(self, repo: Repository, entries: Iterable[RemoteEntry], public: bool = False) -> None:
        sizes = calculate_folder_sizes(entries)
        now = int(time.time())
        rows = [
            (int(public), repo.repo_type, repo.repo_id, path, size, now)
            for path, size in sizes.items()
        ]
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "DELETE FROM folder_sizes WHERE public=? AND repo_type=? AND repo_id=?",
                    (int(public), repo.repo_type, repo.repo_id),
                )
                connection.executemany(
                    "INSERT INTO folder_sizes VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
                connection.commit()
            finally:
                connection.close()
            self._cache[(bool(public), repo.repo_type, repo.repo_id)] = dict(sizes)

    def update_folder(self, repo: Repository, folder_path: str, entries: Iterable[RemoteEntry], public: bool = False) -> int:
        """Refresh one folder's aggregate size without rewriting the repository index."""
        prefix = folder_path.strip("/")
        size = sum(
            max(0, int(entry.size)) for entry in entries
            if not entry.is_dir and (not prefix or entry.path.startswith(prefix + "/"))
        )
        now = int(time.time())
        key = (bool(public), repo.repo_type, repo.repo_id)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    """INSERT INTO folder_sizes(public, repo_type, repo_id, folder_path, total_size, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(public, repo_type, repo_id, folder_path)
                       DO UPDATE SET total_size=excluded.total_size, indexed_at=excluded.indexed_at""",
                    (int(public), repo.repo_type, repo.repo_id, prefix, size, now),
                )
                connection.commit()
            finally:
                connection.close()
            self._cache.setdefault(key, {})[prefix] = size
        return size

    def remove_repository(self, repo: Repository, public: bool = False) -> None:
        key = (bool(public), repo.repo_type, repo.repo_id)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute(
                    "DELETE FROM folder_sizes WHERE public=? AND repo_type=? AND repo_id=?",
                    (int(public), repo.repo_type, repo.repo_id),
                )
                connection.commit()
            finally:
                connection.close()
            self._cache.pop(key, None)

    def remove_entries(
        self,
        repo: Repository,
        entries: Iterable[RemoteEntry],
        removed_prefixes: Iterable[str] = (),
        public: bool = False,
    ) -> None:
        decrements: dict[str, int] = {}
        for entry in entries:
            if entry.is_dir:
                continue
            size = max(0, int(entry.size))
            decrements[""] = decrements.get("", 0) + size
            parent = PurePosixPath(entry.path).parent
            while str(parent) not in {"", "."}:
                path = str(parent)
                decrements[path] = decrements.get(path, 0) + size
                parent = parent.parent
        prefixes = list(dict.fromkeys(path.strip("/") for path in removed_prefixes if path.strip("/")))
        key = (bool(public), repo.repo_type, repo.repo_id)
        with self._lock:
            connection = self._connect()
            try:
                connection.executemany(
                    """UPDATE folder_sizes SET total_size=MAX(0, total_size-?), indexed_at=?
                       WHERE public=? AND repo_type=? AND repo_id=? AND folder_path=?""",
                    [
                        (size, int(time.time()), int(public), repo.repo_type, repo.repo_id, path)
                        for path, size in decrements.items()
                    ],
                )
                for prefix in prefixes:
                    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    connection.execute(
                        """DELETE FROM folder_sizes WHERE public=? AND repo_type=? AND repo_id=?
                           AND (folder_path=? OR folder_path LIKE ? ESCAPE '\\')""",
                        (int(public), repo.repo_type, repo.repo_id, prefix, escaped + "/%"),
                    )
                connection.commit()
            finally:
                connection.close()
            sizes = self._cache.get(key)
            if sizes is not None:
                for path, size in decrements.items():
                    if path in sizes:
                        sizes[path] = max(0, sizes[path] - size)
                for prefix in prefixes:
                    for path in [value for value in sizes if value == prefix or value.startswith(prefix + "/")]:
                        sizes.pop(path, None)

    def folder_size(self, repo: Repository, folder_path: str = "", public: bool = False) -> int:
        return self.cached_folder_size(repo, folder_path, public) or 0

    def cached_folder_size(
        self, repo: Repository, folder_path: str = "", public: bool = False,
    ) -> int | None:
        key = (bool(public), repo.repo_type, repo.repo_id)
        with self._lock:
            sizes = self._cache.get(key)
            if sizes is None:
                connection = self._connect()
                try:
                    rows = connection.execute(
                        """SELECT folder_path, total_size FROM folder_sizes
                           WHERE public=? AND repo_type=? AND repo_id=?""",
                        (int(public), repo.repo_type, repo.repo_id),
                    ).fetchall()
                finally:
                    connection.close()
                sizes = {str(path): int(size) for path, size in rows}
                self._cache[key] = sizes
            return sizes.get(folder_path.strip("/"))

    def repositories_size(self, repos: Iterable[Repository], public: bool = False) -> int:
        keys = {(repo.repo_type, repo.repo_id) for repo in repos}
        if not keys:
            return 0
        with self._lock:
            connection = self._connect()
            try:
                rows = connection.execute(
                    "SELECT repo_type, repo_id, total_size FROM folder_sizes WHERE public=? AND folder_path=''",
                    (int(public),),
                ).fetchall()
            finally:
                connection.close()
        return sum(int(size) for repo_type, repo_id, size in rows if (repo_type, repo_id) in keys)
