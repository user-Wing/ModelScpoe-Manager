from __future__ import annotations

import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = {
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jxl", ".jpeg",
    ".jpg", ".png", ".svg", ".tif", ".tiff", ".webp",
}


@dataclass(frozen=True)
class ImageRecord:
    image_id: str
    account_id: str
    repo_type: str
    repo_id: str
    remote_path: str
    direct_url: str
    cache_path: str
    size: int
    created_at: int


class ImageStore:
    def __init__(self, database_path: Path, cache_dir: Path):
        self.database_path = database_path
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _record(row: sqlite3.Row) -> ImageRecord:
        return ImageRecord(
            str(row["image_id"]), str(row["account_id"]), str(row["repo_type"]),
            str(row["repo_id"]), str(row["remote_path"]), str(row["direct_url"]),
            str(row["cache_path"]), int(row["size"]), int(row["created_at"]),
        )

    def list_records(self) -> list[ImageRecord]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM image_records ORDER BY created_at DESC").fetchall()
        finally:
            connection.close()
        return [self._record(row) for row in rows]

    def add(
        self,
        account_id: str,
        repo_type: str,
        repo_id: str,
        remote_path: str,
        direct_url: str,
        local_source: Path,
    ) -> ImageRecord:
        image_id = str(uuid.uuid4())
        suffix = local_source.suffix.lower()
        cache_path = self.cache_dir / f"{image_id}{suffix}"
        shutil.copy2(local_source, cache_path)
        size = cache_path.stat().st_size
        created_at = int(time.time())
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO image_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (image_id, account_id, repo_type, repo_id, remote_path, direct_url,
                 str(cache_path), size, created_at),
            )
            connection.commit()
        finally:
            connection.close()
        return ImageRecord(
            image_id, account_id, repo_type, repo_id, remote_path, direct_url,
            str(cache_path), size, created_at,
        )

    def remove(self, image_id: str, remove_cache: bool = True) -> None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT cache_path FROM image_records WHERE image_id=?", (image_id,)
            ).fetchone()
            connection.execute("DELETE FROM image_records WHERE image_id=?", (image_id,))
            connection.commit()
        finally:
            connection.close()
        if remove_cache and row and row[0]:
            try:
                Path(str(row[0])).unlink(missing_ok=True)
            except OSError:
                pass
