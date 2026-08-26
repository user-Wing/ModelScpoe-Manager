from __future__ import annotations

from pathlib import Path
from typing import Iterator


class UnsafeLocalPathError(ValueError):
    pass


def is_link_like(path: Path) -> bool:
    """Reject filesystem indirections that can escape an upload/backup root."""
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        return bool(is_junction and is_junction())
    except OSError:
        return True


def iter_contained_files(source: Path) -> Iterator[Path]:
    """Yield regular files without following symlinks/junctions outside source."""
    source = Path(source)
    if is_link_like(source):
        raise UnsafeLocalPathError(f"不允许上传或备份符号链接/联接点：{source}")
    try:
        root = source.resolve(strict=True)
    except OSError as exc:
        raise UnsafeLocalPathError(f"无法解析本地路径：{source}") from exc

    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileNotFoundError(str(source))

    for candidate in root.rglob("*"):
        if is_link_like(candidate):
            raise UnsafeLocalPathError(f"目录包含符号链接/联接点：{candidate}")
        if not candidate.is_file():
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise UnsafeLocalPathError(f"无法解析本地文件：{candidate}") from exc
        if not resolved.is_relative_to(root):
            raise UnsafeLocalPathError(f"本地文件超出所选目录：{candidate}")
        yield candidate


def validate_upload_source(source: Path) -> Path:
    """Validate a file/folder upload root and all contained files."""
    # Exhaust the generator so an SDK folder upload cannot later discover an
    # unsafe indirection that was not checked by the manager.
    for _ in iter_contained_files(source):
        pass
    return Path(source).resolve(strict=True)
