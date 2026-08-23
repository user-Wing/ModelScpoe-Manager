from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


MAX_BACKUP_FILE_SIZE = 50 * 1024**3


@dataclass
class BackupJob:
    job_id: str = ""
    name: str = ""
    account_id: str = ""
    local_path: str = ""
    repo_type: str = "dataset"
    repo_id: str = ""
    dest_dir: str = ""
    mode: str = "incremental"
    interval_value: float = 30.0
    interval_unit: str = "minute"
    download_limit_mb: float = 10.0
    enabled: bool = True
    last_scan: int = 0
    last_sync: int = 0

    @property
    def interval_seconds(self) -> int:
        multiplier = 3600 if self.interval_unit == "hour" else 60
        return max(1, int(self.interval_value * multiplier))


@dataclass(frozen=True)
class LocalBackupFile:
    path: Path
    relative_path: str
    size: int
    mtime_ns: int


class BackupStore:
    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _job(row: sqlite3.Row) -> BackupJob:
        return BackupJob(
            str(row["job_id"]), str(row["name"]), str(row["account_id"]),
            str(row["local_path"]), str(row["repo_type"]), str(row["repo_id"]),
            str(row["dest_dir"]), str(row["mode"]), float(row["interval_value"]),
            str(row["interval_unit"]), float(row["download_limit_mb"]),
            bool(row["enabled"]), int(row["last_scan"]), int(row["last_sync"]),
        )

    def list_jobs(self) -> list[BackupJob]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM backup_jobs ORDER BY created_at, name").fetchall()
        finally:
            connection.close()
        return [self._job(row) for row in rows]

    def save_job(self, job: BackupJob) -> BackupJob:
        job.job_id = job.job_id or str(uuid.uuid4())
        now = int(time.time())
        connection = self._connect()
        try:
            existing = connection.execute(
                "SELECT created_at, last_scan, last_sync FROM backup_jobs WHERE job_id=?", (job.job_id,)
            ).fetchone()
            created_at = int(existing[0]) if existing else now
            last_scan = int(existing[1]) if existing else int(job.last_scan)
            last_sync = int(existing[2]) if existing else int(job.last_sync)
            connection.execute(
                """INSERT OR REPLACE INTO backup_jobs
                   (job_id, name, account_id, local_path, repo_type, repo_id, dest_dir,
                    mode, interval_value, interval_unit, download_limit_mb, enabled,
                    last_scan, last_sync, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.job_id, job.name.strip() or Path(job.local_path).name or "备份任务",
                 job.account_id, str(Path(job.local_path).resolve()), job.repo_type, job.repo_id,
                 job.dest_dir.strip("/"), job.mode, max(0.01, float(job.interval_value)),
                 job.interval_unit, max(0.01, float(job.download_limit_mb)), int(job.enabled),
                 last_scan, last_sync, created_at, now),
            )
            connection.commit()
        finally:
            connection.close()
        return job

    def remove_job(self, job_id: str) -> None:
        connection = self._connect()
        try:
            connection.execute("DELETE FROM backup_jobs WHERE job_id=?", (job_id,))
            connection.execute("DELETE FROM backup_files WHERE job_id=?", (job_id,))
            connection.commit()
        finally:
            connection.close()

    def due_jobs(self, now: int | None = None) -> list[BackupJob]:
        now = int(now or time.time())
        return [job for job in self.list_jobs() if job.enabled and now - job.last_scan >= job.interval_seconds]

    def scan_changes(self, job: BackupJob) -> tuple[list[LocalBackupFile], list[LocalBackupFile]]:
        root = Path(job.local_path)
        if not root.is_dir():
            raise FileNotFoundError(f"备份文件夹不存在：{root}")
        connection = self._connect()
        try:
            known = {
                str(row["relative_path"]): (int(row["size"]), int(row["mtime_ns"]))
                for row in connection.execute(
                    "SELECT relative_path, size, mtime_ns FROM backup_files WHERE job_id=?", (job.job_id,)
                )
            }
        finally:
            connection.close()
        changed: list[LocalBackupFile] = []
        oversized: list[LocalBackupFile] = []
        current_paths: set[str] = set()
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            current_paths.add(relative)
            item = LocalBackupFile(path, relative, int(stat.st_size), int(stat.st_mtime_ns))
            if item.size >= MAX_BACKUP_FILE_SIZE:
                oversized.append(item)
            elif known.get(relative) != (item.size, item.mtime_ns):
                changed.append(item)
        stale = set(known) - current_paths
        if stale:
            connection = self._connect()
            try:
                connection.executemany(
                    "DELETE FROM backup_files WHERE job_id=? AND relative_path=?",
                    [(job.job_id, relative) for relative in stale],
                )
                connection.commit()
            finally:
                connection.close()
        return changed, oversized

    def mark_uploaded(self, job_id: str, item: LocalBackupFile, remote_path: str) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """INSERT OR REPLACE INTO backup_files
                   (job_id, relative_path, size, mtime_ns, uploaded_at, remote_path)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, item.relative_path, item.size, item.mtime_ns, int(time.time()), remote_path),
            )
            connection.commit()
        finally:
            connection.close()

    def mark_scan(self, job_id: str, when: int | None = None) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE backup_jobs SET last_scan=?, updated_at=? WHERE job_id=?",
                (int(when or time.time()), int(time.time()), job_id),
            )
            connection.commit()
        finally:
            connection.close()

    def mark_sync(self, job_id: str, when: int | None = None) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE backup_jobs SET last_sync=?, updated_at=? WHERE job_id=?",
                (int(when or time.time()), int(time.time()), job_id),
            )
            connection.commit()
        finally:
            connection.close()
